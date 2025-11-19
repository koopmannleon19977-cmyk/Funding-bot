# lighter_adapter.py (FINAL FIX – November 2025)
import asyncio
import logging
import time
from typing import Dict, Tuple, Optional, List
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import config
from lighter.api.order_api import OrderApi
from lighter.api.funding_api import FundingApi
from lighter.api.transaction_api import TransactionApi
from lighter.signer_client import SignerClient
from lighter.api_client import ApiClient
from lighter.configuration import Configuration
from lighter.api.account_api import AccountApi

from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

# === HARDCODED OVERRIDES (nur für absolut kaputte Märkte) ===
MARKET_OVERRIDES = {
    "ASTER-USD": {'ss': Decimal('1'), 'sd': 8},
    "HYPE-USD":  {'ss': Decimal('0.01'), 'sd': 6},
    "MEGA-USD":  {'ss': Decimal('99999999')}  # blacklisted
}

class LighterAdapter(BaseAdapter):
    def __init__(self):
        super().__init__("Lighter")
        self.market_info: Dict[str, dict] = {}
        self.funding_cache: Dict[str, float] = {}
        self.price_cache: Dict[str, float] = {}
        self._signer: Optional[SignerClient] = None
        self._resolved_account_index: Optional[int] = None
        self._resolved_api_key_index: Optional[int] = None
        self.semaphore = asyncio.Semaphore(config.CONCURRENT_REQUEST_LIMIT)
        self._last_market_cache_at: Optional[float] = None

    def _get_base_url(self) -> str:
        return getattr(config, "LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")

    async def _auto_resolve_indices(self) -> Tuple[int, int]:
        return int(config.LIGHTER_ACCOUNT_INDEX), int(config.LIGHTER_API_KEY_INDEX)

    async def _get_signer(self) -> SignerClient:
        if self._signer is None:
            if self._resolved_account_index is None:
                self._resolved_account_index, self._resolved_api_key_index = await self._auto_resolve_indices()
            priv_key = str(getattr(config, "LIGHTER_API_PRIVATE_KEY", ""))
            if not priv_key:
                raise ValueError("LIGHTER_API_PRIVATE_KEY fehlt.")
            self._signer = SignerClient(
                url=self._get_base_url(),
                private_key=priv_key,
                api_key_index=self._resolved_api_key_index,
                account_index=self._resolved_account_index
            )
            logger.info(f"Lighter SignerClient init OK: acct={self._resolved_account_index}, api={self._resolved_api_key_index}")
        return self._signer

    async def _fetch_and_cache_market_data(self, order_api: OrderApi, market_id: int):
        async with self.semaphore:
            try:
                details = await order_api.order_book_details(market_id=market_id)
                if details and details.order_book_details:
                    m = details.order_book_details[0]
                    if symbol := getattr(m, 'symbol', None):
                        normalized_symbol = f"{symbol}-USD" if not symbol.endswith("-USD") else symbol

                        # Standard-Metadaten
                        market_data = {
                            'i': m.market_id,
                            'sd': getattr(m, 'size_decimals', 8),
                            'pd': getattr(m, 'price_decimals', 6),
                            'ss': Decimal(getattr(m, 'step_size', '0.00000001')),
                            'mps': Decimal(getattr(m, 'min_price_step', '0.00001') or '0.00001'),
                            # NEU: ECHTE Min-Notional & Min-Quantity aus der API!
                            'min_notional': float(getattr(m, 'min_notional', 10.0)),
                            'min_quantity': float(getattr(m, 'min_quantity', 0.0)),
                        }

                        # Overrides für kaputte Märkte
                        if normalized_symbol in MARKET_OVERRIDES:
                            override = MARKET_OVERRIDES[normalized_symbol]
                            logger.warning(f"Lighter: Override für {normalized_symbol} anwenden")
                            market_data.update(override)

                        self.market_info[normalized_symbol] = market_data

                        if price := getattr(m, 'last_trade_price', None):
                            self.price_cache[normalized_symbol] = float(price)

                await asyncio.sleep(0.15)  # statt 0.1 → war zu aggressiv
            except Exception as e:
                logger.warning(f"Lighter: Markt-ID {market_id} nicht geladen: {e}")

    async def load_market_cache(self, force: bool = False):
        if not getattr(config, "LIVE_TRADING", False):
            logger.info("Lighter: Dry-Run – Market-Cache simuliert.")
            self.market_info = {"BTC-USD": {"i": 1, "sd": 6, "pd": 2, "min_notional": 10.0}}
            self.price_cache = {"BTC-USD": 60000.0}
            return

        if self.market_info and not force:
            return
        if force:
            self.market_info.clear()
            self.price_cache.clear()

        logger.info("Lighter: Aktualisiere Markt-Info & Preis-Cache...")
        signer = await self._get_signer()
        order_api = OrderApi(signer.api_client)
        try:
            market_list = await order_api.order_books()
            if not market_list or not market_list.order_books:
                logger.error("Lighter: Marktliste leer.")
                return

            tasks = [self._fetch_and_cache_market_data(order_api, m.market_id) for m in market_list.order_books if hasattr(m, 'market_id')]
            await asyncio.gather(*tasks)
            self._last_market_cache_at = time.time()
            logger.info(f"Lighter: {len(self.market_info)} Märkte geladen (inkl. min_notional).")
        except Exception as e:
            logger.error(f"Lighter: Kritischer Fehler beim Markt-Cache: {e}", exc_info=True)

    async def load_funding_rates_and_prices(self):
        if not getattr(config, "LIVE_TRADING", False):
            logger.info("Lighter: Dry-Run – Funding-Rates simuliert.")
            self.funding_cache = {"BTC-USD": 0.001}
            return

        signer = await self._get_signer()
        funding_api = FundingApi(signer.api_client)
        logger.info("Lighter: Lade Funding Rates...")
        try:
            fd_response = await funding_api.funding_rates()
            if fd_response and fd_response.funding_rates:
                rates_by_id = {fr.market_id: fr.rate for fr in fd_response.funding_rates}
                for symbol, data in self.market_info.items():
                    if rate := rates_by_id.get(data['i']):
                        self.funding_cache[symbol] = float(rate)  # Keine Division mehr!

        except Exception as e:
            logger.error(f"Lighter: Funding-Rates Fehler: {e}", exc_info=True)
        logger.info(f"Lighter: {len(self.funding_cache)} Funding-Rates geladen.")

    async def pause_then_load_funding(self, pause_seconds: int = 5):
        logger.info(f"Lighter: Warte {pause_seconds}s vor Funding-Update...")
        await asyncio.sleep(pause_seconds)
        await self.load_funding_rates_and_prices()

    def fetch_24h_vol(self, symbol: str) -> float:
        try:
            price = self.price_cache.get(symbol)
            if not price:
                return 0.0
            # Lighter hat keine direkte 24h % → wir schätzen über last_trade_price vs. 24h ago (falls verfügbar)
            # Aber für jetzt: einfach 0 zurückgeben → X10 ist präziser
            return 0.0
        except:
            return 0.0

    # NEU: Korrekte min_notional_usd nutzt jetzt das echte API-Feld!
    def min_notional_usd(self, symbol: str) -> float:
        return self.market_info.get(symbol, {}).get('min_notional', 20.0)

    def fetch_fees(self, symbol: str) -> dict:
        """Lighter hat (noch) keine per-Markt-Fees → aber wir bauen es robust"""
        if not config.DYNAMIC_FEES_ENABLED:
            return {"maker": config.MAKER_FEE_X10, "taker": config.TAKER_FEE_X10}  # gleiche wie X10

        # Lighter hat aktuell nur globale Fees, aber wir halten die Schnittstelle gleich
        try:
            # Hier könnte später eine API-Abfrage kommen
            return {"maker": 0.0002, "taker": 0.0005}  # aktueller Stand Nov 2025
        except:
            return {"maker": config.MAKER_FEE_X10, "taker": config.TAKER_FEE_X10}

    async def fetch_open_positions(self) -> Optional[List[dict]]:
        if not getattr(config, "LIVE_TRADING", False):
            return []
        try:
            signer = await self._get_signer()
            response = await AccountApi(signer.api_client).account(by="index", value=str(self._resolved_account_index))
            positions = []
            if response and response.accounts and response.accounts[0].positions:
                for p in response.accounts[0].positions:
                    size = float(p.position or 0.0) * (1 if int(p.sign) == 1 else -1)
                    if abs(size) > 1e-8:
                        symbol = f"{p.symbol}-USD" if not p.symbol.endswith("-USD") else p.symbol
                        positions.append({'symbol': symbol, 'size': size})
            logger.info(f"Lighter: {len(positions)} offene Position(en) gefunden.")
            return positions
        except Exception as e:
            logger.error(f"Lighter: Positionsabfrage Fehler: {e}", exc_info=True)
            return None

    # --- NEUE ECHTE BALANCE-ABFRAGE (November 2025 Fix) ---
    async def get_real_available_balance(self) -> float:
        try:
            signer = await self._get_signer()
            response = await AccountApi(signer.api_client).account(by="index", value=str(self._resolved_account_index))
            if response and response.accounts and response.accounts[0]:
                acc = response.accounts[0]
                
                # 1. Versuch: buying_power aus DetailedAccount (falls verfügbar)
                if hasattr(acc, 'total_asset_value') or hasattr(acc, 'buying_power'):
                    # Manche Endpoints geben DetailedAccount zurück
                    buying = getattr(acc, 'buying_power', None) or getattr(acc, 'total_asset_value', '0')
                    return float(buying or 0)
                
                # 2. Fallback: collateral minus locked (funktioniert immer)
                collateral = float(getattr(acc, 'collateral', '0'))
                available = float(getattr(acc, 'available_balance', '0') or '0')
                # buying_power ≈ collateral - locked margin
                estimated_buying_power = collateral * 0.95  # konservativ
                return max(available, estimated_buying_power)
                
            return 0.0
        except Exception as e:
            logger.warning(f"Lighter echte Balance-Abfrage fehlgeschlagen: {e}")
            return 0.0

    # KOMPLETT ÜBERARBEITETE SKALIERUNG – kein 10-USD-Hack mehr!
    def _scale_amounts(self, symbol: str, qty: Decimal, price: Decimal, side: str) -> Tuple[int, int]:
        data = self.market_info.get(symbol)
        if not data:
            raise ValueError(f"Metadaten für {symbol} fehlen.")

        sd = data.get('sd', 8)
        pd = data.get('pd', 6)
        step_size_dec = Decimal(str(data.get('ss', '0.00000001')))
        price_step_dec = Decimal(str(data.get('mps', '0.00001')))
        min_notional = Decimal(str(data.get('min_notional', '10.0')))

        # 1. Menge auf step_size runden (AUFRUNDEN für Sicherheit)
        final_qty = qty
        if step_size_dec > 0 and step_size_dec != Decimal('99999999'):  # nicht blacklisted
            final_qty = ((qty + step_size_dec - Decimal('1e-12')) // step_size_dec) * step_size_dec

        # 2. Min-Notional sicherstellen (ECHT!)
        current_notional = final_qty * price
        if current_notional < min_notional and price > 0:
            required_qty = (min_notional / price).quantize(Decimal('1E-12'), rounding=ROUND_UP)
            # Nochmal auf step_size bringen
            final_qty = ((required_qty + step_size_dec - Decimal('1e-12')) // step_size_dec) * step_size_dec
            logger.info(f"Lighter: Menge für {symbol} auf Min-Notional {min_notional} USD angehoben → {final_qty}")

        # 3. Preis runden
        if price_step_dec <= 0:
            price_step_dec = Decimal('1E-' + str(pd))
        if side.upper() == 'BUY':
            final_price = ((price + price_step_dec - Decimal('1e-12')) // price_step_dec) * price_step_dec
        else:
            final_price = (price // price_step_dec) * price_step_dec

        # 4. Skalierung zu int
        base_amount = int(final_qty * (Decimal(10) ** sd))
        price_int = int(final_price * (Decimal(10) ** pd))

        if base_amount <= 0:
            logger.error(f"Lighter: base_amount wurde 0 für {symbol} – Trade abgebrochen")
            return 0, 0

        return base_amount, price_int

    async def open_live_position(self, symbol: str, side: str, notional_usd: float, reduce_only: bool = False, post_only: bool = False) -> bool:
        if not getattr(config, "LIVE_TRADING", False):
            logger.info(f"Lighter: DRY-RUN Order {side} {symbol} ${notional_usd}")
            return True

        price = self.fetch_mark_price(symbol)
        if not price or price <= 0:
            logger.error(f"Lighter: Kein Preis für {symbol}")
            return False

        try:
            market_id = self.market_info[symbol]['i']
            slippage_pct = Decimal(str(getattr(config, "LIGHTER_MAX_SLIPPAGE_PCT", 0.6)))

            limit_price = Decimal(str(price)) * (Decimal(1) + slippage_pct / 100 if side.upper() == 'BUY' else Decimal(1) - slippage_pct / 100)
            qty = Decimal(str(notional_usd)) / limit_price

            base_amount, price_int = self._scale_amounts(symbol, qty, limit_price, side)
            if base_amount == 0:
                logger.error(f"Lighter: Skalierung ergab 0 für {symbol}")
                return False

            signer = await self._get_signer()
            logger.info(f"Lighter LIVE ORDER: {side} {symbol} qty≈{qty:.6f} @ {limit_price:.6f} (reduce_only={reduce_only})")

            tx_obj, response_obj, err = await signer.create_order(
                market_index=market_id,
                client_order_index=int(time.time() * 1000),
                base_amount=base_amount,
                price=price_int,
                is_ask=(side.upper() == 'SELL'),
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=reduce_only,
                trigger_price=SignerClient.NIL_TRIGGER_PRICE,
                order_expiry=SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
            )

            if err or not getattr(response_obj, 'tx_hash', None):
                logger.error(f"Lighter Order Fehler: {err or response_obj}")
                return False

            logger.info(f"Lighter Order erfolgreich: tx_hash={response_obj.tx_hash}")
            # Post-Only für Lighter ignorieren – bringt nichts bei 0% Fees
            _ = post_only
            return True

        except Exception as e:
            logger.error(f"Lighter open_live_position Exception: {e}", exc_info=True)
            # Post-Only für Lighter ignorieren – bringt nichts bei 0% Fees
            _ = post_only
            return False

    async def close_live_position(self, symbol: str, original_side: str, notional_usd: float) -> bool:
        close_side = 'SELL' if original_side.upper() == 'BUY' else 'BUY'
        return await self.open_live_position(symbol, close_side, notional_usd, reduce_only=True)

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        return self.funding_cache.get(symbol)

    def fetch_mark_price(self, symbol: str) -> Optional[float]:
        return self.price_cache.get(symbol)

    def fetch_next_funding_time(self, symbol: str):
        now = datetime.now()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return int(next_hour.timestamp())

    async def aclose(self):
        if self._signer:
            try:
                await self._signer.close()
            except Exception as e:
                logger.warning(f"LighterAdapter: Signer schließen fehlgeschlagen: {e}")
        logger.info("Lighter Adapter geschlossen.")