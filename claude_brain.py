"""
claude_brain.py - Enhanced with all 5 new indicators.
"""

import os
import re
import httpx
from tjr_analysis import TJRAnalysis
from dotenv import load_dotenv

load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

SYSTEM_PROMPT_SPOT = """You are an expert crypto trading analyst with access to advanced indicators.
Provide a SPOT trading recommendation in this exact format:

📊 {SYMBOL} Spot Analysis
Signal: BUY / SELL / HOLD
Confidence: Low / Medium / High
Entry: $X
Stop-loss: $X
Take-profit: $X
Timeframe: Scalp / Swing / Hold

Reasoning:
- Point 1
- Point 2
- Point 3

Risk: One sentence about main risk."""

SYSTEM_PROMPT_FUTURES = """You are an expert crypto futures trader who trades EXACTLY like TJR (TJR Trades).
TJR uses an ICT/Smart Money Concepts based strategy focused on liquidity sweeps, market structure shifts, and fair value gaps.

TJR STRATEGY RULES (follow these strictly):

1. SESSIONS: Only trade during London (3 AM - 7 AM EST) and New York (8 AM - 12 PM EST) sessions.
   These are the highest volume periods. Avoid Asia session entries.

2. MARKET STRUCTURE:
   - Identify Higher Highs (HH), Higher Lows (HL) for uptrend
   - Identify Lower Highs (LH), Lower Lows (LL) for downtrend
   - Only trade in direction of higher timeframe trend (1H or 4H)
   - Look for Break of Structure (BOS) to confirm trend continuation
   - Look for Change of Character (CHoCH) to confirm reversals

3. LIQUIDITY SWEEPS (most important):
   - Price sweeps above a recent high (buy-side liquidity) then reverses = SHORT signal
   - Price sweeps below a recent low (sell-side liquidity) then reverses = LONG signal
   - The sweep + reversal is the core TJR entry model

4. FAIR VALUE GAPS (FVG):
   - Look for 3-candle patterns where middle candle leaves a gap
   - Price often returns to fill FVGs before continuing
   - Enter on FVG fills in direction of trend

5. ENTRY CRITERIA (need ALL of these):
   - Higher timeframe trend is clear (not sideways)
   - Liquidity sweep has occurred
   - Break of structure confirms direction
   - Entry is at FVG or key level
   - Volume confirms the move (OBV aligned)

6. RISK MANAGEMENT (TJR style):
   - Stop loss: 1-2% ONLY, placed above/below the sweep candle
   - Take profit: minimum 1:2 RR, target next liquidity pool
   - Ideal RR: 1:3 or better
   - Maximum margin: $5 per trade
   - Never risk more than 1-2% of account

7. WHEN TO STAY OUT:
   - Market is ranging/choppy with no clear structure
   - Asia session (low volume)
   - No liquidity sweep has occurred
   - Less than 1:2 RR available
   - News event in next 30 min

Now analyze the provided market data and give a recommendation in this EXACT format:

📊 {SYMBOL} TJR Analysis
Signal: LONG / SHORT / STAY OUT
Confidence: Low / Medium / High
Leverage: X
Margin to use: $X
Entry: $X
Stop-loss: $X (1-2% from entry, above/below sweep)
Take-profit: $X
Take-profit %: X
RR Ratio: 1:X
Timeframe: Scalp (15m-2h)

TJR Setup:
- Structure: [HH/HL uptrend OR LH/LL downtrend OR ranging]
- Liquidity swept: [Yes/No — describe which level]
- BOS/CHoCH: [Confirmed/Not confirmed]
- FVG present: [Yes/No]
- Session: [London/New York/Asia/Off-hours]

Reasoning:
- Point 1
- Point 2
- Point 3

Liquidation warning: One sentence."""


