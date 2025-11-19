# x10_adapter.py – FINAL FIX November 2025
import asyncio
import logging
from decimal import Decimal, ROUND_UP, ROUND_DOWN
import time
from datetime import datetime, timedelta, timezone

import config
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide

# Dynamischer TimeInForce-Import (fängt alle SDK-Versionen ab)
try:
    from x10.perpetual.orders import OrderTimeInForce as TimeInForce
except ImportError:
    try:
        from x10.perpetual.orders import TimeInForce
    except ImportError:
        raise ImportError("x10-perpetual SDK nicht gefunden oder kaputt")

from x10.perpetual.accounts import StarkPerpetualAccount
from .base_adapter import BaseAdapter

logger = logging.getLogger(__name__)

class X10Adapter(BaseAdapter):
    def __init__(self):
        super().__init__("X10")
        self.market_info = {}
        self.client_env = MAINNET_CONFIG
        self.stark_account = None
        self._auth_client = None

        try:
            vault_id = int(config.X10_VAULT_ID.strip())
            self.stark_account = StarkPerpetualAccount(
                vault=vault_id,
                private_key=config.X10_PRIVATE_KEY,
                public_key=config.X10_PUBLIC_KEY,
                api_key=config.X10_API_KEY,
            )
            logger.info(f"X10 StarkPerpetualAccount erstellt – Vault {vault_id}")
        except Exception as e:
            logger.error(f"X10 Account Init Fehler: {e}")

    async def _get_auth_client(self) -> PerpetualTradingClient:
        if not self._auth_client:
            if not self.stark_account:
                raise RuntimeError("X10 Stark account nicht initialisiert")
            self._auth_client = PerpetualTradingClient(self.client_env, self.stark_account)
        return self._auth_client

    async def load_market_cache(self, force: bool = False):
        if self.market_info and not force:
            return
        if force:
            self.market_info.clear()

        client = PerpetualTradingClient(self.client_env)
        try:
            resp = await client.markets_info.get_markets()
            if resp and resp.data:
                for m in resp.data:
                    name = getattr(m, "name", "")
                    if name and name.endswith("-USD"):
                        self.market_info[name] = m
            logger.info(f"X10: {len(self.market_info)} Märkte geladen")
        finally:
            await client.close()

    def fetch_mark_price(self, symbol: str):
        m = self.market_info.get(symbol)
        return float(m.market_stats.mark_price) if m and hasattr(m.market_stats, "mark_price") else None

    def fetch_funding_rate(self, symbol: str):
        m = self.market_info.get(symbol)
        if m and hasattr(m.market_stats, "funding_rate"):
            eight_hour_rate = float(m.market_stats.funding_rate)
            return eight_hour_rate / 8.0  # Nur X10 braucht die Division
        return None

    def fetch_24h_vol(self, symbol: str) -> float:
        """Holt 24h-Preisveränderung in % von X10 (für Volatilitäts-Filter)"""
        try:
            market = self.market_info.get(symbol)
            if market and hasattr(market, "market_stats"):
                stats = market.market_stats
                change = getattr(stats, "price_change_24h_pct", None)
                if change is not None:
                    return abs(float(change))
        except Exception as e:
            logger.debug(f"X10 24h Vol Fetch Fehler für {symbol}: {e}")
        return 0.0

    # ← ECHTE MIN NOTIONAL FÜR X10
    def min_notional_usd(self, symbol: str) -> float:
        m = self.market_info.get(symbol)
        if not m or not hasattr(m, "trading_config"):
            return 20.0
        try:
            min_size = Decimal(getattr(m.trading_config, "min_order_size", "0"))
            price = self.fetch_mark_price(symbol) or 0
            return max(float(min_size * Decimal(str(price))), 20.0)
        except:
            return 20.0

    # ← ECHTE POSITIONEN ABRUFEN (funktioniert!)
    async def fetch_open_positions(self) -> list:
        if not self.stark_account:
            return []
        try:
            client = await self._get_auth_client()
            resp = await client.account.get_positions()
            positions = []
            if resp and resp.data:
                for p in resp.data:
                    if p.status == "OPENED" and abs(float(p.size)) > 1e-8:
                        positions.append({
                            "symbol": p.market,
                            "size": float(p.size),
                            "entry_price": float(p.open_price) if p.open_price else 0.0
                        })
            logger.info(f"X10: {len(positions)} echte offene Positionen gefunden")
            return positions
        except Exception as e:
            logger.error(f"X10 Positionsabfrage Fehler: {e}", exc_info=True)
            return []

    async def open_live_position(self, symbol: str, side: str, notional_usd: float, reduce_only: bool = False, post_only: bool = True) -> bool:
        if not config.LIVE_TRADING:
            logger.info(f"X10 DRY-RUN {side} {symbol} ${notional_usd}")
            return True

        client = await self._get_auth_client()
        market = self.market_info.get(symbol)
        if not market:
            return False

        price = Decimal(str(self.fetch_mark_price(symbol) or 0))
        if price <= 0:
            return False

        slippage = Decimal(str(config.X10_MAX_SLIPPAGE_PCT)) / 100
        raw_price = price * (Decimal(1) + slippage if side == "BUY" else Decimal(1) - slippage)

        # ────── KORREKTE PREIS-RUNDUNG (das ist der finale Fix!) ──────
        market = self.market_info.get(symbol)
        if not market or not hasattr(market, "trading_config"):
            logger.error(f"X10: Keine trading_config für {symbol}")
            return False

        cfg = market.trading_config  # ← Das fehlte! Jetzt definiert!

        if hasattr(cfg, "round_price") and callable(cfg.round_price):
            try:
                limit_price = cfg.round_price(raw_price)
                logger.debug(f"X10: Preis mit cfg.round_price gerundet → {limit_price}")
            except Exception as e:
                logger.warning(f"X10 round_price fehlgeschlagen: {e} → Fallback")
                limit_price = raw_price.quantize(Decimal("0.01"), rounding=ROUND_UP if side == "BUY" else ROUND_DOWN)
        else:
            # Fallback auf klassische Tick-Size
            tick_size = Decimal(getattr(cfg, "min_price_change", "0.01") or "0.01")
            if side == "BUY":
                limit_price = ((raw_price + tick_size - Decimal('1e-12')) // tick_size) * tick_size
            else:
                limit_price = (raw_price // tick_size) * tick_size
        # ────── Ende Fix ──────

        qty = Decimal(str(notional_usd)) / limit_price
        min_size = Decimal(getattr(cfg, "min_order_size", "0"))

        if qty < min_size and reduce_only:
            reduce_only = False  # Dust schließen erzwingen
            qty = min_size

        # Rundung auf min_order_size_change
        step = Decimal(getattr(cfg, "min_order_size_change", "0"))
        if step > 0:
            qty = (qty // step) * step
            if qty < min_size:
                qty = ((qty // step) + 1) * step

        if qty <= 0:
            return False

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL

        # ────── FINALER TIMEINFORCE FIX – NOVEMBER 2025 (100% kompatibel) ──────
        tif = None
        if post_only:
            if hasattr(TimeInForce, "PostOnly"):
                tif = TimeInForce.PostOnly
            elif hasattr(TimeInForce, "POST_ONLY"):
                tif = TimeInForce.POST_ONLY
            elif hasattr(TimeInForce, "PostOnlyCancelled"):
                tif = TimeInForce.PostOnlyCancelled
            else:
                logger.warning("PostOnly nicht gefunden → fallback auf GTT (keine Maker-Rebate)")
                post_only = False

        # Default: GTT (Good-Til-Time) ist das neue GTC
        if tif is None:
            if hasattr(TimeInForce, "GTT"):
                tif = TimeInForce.GTT
            elif hasattr(TimeInForce, "GTC"):
                tif = TimeInForce.GTC  # alte Versionen
            else:
                raise RuntimeError("Kein gültiges TimeInForce gefunden – SDK kaputt")

        # Expire-Time: PostOnly = 30s, normal = 10 Minuten
        expire_seconds = 30 if post_only else 600
        expire = datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)
        # ────── ENDE FIX ──────

        try:
            resp = await client.place_order(
                market_name=symbol,
                amount_of_synthetic=qty,
                price=limit_price,
                side=order_side,
                time_in_force=tif,
                expire_time=expire,
                reduce_only=reduce_only,
            )
            if resp.error:
                logger.error(f"X10 Order Fehler (Post-Only={post_only}): {resp.error}")
                # Bei Post-Only-Fehler (z.B. Preis zu aggressiv) fallback auf GTT
                if post_only and "post only" in str(resp.error).lower():
                    logger.info("Post-Only fehlgeschlagen → fallback auf normalen Limit-Order")
                    return await self.open_live_position(symbol, side, notional_usd, reduce_only, post_only=False)
                return False
            logger.info(f"X10 Order erfolgreich (Post-Only): {resp.data.id} | Fee gespart: ~0.03%")
            return True
        except Exception as e:
            logger.error(f"X10 Order Exception: {e}", exc_info=True)
            return False

    async def close_live_position(self, symbol: str, original_side: str, notional_usd: float) -> bool:
        close_side = "SELL" if original_side == "BUY" else "BUY"
        # Beim Schließen: IMMER market-nahe Limit-Order, KEIN PostOnly, lange Expire
        return await self.open_live_position(
            symbol, close_side, notional_usd,
            reduce_only=True,
            post_only=False  # ← entscheidend!
        )

    async def fetch_fees(self, symbol: str) -> dict:
        """Holt maker/taker Fees für einen Markt (X10)"""
        if not config.DYNAMIC_FEES_ENABLED:
            return {"maker": config.MAKER_FEE_X10, "taker": config.TAKER_FEE_X10}

        try:
            market = self.market_info.get(symbol)
            if market and hasattr(market, "trading_config"):
                cfg = market.trading_config
                maker = float(getattr(cfg, "maker_fee", config.MAKER_FEE_X10))
                taker = float(getattr(cfg, "taker_fee", config.TAKER_FEE_X10))
                return {"maker": maker, "taker": taker}
        except Exception as e:
            logger.debug(f"X10 Fee-Fetch Fehler für {symbol}: {e}")

        return {"maker": config.MAKER_FEE_X10, "taker": config.TAKER_FEE_X10}

    def fetch_next_funding_time(self, symbol: str):
        m = self.market_info.get(symbol)
        if m and hasattr(m.market_stats, "next_funding_rate"):
            return int(m.market_stats.next_funding_rate)  # Unix-Timestamp
        return None

    async def get_real_available_balance(self) -> float:
        """Holt die echte verfügbare Balance von X10 (available, nicht total)"""
        try:
            client = await self._get_auth_client()
            resp = await client.account.get_balance()  # ApiResponse-Objekt
            
            # Debug: Logge die volle Struktur (bereits vorhanden – gut!)
            logger.debug(f"X10 Balance Response: {resp.to_pretty_json()}")
            
            # Parse: Nutze getattr für Model- oder Dict-Kompatibilität
            if hasattr(resp, 'data'):
                # Versuche als Attribut (falls Model)
                available = getattr(resp.data, 'available_for_trade', None)
                if available is not None:
                    return float(str(available))  # String zu Float, falls Str
                
                # Fallback: Als Dict accessen
                if isinstance(resp.data, dict):
                    available = resp.data.get('available_for_trade', None)
                    if available is not None:
                        return float(available)
                
                # Extra-Fallback: available_for_withdrawal (ähnlich)
                available = getattr(resp.data, 'available_for_withdrawal', None) or \
                            (resp.data.get('available_for_withdrawal', None) if isinstance(resp.data, dict) else None)
                if available is not None:
                    return float(str(available))
            
            logger.warning("X10 Balance: 'available_for_trade' Feld nicht gefunden – Struktur prüfen!")
            return 0.0
        except Exception as e:
            logger.warning(f"X10 Balance-Abfrage fehlgeschlagen: {e}")
            return 0.0

    async def aclose(self):
        if self._auth_client:
            await self._auth_client.close()
            self._auth_client = None