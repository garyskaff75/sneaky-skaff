"""
market_data.py - With debug logging to diagnose data feed issues
"""

import os
import asyncio
import httpx
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
import logging

load_dotenv()
logger = logging.getLogger(__name__)
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")

COINGECKO_IDS = {
    "BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana",
    "BNBUSDT": "binancecoin", "XRPUSDT": "ripple", "ADAUSDT": "cardano",
    "DOGEUSDT": "dogecoin", "AVAXUSDT": "avalanche-2", "LINKUSDT": "chainlink",
    "DOTUSDT": "polkadot", "MATICUSDT": "matic-network", "LTCUSDT": "litecoin",
    "NEARUSDT": "near", "ATOMUSDT": "cosmos", "FTMUSDT": "fantom",
    "ARBUSDT": "arbitrum", "OPUSDT": "optimism", "INJUSDT": "injective-protocol",
}


class MarketData:

    async def get_binance_ticker(self, symbol: str) -> dict:
        # Ensure symbol has USDT suffix
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        # Try Bybit public API
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://api.bybit.com/v5/market/tickers",
                    params={"category": "linear", "symbol": symbol}
                )
                logger.info(f"Bybit ticker {symbol}: status={r.status_code} body={r.text[:200]}")
                if r.status_code == 200:
                    d = r.json()
                    if d.get("retCode") == 0 and d["result"]["list"]:
                        t = d["result"]["list"][0]
                        price = float(t["lastPrice"])
                        logger.info(f"Bybit price for {symbol}: ${price}")
                        return {
                            "price":      price,
                            "change_24h": float(t["price24hPcnt"]) * 100,
                            "volume_24h": float(t["turnover24h"]),
                            "high_24h":   float(t["highPrice24h"]),
                            "low_24h":    float(t["lowPrice24h"]),
                            "source":     "Bybit",
                        }
        except Exception as e:
            logger.error(f"Bybit ticker error: {e}")

        # Try CoinGecko
        cg_id = COINGECKO_IDS.get(symbol)
        if cg_id:
            try:
                url = f"https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true"
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(url)
                    logger.info(f"CoinGecko {symbol}: status={r.status_code} body={r.text[:200]}")
                    if r.status_code == 200:
                        d     = r.json().get(cg_id, {})
                        price = float(d.get("usd", 0))
                        if price > 0:
                            return {
                                "price": price, "change_24h": float(d.get("usd_24h_change", 0)),
                                "volume_24h": float(d.get("usd_24h_vol", 0)),
                                "high_24h": price * 1.02, "low_24h": price * 0.98,
                                "source": "CoinGecko",
                            }
            except Exception as e:
                logger.error(f"CoinGecko error: {e}")

        logger.error(f"All price sources failed for {symbol}")
        return {"price": 0, "change_24h": 0, "volume_24h": 0, "high_24h": 0, "low_24h": 0}

    async def get_klines(self, symbol: str, interval: str = "1h", limit: int = 200) -> list:
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        # Map interval formats to Bybit format
        interval_map = {
            "1h": "60", "60": "60", "1H": "60",
            "4h": "240", "240": "240", "4H": "240",
            "D": "D", "1d": "D", "daily": "D",
            "15": "15", "15m": "15", "15M": "15",
            "5": "5", "5m": "5",
        }
        bybit_interval = interval_map.get(interval, "60")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://api.bybit.com/v5/market/kline",
                    params={"category": "linear", "symbol": symbol, "interval": bybit_interval, "limit": limit}
                )
                logger.info(f"Bybit klines {symbol}: status={r.status_code} body={r.text[:100]}")
                if r.status_code == 200:
                    d = r.json()
                    if d.get("retCode") == 0 and d["result"]["list"]:
                        klines = [
                            [float(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
                            for k in reversed(d["result"]["list"])
                        ]
                        logger.info(f"Got {len(klines)} klines for {symbol}")
                        return klines
        except Exception as e:
            logger.error(f"Bybit klines error: {e}")
        return []

    def calculate_indicators(self, klines: list) -> dict:
        if not klines or len(klines) < 20:
            logger.warning(f"Not enough klines: {len(klines)}")
            return {}
        closes  = np.array([k[4] for k in klines])
        highs   = np.array([k[2] for k in klines])
        lows    = np.array([k[3] for k in klines])
        volumes = np.array([k[5] for k in klines])
        result  = {}

        rsi = self._rsi(closes, 14)
        result["rsi"] = round(rsi, 1) if rsi else None

        stoch_k, stoch_d = self._stochastic_rsi(closes)
        result["stoch_rsi_k"] = round(stoch_k, 1) if stoch_k is not None else None
        result["stoch_rsi_d"] = round(stoch_d, 1) if stoch_d is not None else None
        if stoch_k is not None:
            if stoch_k < 20:   result["stoch_signal"] = "Oversold (bullish)"
            elif stoch_k > 80: result["stoch_signal"] = "Overbought (bearish)"
            else:              result["stoch_signal"] = "Neutral"

        price  = closes[-1]
        ema8   = float(self._ema(closes, 8)[-1])   if len(closes) >= 8   else None
        ema21  = float(self._ema(closes, 21)[-1])  if len(closes) >= 21  else None
        ema55  = float(self._ema(closes, 55)[-1])  if len(closes) >= 55  else None
        ema200 = float(self._ema(closes, 200)[-1]) if len(closes) >= 200 else None

        result.update({"ema_8": round(ema8,2) if ema8 else None,
                        "ema_21": round(ema21,2) if ema21 else None,
                        "ema_55": round(ema55,2) if ema55 else None,
                        "ema_200": round(ema200,2) if ema200 else None})

        if all(v is not None for v in [ema8, ema21, ema55]):
            if ema8 > ema21 > ema55 and price > ema8:     result["ema_trend"] = "Strong uptrend"
            elif ema8 < ema21 < ema55 and price < ema8:   result["ema_trend"] = "Strong downtrend"
            elif ema8 > ema21:                              result["ema_trend"] = "Short-term bullish"
            elif ema8 < ema21:                              result["ema_trend"] = "Short-term bearish"
            else:                                           result["ema_trend"] = "Neutral"

        if ema200:
            result["price_vs_ema200"] = "Above EMA200 (bullish)" if price > ema200 else "Below EMA200 (bearish)"

        result["sma_20"] = round(float(np.mean(closes[-20:])), 2) if len(closes) >= 20 else None
        result["sma_50"] = round(float(np.mean(closes[-50:])), 2) if len(closes) >= 50 else None

        if len(closes) >= 20:
            sma = np.mean(closes[-20:])
            std = np.std(closes[-20:])
            result["bb_upper"]    = round(float(sma + 2*std), 2)
            result["bb_lower"]    = round(float(sma - 2*std), 2)
            result["bb_width"]    = round(float((4*std)/sma*100), 2)
            result["bb_position"] = round(float((price-(sma-2*std))/(4*std)*100), 1)

        result["macd_signal"]    = self._macd_signal(closes)
        atr = self._atr(highs, lows, closes, 14)
        if atr:
            result["atr"]        = round(atr, 4)
            result["atr_pct"]    = round(atr/closes[-1]*100, 2)
            result["volatility"] = "High" if result["atr_pct"] > 3 else ("Medium" if result["atr_pct"] > 1.5 else "Low")
        result["obv_trend"]      = self._obv_trend(closes, volumes)
        result["volume_profile"] = self._volume_profile(closes, volumes)
        return result

    def _rsi(self, prices, period=14):
        if len(prices) < period+1: return None
        deltas = np.diff(prices)
        gains  = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        ag, al = np.mean(gains[-period:]), np.mean(losses[-period:])
        return 100.0 if al == 0 else 100-(100/(1+ag/al))

    def _stochastic_rsi(self, prices, rsi_period=14, stoch_period=14, k_period=3, d_period=3):
        if len(prices) < rsi_period+stoch_period+k_period+d_period: return None, None
        rsi_vals = [self._rsi(prices[:i+1], rsi_period) for i in range(rsi_period, len(prices))]
        rsi_vals = [r for r in rsi_vals if r is not None]
        if len(rsi_vals) < stoch_period: return None, None
        stoch = []
        for i in range(stoch_period-1, len(rsi_vals)):
            w = rsi_vals[i-stoch_period+1:i+1]
            d = max(w)-min(w)
            stoch.append((rsi_vals[i]-min(w))/d*100 if d else 50)
        if len(stoch) < k_period: return None, None
        k = [np.mean(stoch[i-k_period+1:i+1]) for i in range(k_period-1, len(stoch))]
        return k[-1], float(np.mean(k[-d_period:])) if len(k) >= d_period else None

    def _ema(self, prices, period):
        k, ema = 2/(period+1), [prices[0]]
        for p in prices[1:]: ema.append(p*k+ema[-1]*(1-k))
        return np.array(ema)

    def _macd_signal(self, closes):
        if len(closes) < 26: return "N/A"
        macd = self._ema(closes,12)[-9:] - self._ema(closes,26)[-9:]
        sig  = self._ema(macd, 9)
        return "Bullish crossover" if macd[-1]>sig[-1] else ("Bearish crossover" if macd[-1]<sig[-1] else "Neutral")

    def _atr(self, highs, lows, closes, period=14):
        if len(closes) < period+1: return None
        tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
        return float(np.mean(tr[-period:]))

    def _obv_trend(self, closes, volumes):
        if len(closes) < 20: return "N/A"
        obv = [0]
        for i in range(1, len(closes)):
            obv.append(obv[-1] + (volumes[i] if closes[i]>closes[i-1] else (-volumes[i] if closes[i]<closes[i-1] else 0)))
        obv = np.array(obv)
        pu, ou = closes[-1]>closes[-20], obv[-1]>obv[-20]
        if pu and ou: return "Bullish confirmation"
        elif not pu and not ou: return "Bearish confirmation"
        elif pu and not ou: return "Bearish divergence"
        else: return "Bullish divergence"

    def _volume_profile(self, closes, volumes):
        if len(closes) < 20: return "N/A"
        rc, rv = closes[-50:], volumes[-50:]
        mn, mx = rc.min(), rc.max()
        if mx == mn: return "N/A"
        bs = (mx-mn)/10
        buckets = np.zeros(10)
        for p, v in zip(rc, rv):
            buckets[min(int((p-mn)/bs), 9)] += v
        poc = mn + (np.argmax(buckets)+0.5)*bs
        cur = closes[-1]
        if cur > poc*1.02: return f"Above POC ${poc:,.2f} (support below)"
        elif cur < poc*0.98: return f"Below POC ${poc:,.2f} (resistance above)"
        return f"Near POC ${poc:,.2f} (consolidation)"

    async def get_fear_greed(self):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get("https://api.alternative.me/fng/?limit=1")
                d = r.json()
                return f"{d['data'][0]['value']} ({d['data'][0]['value_classification']})"
        except: return "N/A"

    async def get_oil_price(self):
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get("https://query1.finance.yahoo.com/v8/finance/chart/CL=F?interval=1d&range=1d",
                                     headers={"User-Agent": "Mozilla/5.0"})
                return round(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"], 2)
        except: return None

    async def get_news(self, currency="BTC"):
        news = []

        # Source 1: CoinDesk RSS (always free, no auth)
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"https://cryptopanic.com/api/free/v1/posts/?auth_token=free&currencies={currency}&kind=news",
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                if r.status_code == 200:
                    items = r.json().get("results", [])[:3]
                    news.extend([{"title": i["title"], "url": i["url"]} for i in items])
        except Exception:
            pass

        # Source 2: Coinpaprika news (free, no key)
        if len(news) < 3:
            try:
                coin_map = {
                    "BTC": "btc-bitcoin", "ETH": "eth-ethereum",
                    "SOL": "sol-solana", "BNB": "bnb-binance-coin",
                    "AVAX": "avax-avalanche", "XRP": "xrp-xrp",
                    "DOGE": "doge-dogecoin",
                }
                coin_id = coin_map.get(currency, "btc-bitcoin")
                async with httpx.AsyncClient(timeout=8) as client:
                    r = await client.get(f"https://api.coinpaprika.com/v1/coins/{coin_id}/events")
                    if r.status_code == 200:
                        items = r.json()[:3]
                        news.extend([{"title": i.get("name", ""), "url": i.get("link", "")} for i in items if i.get("name")])
            except Exception:
                pass

        # Source 3: Alternative.me crypto news (free)
        if len(news) < 2:
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    r = await client.get("https://api.alternative.me/v1/ticker/?limit=1")
                    if r.status_code == 200:
                        news.append({"title": f"Market update: Fear & Greed and latest crypto sentiment", "url": "https://alternative.me"})
            except Exception:
                pass

        return news[:5]

    async def get_full_snapshot(self, symbol: str) -> dict:
        ticker, klines, fear_greed, oil_price, news = await asyncio.gather(
            self.get_binance_ticker(symbol),
            self.get_klines(symbol),
            self.get_fear_greed(),
            self.get_oil_price(),
            self.get_news(symbol.replace("USDT", "")),
            return_exceptions=True
        )
        snapshot = {"symbol": symbol, "timestamp": datetime.utcnow().isoformat()}
        if isinstance(ticker, dict): snapshot.update(ticker)
        if isinstance(klines, list) and len(klines) >= 20:
            snapshot.update(self.calculate_indicators(klines))
        snapshot["fear_greed"] = fear_greed if not isinstance(fear_greed, Exception) else "N/A"
        snapshot["oil_price"]  = oil_price  if not isinstance(oil_price, Exception)  else "N/A"
        snapshot["news"]       = news        if isinstance(news, list)                else []
        return snapshot
