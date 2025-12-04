# src/prediction_v2.py → FINAL & BULLETPROOF VERSION (Updated for BTC Correlation)

import asyncio
import json
import logging
import time
import numpy as np
from collections import deque
from pathlib import Path
from typing import Dict, Tuple, Optional, List

import config
from src.adapters.lighter_adapter import LighterAdapter
from src.adapters.x10_adapter import X10Adapter
from src.btc_correlation import get_btc_monitor  # <--- NEW IMPORT

logger = logging.getLogger(__name__)


class FundingPredictorV2:
    HISTORY_FILE = "data/prediction_history.json"
    SAVE_INTERVAL = 60  # Sekunden zwischen Auto-Saves
    
    def __init__(self):
        self.rate_history: Dict[str, deque] = {}
        self.ob_imbalance_history: Dict[str, deque] = {}
        self.max_history = 200
        
        # Orderbook imbalance cache (refreshed via WS)
        self.latest_ob_imbalance: Dict[str, float] = {}
        self.latest_oi_change: Dict[str, float] = {}
        self.oi_cache: Dict[str, float] = {}
        
        # BTC Monitor Reference
        self.btc_monitor = get_btc_monitor()
        
        # Auto-save task
        self._save_task: Optional[asyncio.Task] = None
        self._running = False
        
        logger.info("FundingPredictorV2 initialized with BTC Correlation")

    async def predict_next_funding_rate(
        self,
        symbol: str,
        current_lighter_rate: float,
        current_x10_rate: float,
        lighter_adapter: LighterAdapter,
        x10_adapter: X10Adapter,
        btc_price: float = 0.0 # Passed from caller
    ) -> Tuple[float, float, float]:
        
        # 1. Feed BTC Price to Monitor
        if btc_price > 0:
            self.btc_monitor.update_price(btc_price)

        # 2. Get BTC Safety Factor
        btc_regime, btc_factor = self.btc_monitor.get_market_regime()

        # === Basis-Berechnung ===
        current_rate = (current_lighter_rate + current_x10_rate) / 2
        divergence = current_lighter_rate - current_x10_rate
        abs_div = abs(divergence)

        # === Orderbook Imbalance ===
        imbalance = self.latest_ob_imbalance.get(symbol, 0.0)
        imbalance_score = 0.0
        if abs(imbalance) > 0.15:
            imbalance_score = np.tanh(abs(imbalance) * 8) * (1 if imbalance > 0 else -1)

        # === OI Velocity ===
        oi_velocity = self.latest_oi_change.get(symbol, 0.0)
        oi_score = np.tanh(oi_velocity * 5000)

        # === Historische Trends ===
        hist = self.rate_history.get(symbol, deque())
        vel, acc = self._vel_acc(hist)

        # === Prediction Calculation ===
        base_pred = current_rate + vel * 3600 + acc * 1800
        divergence_pull = -divergence * 0.85
        external_pressure = (imbalance_score * 0.00003) + (oi_score * 0.00002)

        predicted_rate = base_pred + divergence_pull + external_pressure
        
        # === Apply BTC Correlation ===
        # If BTC is crashing, Funding Rates drop. 
        # If we predict positive funding (Long bias), punish it with the btc_factor.
        if predicted_rate > 0 and btc_factor < 1.0:
            predicted_rate *= btc_factor
            
        delta = predicted_rate - current_rate

        # === Confidence ===
        confidence = 0.5
        confidence += min(0.30, abs_div * 15)
        confidence += min(0.25, abs(imbalance) * 1.2)
        confidence += min(0.20, abs(oi_velocity) * 0.00001)
        confidence += min(0.15, abs(vel) * 36000)
        
        # Penalize confidence if BTC market is chaotic
        if btc_regime == "CRASH":
            confidence *= 0.5
        elif btc_regime == "DUMP":
            confidence *= 0.8

        confidence = min(0.99, confidence)

        # Log critical events only (to avoid spam)
        if btc_factor < 0.8:
             logger.debug(f"⚠️ BTC {btc_regime}: Reduced prediction for {symbol} by factor {btc_factor}")

        return float(predicted_rate), float(delta), float(confidence)

    # === Live Data Feeds ===
    def update_orderbook_imbalance(self, symbol: str, imbalance: float):
        self.latest_ob_imbalance[symbol] = imbalance

    def update_oi_velocity(self, symbol: str, current_oi: float):
        now = time.time()
        prev_oi = self.oi_cache.get(symbol)
        if prev_oi is not None:
            velocity = (current_oi - prev_oi) / 300.0
            self.latest_oi_change[symbol] = velocity
        self.oi_cache[symbol] = current_oi
    
    def _update_history(self, d: Dict[str, deque], symbol: str, value: float, ts: float):
        if symbol not in d:
            d[symbol] = deque(maxlen=self.max_history)
        d[symbol].append((ts, value))

    def _vel_acc(self, history: deque) -> Tuple[float, float]:
        if len(history) < 10:
            return 0.0, 0.0
        values = np.array([v for _, v in list(history)[-30:]])
        dt = np.array([t for t, _ in list(history)[-30:]])
        if len(values) < 10:
            return 0.0, 0.0
        try:
            vel = np.gradient(values, dt)[-1] * 3600
            acc = np.gradient(np.gradient(values, dt), dt)[-1] * 3600
            return vel, acc
        except Exception:
            return 0.0, 0.0
    
    # === Compatibility Methods ===
    async def add_observation(
        self,
        symbol: str,
        rate_x10: float,
        rate_lighter: float,
        mark_price: float = 0.0,
        open_interest: float = 0.0,
        timestamp: Optional[int] = None
    ):
        """
        Add a new funding rate observation to history.
        Compatible with update_funding_data() from prediction.py
        """
        now = time.time()
        current_rate = (rate_lighter + rate_x10) / 2.0
        self._update_history(self.rate_history, symbol, current_rate, now)
        
        # Also update OI if provided
        if open_interest > 0:
            self.update_oi_velocity(symbol, open_interest)
    
    async def get_top_opportunities(
        self,
        symbols: List[str],
        min_apy: float = 10.0,
        min_confidence: float = 0.5,
        limit: int = 10,
        lighter_adapter = None,
        x10_adapter = None,
        btc_price: float = 0.0
    ) -> List[Dict]:
        """
        Get top trading opportunities sorted by expected APY.
        Compatible with get_best_opportunities() from prediction.py
        
        Returns list of dicts with: symbol, predicted_apy, confidence, should_trade
        """
        results = []
        
        for symbol in symbols:
            try:
                # Get current rates from adapters if available
                current_lighter_rate = 0.0
                current_x10_rate = 0.0
                
                if lighter_adapter:
                    try:
                        current_lighter_rate = lighter_adapter.fetch_funding_rate(symbol) or 0.0
                    except:
                        pass
                
                if x10_adapter:
                    try:
                        current_x10_rate = x10_adapter.fetch_funding_rate(symbol) or 0.0
                    except:
                        pass
                
                # Skip if no rates available
                if current_lighter_rate == 0.0 and current_x10_rate == 0.0:
                    continue
                
                # Get prediction
                pred_rate, delta, conf = await self.predict_next_funding_rate(
                    symbol=symbol,
                    current_lighter_rate=current_lighter_rate,
                    current_x10_rate=current_x10_rate,
                    lighter_adapter=lighter_adapter,
                    x10_adapter=x10_adapter,
                    btc_price=btc_price
                )
                
                # Calculate APY (annualized from hourly rate)
                predicted_apy = abs(pred_rate) * 24 * 365 * 100  # Convert to percentage
                
                # Filter by thresholds
                if predicted_apy >= min_apy and conf >= min_confidence:
                    results.append({
                        'symbol': symbol,
                        'predicted_apy': predicted_apy,
                        'confidence': conf,
                        'should_trade': True,
                        'predicted_rate': pred_rate,
                        'delta': delta
                    })
            except Exception as e:
                logger.debug(f"[PREDICT V2] {symbol}: Error: {e}")
        
        # Sort by APY * confidence (risk-adjusted)
        results.sort(
            key=lambda p: p['predicted_apy'] * p['confidence'],
            reverse=True
        )
        
        return results[:limit]

    # === Persistence Methods ===
    async def save_history(self):
        """Save rate history to disk for persistence across restarts"""
        try:
            history_data = {}
            for symbol, deque_data in self.rate_history.items():
                # Convert deque to list of (timestamp, rate) tuples
                history_data[symbol] = list(deque_data)
            
            # Ensure directory exists
            Path(self.HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.HISTORY_FILE, 'w') as f:
                json.dump({
                    'rate_history': history_data,
                    'oi_cache': self.oi_cache,
                    'saved_at': time.time()
                }, f)
            
            logger.debug(f"📸 Saved prediction history: {len(history_data)} symbols")
        except Exception as e:
            logger.warning(f"Failed to save prediction history: {e}")

    async def load_history(self):
        """Load rate history from disk"""
        try:
            if not Path(self.HISTORY_FILE).exists():
                logger.info("📂 No prediction history file found, starting fresh")
                return False
            
            with open(self.HISTORY_FILE, 'r') as f:
                data = json.load(f)
            
            # Check if data is too old (more than 24 hours)
            saved_at = data.get('saved_at', 0)
            if time.time() - saved_at > 86400:  # 24 hours
                logger.info("📂 Prediction history too old (>24h), starting fresh")
                return False
            
            # Restore rate history
            for symbol, history_list in data.get('rate_history', {}).items():
                self.rate_history[symbol] = deque(history_list, maxlen=self.max_history)
            
            # Restore OI cache
            self.oi_cache = data.get('oi_cache', {})
            
            loaded_count = len(self.rate_history)
            logger.info(f"📂 Loaded prediction history: {loaded_count} symbols")
            return True
            
        except Exception as e:
            logger.warning(f"Failed to load prediction history: {e}")
            return False

    async def start(self):
        """Start the predictor with history loading and auto-save"""
        if self._running:
            return
        
        # Load saved history
        await self.load_history()
        
        # Start auto-save task
        self._running = True
        self._save_task = asyncio.create_task(self._auto_save_loop())
        logger.info("✅ FundingPredictorV2 started with persistence")

    async def stop(self):
        """Stop the predictor and save history"""
        self._running = False
        
        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        
        # Final save
        await self.save_history()
        logger.info("✅ FundingPredictorV2 stopped, history saved")

    async def _auto_save_loop(self):
        """Periodically save history to disk"""
        while self._running:
            try:
                await asyncio.sleep(self.SAVE_INTERVAL)
                await self.save_history()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Auto-save error: {e}")

# Singleton
_predictor_v2: Optional[FundingPredictorV2] = None

def get_predictor() -> FundingPredictorV2:
    global _predictor_v2
    if _predictor_v2 is None:
        _predictor_v2 = FundingPredictorV2()
    return _predictor_v2