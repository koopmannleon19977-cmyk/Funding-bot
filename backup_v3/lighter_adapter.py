import asyncio
import logging
import time  # Für Zeitstempel
import json
from typing import Dict, Tuple, Optional, List
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import lighter
import config
from lighter.api.order_api import OrderApi
from lighter.api.funding_api import FundingApi
from lighter.api.transaction_api import TransactionApi
from lighter.signer_client import SignerClient
from lighter.nonce_manager import NonceManagerType

from lighter.api_client import ApiClient
from lighter.configuration import Configuration


class LighterAdapter:
    def __init__(self):
        logging.info("Initialisiere LighterAdapter...")
        self.market_info: Dict[str, dict] = {}
        self.funding_cache: Dict[str, float] = {}
        self.price_cache: Dict[str, float] = {}
        self._signer: Optional[SignerClient] = None
        self._resolved_account_index: Optional[int] = None
        self._resolved_api_key_index: Optional[int] = None

    # ----------------- Signer/Client -----------------
    def _get_nonce_mode(self) -> NonceManagerType:
        mode = str(getattr(config, "LIGHTER_NONCE_MANAGEMENT", "OPTIMISTIC")).upper()
        return NonceManagerType.API if mode == "API" else NonceManagerType.OPTIMISTIC

    def _get_base_url(self) -> str:
        return getattr(config, "LIGHTER_BASE_URL", None) or "https://mainnet.zklighter.elliot.ai"

    def _get_api_client(self) -> ApiClient:
        try:
            conf = Configuration(host=self._get_base_url())
            return ApiClient(configuration=conf)
        except TypeError:
            logging.warning("SDK-Warnung: Fallback-Initialisierung für ApiClient (mit 'host').")
            return ApiClient(host=self._get_base_url())
        except Exception as e:
            logging.error(f"Kritischer SDK-Fehler: Konnte ApiClient nicht initialisieren: {e}")
            return ApiClient(configuration=Configuration(host=self._get_base_url()))

    async def _auto_resolve_indices(self) -> Tuple[int, int]:
        if not getattr(config, "LIGHTER_AUTO_ACCOUNT_INDEX", False):
            return int(getattr(config, "LIGHTER_ACCOUNT_INDEX", 0)), int(getattr(config, "LIGHTER_API_KEY_INDEX", 0))

        logging.info("Lighter: Starte automatische Ermittlung von account_index/api_key_index...")
        client = self._get_api_client()
        tx_api = TransactionApi(client)
        candidates: List[Tuple[int, int, int]] = []

        try:
            for acct_idx in range(6):
                for api_idx in range(6):
                    try:
                        nn = await tx_api.next_nonce(account_index=acct_idx, api_key_index=api_idx)
                        nonce = getattr(nn, "nonce", None)
                        if nonce is not None:
                            logging.info(f"  AUTO OK: account_index={acct_idx} api_key_index={api_idx} nonce={nonce}")
                            candidates.append((acct_idx, api_idx, int(nonce)))
                    except Exception:
                        pass
        finally:
            try:
                await client.close()
            except Exception:
                pass

        if not candidates:
            raise RuntimeError("Lighter AUTO: Keine gültigen Indizes gefunden.")

        candidates.sort(key=lambda x: (-x[2], x[0], x[1]))
        base_url = self._get_base_url()
        priv = getattr(config, "LIGHTER_PRIVATE_KEY", None)
        if not priv or not str(priv).strip():
            raise RuntimeError("LIGHTER_PRIVATE_KEY fehlt. Setze ihn in der .env / config.")

        for acct_idx, api_idx, nonce in candidates:
            signer = None
            try:
                signer = SignerClient(
                    url=base_url,
                    private_key=priv,
                    api_key_index=api_idx,
                    account_index=acct_idx,
                    max_api_key_index=int(getattr(config, "LIGHTER_MAX_API_KEY_INDEX", -1)),
                    private_keys={int(api_idx): str(priv)},
                    nonce_management_type=self._get_nonce_mode(),
                )
                signer.switch_api_key(int(api_idx))
                signer.check_client()
                logging.info(f"Lighter AUTO gewählt: account_index={acct_idx}, api_key_index={api_idx}, nonce={nonce}")
                return acct_idx, api_idx
            except Exception as e:
                logging.warning(f"Lighter AUTO Kandidat verworfen: acct={acct_idx}, api={api_idx} -> {e}")
            finally:
                if signer:
                    try:
                        await signer.close()
                    except Exception:
                        pass

        raise RuntimeError("Lighter AUTO: Kein Kandidat mit gültiger Signatur gefunden.")

    async def _ensure_indices(self) -> Tuple[int, int]:
        if self._resolved_account_index is not None and self._resolved_api_key_index is not None:
            return self._resolved_account_index, self._resolved_api_key_index
        acct_idx, api_idx = await self._auto_resolve_indices()
        self._resolved_account_index = acct_idx
        self._resolved_api_key_index = api_idx
        return acct_idx, api_idx

    async def _get_signer(self) -> SignerClient:
        if self._signer is None:
            acct_idx, api_idx = await self._ensure_indices()
            base_url = self._get_base_url()
            api_priv = getattr(config, "LIGHTER_API_PRIVATE_KEY", None)
            if not api_priv or not str(api_priv).strip():
                raise RuntimeError("LIGHTER_API_PRIVATE_KEY fehlt. Hinterlege den API-Private-Key in .env.")

            nonce_mode = self._get_nonce_mode()
            signer = SignerClient(
                url=base_url,
                private_key=api_priv,
                api_key_index=api_idx,
                account_index=acct_idx,
                max_api_key_index=int(getattr(config, "LIGHTER_MAX_API_KEY_INDEX", -1)),
                private_keys={api_idx: str(api_priv)},
                nonce_management_type=nonce_mode,
            )
            signer.switch_api_key(api_idx)
            signer.check_client()
            self._signer = signer
            logging.info(f"Lighter SignerClient init OK: acct={acct_idx}, api={api_idx}, mode={nonce_mode.name}")
        return self._signer

    async def reset_signer(self):
        if self._signer:
            try:
                await self._signer.close()
            except Exception:
                pass
        self._signer = None
        self._resolved_account_index = None
        self._resolved_api_key_index = None

    # ----------------- Öffentliche Daten / Caches -----------------
    async def _fetch_market_info_async(self) -> Dict[str, dict]:
        client = None
        info: Dict[str, dict] = {}
        skip = {'GBPUSD', 'USDCHF', 'EURUSD', 'USDJPY', 'USDCAD', 'XAG', 'XAU', 'SPX'}
        try:
            client = self._get_api_client()
            api = OrderApi(client)
            data = await api.order_books()
            if data and data.order_books:
                for m in data.order_books:
                    name = getattr(m, 'symbol', '') or ''
                    if not name or name in skip:
                        continue
                    info[f"{name}-USD"] = {'id': getattr(m, 'market_id', 0), 'data': m}
            else:
                logging.warning("Lighter: Keine order_books-Daten.")
            return info
        except Exception as e:
            logging.error(f"Lighter Fehler (_fetch_market_info_async): {e}")
            return {}
        finally:
            if client:
                try:
                    await client.close()
                except Exception:
                    pass

    async def _fetch_funding_rates_async(self) -> None:
        prev = dict(self.funding_cache)
        max_retries, base = 3, 2.0
        id2sym = {d['id']: s for s, d in self.market_info.items()}
        for i in range(max_retries):
            client = None
            try:
                client = self._get_api_client()
                api = FundingApi(client)
                fd = await api.funding_rates()
                if not fd or not getattr(fd, 'funding_rates', None):
                    raise RuntimeError("Leere Funding-Rates")
                new = {}
                for r in fd.funding_rates:
                    mid = getattr(r, 'market_id', None)
                    if mid in id2sym:
                        eight = float(getattr(r, 'rate', 0.0))
                        new[id2sym[mid]] = eight / 8.0
                if new:
                    self.funding_cache = new
                    logging.info(f"Lighter: {len(new)} Funding Rates geladen.")
                return
            except Exception as e:
                if i < max_retries - 1:
                    d = base * (2 ** i)
                    logging.error(f"Lighter Funding-Rates Fehler (Versuch {i+1}/{max_retries}): {e}. Retry in {d:.1f}s.")
                    await asyncio.sleep(d)
                else:
                    logging.error(f"Lighter Funding-Rates endgültig fehlgeschlagen: {e}. Behalte {len(prev)} alte Einträge.")
                    self.funding_cache = prev
            finally:
                if client:
                    try:
                        await client.close()
                    except Exception:
                        pass

    async def _fetch_single_price_async(self, symbol, market_id, client, sem):
        async with sem:
            try:
                await asyncio.sleep(0.02)
                api = OrderApi(client)
                det = await api.order_book_details(market_id=market_id)
                if det and getattr(det, 'order_book_details', None):
                    actual = det.order_book_details[0]
                    px = getattr(actual, 'last_trade_price', None)
                    return symbol, (float(px) if px is not None else None)
                return symbol, None
            except Exception as e:
                logging.warning(f"Lighter: Fehler beim Abrufen des Preises für {symbol} (ID: {market_id}): {e}")
                return symbol, None

    async def _fetch_prices_async(self) -> None:
        prev = dict(self.price_cache)
        client = None
        try:
            client = self._get_api_client()
            sem = asyncio.Semaphore(int(getattr(config, "CONCURRENT_REQUEST_LIMIT", 5)))
            tasks = [self._fetch_single_price_async(s, d['id'], client, sem) for s, d in self.market_info.items()]
            results = await asyncio.gather(*tasks)
            new = {s: p for s, p in results if p is not None}
            if new:
                self.price_cache = new
                logging.info(f"Lighter: {len(new)} Preise geladen.")
            else:
                logging.warning("Lighter: Keine Preise geladen – behalte alten Cache.")
        except Exception as e:
            logging.error(f"Lighter Fehler (_fetch_prices_async): {e}. Behalte {len(prev)} alte Preise.")
            self.price_cache = prev
        finally:
            if client:
                try:
                    await client.close()
                except Exception:
                    pass

    async def load_market_cache(self) -> None:
        logging.info("Lighter: Aktualisiere Markt-Info-Cache...")
        self.market_info = await self._fetch_market_info_async()
        logging.info(f"Lighter: {len(self.market_info)} PERP-Märkte gefunden.")
        logging.info("Lighter: Starte Ladevorgang für Funding-Rates und Preise...")
        await asyncio.gather(self._fetch_funding_rates_async(), self._fetch_prices_async())

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        return self.price_cache.get(symbol)

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        return self.funding_cache.get(symbol)

    # ----------------- Rounding/Min-Checks -----------------
    def _market_meta(self, symbol: str) -> tuple[int, int, Decimal, Decimal]:
        m = self.market_info.get(symbol, {}).get('data')
        if not m:
            return 8, 2, Decimal("0"), Decimal("0")
        size_dec = int(getattr(m, "supported_size_decimals", 8) or 8)
        price_dec = int(getattr(m, "supported_price_decimals", 2) or 2)
        try:
            min_base = Decimal(str(getattr(m, "min_base_amount", "0") or "0"))
        except InvalidOperation:
            min_base = Decimal("0")
        try:
            min_quote = Decimal(str(getattr(m, "min_quote_amount", "0") or "0"))
        except InvalidOperation:
            min_quote = Decimal("0")
        return size_dec, price_dec, min_base, min_quote

    def _quant(self, decimals: int) -> Decimal:
        return Decimal(1).scaleb(-decimals)

    def _round_qty_for(self, symbol: str, qty: Decimal) -> Decimal:
        size_dec, _, min_base, _ = self._market_meta(symbol)
        qty = qty.quantize(self._quant(size_dec), rounding=ROUND_DOWN)
        if qty < min_base:
            qty = min_base
        return qty

    def _round_price_for(self, symbol: str, price: Decimal) -> Decimal:
        _, price_dec, _, _ = self._market_meta(symbol)
        return price.quantize(self._quant(price_dec), rounding=ROUND_DOWN)

    def _ensure_min_quote(self, symbol: str, qty: Decimal, price: Decimal) -> Decimal:
        _, _, _, min_quote = self._market_meta(symbol)
        if price <= 0:
            return qty
        notional = qty * price
        if notional < min_quote:
            needed_qty = (min_quote / price)
            qty = self._round_qty_for(symbol, needed_qty)
        return qty

    def min_notional_usd(self, symbol: str) -> float:
        try:
            price = self.fetch_mark_price(symbol)
            if not price or price <= 0:
                return 0.0
            _, _, min_base, min_quote = self._market_meta(symbol)
            notional = max(min_base * Decimal(str(price)), min_quote)
            return float(notional)
        except Exception:
            return 0.0

    def _aggressive_limit_price(self, symbol: str, side: str, mark_price: Decimal) -> Decimal:
        eps = Decimal(str(getattr(config, "LIGHTER_PRICE_EPSILON_PCT", 0.25)))
        px = mark_price * (Decimal(1) + eps / Decimal(100)) if side.upper() == "BUY" else mark_price * (Decimal(1) - eps / Decimal(100))
        return self._round_price_for(symbol, px)

    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal) -> Tuple[int, int]:
        size_dec, price_dec, _, _ = self._market_meta(symbol)
        base_amount_int = int((qty * (Decimal(10) ** size_dec)).to_integral_value(rounding=ROUND_DOWN))
        price_int = int((price * (Decimal(10) ** price_dec)).to_integral_value(rounding=ROUND_DOWN))
        return base_amount_int, price_int

    # ----------------- Signed Order -----------------
    async def _place_order_signed(
        self,
        *,
        symbol: str,
        market_index: int,
        side: str,
        price: Decimal,
        qty: Decimal,
        client_order_index: int,
        reduce_only: bool,
        order_expiry: int,  # siehe SignerClient: -1 = 28 Tage, 0 = IOC
    ) -> bool:
        if getattr(config, "LIGHTER_DRY_RUN", True):
            logging.warning(
                f"LIGHTER DRY-RUN: Place {side} market_index={market_index} qty={qty} price={price} "
                f"coi={client_order_index} reduce_only={reduce_only} expiry={order_expiry}"
            )
            return True

        try:
            signer = await self._get_signer()
            is_ask = side.upper() == "SELL"
            order_type = signer.ORDER_TYPE_LIMIT
            tif = signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
            trigger_price_int = 0

            base_amount_int, price_int = self._scale_amounts(symbol, qty, price)

            logging.info(
                f"Lighter DEBUG: create_order params: "
                f"market_index={market_index}, coi={client_order_index}, base_amount={base_amount_int}, "
                f"price_int={price_int}, is_ask={is_ask}, order_type={order_type}, tif={tif}, "
                f"reduce_only={reduce_only}, trigger_price={trigger_price_int}, order_expiry={order_expiry}"
            )

            tx_obj, response_obj, err = await signer.create_order(
                market_index=market_index,
                client_order_index=client_order_index,
                base_amount=base_amount_int,
                price=price_int,
                is_ask=is_ask,
                order_type=order_type,
                time_in_force=tif,
                reduce_only=bool(reduce_only),
                trigger_price=trigger_price_int,
                order_expiry=order_expiry,
                nonce=-1,
                api_key_index=-1,
            )

            if err:
                logging.error(f"Lighter create_order ERROR: {err}")
                return False

            # Extrahiere den eigentlichen Hash aus dem Antwortobjekt
            tx_hash = getattr(response_obj, 'tx_hash', None)

            if tx_hash:
                logging.info(f"Lighter create_order OK: tx_hash={tx_hash}")
                return True
            
            logging.error(f"Lighter create_order WARN: Kein Fehler, aber auch kein tx_hash empfangen. Antwort: {response_obj}")
            return False

        except Exception as e:
            msg = str(e)
            logging.error(f"Lighter Order-Exception (sign/send): {repr(e)}")
            if "invalid account index" in msg.lower() or "nonce" in msg.lower():
                logging.error(f"Lighter Order-Fehler (Index/Nonce): {e} -> reset Signer.")
                await self.reset_signer()
            elif "invalid signature" in msg.lower():
                logging.error(f"Lighter Order-Fehler (invalid signature): {e} -> Signer neu.")
                await self.reset_signer()
            return False

    async def open_live_position(self, symbol: str, side: str, position_size_usd: float) -> bool:
        if symbol not in self.market_info:
            logging.error(f"LighterAdapter: Symbol {symbol} nicht im Markt-Cache.")
            return False

        mark = self.fetch_mark_price(symbol)
        if not mark or mark <= 0:
            logging.error(f"LighterAdapter: Kein Mark-Preis für {symbol}.")
            return False

        try:
            mark_d = Decimal(str(mark))
            raw_qty = Decimal(str(position_size_usd)) / mark_d
        except Exception as e:
            logging.error(f"LighterAdapter: Mengenberechnung fehlgeschlagen: {e}")
            return False

        limit = self._aggressive_limit_price(symbol, side, mark_d)
        qty = self._round_qty_for(symbol, raw_qty)
        qty = self._ensure_min_quote(symbol, qty, limit)

        # Slippage-Guard
        try:
            slip_pct = abs((limit - mark_d) / mark_d) * Decimal(100)
            max_slip = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
            if slip_pct > max_slip:
                logging.error(f"LighterAdapter: Slippage {slip_pct:.3f}% > {max_slip}% – Abbruch.")
                return False
        except Exception:
            pass

        market_index = int(self.market_info[symbol]['id'])
        client_order_index = int(time.time() * 1000) & 0x7FFFFFFF

        # Verwende Lighter-Default: 28-Tage-Expiry
        signer = await self._get_signer()
        order_expiry = signer.DEFAULT_28_DAY_ORDER_EXPIRY

        logging.info(f"Lighter LIVE ORDER: {side} {symbol} qty~{qty} @ {limit} (mark~{mark_d})")
        return await self._place_order_signed(
            symbol=symbol,
            market_index=market_index,
            side=side,
            price=limit,
            qty=qty,
            client_order_index=client_order_index,
            reduce_only=False,
            order_expiry=order_expiry,
        )

    async def close_live_position(self, symbol: str, side: str, position_size_usd: float) -> bool:
        opposite = "SELL" if side.upper() == "BUY" else "BUY"

        if symbol not in self.market_info:
            logging.error(f"LighterAdapter: Symbol {symbol} nicht im Markt-Cache.")
            return False

        mark = self.fetch_mark_price(symbol)
        if not mark or mark <= 0:
            logging.error(f"LighterAdapter: Kein Mark-Preis für {symbol}.")
            return False

        try:
            mark_d = Decimal(str(mark))
            raw_qty = Decimal(str(position_size_usd)) / mark_d
        except Exception as e:
            logging.error(f"LighterAdapter: Mengenberechnung fehlgeschlagen: {e}")
            return False

        limit = self._aggressive_limit_price(symbol, opposite, mark_d)
        qty = self._round_qty_for(symbol, raw_qty)
        qty = self._ensure_min_quote(symbol, qty, limit)

        # Slippage-Guard
        try:
            slip_pct = abs((limit - mark_d) / mark_d) * Decimal(100)
            max_slip = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))
            if slip_pct > max_slip:
                logging.error(f"LighterAdapter: Slippage {slip_pct:.3f}% > {max_slip}% – Abbruch.")
                return False
        except Exception:
            pass

        market_index = int(self.market_info[symbol]['id'])
        client_order_index = int(time.time() * 1000) & 0x7FFFFFFF

        signer = await self._get_signer()
        order_expiry = signer.DEFAULT_28_DAY_ORDER_EXPIRY

        logging.info(f"Lighter LIVE CLOSE: {opposite} {symbol} qty~{qty} @ {limit} (reduce_only=True)")
        return await self._place_order_signed(
            symbol=symbol,
            market_index=market_index,
            side=opposite,
            price=limit,
            qty=qty,
            client_order_index=client_order_index,
            reduce_only=True,
            order_expiry=order_expiry,
        )

    async def aclose(self):
        await self.reset_signer()
        logging.info("LighterAdapter geschlossen.")

        # In lighter_adapter.py (neue Methode hinzufügen)

async def fetch_open_positions(self) -> list[dict]:
    signer = await self._get_signer()
    
    try:
        # Lighter SDK AccountApi zum Abrufen der Kontodetails verwenden
        account_api = lighter.api.account_api.AccountApi(signer.api_client)
        
        # Abfrage des Kontos anhand des Account-Index
        response = await account_api.account(
            by="index",
            value=str(signer.account_index),
            api_key_index=signer.api_key_index # Optional, kann weggelassen werden
        )

        positions = []
        if response and response.positions:
            for p in response.positions:
                # Prüfe, ob die Positionsgröße ungleich Null ist
                if p.size != 0.0:
                    positions.append({
                        'symbol': p.symbol,
                        'size': float(p.size),
                        'entry_price': float(p.entry_price),
                        'exchange': 'Lighter'
                        # Lighter gibt PnL nicht direkt über diesen Endpunkt aus
                    })
        return positions

    except Exception as e:
        # Der Fehler 21102 (invalid account index) sollte jetzt behoben sein!
        logging.error(f"Lighter Position-Abruf Fehler: {e}")
        return []
    finally:
        # Wichtig: SignerClient ist langlebig, schließen Sie nur den API-Client, wenn nötig
        pass