"""
Kelly Criterion Position Sizing für Funding Rate Arbitrage

Das Kelly Criterion optimiert die Position Size basierend auf:
- Historische Win Rate
- Durchschnittlicher Gewinn vs. Verlust
- Konfidenz-Level der Opportunity

Wir verwenden "Fractional Kelly" (Quarter Kelly) für konservativeres Sizing.

IMPORTANT: All PnL calculations use Decimal for precision.
"""

import logging
from dataclasses import dataclass
from typing import Optional, Union
from collections import deque
from decimal import Decimal, ROUND_HALF_UP
import time

from src.utils import safe_decimal, quantize_usd, USD_PRECISION

logger = logging.getLogger(__name__)

# Importiere Config
try:
    import config
except ImportError:
    config = None


@dataclass
class TradeResult:
    """
    Ergebnis eines abgeschlossenen Trades.
    
    IMPORTANT: pnl_usd is stored as Decimal for precision.
    """
    symbol: str
    pnl_usd: Decimal  # Changed from float to Decimal
    hold_time_seconds: float
    entry_apy: float
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
        # Ensure pnl_usd is Decimal
        if not isinstance(self.pnl_usd, Decimal):
            self.pnl_usd = safe_decimal(self.pnl_usd)
    
    @property
    def is_winner(self) -> bool:
        return self.pnl_usd > Decimal('0')
    
    @property
    def pnl_float(self) -> float:
        """Return pnl as float for APIs that require it."""
        return float(self.pnl_usd)


