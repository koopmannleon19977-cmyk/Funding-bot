import asyncio
import logging
import json
import aiohttp
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional

import config
from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.orders import OrderSide

try:
    from x10.perpetual.orders import OrderTimeInForce as TimeInForce
except ImportError:
    from x10.perpetual.orders import TimeInForce

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
        self.price_cache = {}
        self.funding_cache = {}

        try:
            if config.X10_VAULT_ID:
                vault_id = int(str(config.X10_VAULT_ID).strip())
                
                self.stark_account = StarkPerpetualAccount(
                    vault=vault_id,
                    private_key=config.X10_PRIVATE_KEY,
                    public_key=config.X10_PUBLIC_KEY,
                    api_key=config.X10_API_KEY,
                )
                logger.info("✅ X10 Account initialisiert.")
            else:
                logger.warning("⚠️ X10 Config fehlt (Vault ID leer).")
        except Exception as e:
            logger.error(f"❌ X10 Account Init Error: {e}")

    async def get_order_fee(self, order_id: str) -> float:
        """
        X10 Flat Fee Structure:
        - Maker: 0.00% (Post-Only)
        - Taker: 0.025%
        Quelle: Extended Docs
        """
        return 0.0000

    async def start_websocket(self):
        """X10 Extended WebSocket"""
        base_host = "wss://api.starknet.extended.exchange"
        path = "/stream.extended.exchange/v1/publicTrades"
        url = f"{base_host}{path}"
        
        ws_failed_logged = False

        while True:
            try:
                session = aiohttp.ClientSession()
                async with session.ws_connect(url) as ws:
                    logger.info(f"🔌 X10 WebSocket verbunden: {path}")
                    ws_failed_logged = False
                    
                    if not self.market_info:
                        await self.load_market_cache(force=True)
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            
                            trigger_update = False
                            if "data" in data:
                                trades = data["data"]
                                if isinstance(trades, list):
                                    for trade in trades:
                                        sym = trade.get("m")
                                        px_str = trade.get("p")
                                        if sym and px_str:
                                            try:
                                                self.price_cache[sym] = float(px_str)
                                                trigger_update = True
                                            except ValueError:
                                                pass
                            
                            if trigger_update and hasattr(self, 'price_update_event'):
                                self.price_update_event.set()

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"X10 WS Error: {msg}")
                            break
            except Exception as e:
                if not ws_failed_logged:
                    logger.warning(f"⚠️ X10 WS Fehler ({e}). Nutze REST-Fallback...")
                    ws_failed_logged = True
                try:
                    await self.load_market_cache(force=True)
                    for name, m in self.market_info.items():
                        if hasattr(m.market_stats, "mark_price"):
                            self.price_cache[name] = float(m.market_stats.mark_price)
                    await asyncio.sleep(1.5)
                except:
                    await asyncio.sleep(5)
            finally:
                if 'session' in locals() and not session.closed:
                    await session.close()

    async def _get_auth_client(self) -> PerpetualTradingClient:
        if not self._auth_client:
            if not self.stark_account:
                raise RuntimeError("X10 Stark account missing")
            self._auth_client = PerpetualTradingClient(self.client_env, self.stark_account)
        return self._auth_client

    async def load_market_cache(self, force: bool = False):
        if self.market_info and not force:
            return
        client = PerpetualTradingClient(self.client_env)
        try:
            resp = await client.markets_info.get_markets()
            if resp and resp.data:
                for m in resp.data:
                    name = getattr(m, "name", "")
                    if name.endswith("-USD"):
                        self.market_info[name] = m
            logger.info(f"✅ X10: {len(self.market_info)} Märkte geladen")
        finally:
            await client.close()

    def fetch_mark_price(self, symbol: str):
        if symbol in self.price_cache:
            return self.price_cache[symbol]
        m = self.market_info.get(symbol)
        return float(m.market_stats.mark_price) if m and hasattr(m.market_stats, "mark_price") else None

    def fetch_funding_rate(self, symbol: str):
        """
        X10 Extended: STÜNDLICHE (1h) Funding Rates.
        Quelle: https://api.docs.extended.exchange/#markets-info
        """
        m = self.market_info.get(symbol)
        return float(m.market_stats.funding_rate) if m and hasattr(m.market_stats, "funding_rate") else None

    def fetch_24h_vol(self, symbol: str) -> float:
        try:
            m = self.market_info.get(symbol)
            if m:
                return abs(float(getattr(m.market_stats, "price_change_24h_pct", 0)))
        except:
            pass
        return 0.0

    def get_24h_change_pct(self, symbol: str = "BTC-USD") -> float:
        """24h-Preisänderung in Prozent"""
        try:
            m = self.market_info.get(symbol)
            if m and hasattr(m, "market_stats"):
                val = getattr(m.market_stats, "price_change_24h_pct", 0)
                return float(str(val)) * 100.0
        except:
            pass
        return 0.0

    def min_notional_usd(self, symbol: str) -> float:
        """
        🔧 UNIVERSAL: Berechnet echtes Minimum mit Safety-Buffer
        """
        HARD_MIN_USD = 15.0
        SAFETY_BUFFER = 1.10  # 10% Buffer
        
        m = self.market_info.get(symbol)
        if not m:
            return HARD_MIN_USD

        try:
            price = self.fetch_mark_price(symbol)
            if not price or price <= 0:
                return HARD_MIN_USD
            
            # X10: min_order_size ist in Base Currency
            min_size = Decimal(getattr(m.trading_config, "min_order_size", "0"))
            api_min = float(min_size * Decimal(str(price)))
            
            # Safety-Buffer (10% drauf)
            safe_min = api_min * SAFETY_BUFFER
            
            # Niemals unter HARD_MIN
            result = max(safe_min, HARD_MIN_USD)
            
            return result
            
        except Exception as e:
            logger.warning(f"⚠️ {symbol} min_notional_usd Error: {e}")
            return HARD_MIN_USD

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
            logger.info(f"X10: {len(positions)} offene Positionen")
            return positions
        except Exception as e:
            logger.error(f"X10 Positions Error: {e}")
            return []

    async def open_live_position(
        self, 
        symbol: str, 
        side: str, 
        notional_usd: float, 
        reduce_only: bool = False, 
        post_only: bool = True
    ) -> Tuple[bool, Optional[str]]:
        if not config.LIVE_TRADING:
            return True, None
        
        client = await self._get_auth_client()
        market = self.market_info.get(symbol)
        if not market:
            return False, None

        price = Decimal(str(self.fetch_mark_price(symbol) or 0))
        if price <= 0:
            return False, None

        slippage = Decimal(str(config.X10_MAX_SLIPPAGE_PCT)) / 100
        raw_price = price * (
            Decimal(1) + slippage if side == "BUY" else Decimal(1) - slippage
        )

        cfg = market.trading_config
        if hasattr(cfg, "round_price") and callable(cfg.round_price):
            try:
                limit_price = cfg.round_price(raw_price)
            except:
                limit_price = raw_price.quantize(
                    Decimal("0.01"), 
                    rounding=ROUND_UP if side == "BUY" else ROUND_DOWN
                )
        else:
            tick_size = Decimal(getattr(cfg, "min_price_change", "0.01"))
            if side == "BUY":
                limit_price = ((raw_price + tick_size - Decimal('1e-12')) // tick_size) * tick_size
            else:
                limit_price = (raw_price // tick_size) * tick_size

        qty = Decimal(str(notional_usd)) / limit_price
        step = Decimal(getattr(cfg, "min_order_size_change", "0"))
        min_size = Decimal(getattr(cfg, "min_order_size", "0"))
        if step > 0:
            qty = (qty // step) * step
            if qty < min_size:
                qty = ((qty // step) + 1) * step

        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        tif = TimeInForce.GTT
        if post_only:
            if hasattr(TimeInForce, "POST_ONLY"):
                tif = TimeInForce.POST_ONLY
            elif hasattr(TimeInForce, "PostOnly"):
                tif = TimeInForce.PostOnly
            else:
                post_only = False

        expire = datetime.now(timezone.utc) + timedelta(seconds=30 if post_only else 600)

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
                err_msg = str(resp.error)
                if reduce_only and (
                    "1137" in err_msg or 
                    "1138" in err_msg or 
                    "Position is missing" in err_msg or 
                    "Position is same side" in err_msg
                ):
                    logger.warning(f"X10: Position {symbol} bereits zu (1137/1138). Erfolg.")
                    return True, None
                
                logger.error(f"X10 Order Fail: {resp.error}")
                if post_only and "post only" in err_msg.lower():
                    logger.info("Retry ohne PostOnly...")
                    return await self.open_live_position(
                        symbol, side, notional_usd, reduce_only, post_only=False
                    )
                return False, None
                
            logger.info(f"✅ X10 Order: {resp.data.id}")
            return True, str(resp.data.id)
        except Exception as e:
            err_str = str(e)
            if reduce_only and (
                "1137" in err_str or 
                "1138" in err_str or 
                "Position is missing" in err_str or 
                "Position is same side" in err_str
            ):
                return True, None
            logger.error(f"X10 Order Exception: {e}")
            return False, None

    async def close_live_position(
        self, 
        symbol: str, 
        original_side: str, 
        notional_usd: float
    ) -> Tuple[bool, Optional[str]]:
        close_side = "SELL" if original_side == "BUY" else "BUY"
        return await self.open_live_position(
            symbol, close_side, notional_usd, reduce_only=True, post_only=False
        )
    
    async def get_real_available_balance(self) -> float:
        try:
            client = await self._get_auth_client()
            resp = await client.account.get_balance()
            if hasattr(resp, 'data'):
                available = getattr(resp.data, 'available_for_trade', None)
                if available:
                    return float(str(available))
            return 0.0
        except Exception as e:
            logger.error(f"❌ X10 Balance Error: {e}")
            return 0.0

    async def aclose(self):
        if self._auth_client:
            await self._auth_client.close()