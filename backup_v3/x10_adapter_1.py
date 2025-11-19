import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
import time
import config

from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide, TimeInForce
from x10.perpetual.accounts import StarkPerpetualAccount


def _mask(s: str | None, show: int = 4) -> str:
    if not s:
        return "<empty>"
    s = str(s)
    if len(s) <= show * 2:
        return f"{s[:1]}***{s[-1:]}"
    return f"{s[:show]}***{s[-show:]}"


class X10Adapter:
    def __init__(self):
        logging.info("Initialisiere X10Adapter...")
        self.market_info: dict[str, object] = {}
        self.client_env = MAINNET_CONFIG

        # Account-Objekt für Live-Handel (nur wenn .env vollständig)
        self.stark_account: StarkPerpetualAccount | None = None
        try:
            # Diagnose: Keys vorhanden?
            missing = []
            for name in ["X10_PRIVATE_KEY", "X10_PUBLIC_KEY", "X10_API_KEY", "X10_VAULT_ID"]:
                val = getattr(config, name, None)
                if val is None or str(val).strip() == "":
                    missing.append(name)

            if missing:
                logging.error(f"X10Adapter: Fehlende/Leere X10-Variablen: {', '.join(missing)}")
                logging.info(
                    "X10Adapter: Aktuelle (maskierte) Werte: "
                    f"VAULT_ID={_mask(config.X10_VAULT_ID)}, "
                    f"PRIVATE={_mask(config.X10_PRIVATE_KEY)}, "
                    f"PUBLIC={_mask(config.X10_PUBLIC_KEY)}, "
                    f"API={_mask(config.X10_API_KEY)}"
                )
                return

            # Vault-ID validieren
            try:
                vault_id_int = int(str(config.X10_VAULT_ID).strip())
            except Exception as e:
                logging.error(f"X10Adapter: X10_VAULT_ID ist nicht numerisch: '{config.X10_VAULT_ID}'. Fehler: {e}")
                return

            # Diagnose: Maskierte Anzeige vor Erstellung
            logging.info(
                "X10Adapter: Erstelle StarkPerpetualAccount mit "
                f"VAULT_ID={vault_id_int}, "
                f"PRIVATE={_mask(config.X10_PRIVATE_KEY)}, "
                f"PUBLIC={_mask(config.X10_PUBLIC_KEY)}, "
                f"API={_mask(config.X10_API_KEY)}"
            )

            self.stark_account = StarkPerpetualAccount(
                vault=vault_id_int,
                private_key=config.X10_PRIVATE_KEY,
                public_key=config.X10_PUBLIC_KEY,
                api_key=config.X10_API_KEY,
            )

        except Exception as e:
            logging.error(f"X10Adapter: Konnte StarkPerpetualAccount nicht erstellen. Fehler: {repr(e)}")

    async def _fetch_market_info_async(self):
        client: PerpetualTradingClient | None = None
        info: dict[str, object] = {}
        non_crypto_symbols = {
            "EUR-USD", "GBP-USD", "USD-CHF", "USD-JPY", "USD-CAD",
            "XAG-USD", "XAU-USD", "SPX-USD",
        }

        try:
            client = PerpetualTradingClient(self.client_env)
            stats_list = await client.markets_info.get_markets()

            if stats_list and stats_list.data:
                for market_data in stats_list.data:
                    market_name = getattr(market_data, "name", "")
                    if not market_name or market_name in non_crypto_symbols:
                        continue
                    if market_name.endswith("-USD"):
                        info[market_name] = market_data
            else:
                logging.warning("X10Adapter: Keine '.data' in der API-Antwort gefunden.")
            return info
        except Exception as e:
            logging.error(f"X10Adapter Fehler (_fetch_market_info_async): {e}")
            return {}
        finally:
            if client and hasattr(client, "close"):
                try:
                    await client.close()
                except Exception:
                    pass

    async def load_market_cache(self):
        logging.info("X10: Aktualisiere Markt-Info-Cache...")
        self.market_info = await self._fetch_market_info_async()
        logging.info(f"X10: {len(self.market_info)} PERP-Märkte gefunden.")

    def list_symbols(self):
        return list(self.market_info.keys())

    def fetch_mark_price(self, symbol: str) -> float | None:
        if symbol not in self.market_info:
            return None
        try:
            market_data = self.market_info[symbol]
            if getattr(market_data, "market_stats", None) and getattr(market_data.market_stats, "mark_price", None):
                return float(market_data.market_stats.mark_price)
            return None
        except Exception as e:
            logging.error(f"X10 Fehler (fetch_mark_price für {symbol}): {e}")
            return None

    def fetch_funding_rate(self, symbol: str) -> float | None:
        if symbol not in self.market_info:
            return None
        try:
            market_data = self.market_info[symbol]
            if getattr(market_data, "market_stats", None) and market_data.market_stats.funding_rate is not None:
                eight_hour_rate_decimal = float(market_data.market_stats.funding_rate)
                hourly_rate_decimal = eight_hour_rate_decimal / 8.0
                return None if hourly_rate_decimal is None else float(hourly_rate_decimal)
            return None
        except Exception as e:
            logging.error(f"X10 Fehler (fetch_funding_rate für {symbol}): {e}")
            return None

    # --- Helper: Runden und Sicherheits-Checks ---
    def _round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        market = self.market_info.get(symbol)
        if not market:
            return qty
        trading_cfg = getattr(market, "trading_config", None)
        if trading_cfg and hasattr(trading_cfg, "min_order_size"):
            min_size = Decimal(str(trading_cfg.min_order_size))
            if qty < min_size:
                qty = min_size
        if trading_cfg and hasattr(trading_cfg, "round_size"):
            try:
                return trading_cfg.round_size(qty)
            except Exception:
                pass
        
        # Fallback auf 4 Dezimalstellen (für BTC-Präzision)
        return qty.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

    def _aggressive_limit_price(self, symbol: str, side: OrderSide, epsilon_pct: float, fallback_mark: Decimal) -> Decimal:
        market = self.market_info.get(symbol)
        if not market or not getattr(market, "market_stats", None):
            return fallback_mark

        bid = getattr(market.market_stats, "bid_price", None)
        ask = getattr(market.market_stats, "ask_price", None)

        price = fallback_mark
        try:
            if side == OrderSide.BUY and ask:
                price = Decimal(str(ask)) * (Decimal(1) + Decimal(epsilon_pct) / Decimal(100))
            elif side == OrderSide.SELL and bid:
                price = Decimal(str(bid)) * (Decimal(1) - Decimal(epsilon_pct) / Decimal(100))
        except Exception:
            price = fallback_mark

        trading_cfg = getattr(market, "trading_config", None)
        if trading_cfg and hasattr(trading_cfg, "round_price"):
            try:
                return trading_cfg.round_price(price)
            except Exception:
                pass
        return price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    # --- LIVE-HANDEL ---
    async def open_live_position(self, symbol: str, side: str, position_size_usd: float) -> bool:
        if not self.stark_account:
            logging.error("X10Adapter: LIVE-HANDEL FEHLGESCHLAGEN. Stark-Account nicht initialisiert.")
            return False

        if symbol not in self.market_info:
            logging.error(f"X10Adapter: Kann {symbol} nicht handeln, nicht im Markt-Cache.")
            return False

        mark_price_f = self.fetch_mark_price(symbol)
        if not mark_price_f or mark_price_f <= 0:
            logging.error(f"X10Adapter: Kein gültiger Mark-Preis für {symbol}.")
            return False

        max_slippage_pct = float(getattr(config, "X10_MAX_SLIPPAGE_PCT", 0.5))
        epsilon_pct = float(getattr(config, "X10_PRICE_EPSILON_PCT", 0.2))

        try:
            mark_price = Decimal(str(mark_price_f))
            qty = Decimal(str(position_size_usd)) / mark_price
            qty = self._round_qty(symbol, qty)
        except Exception as e:
            logging.error(f"X10Adapter: Konnte Menge berechnen/runden: {e}")
            return False

        order_side = OrderSide.BUY if str(side).upper() == "BUY" else OrderSide.SELL
        limit_price = self._aggressive_limit_price(symbol, order_side, epsilon_pct, mark_price)

        try:
            slippage_pct = abs((limit_price - mark_price) / mark_price) * Decimal(100)
            
            # KORREKTUR: 'max_slip_pct' zu 'max_slippage_pct' geändert
            if slippage_pct > Decimal(str(max_slippage_pct)):
                logging.error(
                    f"X10Adapter: Slippage {slippage_pct:.3f}% > {max_slippage_pct}% – Abbruch. "
                    f"limit={limit_price} mark={mark_price}"
                )
                return False
        except Exception:
            pass

        logging.info(
            f"X10 LIVE ORDER: {order_side.name} {symbol} qty≈{qty} @ {limit_price} "
            f"(mark≈{mark_price}, eps={epsilon_pct}% , slippage≤{max_slippage_pct}%)"
        )

        client: PerpetualTradingClient | None = None
        try:
            client = PerpetualTradingClient(self.client_env, self.stark_account)
            external_id = f"bot-{symbol}-{order_side.name}-{int(time.time()*1000)}"

            from datetime import datetime, timedelta, timezone

            response_wrapper = await client.place_order(
                market_name=symbol,
                amount_of_synthetic=qty,
                price=limit_price,
                side=order_side,
                time_in_force=TimeInForce.GTT,         # FOK wird vom SDK abgelehnt
                expire_time=datetime.now(timezone.utc) + timedelta(minutes=2),  # kurze Laufzeit in UTC
                external_id=external_id,
)

            if response_wrapper.error:
                logging.error(f"X10 LIVE ORDER FEHLER: {response_wrapper.error}")
                return False

            placed = response_wrapper.data
            if placed and getattr(placed, "id", None) is not None:
                logging.info(f"X10 LIVE ORDER ERFOLGREICH: Order-ID {placed.id} (external_id={external_id})")
                return True

            logging.error(f"X10 LIVE ORDER FEHLER: Unerwartete API-Antwort: {response_wrapper}")
            return False

        except Exception as e:
            logging.error(f"X10 LIVE ORDER FEHLER (Exception): {e}")
            return False
        finally:
            if client and hasattr(client, "close"):
                try:
                    await client.close()
                except Exception:
                    pass

    async def close_live_position(self, symbol: str, side: str, position_size_usd: float) -> bool:
        logging.info(f"X10 LIVE CLOSE: Schließe {symbol} mit einer {side}-Order...")
        return await self.open_live_position(symbol, side, position_size_usd)

    async def aclose(self):
        logging.info("X10Adapter geschlossen.")
        pass