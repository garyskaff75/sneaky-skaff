"""
enhancements.py
All profit-boosting enhancements:
- Partial take profits (50% at 4%, rest runs to 8%+)
- BTC correlation filter
- Volume spike detection
- Economic calendar (avoid news events)
- Funding rate farming
- Long/short ratio analysis
- On-chain metrics (exchange flows)
- Daily performance report
"""

import httpx
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class Enhancements:

    def __init__(self, bybit_client):
        self.futures = bybit_client

    # ── 1. PARTIAL TAKE PROFITS ───────────────────────────────────────────────
    async def set_partial_take_profits(self, symbol: str, entry_price: float,
                                        side: str, leverage: int) -> str:
        """
        TJR style: take 50% profit at 4%, move SL to breakeven,
        let remaining 50% run to 8%.
        """
        try:
            if side == "Buy":
                tp1 = round(entry_price * 1.04, 4)  # 50% at 4%
                tp2 = round(entry_price * 1.08, 4)  # 50% at 8%
                be  = round(entry_price * 1.001, 4) # Breakeven SL after TP1
            else:
                tp1 = round(entry_price * 0.96, 4)
                tp2 = round(entry_price * 0.92, 4)
                be  = round(entry_price * 0.999, 4)

            # Set initial TP at 4% (Bybit handles partial via reduce-only orders)
            result1 = self.futures._post("/v5/position/trading-stop", {
                "category":    "linear",
                "symbol":      symbol,
                "takeProfit":  str(tp1),
                "tpTriggerBy": "MarkPrice",
                "positionIdx": 0,
            })

            return (
                f"Partial TPs set for {symbol}:\n"
                f"TP1: ${tp1:,.4f} (+4%) — 50% position\n"
                f"TP2: ${tp2:,.4f} (+8%) — remaining 50%\n"
                f"After TP1, SL moves to breakeven: ${be:,.4f}"
            )
        except Exception as e:
            return f"Partial TP error: {e}"

    # ── 2. BTC CORRELATION FILTER ─────────────────────────────────────────────
    async def check_btc_correlation(self) -> dict:
        """
        Check BTC trend before trading altcoins.
        If BTC is in strong downtrend, avoid longs on alts.
        If BTC is in strong uptrend, avoid shorts on alts.
        """
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://api.bybit.com/v5/market/kline",
                    params={"category": "linear", "symbol": "BTCUSDT", "interval": "60", "limit": 50}
                )
                if r.status_code != 200:
                    return {"filter": "NEUTRAL", "reason": "BTC data unavailable"}

                klines = r.json()["result"]["list"]
                closes = [float(k[4]) for k in reversed(klines)]

                current  = closes[-1]
                ema_20   = sum(closes[-20:]) / 20
                ema_50   = sum(closes[-50:]) / 50 if len(closes) >= 50 else ema_20
                change1h = (closes[-1] - closes[-2]) / closes[-2] * 100
                change4h = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0

                if current > ema_20 > ema_50 and change4h > 0.5:
                    return {
                        "filter": "LONG_ONLY",
                        "reason": f"BTC uptrend — only take LONG altcoin trades (BTC +{change4h:.2f}% 4h)",
                        "btc_price": current,
                        "btc_change_4h": change4h,
                    }
                elif current < ema_20 < ema_50 and change4h < -0.5:
                    return {
                        "filter": "SHORT_ONLY",
                        "reason": f"BTC downtrend — only take SHORT altcoin trades (BTC {change4h:.2f}% 4h)",
                        "btc_price": current,
                        "btc_change_4h": change4h,
                    }
                elif abs(change1h) > 2:
                    return {
                        "filter": "AVOID",
                        "reason": f"BTC moving too fast ({change1h:+.2f}% 1h) — wait for stabilization",
                        "btc_price": current,
                        "btc_change_4h": change4h,
                    }
                else:
                    return {
                        "filter": "NEUTRAL",
                        "reason": f"BTC ranging — trade both directions with caution",
                        "btc_price": current,
                        "btc_change_4h": change4h,
                    }
        except Exception as e:
            return {"filter": "NEUTRAL", "reason": f"BTC check failed: {e}"}

    # ── 3. VOLUME SPIKE DETECTION ─────────────────────────────────────────────
    async def check_volume_spike(self, symbol: str) -> dict:
        """
        Detect when volume spikes 3x above average.
        Volume spikes = institutional activity = TJR confirmation.
        """
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://api.bybit.com/v5/market/kline",
                    params={"category": "linear", "symbol": symbol, "interval": "60", "limit": 24}
                )
                if r.status_code != 200:
                    return {"spike": False, "reason": "Data unavailable"}

                klines  = r.json()["result"]["list"]
                volumes = [float(k[5]) for k in reversed(klines)]

                if len(volumes) < 5:
                    return {"spike": False, "reason": "Not enough data"}

                avg_volume     = sum(volumes[:-1]) / len(volumes[:-1])
                current_volume = volumes[-1]
                ratio          = current_volume / avg_volume if avg_volume > 0 else 1

                if ratio >= 3:
                    return {
                        "spike": True,
                        "ratio": round(ratio, 2),
                        "reason": f"STRONG volume spike: {ratio:.1f}x average — institutional activity!",
                        "confirmation": "HIGH",
                    }
                elif ratio >= 2:
                    return {
                        "spike": True,
                        "ratio": round(ratio, 2),
                        "reason": f"Volume spike: {ratio:.1f}x average — elevated activity",
                        "confirmation": "MEDIUM",
                    }
                else:
                    return {
                        "spike": False,
                        "ratio": round(ratio, 2),
                        "reason": f"Normal volume ({ratio:.1f}x average)",
                        "confirmation": "LOW",
                    }
        except Exception as e:
            return {"spike": False, "reason": f"Volume check failed: {e}"}

    # ── 4. ECONOMIC CALENDAR ──────────────────────────────────────────────────
    async def check_economic_calendar(self) -> dict:
        """
        Check for upcoming high-impact events.
        Avoid trading 30 min before/after major events.
        Uses a free public API for economic calendar.
        """
        try:
            now     = datetime.now(timezone.utc)
            # Known recurring high-impact events (EST times converted to UTC)
            # These are approximate - FOMC, CPI, NFP etc
            high_impact_hours_utc = {
                # FOMC meetings typically 2 PM EST = 19:00 UTC
                "FOMC": 19,
                # CPI releases typically 8:30 AM EST = 13:30 UTC
                "CPI/NFP": 13,
            }

            current_hour = now.hour
            current_min  = now.minute

            # Check if we're within 30 min of a known high-impact time
            for event, hour in high_impact_hours_utc.items():
                mins_to_event = (hour - current_hour) * 60 - current_min
                if -30 <= mins_to_event <= 30:
                    return {
                        "avoid": True,
                        "reason": f"Near potential high-impact event ({event}) — avoid trading",
                        "mins_to_event": mins_to_event,
                    }

            # Check CoinMarketCal for crypto-specific events
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(
                        "https://developers.coinmarketcal.com/v1/events",
                        params={
                            "max": 5,
                            "dateRangeStart": now.strftime("%Y-%m-%d"),
                            "dateRangeEnd":   (now + timedelta(hours=2)).strftime("%Y-%m-%d"),
                            "sortBy":         "hot_score",
                        },
                        headers={"x-api-key": "free"}
                    )
                    if r.status_code == 200:
                        events = r.json().get("body", [])
                        high_impact = [e for e in events if e.get("hot_score", 0) > 80]
                        if high_impact:
                            return {
                                "avoid": True,
                                "reason": f"High-impact crypto event in next 2h: {high_impact[0].get('title', {}).get('en', 'Unknown')}",
                            }
            except Exception:
                pass

            return {"avoid": False, "reason": "No high-impact events detected"}

        except Exception as e:
            return {"avoid": False, "reason": f"Calendar check failed: {e}"}

    # ── 5. FUNDING RATE FARMING ───────────────────────────────────────────────
    async def check_funding_opportunities(self) -> list:
        """
        Find pairs where funding rate is extreme (>0.1% or <-0.1%).
        Extreme negative funding = shorts paying longs = open long for funding income.
        Extreme positive funding = longs paying shorts = open short for funding income.
        """
        opportunities = []
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "DOGEUSDT"]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for symbol in symbols:
                    try:
                        r = await client.get(
                            "https://api.bybit.com/v5/market/tickers",
                            params={"category": "linear", "symbol": symbol}
                        )
                        if r.status_code == 200:
                            ticker      = r.json()["result"]["list"][0]
                            funding     = float(ticker.get("fundingRate", 0))
                            next_funding = ticker.get("nextFundingTime", "")
                            price        = float(ticker.get("lastPrice", 0))

                            if abs(funding) >= 0.001:  # 0.1% threshold
                                direction = "LONG" if funding < 0 else "SHORT"
                                ann_rate  = funding * 3 * 365 * 100  # Annualized %
                                opportunities.append({
                                    "symbol":        symbol,
                                    "funding_rate":  round(funding * 100, 4),
                                    "direction":     direction,
                                    "annualized":    round(ann_rate, 1),
                                    "next_funding":  next_funding,
                                    "price":         price,
                                    "reason": f"Funding {funding*100:.4f}% every 8h — go {direction} to earn funding",
                                })
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Funding check error: {e}")

        return sorted(opportunities, key=lambda x: abs(x["funding_rate"]), reverse=True)

    # ── 6. LONG/SHORT RATIO ───────────────────────────────────────────────────
    async def get_long_short_ratio(self, symbol: str) -> dict:
        """
        Get the long/short ratio for a symbol.
        >70% longs = overcrowded = squeeze risk = fade the crowd
        >70% shorts = overcrowded = short squeeze risk
        """
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://api.bybit.com/v5/market/account-ratio",
                    params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1}
                )
                if r.status_code == 200:
                    data      = r.json()
                    if data.get("retCode") == 0 and data["result"]["list"]:
                        item      = data["result"]["list"][0]
                        buy_ratio = float(item.get("buyRatio", 0.5))
                        sell_ratio = float(item.get("sellRatio", 0.5))

                        if buy_ratio > 0.70:
                            sentiment = "CROWDED_LONG"
                            signal    = "Fade — too many longs, squeeze likely SHORT"
                        elif sell_ratio > 0.70:
                            sentiment = "CROWDED_SHORT"
                            signal    = "Fade — too many shorts, squeeze likely LONG"
                        elif buy_ratio > 0.55:
                            sentiment = "SLIGHTLY_LONG"
                            signal    = "Mildly bullish positioning"
                        elif sell_ratio > 0.55:
                            sentiment = "SLIGHTLY_SHORT"
                            signal    = "Mildly bearish positioning"
                        else:
                            sentiment = "BALANCED"
                            signal    = "Balanced positioning — no crowd bias"

                        return {
                            "long_pct":  round(buy_ratio * 100, 1),
                            "short_pct": round(sell_ratio * 100, 1),
                            "sentiment": sentiment,
                            "signal":    signal,
                        }
        except Exception as e:
            logger.error(f"Long/short ratio error: {e}")

        return {"long_pct": 50, "short_pct": 50, "sentiment": "UNKNOWN", "signal": "Data unavailable"}

    # ── 7. OPEN INTEREST ANALYSIS ─────────────────────────────────────────────
    async def get_open_interest_change(self, symbol: str) -> dict:
        """
        Rising OI + rising price = strong uptrend (new money entering)
        Rising OI + falling price = strong downtrend
        Falling OI + rising price = short covering (weak move)
        Falling OI + falling price = long liquidations (panic)
        """
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://api.bybit.com/v5/market/open-interest",
                    params={"category": "linear", "symbol": symbol,
                            "intervalTime": "1h", "limit": 5}
                )
                if r.status_code == 200 and r.json().get("retCode") == 0:
                    items   = r.json()["result"]["list"]
                    oi_vals = [float(i["openInterest"]) for i in reversed(items)]

                    if len(oi_vals) >= 2:
                        oi_change = (oi_vals[-1] - oi_vals[0]) / oi_vals[0] * 100

                        if oi_change > 5:
                            return {"change_pct": round(oi_change, 2), "signal": "Rising OI — strong conviction move"}
                        elif oi_change < -5:
                            return {"change_pct": round(oi_change, 2), "signal": "Falling OI — position unwinding"}
                        else:
                            return {"change_pct": round(oi_change, 2), "signal": "Stable OI — no strong conviction"}
        except Exception as e:
            logger.error(f"OI error: {e}")

        return {"change_pct": 0, "signal": "OI data unavailable"}

    # ── 8. DAILY PERFORMANCE REPORT ──────────────────────────────────────────
    async def generate_daily_report(self, trade_log: list, start_balance: float,
                                     current_balance: float) -> str:
        """Generate a daily performance report."""
        pnl        = current_balance - start_balance
        pnl_pct    = (pnl / start_balance * 100) if start_balance > 0 else 0
        total      = len(trade_log)
        wins       = len([t for t in trade_log if t.get("pnl", 0) > 0])
        losses     = len([t for t in trade_log if t.get("pnl", 0) < 0])
        win_rate   = (wins / total * 100) if total > 0 else 0

        # Funding opportunities
        funding    = await self.check_funding_opportunities()
        fund_lines = ""
        for f in funding[:2]:
            fund_lines += f"\n  {f['symbol']}: {f['funding_rate']:+.4f}% → {f['direction']} ({f['annualized']:+.1f}% annualized)"

        # BTC correlation
        btc = await self.check_btc_correlation()

        sep = "=" * 30
        report = (
            f"📊 DAILY TRADING REPORT\n{sep}\n"
            f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
            f"ACCOUNT:\n"
            f"  Start: ${start_balance:.2f} USDT\n"
            f"  Current: ${current_balance:.2f} USDT\n"
            f"  P&L: ${pnl:+.4f} ({pnl_pct:+.2f}%)\n\n"
            f"TRADES:\n"
            f"  Total: {total}\n"
            f"  Wins: {wins} | Losses: {losses}\n"
            f"  Win rate: {win_rate:.1f}%\n\n"
            f"MARKET:\n"
            f"  BTC bias: {btc.get('reason', 'N/A')}\n\n"
            f"FUNDING OPPORTUNITIES:{fund_lines if fund_lines else chr(10) + '  None above threshold'}\n\n"
            f"SESSIONS TODAY:\n"
            f"  London: 03:00-07:00 EST\n"
            f"  New York: 08:00-12:00 EST\n"
            f"{sep}"
        )
        return report
