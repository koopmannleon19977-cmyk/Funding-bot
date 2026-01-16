"""
Professional Exit Strategies - Research Implementation.

Basierend auf Hedge Fund, Quant und Institutional Best Practices.
Diese Datei enthält neue Exit-Strategien zur Integration in den Bot.

WICHTIG: Dies ist ein KONZEPT-Modul. Vor Produktiveinsatz:
1. Unit Tests schreiben
2. Backtesting durchführen
3. Paper Trading validieren
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

# =============================================================================
# RESULT TYPES
# =============================================================================


@dataclass(frozen=True)
class ExitSignal:
    """Result of an exit strategy evaluation."""

    should_exit: bool
    reason: str
    confidence: Decimal = Decimal("1.0")  # 0.0-1.0 confidence level
    priority: int = 5  # 1=highest priority (emergency), 10=lowest


# =============================================================================
# 1. Z-SCORE MEAN REVERSION EXIT
# =============================================================================


class ZScoreExitStrategy:
    """
    Exit basierend auf statistischer Z-Score Mean Reversion.

    Verwendet von: Renaissance Technologies, Two Sigma, Citadel

    Prinzip:
    - Z-Score = (current_value - mean) / std_dev
    - Exit wenn APY zur statistischen Norm zurückkehrt (Z < -1.0)
    - Emergency Exit bei Z < -2.0
    """

    def __init__(
        self,
        exit_threshold: Decimal = Decimal("-1.0"),
        emergency_threshold: Decimal = Decimal("-2.0"),
        min_samples: int = 6,  # Minimum 6 Stunden Daten
    ):
        self.exit_threshold = exit_threshold
        self.emergency_threshold = emergency_threshold
        self.min_samples = min_samples

    def calculate_z_score(
        self,
        current_value: Decimal,
        historical_values: list[Decimal],
    ) -> Decimal | None:
        """
        Berechne Z-Score für aktuellen Wert.

        Returns None wenn nicht genug Daten vorhanden.
        """
        if len(historical_values) < self.min_samples:
            return None

        n = len(historical_values)
        mean: Decimal = sum(historical_values, Decimal("0")) / Decimal(n)

        # Varianz berechnen (use Decimal arithmetic)
        variance: Decimal = sum(((x - mean) ** 2 for x in historical_values), Decimal("0")) / Decimal(n)
        std_dev = variance.sqrt()

        if std_dev == 0:
            return Decimal("0")

        return (current_value - mean) / std_dev

    def evaluate(
        self,
        current_apy: Decimal,
        historical_apy: list[Decimal],
    ) -> ExitSignal:
        """Evaluate Z-Score exit condition."""
        z_score = self.calculate_z_score(current_apy, historical_apy)

        if z_score is None:
            return ExitSignal(
                should_exit=False,
                reason=f"Z-SCORE: Insufficient data ({len(historical_apy)}/{self.min_samples} samples)",
                priority=10,
            )

        # Emergency Exit bei extremem negativem Z-Score
        if z_score <= self.emergency_threshold:
            return ExitSignal(
                should_exit=True,
                reason=f"Z-SCORE EMERGENCY: {z_score:.2f} <= {self.emergency_threshold:.2f}",
                confidence=Decimal("0.95"),
                priority=1,
            )

        # Standard Exit bei negativem Z-Score
        if z_score <= self.exit_threshold:
            return ExitSignal(
                should_exit=True,
                reason=f"Z-SCORE REVERT: {z_score:.2f} <= {self.exit_threshold:.2f} (mean zone)",
                confidence=Decimal("0.80"),
                priority=3,
            )

        return ExitSignal(
            should_exit=False,
            reason=f"Z-SCORE HOLD: {z_score:.2f} > {self.exit_threshold:.2f}",
            priority=10,
        )


# =============================================================================
# 2. ATR-BASED DYNAMIC TRAILING STOP
# =============================================================================


class ATRTrailingStopStrategy:
    """
    Volatilitäts-angepasster Trailing Stop basierend auf ATR.

    Vorteile:
    - Weitet sich bei hoher Volatilität → verhindert vorzeitige Exits
    - Verengt sich bei niedriger Volatilität → schützt Gewinne besser

    Parameter:
    - atr_period: Anzahl Perioden für ATR-Berechnung (typisch 14)
    - multiplier: ATR-Multiplikator für Stop-Distance (1.5-3.5)
    """

    def __init__(
        self,
        atr_period: int = 14,
        multiplier: Decimal = Decimal("2.0"),
        min_activation_profit: Decimal = Decimal("3.0"),
    ):
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.min_activation_profit = min_activation_profit

    def calculate_atr(
        self,
        price_data: list[dict],  # [{"high": x, "low": y, "close": z}, ...]
    ) -> Decimal:
        """
        Berechne Average True Range.

        True Range = max(
            high - low,
            |high - prev_close|,
            |low - prev_close|
        )
        """
        if len(price_data) < 2:
            return Decimal("0")

        true_ranges: list[Decimal] = []

        for i in range(1, len(price_data)):
            high = Decimal(str(price_data[i].get("high", 0)))
            low = Decimal(str(price_data[i].get("low", 0)))
            prev_close = Decimal(str(price_data[i - 1].get("close", 0)))

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        # Durchschnitt über ATR-Periode
        period_data = true_ranges[-self.atr_period :]
        if not period_data:
            return Decimal("0")

        return Decimal(sum(period_data)) / Decimal(len(period_data))

    def calculate_pnl_atr(
        self,
        pnl_history: list[Decimal],
    ) -> Decimal:
        """
        ATR-Analogon für PnL-Bewegungen.

        Misst die durchschnittliche Schwankung des PnL.
        """
        if len(pnl_history) < 2:
            return Decimal("0.50")  # Default $0.50 ATR

        # Berechne absolute Differenzen
        diffs = [abs(pnl_history[i] - pnl_history[i - 1]) for i in range(1, len(pnl_history))]

        period_data = diffs[-self.atr_period :]
        if not period_data:
            return Decimal("0.50")

        return Decimal(sum(period_data)) / Decimal(len(period_data))

    def get_stop_level(
        self,
        high_water_mark: Decimal,
        atr_value: Decimal,
    ) -> Decimal:
        """Berechne dynamischen Stop-Level."""
        stop_distance = atr_value * self.multiplier
        return high_water_mark - stop_distance

    def evaluate(
        self,
        current_pnl: Decimal,
        high_water_mark: Decimal,
        pnl_history: list[Decimal],
    ) -> ExitSignal:
        """Evaluate ATR trailing stop condition."""
        # Nicht aktivieren wenn Profit unter Schwelle
        if high_water_mark < self.min_activation_profit:
            return ExitSignal(
                should_exit=False,
                reason=f"ATR-STOP: Not activated (HWM ${high_water_mark:.2f} < ${self.min_activation_profit:.2f})",
                priority=10,
            )

        atr_value = self.calculate_pnl_atr(pnl_history)
        stop_level = self.get_stop_level(high_water_mark, atr_value)

        if current_pnl <= stop_level:
            return ExitSignal(
                should_exit=True,
                reason=(
                    f"ATR-TRAILING: PnL ${current_pnl:.2f} <= Stop ${stop_level:.2f} "
                    f"(ATR=${atr_value:.2f}, {self.multiplier}x)"
                ),
                confidence=Decimal("0.85"),
                priority=2,
            )

        return ExitSignal(
            should_exit=False,
            reason=f"ATR-HOLD: PnL ${current_pnl:.2f} > Stop ${stop_level:.2f}",
            priority=10,
        )


# =============================================================================
# 3. FUNDING RATE VELOCITY EXIT
# =============================================================================


class FundingVelocityExitStrategy:
    """
    Exit basierend auf der Änderungsgeschwindigkeit der Funding Rate.

    Erkennt schnelle Verschlechterungen bevor sie kritisch werden.

    - Velocity: Erste Ableitung (Änderungsrate)
    - Acceleration: Zweite Ableitung (Beschleunigung der Änderung)
    """

    def __init__(
        self,
        velocity_threshold: Decimal = Decimal("-0.0015"),  # -0.15% pro Stunde
        acceleration_threshold: Decimal = Decimal("-0.0008"),  # Beschleunigung
        lookback_hours: int = 6,
    ):
        self.velocity_threshold = velocity_threshold
        self.acceleration_threshold = acceleration_threshold
        self.lookback_hours = lookback_hours

    def calculate_velocity(
        self,
        funding_rates: list[Decimal],
    ) -> Decimal:
        """
        Berechne Velocity (erste Ableitung) via lineare Regression.
        """
        if len(funding_rates) < 3:
            return Decimal("0")

        rates = list(funding_rates[-self.lookback_hours :])
        n = len(rates)

        # Lineare Regression: y = mx + b, wir wollen m (Steigung)
        x_mean = Decimal(n - 1) / 2
        y_mean = Decimal(sum(rates)) / Decimal(n)

        numerator = Decimal(sum((Decimal(i) - x_mean) * (rates[i] - y_mean) for i in range(n)))
        denominator = Decimal(sum((Decimal(i) - x_mean) ** 2 for i in range(n)))

        if denominator == 0:
            return Decimal("0")

        return numerator / denominator

    def calculate_acceleration(
        self,
        funding_rates: list[Decimal],
    ) -> Decimal:
        """
        Berechne Acceleration (zweite Ableitung).

        Vergleicht Velocity der ersten Hälfte mit zweiter Hälfte.
        """
        if len(funding_rates) < 6:
            return Decimal("0")

        mid = len(funding_rates) // 2
        velocity_old = self.calculate_velocity(funding_rates[: mid + 1])
        velocity_new = self.calculate_velocity(funding_rates[mid:])

        return velocity_new - velocity_old

    def evaluate(
        self,
        funding_rates: list[Decimal],
    ) -> ExitSignal:
        """Evaluate funding velocity exit condition."""
        if len(funding_rates) < 3:
            return ExitSignal(
                should_exit=False,
                reason=f"VELOCITY: Insufficient data ({len(funding_rates)} samples)",
                priority=10,
            )

        velocity = self.calculate_velocity(funding_rates)
        acceleration = self.calculate_acceleration(funding_rates)

        # Exit bei schneller negativer Velocity
        if velocity <= self.velocity_threshold:
            return ExitSignal(
                should_exit=True,
                reason=f"VELOCITY-EXIT: Rate falling at {velocity:.4%}/h (< {self.velocity_threshold:.4%})",
                confidence=Decimal("0.85"),
                priority=2,
            )

        # Early Exit bei negativer Beschleunigung (Trend verschlechtert sich)
        if acceleration <= self.acceleration_threshold and velocity < 0:
            return ExitSignal(
                should_exit=True,
                reason=f"ACCEL-EXIT: Acceleration {acceleration:.4%}/h² indicates worsening (v={velocity:.4%}/h)",
                confidence=Decimal("0.75"),
                priority=3,
            )

        return ExitSignal(
            should_exit=False,
            reason=f"VELOCITY-HOLD: v={velocity:.4%}/h, a={acceleration:.4%}/h²",
            priority=10,
        )


# =============================================================================
# 4. DECELERATING STOP LOSS
# =============================================================================


class DeceleratingStopLossStrategy:
    """
    Stop-Loss der sich mit zunehmendem Profit lockert.

    Verwendet von: Intraday Quant Firms

    Prinzip:
    - Anfangs: Enger Stop (z.B. 15% unter Entry)
    - Mit steigendem Profit: Stop "verlangsamt" sich
    - Erlaubt Gewinnern zu laufen, schützt aber Basiskapital
    """

    def __init__(
        self,
        initial_stop_pct: Decimal = Decimal("0.015"),  # 1.5%
        deceleration_rate: Decimal = Decimal("0.4"),  # 40% Decay pro $1
        min_stop_pct: Decimal = Decimal("0.003"),  # 0.3% minimum
    ):
        self.initial_stop_pct = initial_stop_pct
        self.deceleration_rate = deceleration_rate
        self.min_stop_pct = min_stop_pct

    def get_current_stop_percent(
        self,
        accumulated_profit: Decimal,
    ) -> Decimal:
        """
        Stop-Prozent nimmt exponentiell mit Profit ab.
        """
        if accumulated_profit <= 0:
            return self.initial_stop_pct

        # Exponentieller Decay: stop = initial * e^(-rate * profit)
        decay_factor = Decimal(str(math.exp(-float(self.deceleration_rate) * float(accumulated_profit))))
        current_stop = self.initial_stop_pct * decay_factor

        return max(current_stop, self.min_stop_pct)

    def get_stop_level(
        self,
        high_water_mark: Decimal,
    ) -> Decimal:
        """Berechne Stop-Level basierend auf HWM."""
        current_stop_pct = self.get_current_stop_percent(high_water_mark)
        return high_water_mark * (1 - current_stop_pct)

    def evaluate(
        self,
        current_pnl: Decimal,
        high_water_mark: Decimal,
    ) -> ExitSignal:
        """Evaluate decelerating stop loss condition."""
        if high_water_mark <= 0:
            return ExitSignal(
                should_exit=False,
                reason="DECEL-STOP: Not activated (no profit yet)",
                priority=10,
            )

        stop_level = self.get_stop_level(high_water_mark)
        current_stop_pct = self.get_current_stop_percent(high_water_mark)

        if current_pnl <= stop_level:
            return ExitSignal(
                should_exit=True,
                reason=f"DECEL-STOP: PnL ${current_pnl:.2f} <= Stop ${stop_level:.2f} ({current_stop_pct:.2%})",
                confidence=Decimal("0.80"),
                priority=2,
            )

        return ExitSignal(
            should_exit=False,
            reason=f"DECEL-HOLD: PnL ${current_pnl:.2f} > Stop ${stop_level:.2f} ({current_stop_pct:.2%})",
            priority=10,
        )


# =============================================================================
# 5. EXPECTED SHORTFALL (CVaR) EXIT
# =============================================================================


class ExpectedShortfallExitStrategy:
    """
    Exit basierend auf Expected Shortfall (Conditional VaR).

    Verwendet von: Alle großen Hedge Funds (regulatorische Anforderung)

    ES ist besser als VaR weil es Tail-Risk berücksichtigt:
    - VaR: "Mit 95% Wahrscheinlichkeit verlierst du maximal X"
    - ES: "Wenn du mehr als VaR verlierst, ist der erwartete Verlust Y"
    """

    def __init__(
        self,
        confidence_level: Decimal = Decimal("0.95"),
        max_es_percent: Decimal = Decimal("0.03"),  # 3% max ES
    ):
        self.confidence_level = confidence_level
        self.max_es_percent = max_es_percent

    def calculate_expected_shortfall(
        self,
        returns: list[Decimal],
    ) -> Decimal:
        """
        Berechne Expected Shortfall bei gegebenem Confidence Level.
        """
        if not returns:
            return Decimal("0")

        sorted_returns = sorted(returns)
        cutoff_index = int(len(sorted_returns) * (1 - float(self.confidence_level)))

        if cutoff_index == 0:
            cutoff_index = 1

        # Tail-Verluste sind die schlechtesten Ergebnisse
        tail_losses = sorted_returns[:cutoff_index]

        if not tail_losses:
            return Decimal("0")

        avg_tail_loss = Decimal(sum(tail_losses)) / Decimal(len(tail_losses))
        return abs(avg_tail_loss)

    def evaluate(
        self,
        hourly_returns: list[Decimal],  # Stündliche Returns in %
        position_notional: Decimal,
    ) -> ExitSignal:
        """Evaluate Expected Shortfall exit condition."""
        if len(hourly_returns) < 24:  # Mindestens 24h Daten
            return ExitSignal(
                should_exit=False,
                reason=f"ES: Insufficient data ({len(hourly_returns)}/24h)",
                priority=10,
            )

        es_value = self.calculate_expected_shortfall(hourly_returns)
        es_percent = es_value  # Returns sind bereits in %

        if es_percent >= self.max_es_percent:
            es_usd = es_percent * position_notional
            return ExitSignal(
                should_exit=True,
                reason=f"ES-EXIT: Expected Shortfall {es_percent:.2%} >= {self.max_es_percent:.2%} (${es_usd:.2f})",
                confidence=Decimal("0.90"),
                priority=2,
            )

        return ExitSignal(
            should_exit=False,
            reason=f"ES-HOLD: {es_percent:.2%} < {self.max_es_percent:.2%}",
            priority=10,
        )


# =============================================================================
# 6. KELLY ROTATION CRITERION
# =============================================================================


class KellyRotationStrategy:
    """
    Kelly Criterion basierte Opportunity Rotation.

    Verwendet von: Renaissance Technologies, Edward Thorp

    Rotiere zu besserer Opportunity NUR wenn Kelly-Wert
    signifikant höher ist.
    """

    def __init__(
        self,
        min_kelly_improvement: Decimal = Decimal("0.05"),  # 5%
        use_half_kelly: bool = True,
        min_historical_trades: int = 20,
    ):
        self.min_kelly_improvement = min_kelly_improvement
        self.use_half_kelly = use_half_kelly
        self.min_historical_trades = min_historical_trades

    def calculate_kelly(
        self,
        win_probability: Decimal,
        win_loss_ratio: Decimal,
    ) -> Decimal:
        """
        Berechne Kelly Fraction.

        kelly = p - (1-p) / b

        Wo:
        - p = Gewinnwahrscheinlichkeit
        - b = Gewinn/Verlust Verhältnis
        """
        if win_loss_ratio <= 0:
            return Decimal("0")

        q = 1 - win_probability
        kelly = win_probability - (q / win_loss_ratio)

        if self.use_half_kelly:
            kelly *= Decimal("0.5")

        # Begrenzen auf 0-25%
        return max(min(kelly, Decimal("0.25")), Decimal("0"))

    def estimate_kelly_from_apy(
        self,
        current_apy: Decimal,
        historical_win_rate: Decimal = Decimal("0.65"),
        historical_wl_ratio: Decimal = Decimal("1.5"),
    ) -> Decimal:
        """
        Schätze Kelly basierend auf APY und historischen Daten.

        Höhere APY → höhere Win-Wahrscheinlichkeit Annahme
        """
        # APY beeinflusst die geschätzte Win-Rate
        # Höhere APY = bessere Chancen (vereinfachte Annahme)
        apy_factor = min(current_apy / Decimal("0.50"), Decimal("1.5"))  # Normalisiert auf 50% APY
        adjusted_win_rate = min(historical_win_rate * apy_factor, Decimal("0.85"))

        return self.calculate_kelly(adjusted_win_rate, historical_wl_ratio)

    def evaluate(
        self,
        current_apy: Decimal,
        new_opportunity_apy: Decimal,
        historical_win_rate: Decimal = Decimal("0.65"),
        historical_wl_ratio: Decimal = Decimal("1.5"),
    ) -> ExitSignal:
        """Evaluate Kelly rotation condition."""
        current_kelly = self.estimate_kelly_from_apy(current_apy, historical_win_rate, historical_wl_ratio)
        new_kelly = self.estimate_kelly_from_apy(new_opportunity_apy, historical_win_rate, historical_wl_ratio)

        improvement = new_kelly - current_kelly

        if improvement >= self.min_kelly_improvement:
            return ExitSignal(
                should_exit=True,
                reason=(
                    f"KELLY-ROTATE: New {new_kelly:.2%} > Current {current_kelly:.2%} "
                    f"+ {self.min_kelly_improvement:.2%}"
                ),
                confidence=Decimal("0.75"),
                priority=4,
            )

        return ExitSignal(
            should_exit=False,
            reason=f"KELLY-HOLD: Improvement {improvement:.2%} < {self.min_kelly_improvement:.2%}",
            priority=10,
        )


# =============================================================================
# 7. COMPOSITE EXIT EVALUATOR
# =============================================================================


class ProfessionalExitEvaluator:
    """
    Kombiniert alle professionellen Exit-Strategien.

    Verwendet Priority-basiertes Voting:
    - Priority 1-2: Emergency/High-Priority → sofort exit
    - Priority 3-4: Standard → exit wenn Confidence > 0.7
    - Priority 5+: Low → ignorieren außer mehrere stimmen zu
    """

    def __init__(
        self,
        z_score: ZScoreExitStrategy | None = None,
        atr_trailing: ATRTrailingStopStrategy | None = None,
        velocity: FundingVelocityExitStrategy | None = None,
        decel_stop: DeceleratingStopLossStrategy | None = None,
        expected_shortfall: ExpectedShortfallExitStrategy | None = None,
        kelly_rotation: KellyRotationStrategy | None = None,
    ):
        self.z_score = z_score or ZScoreExitStrategy()
        self.atr_trailing = atr_trailing or ATRTrailingStopStrategy()
        self.velocity = velocity or FundingVelocityExitStrategy()
        self.decel_stop = decel_stop or DeceleratingStopLossStrategy()
        self.expected_shortfall = expected_shortfall or ExpectedShortfallExitStrategy()
        self.kelly_rotation = kelly_rotation or KellyRotationStrategy()

    def evaluate_all(
        self,
        current_apy: Decimal,
        historical_apy: list[Decimal],
        current_pnl: Decimal,
        high_water_mark: Decimal,
        pnl_history: list[Decimal],
        funding_rates: list[Decimal],
        hourly_returns: list[Decimal],
        position_notional: Decimal,
        best_opportunity_apy: Decimal = Decimal("0"),
    ) -> tuple[bool, str, list[ExitSignal]]:
        """
        Evaluate all exit strategies and return combined decision.

        Returns:
            - should_exit: bool
            - final_reason: str
            - all_signals: list[ExitSignal]
        """
        signals: list[ExitSignal] = []

        # 1. Z-Score
        signals.append(self.z_score.evaluate(current_apy, historical_apy))

        # 2. ATR Trailing
        signals.append(self.atr_trailing.evaluate(current_pnl, high_water_mark, pnl_history))

        # 3. Velocity
        signals.append(self.velocity.evaluate(funding_rates))

        # 4. Decelerating Stop
        signals.append(self.decel_stop.evaluate(current_pnl, high_water_mark))

        # 5. Expected Shortfall
        signals.append(self.expected_shortfall.evaluate(hourly_returns, position_notional))

        # 6. Kelly Rotation
        if best_opportunity_apy > 0:
            signals.append(self.kelly_rotation.evaluate(current_apy, best_opportunity_apy))

        # Filter exit signals
        exit_signals = [s for s in signals if s.should_exit]

        if not exit_signals:
            return False, "No exit conditions met", signals

        # Sort by priority (1=highest)
        exit_signals.sort(key=lambda s: (s.priority, -float(s.confidence)))

        # High priority signals (1-2) trigger immediate exit
        high_priority = [s for s in exit_signals if s.priority <= 2]
        if high_priority:
            best = high_priority[0]
            return True, best.reason, signals

        # Medium priority (3-4) with high confidence
        medium_priority = [s for s in exit_signals if s.priority <= 4 and s.confidence >= Decimal("0.75")]
        if medium_priority:
            best = medium_priority[0]
            return True, best.reason, signals

        # Multiple low-priority signals agreeing
        if len(exit_signals) >= 2:
            reasons = " + ".join(s.reason.split(":")[0] for s in exit_signals[:3])
            return True, f"CONSENSUS ({len(exit_signals)} signals): {reasons}", signals

        return False, "Exit signals insufficient", signals


# =============================================================================
# CONFIG INTEGRATION HELPER
# =============================================================================


def create_evaluator_from_config(config: dict) -> ProfessionalExitEvaluator:
    """
    Erstelle ProfessionalExitEvaluator aus Config-Dictionary.

    Beispiel config:
    {
        "z_score_exit_enabled": True,
        "z_score_exit_threshold": -1.0,
        "atr_trailing_enabled": True,
        "atr_multiplier": 2.0,
        ...
    }
    """
    z_score = None
    if config.get("z_score_exit_enabled", True):
        z_score = ZScoreExitStrategy(
            exit_threshold=Decimal(str(config.get("z_score_exit_threshold", -1.0))),
            emergency_threshold=Decimal(str(config.get("z_score_emergency_threshold", -2.0))),
        )

    atr_trailing = None
    if config.get("atr_trailing_enabled", True):
        atr_trailing = ATRTrailingStopStrategy(
            atr_period=config.get("atr_period", 14),
            multiplier=Decimal(str(config.get("atr_multiplier", 2.0))),
            min_activation_profit=Decimal(str(config.get("atr_min_activation_usd", 3.0))),
        )

    velocity = None
    if config.get("velocity_exit_enabled", True):
        velocity = FundingVelocityExitStrategy(
            velocity_threshold=Decimal(str(config.get("velocity_threshold_hourly", -0.0015))),
            acceleration_threshold=Decimal(str(config.get("acceleration_threshold", -0.0008))),
        )

    decel_stop = None
    if config.get("decel_stop_enabled", True):
        decel_stop = DeceleratingStopLossStrategy(
            initial_stop_pct=Decimal(str(config.get("decel_initial_stop_pct", 0.015))),
            deceleration_rate=Decimal(str(config.get("decel_rate", 0.4))),
            min_stop_pct=Decimal(str(config.get("decel_min_stop_pct", 0.003))),
        )

    es = None
    if config.get("es_exit_enabled", True):
        es = ExpectedShortfallExitStrategy(
            confidence_level=Decimal(str(config.get("es_confidence_level", 0.95))),
            max_es_percent=Decimal(str(config.get("es_max_percent", 0.03))),
        )

    kelly = None
    if config.get("kelly_rotation_enabled", True):
        kelly = KellyRotationStrategy(
            min_kelly_improvement=Decimal(str(config.get("kelly_min_improvement", 0.05))),
            use_half_kelly=config.get("kelly_half_kelly", True),
        )

    return ProfessionalExitEvaluator(
        z_score=z_score,
        atr_trailing=atr_trailing,
        velocity=velocity,
        decel_stop=decel_stop,
        expected_shortfall=es,
        kelly_rotation=kelly,
    )
