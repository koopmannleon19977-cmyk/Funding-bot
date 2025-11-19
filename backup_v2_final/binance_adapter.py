import ccxt.async_support as ccxt  # NEU: Importiert async-Version

class BinanceAdapter:
    def __init__(self):
        print("Binance Adapter: Initialisiere...")
        # Wir nutzen 'binanceusdm' für Perpetual Futures und machen den Client async
        self.exchange = ccxt.binanceusdm() 

    async def fetch_mark_price_async(self, symbol): # NEU: Funktion ist jetzt async
        """
        Holt den aktuellen Index Price von Binance (als neutrale Referenz).
        """
        try:
            ccxt_symbol = symbol.replace('-USD', '/USDT')
            
            # Wir nutzen fetch_index_price für den fairsten Preis
            index_price_data = await self.exchange.fetch_index_price(ccxt_symbol) # NEU: await
            
            if index_price_data is not None and index_price_data.get('indexPrice') is not None:
                return float(index_price_data.get('indexPrice'))
            else:
                return None
            
        except Exception as e:
            # print(f"Binance Fehler (fetch_mark_price für {symbol}): {e}")
            return None

    async def close(self): # NEU: Fügt eine close-Methode hinzu
        await self.exchange.close()