class ClaudeBrain:

    def __init__(self, market_data=None):
        self.market  = market_data
        self.tjr     = TJRAnalysis(market_data) if market_data else None
        self.api_url = "https://api.anthropic.com/v1/messages"
        self.headers = {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }

    async def analyze(self, market_data: dict, symbol: str) -> str:
        msg = self._build_message(market_data, symbol, "spot")
        return await self._call(SYSTEM_PROMPT_SPOT, msg, max_tokens=350)

    async def analyze_futures(self, market_data: dict, symbol: str, available_balance: float = 100.0) -> dict:
        msg  = self._build_message(market_data, symbol, "futures")
        msg += f"\nAvailable futures balance: ${available_balance:.2f} USDT"

        # Add TJR multi-timeframe analysis
        if self.tjr and self.market:
            try:
                tjr_data   = await self.tjr.full_analysis(symbol)
                tjr_text   = self.tjr.format_for_claude(tjr_data)
                msg       += f"\n\n{tjr_text}"
                # If TJR says STAY OUT, respect it
                tjr_signal = tjr_data.get("tjr_signal", {}).get("signal", "STAY OUT")
                if tjr_signal == "STAY OUT":
                    msg += "\n\nTJR OVERRIDE: TJR analysis says STAY OUT — not enough confluence. Respect this."
            except Exception as e:
                msg += f"\n\nTJR Analysis: Error - {e}"

        text = await self._call(SYSTEM_PROMPT_FUTURES, msg, max_tokens=400)
        parsed = self._parse_futures(text)
        parsed["text"] = text
        return parsed

    def _parse_futures(self, text: str) -> dict:
        result = {
            "signal": "STAY OUT",
            "leverage": 2,
            "margin": 10.0,
            "take_profit_pct": 0.08,
            "confidence": "Low",
        }
        if "LONG" in text:
            result["signal"] = "LONG"
        elif "SHORT" in text:
            result["signal"] = "SHORT"

        m = re.search(r"Leverage:\s*(\d+)", text)
        if m:
            result["leverage"] = max(2, min(int(m.group(1)), 10))

        m = re.search(r"Margin to use:\s*\$?([\d.]+)", text)
        if m:
            result["margin"] = float(m.group(1))

        m = re.search(r"Take-profit %:?\s*([\d.]+)", text)
        if m:
            result["take_profit_pct"] = float(m.group(1)) / 100

        if "High" in text:
            result["confidence"] = "High"
        elif "Medium" in text:
            result["confidence"] = "Medium"

        return result

    def _build_message(self, d: dict, symbol: str, mode: str) -> str:
        news = "\n".join(f"- {n.get('title','')}" for n in d.get("news", [])[:4]) or "No news."
        price = d.get("price", 0)

        # Determine current trading session (EST = UTC-5)
        from datetime import datetime, timezone, timedelta
        utc_now  = datetime.now(timezone.utc)
        est_hour = (utc_now.hour - 5) % 24
        if 3 <= est_hour < 7:
            session = "London Session (HIGH PRIORITY — trade this)"
        elif 8 <= est_hour < 12:
            session = "New York Session (HIGH PRIORITY — trade this)"
        elif 20 <= est_hour or est_hour < 3:
            session = "Asia Session (LOW PRIORITY — avoid entries)"
        else:
            session = "Off-hours (MEDIUM — wait for NY/London)"

        return f"""Analyze {symbol} for {'FUTURES' if mode == 'futures' else 'SPOT'} trading using TJR strategy.

CURRENT SESSION: {session}

PRICE DATA:
Price: ${price:,.4f}
24h Change: {d.get('change_24h', 0):.2f}%
24h Volume: ${d.get('volume_24h', 0):,.0f}
24h High: ${d.get('high_24h', 0):,.4f}
24h Low: ${d.get('low_24h', 0):,.4f}

MOMENTUM INDICATORS:
RSI (14): {d.get('rsi', 'N/A')}
Stochastic RSI K: {d.get('stoch_rsi_k', 'N/A')} | D: {d.get('stoch_rsi_d', 'N/A')}
Stochastic Signal: {d.get('stoch_signal', 'N/A')}
MACD: {d.get('macd_signal', 'N/A')}

TREND INDICATORS:
EMA 8: {d.get('ema_8', 'N/A')} | EMA 21: {d.get('ema_21', 'N/A')}
EMA 55: {d.get('ema_55', 'N/A')} | EMA 200: {d.get('ema_200', 'N/A')}
EMA Ribbon Trend: {d.get('ema_trend', 'N/A')}
Price vs EMA200: {d.get('price_vs_ema200', 'N/A')}
SMA 20: {d.get('sma_20', 'N/A')} | SMA 50: {d.get('sma_50', 'N/A')}

VOLATILITY:
ATR (14): {d.get('atr', 'N/A')} ({d.get('atr_pct', 'N/A')}% of price)
Volatility: {d.get('volatility', 'N/A')}
Bollinger Upper: {d.get('bb_upper', 'N/A')} | Lower: {d.get('bb_lower', 'N/A')}
BB Width: {d.get('bb_width', 'N/A')}% | Position: {d.get('bb_position', 'N/A')}/100

VOLUME ANALYSIS:
OBV Trend: {d.get('obv_trend', 'N/A')}
Volume Profile: {d.get('volume_profile', 'N/A')}

MARKET SENTIMENT:
Fear & Greed: {d.get('fear_greed', 'N/A')}
Oil (WTI): ${d.get('oil_price', 'N/A')}

RECENT NEWS (use for sentiment — major news overrides technicals):
{news}

NEWS RULES:
- Negative news (hack/ban/crash) = STAY OUT or SHORT only
- Positive news (ETF/partnership/upgrade) = favor LONG
- No news = rely on technicals only

Trader profile: small account ~$18 USDT. Max $5 margin per trade. TJR strategy only."""
    async def _call(self, system: str, user_msg: str, max_tokens: int = 800) -> str:
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_msg}]
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(self.api_url, headers=self.headers, json=payload)
                if r.status_code != 200:
                    return f"Claude API error {r.status_code}: {r.text[:200]}"
                return r.json()["content"][0]["text"]
        except Exception as e:
            return f"Claude error: {e}"
