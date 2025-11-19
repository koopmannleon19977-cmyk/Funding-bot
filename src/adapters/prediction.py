# src/prediction.py – vereinfacht & realistisch
import pandas as pd
import sqlite3
from datetime import datetime, timedelta

DB_FILE = "funding.db"

def get_history(symbol: str, hours: int = 96) -> pd.DataFrame:
    with sqlite3.connect(DB_FILE) as conn:
        cutoff = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())
        df = pd.read_sql("""
            SELECT timestamp, lighter_rate, x10_rate 
            FROM funding_history WHERE symbol = ? AND timestamp > ?
            ORDER BY timestamp
        """, conn, params=(symbol, cutoff))
        if df.empty: return df
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
        df.set_index('timestamp', inplace=True)
        return df

def predict_next_funding_rates(symbol: str):
    df = get_history(symbol)
    if len(df) < 12:
        return 0.0, 0.0, 0.0

    l_ewma = df['lighter_rate'].ewm(span=8).mean().iloc[-1]
    x_ewma = df['x10_rate'].ewm(span=8).mean().iloc[-1]

    pred_l = l_ewma
    pred_x = x_ewma

    # Realistische Confidence
    std = df['lighter_rate'].tail(24).std()
    confidence = max(0.0, min(1.0, 1.0 - std * 100))   # ~0.01% std → conf 0.0

    return float(pred_l), float(pred_x), float(confidence)

def should_flip_soon(symbol: str, current_net: float) -> bool:
    pl, px, conf = predict_next_funding_rates(symbol)
    if conf < 0.6: return False
    pred_net = pl - px
    return (current_net > 0 and pred_net < current_net * 0.4) or \
           (current_net < 0 and pred_net > current_net * 0.4)