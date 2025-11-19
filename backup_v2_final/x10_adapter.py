import asyncio
from x10.perpetual.trading_client.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG

class X10Adapter:
    def __init__(self):
        """
        Initialisiert den Adapter.
        Wir erstellen einen Cache für Markt-Infos (Symbol -> market_data).
        """
        print("Initialisiere X10Adapter...")
        self.market_info = {} # Cache
        self.client_env = MAINNET_CONFIG # Wir nutzen Mainnet
        #self._update_market_info()

    async def _fetch_market_info_async(self):
        """
        Interne (async) Funktion, um alle Märkte abzurufen.
        """
        client = None
        info = {}
        
        # NEU: Liste der Symbole, die wir ignorieren wollen (Forex, Metalle)
        # X10 verwendet das Standard-Format (z.B. EUR-USD)
        non_crypto_symbols = {
            'EUR-USD', 'GBP-USD', 'USD-CHF', 'USD-JPY', 'USD-CAD', 
            'XAG-USD', 'XAU-USD', 'SPX-USD'
        }
        
        try:
            client = PerpetualTradingClient(self.client_env)
            stats_list = await client.markets_info.get_markets()
            
            if stats_list and stats_list.data:
                for market_data in stats_list.data:
                    
                    market_name = getattr(market_data, 'name', '') 
                    
                    # Schritt 1: Überspringe leere oder nicht-Krypto Symbole
                    if not market_name or market_name in non_crypto_symbols:
                        continue
                        
                    # Schritt 2: Prüfe, ob es ein USD-Paar ist
                    if market_name.endswith('-USD'):
                        info[market_name] = market_data
            else:
                print("X10 Warnung: Keine '.data' in der API-Antwort gefunden.")
                
            return info
        except Exception as e:
            print(f"X10 Fehler (_fetch_market_info_async): {e}")
            return {}
        finally:
            if client:
                await client.close() # Wichtig: Client immer schließen

    async def load_market_cache(self):
        """
        Lädt den Markt-Cache asynchron (ersetzt _update_market_info).
        """
        print("X10: Aktualisiere Markt-Info-Cache...")
        self.market_info = await self._fetch_market_info_async()
        print(f"X10: {len(self.market_info)} PERP-Märkte gefunden.")

    def list_symbols(self):
        """
        Gibt alle gefundenen PERP-Symbole aus dem Cache zurück.
        """
        return list(self.market_info.keys())

    def fetch_mark_price(self, symbol):
        """
        Holt den Mark-Price.
        Wir holen die Daten aus dem Cache (market_info), der bei der
        Initialisierung gefüllt wurde.
        """
        if symbol not in self.market_info:
            print(f"X10 Fehler: Symbol {symbol} nicht im Cache gefunden.")
            return None
        
        try:
            market_data = self.market_info[symbol]
            
            # KORREKTUR: Prüfe zuerst, ob 'market_stats' überhaupt existiert
            if market_data.market_stats and market_data.market_stats.mark_price:
                return float(market_data.market_stats.mark_price)
            else:
                # Diese Warnung ist normal für inaktive Märkte
                # print(f"X10 Warnung: Kein 'mark_price' für {symbol} im Cache.")
                return None
        except Exception as e:
            print(f"X10 Fehler (fetch_mark_price für {symbol}): {e}")
            return None

    def fetch_funding_rate(self, symbol):
        """
        Holt die Funding-Rate.
        """
        if symbol not in self.market_info:
            # print(f"X10 Fehler: Symbol {symbol} nicht im Cache gefunden.")
            return None
            
        try:
            market_data = self.market_info[symbol]

            # KORREKTUR: 'funding_rate' im Cache ist die 8-Stunden-Rate (z.B. 0.0001 = 0.01%).
            # Wir brauchen die STÜNDLICHE Rate (als Dezimalzahl) für die APY-Berechnung.
            if market_data.market_stats and market_data.market_stats.funding_rate is not None:
                # KORREKTUR: Der Wert 'funding_rate' ist die 8-Stunden-Rate
                return float(market_data.market_stats.funding_rate)
            else:
                return None
        except Exception as e:
            # print(f"X10 Fehler (fetch_funding_rate für {symbol}): {e}")
            return None

    def close(self):
        """
        Schließt den Adapter.
        (Da wir pro Aufruf einen Client öffnen/schließen, ist hier nichts zu tun).
        """
        print("X10Adapter geschlossen.")
        pass