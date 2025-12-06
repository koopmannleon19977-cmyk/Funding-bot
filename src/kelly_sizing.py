"""
Kelly Criterion Position Sizing für Funding Rate Arbitrage

Das Kelly Criterion optimiert die Position Size basierend auf:
- Historische Win Rate
- Durchschnittlicher Gewinn vs. Verlust
- Konfidenz-Level der Opportunity

Wir verwenden "Fractional Kelly" (Quarter Kelly) für konservativeres Sizing.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from collections import deque
import time

logger = logging.getLogger(__name__)

# Importiere Config
try:
    import config
except ImportError:
    config = None


@dataclass
class TradeResult:
    """Ergebnis eines abgeschlossenen Trades."""
    symbol: str
    pnl_usd: float
    hold_time_seconds: float
    entry_apy: float
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
    
    @property
    def is_winner(self) -> bool:
        return self.pnl_usd > 0


@dataclass 
class KellyResult:
    """Ergebnis der Kelly-Berechnung."""
    kelly_fraction: float      # Rohe Kelly Fraction (0-1)
    safe_fraction: float       # Mit Safety Factor (Quarter Kelly)
    recommended_size_usd: float
    win_rate: float
    avg_win: float
    avg_loss: float
    sample_size: int
    confidence: str            # "LOW", "MEDIUM", "HIGH"


class KellyPositionSizer:
    """
    Berechnet optimale Position Sizes basierend auf Kelly Criterion.
    
    Features:
    - Tracking der letzten N Trades für Win Rate Berechnung
    - Symbol-spezifische Statistiken
    - Fractional Kelly für Risiko-Reduktion
    - Minimum Sample Size für statistische Signifikanz
    """
    
    # Konfiguration
    MIN_SAMPLE_SIZE = 20           # Mehr Samples bevor volle Kelly
    RAMP_UP_PERIOD = 50            # Graduelles Hochfahren über 50 Trades
    MAX_HISTORY_SIZE = 100         # Maximale Trade-History
    SAFETY_FACTOR = 0.5            # Half Kelly (aggressiver)
    MAX_POSITION_FRACTION = 0.25   # Maximal 25% des Kapitals pro Trade
    MIN_POSITION_FRACTION = 0.02   # Mindestens 2% des Kapitals
    
    # Default-Werte wenn keine History vorhanden
    # KONSERVATIVER: Lieber zu klein als zu groß bei Unsicherheit
    DEFAULT_WIN_RATE = 0.60        # 60% - konservativer Start
    DEFAULT_WIN_LOSS_RATIO = 1.0   # 1:1 - keine Annahme über Edge
    
    def __init__(self):
        # Globale Trade History
        self._trade_history: deque[TradeResult] = deque(maxlen=self.MAX_HISTORY_SIZE)
        
        # Symbol-spezifische History
        self._symbol_history: dict[str, deque[TradeResult]] = {}
        
        # Cache für Performance
        self._stats_cache: dict[str, tuple[float, KellyResult]] = {}
        self._cache_ttl = 60  # 60 Sekunden Cache
        
        # Lade Config-Werte falls vorhanden
        if config:
            self. SAFETY_FACTOR = getattr(config, 'KELLY_SAFETY_FACTOR', 0.25)
            self.MAX_POSITION_FRACTION = getattr(config, 'MAX_SINGLE_TRADE_RISK_PCT', 0.10)
        
        logger.info(f"🎲 KellyPositionSizer initialized (Safety={self.SAFETY_FACTOR}, MaxFraction={self.MAX_POSITION_FRACTION})")
    
    async def load_history_from_db(self, trade_repo) -> int:
        """
        Load closed trades from database into Kelly history.
        
        Call this on startup to restore Kelly's historical data.
        
        Args:
            trade_repo: TradeRepository instance
            
        Returns:
            Number of trades loaded
        """
        try:
            trades = await trade_repo.get_closed_trades_for_kelly(limit=self.MAX_HISTORY_SIZE)
            
            loaded = 0
            for t in trades:
                pnl = t.get('pnl', 0)
                if pnl is None:
                    continue
                    
                self.record_trade(
                    symbol=t.get('symbol', 'UNKNOWN'),
                    pnl_usd=float(pnl),
                    hold_time_seconds=float(t.get('hold_time_seconds', 3600)),
                    entry_apy=0.10  # Fallback - not stored in DB
                )
                loaded += 1
            
            if loaded > 0:
                # Calculate stats for logging
                trades_list = list(self._trade_history)
                winners = sum(1 for t in trades_list if t.is_winner)
                win_rate = winners / loaded if loaded > 0 else 0
                
                logger.info(
                    f"📂 Kelly loaded {loaded} historical trades from DB "
                    f"(Win Rate: {win_rate:.1%}, Winners: {winners}, Losers: {loaded - winners})"
                )
            else:
                logger.info("📂 Kelly: No historical trades found in DB")
                
            return loaded
            
        except Exception as e:
            logger.warning(f"⚠️ Kelly history load failed: {e}")
            return 0
    
    def record_trade(self, symbol: str, pnl_usd: float, hold_time_seconds: float, entry_apy: float):
        """Zeichnet einen abgeschlossenen Trade auf."""
        result = TradeResult(
            symbol=symbol,
            pnl_usd=pnl_usd,
            hold_time_seconds=hold_time_seconds,
            entry_apy=entry_apy
        )
        
        # Globale History
        self._trade_history. append(result)
        
        # Symbol-spezifische History
        if symbol not in self._symbol_history:
            self._symbol_history[symbol] = deque(maxlen=50)
        self._symbol_history[symbol].append(result)
        
        # Cache invalidieren
        self._stats_cache.pop(symbol, None)
        self._stats_cache. pop("__global__", None)
        
        status = "✅ WIN" if result.is_winner else "❌ LOSS"
        logger.debug(f"📊 Trade recorded: {symbol} {status} ${pnl_usd:.2f}")
    
    def _calculate_stats(self, trades: list[TradeResult]) -> tuple[float, float, float]:
        """Berechnet Win Rate, Avg Win, Avg Loss aus Trade-Liste."""
        if not trades:
            return self.DEFAULT_WIN_RATE, 1.0, 1.0
        
        winners = [t for t in trades if t. is_winner]
        losers = [t for t in trades if not t.is_winner]
        
        win_rate = len(winners) / len(trades) if trades else self.DEFAULT_WIN_RATE
        
        avg_win = sum(t.pnl_usd for t in winners) / len(winners) if winners else 1.0
        avg_loss = abs(sum(t. pnl_usd for t in losers) / len(losers)) if losers else 1.0
        
        # Prevent division by zero
        if avg_loss == 0:
            avg_loss = 0.01
        
        return win_rate, avg_win, avg_loss
    
    def _kelly_formula(self, win_rate: float, win_loss_ratio: float) -> float:
        """
        Berechnet die Kelly Fraction. 
        
        Formula: f* = (b × p - q) / b
        Wobei: b = win_loss_ratio, p = win_rate, q = 1 - p
        """
        if win_rate <= 0 or win_loss_ratio <= 0:
            return 0.0
        
        p = win_rate
        q = 1 - p
        b = win_loss_ratio
        
        kelly = (b * p - q) / b
        
        # Kelly kann negativ sein wenn Edge negativ ist
        return max(0.0, kelly)
    
    def calculate_position_size(
        self,
        symbol: str,
        available_capital: float,
        current_apy: float,
        use_symbol_stats: bool = True
    ) -> KellyResult:
        """
        Berechnet die optimale Position Size für ein Symbol.
        
        Args:
            symbol: Trading Pair (z.B. "BTC-USD")
            available_capital: Verfügbares Kapital in USD
            current_apy: Aktueller APY der Opportunity
            use_symbol_stats: True = Symbol-spezifische Stats, False = Globale Stats
        
        Returns:
            KellyResult mit empfohlener Position Size
        """
        cache_key = symbol if use_symbol_stats else "__global__"
        
        # Check Cache
        if cache_key in self._stats_cache:
            cached_time, cached_result = self._stats_cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                # Update nur die Size basierend auf aktuellem Kapital
                new_size = cached_result.safe_fraction * available_capital
                return KellyResult(
                    kelly_fraction=cached_result.kelly_fraction,
                    safe_fraction=cached_result.safe_fraction,
                    recommended_size_usd=new_size,
                    win_rate=cached_result.win_rate,
                    avg_win=cached_result.avg_win,
                    avg_loss=cached_result.avg_loss,
                    sample_size=cached_result.sample_size,
                    confidence=cached_result.confidence
                )
        
        # Hole relevante Trades
        if use_symbol_stats and symbol in self._symbol_history:
            trades = list(self._symbol_history[symbol])
        else:
            trades = list(self._trade_history)
        
        sample_size = len(trades)
        
        # Berechne Statistiken
        win_rate, avg_win, avg_loss = self._calculate_stats(trades)
        win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else self.DEFAULT_WIN_LOSS_RATIO
        
        # Kelly Fraction berechnen
        kelly_fraction = self._kelly_formula(win_rate, win_loss_ratio)
        
        # Safety Factor anwenden (Quarter Kelly)
        safe_fraction = kelly_fraction * self. SAFETY_FACTOR
        
        # ═══════════════════════════════════════════════════════════════
        # NEU: GRADUELLES RAMP-UP
        # ═══════════════════════════════════════════════════════════════
        if sample_size < self.MIN_SAMPLE_SIZE:
            # Sehr konservativ bei wenig Daten
            confidence = "LOW"
            # Nutze nur 50% des berechneten Kelly
            kelly_discount = 0.5
        elif sample_size < self.RAMP_UP_PERIOD:
            # Graduell hochfahren
            confidence = "MEDIUM"
            # Linear von 50% zu 100% über RAMP_UP_PERIOD
            ramp_factor = (sample_size - self.MIN_SAMPLE_SIZE) / (self.RAMP_UP_PERIOD - self.MIN_SAMPLE_SIZE)
            kelly_discount = 0.5 + (0.5 * ramp_factor)
        else:
            confidence = "HIGH"
            kelly_discount = 1.0
        
        # Apply discount
        safe_fraction = safe_fraction * kelly_discount
        
        # APY-basierter Bonus/Malus
        # Höherer APY = mehr Konfidenz = leicht größere Position
        apy_multiplier = self._apy_confidence_multiplier(current_apy)
        safe_fraction *= apy_multiplier
        
        # Grenzen anwenden
        safe_fraction = max(self.MIN_POSITION_FRACTION, 
                          min(self.MAX_POSITION_FRACTION, safe_fraction))
        
        # Finale Position Size
        recommended_size = safe_fraction * available_capital
        
        result = KellyResult(
            kelly_fraction=kelly_fraction,
            safe_fraction=safe_fraction,
            recommended_size_usd=recommended_size,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            sample_size=sample_size,
            confidence=confidence
        )
        
        # Cache speichern
        self._stats_cache[cache_key] = (time.time(), result)
        
        return result
    
    def _apy_confidence_multiplier(self, apy: float) -> float:
        """
        Gibt einen Multiplier basierend auf APY zurück. 
        
        Höherer APY = Höhere Konfidenz = Größere Position (bis zu einem Limit)
        """
        if apy < 0.10:      # < 10% APY
            return 0.7      # Reduzierte Size
        elif apy < 0.30:    # 10-30% APY
            return 1.0      # Normal
        elif apy < 0.50:    # 30-50% APY
            return 1.1      # Leicht erhöht
        elif apy < 1.00:    # 50-100% APY
            return 1.2      # Erhöht
        else:               # > 100% APY
            return 1.3      # Maximum Boost (aber Vorsicht!)
    
    def get_stats_summary(self) -> dict:
        """Gibt eine Zusammenfassung der Trading-Statistiken zurück."""
        trades = list(self._trade_history)
        
        if not trades:
            return {
                "total_trades": 0,
                "message": "No trades recorded yet"
            }
        
        winners = [t for t in trades if t. is_winner]
        losers = [t for t in trades if not t.is_winner]
        
        total_pnl = sum(t.pnl_usd for t in trades)
        
        return {
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": len(winners) / len(trades),
            "total_pnl_usd": total_pnl,
            "avg_pnl_per_trade": total_pnl / len(trades),
            "avg_win": sum(t.pnl_usd for t in winners) / len(winners) if winners else 0,
            "avg_loss": sum(t.pnl_usd for t in losers) / len(losers) if losers else 0,
            "symbols_traded": len(self._symbol_history),
            "best_symbol": self._get_best_symbol(),
            "worst_symbol": self._get_worst_symbol()
        }
    
    def _get_best_symbol(self) -> Optional[str]:
        """Findet das Symbol mit der besten Performance."""
        if not self._symbol_history:
            return None
        
        best = None
        best_pnl = float('-inf')
        
        for symbol, trades in self._symbol_history.items():
            total = sum(t.pnl_usd for t in trades)
            if total > best_pnl:
                best_pnl = total
                best = symbol
        
        return best
    
    def _get_worst_symbol(self) -> Optional[str]:
        """Findet das Symbol mit der schlechtesten Performance."""
        if not self._symbol_history:
            return None
        
        worst = None
        worst_pnl = float('inf')
        
        for symbol, trades in self._symbol_history.items():
            total = sum(t.pnl_usd for t in trades)
            if total < worst_pnl:
                worst_pnl = total
                worst = symbol
        
        return worst


# Singleton Instance
_kelly_sizer: Optional[KellyPositionSizer] = None


def get_kelly_sizer() -> KellyPositionSizer:
    """Gibt die Singleton-Instanz des Kelly Sizers zurück."""
    global _kelly_sizer
    if _kelly_sizer is None:
        _kelly_sizer = KellyPositionSizer()
    return _kelly_sizer


def calculate_smart_size(
    symbol: str,
    available_capital: float,
    current_apy: float,
    min_size: float = 5.0,
    max_size: float = 100.0
) -> float:
    """
    Convenience-Funktion für schnelle Size-Berechnung.
    
    Args:
        symbol: Trading Pair
        available_capital: Verfügbares Kapital
        current_apy: Aktueller APY
        min_size: Minimale Position Size
        max_size: Maximale Position Size
    
    Returns:
        Empfohlene Position Size in USD
    """
    sizer = get_kelly_sizer()
    result = sizer.calculate_position_size(symbol, available_capital, current_apy)
    
    # Grenzen anwenden
    size = max(min_size, min(max_size, result.recommended_size_usd))
    
    logger.info(
        f"📊 Kelly Size for {symbol}: ${size:.2f} "
        f"(Kelly={result.kelly_fraction:.3f}, Safe={result.safe_fraction:.3f}, "
        f"WinRate={result.win_rate:.1%}, Confidence={result.confidence})"
    )
    
    return size