@dataclass 
class KellyResult:
    """
    Ergebnis der Kelly-Berechnung.
    
    Financial values (recommended_size_usd, avg_win, avg_loss) are Decimal.
    Ratios (kelly_fraction, safe_fraction, win_rate) remain float as they are percentages.
    """
    kelly_fraction: float           # Rohe Kelly Fraction (0-1)
    safe_fraction: float            # Mit Safety Factor (Quarter Kelly)
    recommended_size_usd: Decimal   # Changed to Decimal
    win_rate: float
    avg_win: Decimal                # Changed to Decimal
    avg_loss: Decimal               # Changed to Decimal
    sample_size: int
    confidence: str                 # "LOW", "MEDIUM", "HIGH"
    
    @property
    def recommended_size_float(self) -> float:
        """Return size as float for APIs that require it."""
        return float(self.recommended_size_usd)


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
            self.SAFETY_FACTOR = getattr(config, 'KELLY_SAFETY_FACTOR', 0.25)
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
            skipped_zero = 0
            for t in trades:
                pnl = t.get('pnl', 0)
                if pnl is None:
                    continue
                
                # Convert to Decimal for precision
                pnl_decimal = safe_decimal(pnl)
                
                # Skip trades with zero PnL (likely data recording bug)
                if pnl_decimal == Decimal('0'):
                    skipped_zero += 1
                    continue
                    
                self.record_trade(
                    symbol=t.get('symbol', 'UNKNOWN'),
                    pnl_usd=pnl_decimal,  # Already Decimal
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
                if skipped_zero > 0:
                    logger.warning(
                        f"⚠️ Kelly skipped {skipped_zero} trades with $0.00 PnL "
                        f"(possible data recording bug)"
                    )
            else:
                if skipped_zero > 0:
                    logger.warning(
                        f"⚠️ Kelly: All {skipped_zero} trades had $0.00 PnL. "
                        f"This is expected for new deployments or if PnL tracking was recently enabled. "
                        f"Proceeding with defaults."
                    )
                else:
                    logger.info("📂 Kelly: No historical trades found in DB")
                
            return loaded
            
        except Exception as e:
            logger.warning(f"⚠️ Kelly history load failed: {e}")
            return 0
    
    def record_trade(
        self, 
        symbol: str, 
        pnl_usd: Union[float, Decimal, str], 
        hold_time_seconds: float, 
        entry_apy: float
    ):
        """
        Zeichnet einen abgeschlossenen Trade auf.
        
        Args:
            symbol: Trading pair
            pnl_usd: PnL in USD (will be converted to Decimal)
            hold_time_seconds: How long the trade was held
            entry_apy: APY at entry
        """
        # Convert to Decimal for precision
        pnl_decimal = safe_decimal(pnl_usd)
        
        result = TradeResult(
            symbol=symbol,
            pnl_usd=pnl_decimal,
            hold_time_seconds=hold_time_seconds,
            entry_apy=entry_apy
        )
        
        # Globale History
        self._trade_history.append(result)
        
        # Symbol-spezifische History
        if symbol not in self._symbol_history:
            self._symbol_history[symbol] = deque(maxlen=50)
        self._symbol_history[symbol].append(result)
        
        # Cache invalidieren
        self._stats_cache.pop(symbol, None)
        self._stats_cache.pop("__global__", None)
        
        status = "✅ WIN" if result.is_winner else "❌ LOSS"
        logger.debug(f"📊 Trade recorded: {symbol} {status} ${float(pnl_decimal):.4f}")
    
    def _calculate_stats(self, trades: list[TradeResult]) -> tuple[float, Decimal, Decimal]:
        """
        Berechnet Win Rate, Avg Win, Avg Loss aus Trade-Liste.
        
        Returns:
            (win_rate: float, avg_win: Decimal, avg_loss: Decimal)
        """
        if not trades:
            return self.DEFAULT_WIN_RATE, Decimal('1.0'), Decimal('1.0')
        
        winners = [t for t in trades if t.is_winner]
        losers = [t for t in trades if not t.is_winner]
        
        win_rate = len(winners) / len(trades) if trades else self.DEFAULT_WIN_RATE
        
        # Use Decimal for PnL calculations
        if winners:
            total_wins = sum((t.pnl_usd for t in winners), Decimal('0'))
            avg_win = total_wins / Decimal(str(len(winners)))
        else:
            avg_win = Decimal('1.0')
        
        if losers:
            total_losses = sum((t.pnl_usd for t in losers), Decimal('0'))
            avg_loss = abs(total_losses / Decimal(str(len(losers))))
        else:
            avg_loss = Decimal('1.0')
        
        # Prevent division by zero
        if avg_loss == Decimal('0'):
            avg_loss = Decimal('0.01')
        
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
        available_capital: Union[float, Decimal],
        current_apy: float,
        use_symbol_stats: bool = True
    ) -> KellyResult:
        """
        Berechnet die optimale Position Size für ein Symbol.
        
        HACK: Kelly Logic bypassed! Returns fixed size from config.DESIRED_NOTIONAL_USD.
        """
        # HACK: Immer die Config-Größe nehmen
        fixed_val = getattr(config, 'DESIRED_NOTIONAL_USD', 60.0)
        fixed_size = safe_decimal(fixed_val)
        
        return KellyResult(
            kelly_fraction=1.0,
            safe_fraction=1.0,
            recommended_size_usd=fixed_size,
            win_rate=0.5, # Dummy
            avg_win=Decimal('1'),
            avg_loss=Decimal('1'),
            sample_size=0,
            confidence="FIXED_HACK"
        )
    
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
        
        winners = [t for t in trades if t.is_winner]
        losers = [t for t in trades if not t.is_winner]
        
        # Use Decimal for PnL sums
        total_pnl = sum((t.pnl_usd for t in trades), Decimal('0'))
        
        avg_win = Decimal('0')
        if winners:
            avg_win = sum((t.pnl_usd for t in winners), Decimal('0')) / Decimal(str(len(winners)))
        
        avg_loss = Decimal('0')
        if losers:
            avg_loss = sum((t.pnl_usd for t in losers), Decimal('0')) / Decimal(str(len(losers)))
        
        return {
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": len(winners) / len(trades),
            "total_pnl_usd": float(total_pnl),  # Convert for JSON serialization
            "avg_pnl_per_trade": float(total_pnl / Decimal(str(len(trades)))),
            "avg_win": float(avg_win),
            "avg_loss": float(avg_loss),
            "symbols_traded": len(self._symbol_history),
            "best_symbol": self._get_best_symbol(),
            "worst_symbol": self._get_worst_symbol()
        }
    
    def _get_best_symbol(self) -> Optional[str]:
        """Findet das Symbol mit der besten Performance."""
        if not self._symbol_history:
            return None
        
        best = None
        best_pnl = Decimal('-999999999')  # Use Decimal instead of float('-inf')
        
        for symbol, trades in self._symbol_history.items():
            total = sum((t.pnl_usd for t in trades), Decimal('0'))
            if total > best_pnl:
                best_pnl = total
                best = symbol
        
        return best
    
    def _get_worst_symbol(self) -> Optional[str]:
        """Findet das Symbol mit der schlechtesten Performance."""
        if not self._symbol_history:
            return None
        
        worst = None
        worst_pnl = Decimal('999999999')  # Use Decimal instead of float('inf')
        
        for symbol, trades in self._symbol_history.items():
            total = sum((t.pnl_usd for t in trades), Decimal('0'))
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
    available_capital: Union[float, Decimal],
    current_apy: float,
    min_size: Union[float, Decimal] = 5.0,
    max_size: Union[float, Decimal] = 100.0
) -> Decimal:
    """
    Convenience-Funktion für schnelle Size-Berechnung.
    
    Args:
        symbol: Trading Pair
        available_capital: Verfügbares Kapital
        current_apy: Aktueller APY
        min_size: Minimale Position Size
        max_size: Maximale Position Size
    
    Returns:
        Empfohlene Position Size in USD (Decimal)
    """
    sizer = get_kelly_sizer()
    result = sizer.calculate_position_size(symbol, available_capital, current_apy)
    
    # Convert limits to Decimal
    min_decimal = safe_decimal(min_size)
    max_decimal = safe_decimal(max_size)
    
    # Grenzen anwenden
    size = max(min_decimal, min(max_decimal, result.recommended_size_usd))
    size = quantize_usd(size)
    
    logger.info(
        f"📊 Kelly Size for {symbol}: ${float(size):.2f} "
        f"(Kelly={result.kelly_fraction:.3f}, Safe={result.safe_fraction:.3f}, "
        f"WinRate={result.win_rate:.1%}, Confidence={result.confidence})"
    )
    
    return size

