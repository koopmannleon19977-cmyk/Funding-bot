import lighter
import asyncio
import datetime
from datetime import timezone
from lighter.api.order_api import OrderApi
from lighter.api.candlestick_api import CandlestickApi
from lighter.api.funding_api import FundingApi # Die korrekte API

class LighterAdapter:
    def __init__(self):
        """
        Initialisiert den Adapter.
        Wir erstellen einen Cache für Markt-Infos UND Funding-Rates.
        """
        print("Initialisiere LighterAdapter...")
        self.market_info = {} # Cache für Markt-IDs
        self.funding_cache = {} # Cache für Funding Rates

    async def _fetch_market_info_async(self):
        """
        Interne (async) Funktion, um alle Märkte abzurufen.
        (Diese Funktion ist unverändert und korrekt)
        """
        client = None
        info = {}
        non_crypto_symbols = {
            'GBPUSD', 'USDCHF', 'EURUSD', 'USDJPY', 'USDCAD', 
            'XAG', 'XAU', 'SPX'
        }
        try:
            client = lighter.ApiClient()
            api = OrderApi(client)
            order_books_data = await api.order_books() 
            
            if order_books_data and order_books_data.order_books:
                for market in order_books_data.order_books: 
                    market_name = getattr(market, 'symbol', '') 
                    if not market_name or market_name in non_crypto_symbols:
                        continue
                    normalized_name = f"{market_name}-USD"
                    info[normalized_name] = {'id': getattr(market, 'market_id', 0), 'data': market}
            else:
                print("Lighter Warnung: Keine '.order_books' in der API-Antwort gefunden.")
            return info
        except Exception as e:
            print(f"Lighter Fehler (_fetch_market_info_async): {e}")
            return {}
        finally:
            if client:
                await client.close()

    # --- KORRIGIERTE FUNKTION ZUM HOLEN ALLER FUNDING RATES ---
    async def _fetch_funding_rates_async(self):
        """
        Holt ALLE Funding Rates auf einmal und speichert sie im Cache.
        """
        client = None
        self.funding_cache = {} # Cache leeren
        
        # Wir brauchen eine Umkehr-Map: Market_ID -> Symbol_Name
        id_to_symbol_map = {}
        for symbol, data in self.market_info.items():
            id_to_symbol_map[data['id']] = symbol
            
        try:
            client = lighter.ApiClient()
            api = FundingApi(client) 
            
            funding_data = await api.funding_rates() 
            
            # KORREKTUR: Das Attribut heißt '.funding_rates' (nicht '.rates')
            if funding_data and funding_data.funding_rates:
                
                for rate_obj in funding_data.funding_rates:
                    market_id = getattr(rate_obj, 'market_id', None)
                    
                    if market_id in id_to_symbol_map:
                        symbol = id_to_symbol_map[market_id]
                        
                        # KORREKTUR (laut Discord): API liefert 8-Stunden-Rate
                        eight_hour_rate = float(getattr(rate_obj, 'rate', 0.0))
                        
                        # Wir speichern die STÜNDLICHE Rate im Cache
                        self.funding_cache[symbol] = eight_hour_rate / 8.0
                        
        except Exception as e:
            print(f"Lighter Fehler (_fetch_funding_rates_async): {e}")
        finally:
            if client: await client.close()
    
    # --- KORRIGIERTE LADEFUNKTION ---
    async def load_market_cache(self):
        """
        Lädt den Markt-Cache UND den Funding-Cache asynchron.
        """
        print("Lighter: Aktualisiere Markt-Info-Cache...")
        self.market_info = await self._fetch_market_info_async()
        print(f"Lighter: {len(self.market_info)} PERP-Märkte gefunden.")
        
        # Jetzt die Funding-Rates holen
        print("Lighter: Aktualisiere Funding-Rate-Cache...")
        await self._fetch_funding_rates_async()
        print(f"Lighter: {len(self.funding_cache)} Funding Rates geladen.")


    def list_symbols(self):
        """
        Gibt alle gefundenen PERP-Symbole aus dem Cache zurück.
        (Unverändert)
        """
        return list(self.market_info.keys())

    # --- WIEDERHERGESTELLTE FUNKTION ---
    async def fetch_mark_price_async(self, symbol):
        """
        Holt den Mark-Price für 1 Symbol.
        """
        if symbol not in self.market_info:
            # print(f"Lighter Fehler: Symbol {symbol} nicht im Cache gefunden.")
            return None
        
        market_id = self.market_info[symbol]['id']
        client = None
        try:
            client = lighter.ApiClient()
            api = lighter.OrderApi(client)
            details_wrapper = await api.order_book_details(market_id=market_id)
            
            if details_wrapper and details_wrapper.order_book_details and len(details_wrapper.order_book_details) > 0:
                actual_details = details_wrapper.order_book_details[0]
                price_str = actual_details.last_trade_price
                return float(price_str)
            else:
                # print(f"Lighter Fehler: 'order_book_details'-Liste ist leer oder nicht vorhanden.")
                return None
        except Exception as e:
            # print(f"Lighter Fehler (fetch_mark_price für {symbol}): {e}")
            return None
        finally:
            if client:
                await client.close()

    # --- KORRIGIERTE FUNDING RATE FUNKTION (SCHNELL) ---
    async def fetch_funding_rate_async(self, symbol):
        """
        Holt die Funding-Rate (STÜNDLICH) jetzt SCHNELL aus dem Cache.
        Macht keinen API-Aufruf mehr.
        """
        if symbol not in self.funding_cache:
            # print(f"Lighter Warnung: Rate für {symbol} nicht im Cache.")
            return None
        return self.funding_cache.get(symbol) # Holt die stündliche Rate

    def close(self):
        print("LighterAdapter geschlossen.")
        pass