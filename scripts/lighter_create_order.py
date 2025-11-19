import asyncio
import logging
from decimal import Decimal
import time

from lighter.signer_client import SignerClient

logging.basicConfig(level=logging.INFO)


# === HIER ANPASSEN: deine Mainnet-Daten ===
BASE_URL = "https://mainnet.zklighter.elliot.ai"

# Das ist der gleiche Key wie LIGHTER_API_PRIVATE_KEY in deiner .env / config.py
API_KEY_PRIVATE_KEY = "0x211f77be303d7b5d02296e6fec585efba48dec3abb93ff71331816667cb9a3206955e607b83fcc70"  # z.B. "0xabc123..."

# Das sind die gleichen Indizes wie in config.py
ACCOUNT_INDEX = 60113         # LIGHTER_ACCOUNT_INDEX
API_KEY_INDEX = 3             # LIGHTER_API_KEY_INDEX

# MarketIndex für BTC-USD laut deinem Markt-Cache (du hast im Log market_index=1 gesehen)
MARKET_INDEX = 1

# Test-Order-Parameter
POSITION_SIZE_USD = 50        # klein halten, nur zum Testen
SIDE = "SELL"                 # oder "BUY"
GOOD_TILL_28_DAYS = -1        # DEFAULT_28_DAY_ORDER_EXPIRY


async def main():
    logging.info("Starte Lighter Testskript (create_order)...")

    # SignerClient initialisieren
    client = SignerClient(
        url=BASE_URL,
        private_key=API_KEY_PRIVATE_KEY,
        account_index=ACCOUNT_INDEX,
        api_key_index=API_KEY_INDEX,
    )

    err = client.check_client()
    if err:
        logging.error(f"check_client Fehler: {err}")
        await client.close()
        return

    logging.info("SignerClient check_client OK")

    # Beispiel: hol dir einen ungefähren Preis aus dem Orderbuch, um etwas Sinnvolles zu haben
    # (optional; du kannst auch einfach einen festen Preis testen)
    try:
        ob = await client.order_api.order_book_orders(MARKET_INDEX, 1)
        if ob and ob.bids:
            # Preis als Int ohne Punkt, wie im SDK
            best_bid_str = ob.bids[0].price  # z.B. "95432.10"
            price_int = int(best_bid_str.replace(".", ""))  # 9543210
        else:
            logging.warning("Kein Orderbuch / Bids gefunden – benutze Fallback-Preis.")
            price_int = 100_000_000  # 1000.00 als Fallback
    except Exception as e:
        logging.error(f"Fehler beim Abrufen des Orderbuchs: {e} – benutze Fallback-Preis.")
        price_int = 100_000_000  # 1000.00

    logging.info(f"Verwende price_int={price_int} für Testorder.")

    # BaseAmount als „Stückzahl in kleinster Einheit“.
    # Für einen simplen Test einfach mal 1000 nutzen (abhängig vom Markt-Schema)
    base_amount = 1000

    is_ask = SIDE.upper() == "SELL"
    client_order_index = int(time.time() * 1000) & 0x7FFFFFFF

    logging.info(
        f"Rufe create_order auf: market_index={MARKET_INDEX}, base_amount={base_amount}, "
        f"price_int={price_int}, is_ask={is_ask}, coi={client_order_index}, expiry={GOOD_TILL_28_DAYS}"
    )

    try:
        tx_obj, tx_hash_obj, err = await client.create_order(
            market_index=MARKET_INDEX,
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=price_int,
            is_ask=is_ask,
            order_type=client.ORDER_TYPE_LIMIT,
            time_in_force=client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False,
            trigger_price=client.NIL_TRIGGER_PRICE,
            order_expiry=GOOD_TILL_28_DAYS,
            nonce=-1,
            api_key_index=-1,
        )

        logging.info(f"create_order Rückgabe: tx_obj={tx_obj}, tx_hash_obj={tx_hash_obj}, err={err}")

        if err is not None:
            logging.error(f"Lighter create_order Fehler-String: {err}")
        elif tx_hash_obj is not None:
            logging.info(f"Lighter create_order OK: tx_hash={tx_hash_obj.tx_hash}, code={tx_hash_obj.code}")
        else:
            logging.error("Lighter create_order: tx_info_or_err war None (kein Fehlerstring, kein tx_hash).")

    except Exception as e:
        logging.error(f"Exception in create_order: {repr(e)}")

    await client.close()
    logging.info("Testskript beendet.")


if __name__ == "__main__":
    asyncio.run(main())