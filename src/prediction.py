# src/prediction.py
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sqlite3
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

DB_FILE = "funding.db"

def get_funding_history(symbol: str, hours: int = 96) -> pd.DataFrame:
    """Holt die letzten X Stunden Funding Rates aus einer neuen DB-Tabelle"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            query = """
            SELECT timestamp, lighter_rate, x10_rate 
            FROM funding_history 
            WHERE symbol = ? AND timestamp > ?
            ORDER BY timestamp ASC
            """
            cutoff = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())
            df = pd.read_sql_query(query, conn, params=(symbol, cutoff))
            if df.empty:
                return pd.DataFrame()
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.set_index('timestamp', inplace=True)
            return df
    except Exception as e:
        logger.debug(f"Prediction History Fehler für {symbol}: {e}")
        return pd.DataFrame()

def predict_next_funding_rates(symbol: str) -> Tuple[float, float, float]:
    """
    Gibt zurück:
    - predicted_lighter_hourly
    - predicted_x10_hourly
    - confidence (0.0 - 1.0)
    """
    df = get_funding_history(symbol, hours=96)
    if len(df) < 12:
        return 0.0, 0.0, 0.0  # Zu wenig Daten

    # EWMA mit Halbwertszeit ~8 Stunden (sehr reaktiv)
    lighter_ewma = df['lighter_rate'].ewm(span=8, adjust=False).mean().iloc[-1]
    x10_ewma = df['x10_rate'].ewm(span=8, adjust=False).mean().iloc[-1]

    # Momentum: Letzte 3 vs letzte 12 Rates
    recent_l = df['lighter_rate'].tail(3).mean()
    older_l = df['lighter_rate'].tail(12).head(9).mean()
    recent_x = df['x10_rate'].tail(3).mean()
    older_x = df['x10_rate'].tail(12).head(9).mean()

    # Kombinierte Prediction
    pred_lighter = 0.7 * lighter_ewma + 0.3 * recent_l
    pred_x10 = 0.7 * x10_ewma + 0.3 * recent_x

    # Confidence: Je stabiler die letzten 24h, desto höher
    std_24h = df['lighter_rate'].tail(24).std()
    confidence = max(0.0, min(1.0, 1.0 - (std_24h * 5000)))  # normiert

    return float(pred_lighter), float(pred_x10), float(confidence)

def should_flip_soon(symbol: str, current_net: float) -> bool:
    """Gibt True zurück, wenn Flip in den nächsten 4h wahrscheinlich"""
    pred_l, pred_x, conf = predict_next_funding_rates(symbol)
    if conf < 0.6:
        return False  # Zu unsicher
    predicted_net = pred_l - pred_x
    return (current_net > 0 and predicted_net < current_net * 0.4) or \
           (current_net < 0 and predicted_net > current_net * 0.4)