# src/prediction.py
import pandas as pd
import aiosqlite
import logging
from datetime import datetime, timedelta
from typing import Tuple
import config

logger = logging.getLogger(__name__)

async def get_funding_history(symbol: str, hours: int = 96) -> pd.DataFrame:
    """Holt die letzten X Stunden Funding Rates asynchron"""
    try:
        async with aiosqlite.connect(config.DB_FILE) as conn:
            query = """
            SELECT timestamp, lighter_rate, x10_rate 
            FROM funding_history 
            WHERE symbol = ? AND timestamp > ?
            ORDER BY timestamp ASC
            """
            cutoff = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())
            
            async with conn.execute(query, (symbol, cutoff)) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return pd.DataFrame()
            
            df = pd.DataFrame(rows, columns=['timestamp', 'lighter_rate', 'x10_rate'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.set_index('timestamp', inplace=True)
            return df
    except Exception as e:
        logger.debug(f"Prediction History Fehler für {symbol}: {e}")
        return pd.DataFrame()

async def predict_next_funding_rates(symbol: str, btc_trend_pct: float = 0.0) -> Tuple[float, float, float]:
    """
    Berechnet Prediction mit Markt-Korrelation.
    
    Returns:
        (predicted_rate, predicted_std, confidence 0.0-1.0)
    """
    df = await get_funding_history(symbol, hours=96)
    
    # --- COLD START LOGIK ---
    # Wenn wir weniger als 12 Datenpunkte haben, können wir keine Statistik berechnen.
    # Aber: Wenn der Spread extrem gut ist (was der Bot vor Aufruf prüft), 
    # wollen wir nicht blockieren. Wir geben eine "neutrale" Confidence zurück.
    if len(df) < 12: 
        # Default Confidence für neue Coins: 0.6 (Mutig genug für Standard-Trades)
        return 0.0, 0.0, 0.6

    # 1. Basis-Confidence aus Volatilität
    # Wenn die Rate stark schwankt, vertrauen wir der Vorhersage weniger.
    std_24h = df['lighter_rate'].tail(24).std()
    if pd.isna(std_24h): 
        confidence = 0.5
    else: 
        # Je höher die StdDev, desto niedriger die Confidence
        # Skalierung: 0.0005 StdDev = -1.0 Confidence
        confidence = max(0.0, min(1.0, 1.0 - (std_24h * 1000)))

    # 2. Markt-Korrelation (BTC Trend Filter)
    current_rate = df['lighter_rate'].iloc[-1]
    correlation_bonus = 0.0
    
    # Wenn BTC stark steigt (>2%), sind Long Funding Rates oft stabil positiv.
    if btc_trend_pct > 2.0 and current_rate > 0:
        correlation_bonus = 0.15
    # Wenn BTC stark fällt (<-2%), sind Short Funding Rates (negativ) oft stabil.
    elif btc_trend_pct < -2.0 and current_rate < 0:
        correlation_bonus = 0.15
    # Warnung: Gegen den Trend spekulieren
    elif (btc_trend_pct > 3.0 and current_rate < 0) or (btc_trend_pct < -3.0 and current_rate > 0):
        correlation_bonus = -0.25

    final_confidence = min(1.0, max(0.1, confidence + correlation_bonus))

    return 0.0, 0.0, float(final_confidence)

def calculate_smart_size(confidence: float, current_balance: float) -> float:
    """
    Berechnet die Positionsgröße basierend auf Confidence und Balance.
    """
    # 1. Basis-Allocation: Wir nutzen bis zu 20% der Balance für High-Confidence Trades
    # Bei $88 Balance sind das ca. $17.60
    max_allocation = current_balance * 0.20 
    
    # 2. Confidence Multiplier
    if confidence >= 0.8:
        multiplier = 1.0      # Volle Größe
    elif confidence >= 0.5:
        multiplier = 0.75     # 75% Größe (Standard)
    else:
        multiplier = 0.25     # Test-Größe
    
    target_size = max_allocation * multiplier
    
    # 3. Hard Limits aus Config respektieren
    # Darf nicht größer sein als das absolute Maximum (z.B. $30)
    target_size = min(target_size, config.MAX_TRADE_SIZE_USD)
    
    # WICHTIG: Das Minimum wird später in execute_trade nochmal geprüft (Börsen-Minimum).
    # Hier geben wir nur den "Wunschwert" zurück.
    
    return round(target_size, 1)