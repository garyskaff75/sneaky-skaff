"""
tjr_analysis.py
Implements TJR's Smart Money Concepts (SMC) trading strategy:
- Multi-timeframe analysis (Daily -> 4H -> 1H -> 15M)
- Liquidity sweep detection
- Fair Value Gap (FVG) detection
- Order Block detection
- Break of Structure (BOS) / Change of Character (CHoCH)
- Market structure analysis
"""

import logging
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)


class TJRAnalysis:

    def __init__(self, market_data):
        self.market = market_data

    async def full_analysis(self, symbol: str) -> dict:
        """
        Run full TJR multi-timeframe analysis on a symbol.
        Returns a comprehensive SMC analysis dict.
        """
        logger.info(f"TJR full analysis starting for {symbol}")

        # Fetch all timeframes in parallel
        import asyncio
        daily_k, h4_k, h1_k, m15_k, m5_k = await asyncio.gather(
            self.market.get_klines(symbol, "D",  100),
            self.market.get_klines(symbol, "240", 100),
            self.market.get_klines(symbol, "60",  200),
            self.market.get_klines(symbol, "15",  200),
            self.market.get_klines(symbol, "5",   200),
            return_exceptions=True
        )

        result = {"symbol": symbol, "timeframes": {}}

        # Analyze each timeframe
        if isinstance(daily_k, list) and len(daily_k) >= 20:
            result["timeframes"]["daily"] = self._analyze_structure(daily_k, "Daily")
        if isinstance(h4_k, list) and len(h4_k) >= 20:
            result["timeframes"]["h4"]    = self._analyze_structure(h4_k, "4H")
        if isinstance(h1_k, list) and len(h1_k) >= 20:
            result["timeframes"]["h1"]    = self._analyze_structure(h1_k, "1H")
        if isinstance(m15_k, list) and len(m15_k) >= 20:
            result["timeframes"]["m15"]   = self._analyze_structure(m15_k, "15M")
        if isinstance(m5_k, list) and len(m5_k) >= 20:
            result["timeframes"]["m5"]    = self._analyze_structure(m5_k, "5M")

        # Higher timeframe bias (Daily + 4H)
        result["htf_bias"]     = self._get_htf_bias(result["timeframes"])

        # Entry timeframe signals (1H + 15M)
        result["entry_signal"] = self._get_entry_signal(result["timeframes"])

        # Overall TJR signal
        result["tjr_signal"]   = self._combine_signals(result)

        return result

    # ── Market Structure ──────────────────────────────────────────────────────
    def _analyze_structure(self, klines: list, tf_name: str) -> dict:
        closes = np.array([k[4] for k in klines])
        highs  = np.array([k[2] for k in klines])
        lows   = np.array([k[3] for k in klines])

        # Find swing highs and lows
        swing_highs = self._find_swing_highs(highs, lookback=5)
        swing_lows  = self._find_swing_lows(lows,   lookback=5)

        # Determine market structure
        structure   = self._determine_structure(highs, lows, swing_highs, swing_lows)

        # Detect BOS and CHoCH
        bos, choch  = self._detect_bos_choch(closes, highs, lows, structure)

        # Detect FVGs
        fvgs        = self._detect_fvg(klines)

        # Detect Order Blocks
        obs         = self._detect_order_blocks(klines, structure)

        # Detect Liquidity Sweeps
        sweeps      = self._detect_liquidity_sweeps(highs, lows, swing_highs, swing_lows, closes)

        return {
            "timeframe":    tf_name,
            "structure":    structure,
            "bos":          bos,
            "choch":        choch,
            "fvgs":         fvgs[:3],   # Top 3 most recent FVGs
            "order_blocks": obs[:2],    # Top 2 most recent OBs
            "sweeps":       sweeps[:2], # Last 2 sweeps
            "current_price": float(closes[-1]),
            "last_high":    float(max(highs[-20:])),
            "last_low":     float(min(lows[-20:])),
        }

    # ── Swing High/Low Detection ──────────────────────────────────────────────
    def _find_swing_highs(self, highs: np.ndarray, lookback: int = 5) -> list:
        """Find significant swing highs."""
        swing_highs = []
        for i in range(lookback, len(highs) - lookback):
            if highs[i] == max(highs[i-lookback:i+lookback+1]):
                swing_highs.append({"index": i, "price": float(highs[i])})
        return swing_highs

    def _find_swing_lows(self, lows: np.ndarray, lookback: int = 5) -> list:
        """Find significant swing lows."""
        swing_lows = []
        for i in range(lookback, len(lows) - lookback):
            if lows[i] == min(lows[i-lookback:i+lookback+1]):
                swing_lows.append({"index": i, "price": float(lows[i])})
        return swing_lows

    # ── Market Structure ──────────────────────────────────────────────────────
    def _determine_structure(self, highs, lows, swing_highs, swing_lows) -> str:
        """Determine if market is in uptrend (HH/HL), downtrend (LH/LL), or ranging."""
        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "Ranging"

        recent_highs = [s["price"] for s in swing_highs[-3:]]
        recent_lows  = [s["price"] for s in swing_lows[-3:]]

        hh = all(recent_highs[i] > recent_highs[i-1] for i in range(1, len(recent_highs)))
        hl = all(recent_lows[i]  > recent_lows[i-1]  for i in range(1, len(recent_lows)))
        lh = all(recent_highs[i] < recent_highs[i-1] for i in range(1, len(recent_highs)))
        ll = all(recent_lows[i]  < recent_lows[i-1]  for i in range(1, len(recent_lows)))

        if hh and hl: return "Uptrend (HH/HL)"
        if lh and ll: return "Downtrend (LH/LL)"
        if hh:        return "Weak Uptrend (HH only)"
        if ll:        return "Weak Downtrend (LL only)"
        return "Ranging"

    # ── BOS and CHoCH Detection ───────────────────────────────────────────────
    def _detect_bos_choch(self, closes, highs, lows, structure) -> tuple:
        """
        BOS = Break of Structure (trend continuation)
        CHoCH = Change of Character (trend reversal)
        """
        bos   = "None"
        choch = "None"

        if len(highs) < 20:
            return bos, choch

        recent_high = float(max(highs[-20:-5]))
        recent_low  = float(min(lows[-20:-5]))
        current     = float(closes[-1])
        prev        = float(closes[-3])

        # BOS: price breaks above recent high (bullish) or below recent low (bearish)
        if current > recent_high and prev <= recent_high:
            bos = f"Bullish BOS — broke ${recent_high:,.4f}"
        elif current < recent_low and prev >= recent_low:
            bos = f"Bearish BOS — broke ${recent_low:,.4f}"

        # CHoCH: opposite break after a trend
        if "Uptrend" in structure and current < recent_low:
            choch = f"Bearish CHoCH — potential reversal below ${recent_low:,.4f}"
        elif "Downtrend" in structure and current > recent_high:
            choch = f"Bullish CHoCH — potential reversal above ${recent_high:,.4f}"

        return bos, choch

    # ── Fair Value Gap Detection ──────────────────────────────────────────────
    def _detect_fvg(self, klines: list) -> list:
        """
        FVG = 3-candle pattern where middle candle leaves a price gap.
        Bullish FVG: candle1 high < candle3 low (gap between them)
        Bearish FVG: candle1 low > candle3 high (gap between them)
        """
        fvgs = []
        for i in range(2, len(klines)):
            c1_high = float(klines[i-2][2])
            c1_low  = float(klines[i-2][3])
            c2_high = float(klines[i-1][2])
            c2_low  = float(klines[i-1][3])
            c3_high = float(klines[i][2])
            c3_low  = float(klines[i][3])
            c3_close = float(klines[i][4])

            # Bullish FVG: gap between candle1 high and candle3 low
            if c3_low > c1_high:
                gap_size = ((c3_low - c1_high) / c1_high) * 100
                if gap_size > 0.1:  # Minimum 0.1% gap
                    fvgs.append({
                        "type":     "Bullish FVG",
                        "top":      c3_low,
                        "bottom":   c1_high,
                        "midpoint": (c3_low + c1_high) / 2,
                        "gap_pct":  round(gap_size, 3),
                        "index":    i,
                        "filled":   c3_close < c1_high,  # Price came back to fill it
                    })

            # Bearish FVG: gap between candle3 high and candle1 low
            elif c1_low > c3_high:
                gap_size = ((c1_low - c3_high) / c3_high) * 100
                if gap_size > 0.1:
                    fvgs.append({
                        "type":     "Bearish FVG",
                        "top":      c1_low,
                        "bottom":   c3_high,
                        "midpoint": (c1_low + c3_high) / 2,
                        "gap_pct":  round(gap_size, 3),
                        "index":    i,
                        "filled":   c3_close > c1_low,
                    })

        # Return unfilled FVGs only, most recent first
        unfilled = [f for f in fvgs if not f["filled"]]
        return sorted(unfilled, key=lambda x: x["index"], reverse=True)

    # ── Order Block Detection ─────────────────────────────────────────────────
    def _detect_order_blocks(self, klines: list, structure: str) -> list:
        """
        Order Block = last bullish/bearish candle before a strong move.
        Bullish OB: last bearish candle before strong upward BOS
        Bearish OB: last bullish candle before strong downward BOS
        """
        obs     = []
        closes  = [float(k[4]) for k in klines]
        opens   = [float(k[1]) for k in klines]
        highs   = [float(k[2]) for k in klines]
        lows    = [float(k[3]) for k in klines]

        for i in range(3, len(klines) - 3):
            # Strong move threshold: 0.5% move in next 3 candles
            next_move = (closes[i+2] - closes[i]) / closes[i] * 100

            if next_move > 0.5:  # Strong bullish move after this candle
                # Look for last bearish candle (bearish OB = potential support)
                if closes[i] < opens[i]:  # Bearish candle
                    obs.append({
                        "type":   "Bullish OB (support)",
                        "top":    highs[i],
                        "bottom": lows[i],
                        "index":  i,
                        "move":   round(next_move, 2),
                    })

            elif next_move < -0.5:  # Strong bearish move after this candle
                if closes[i] > opens[i]:  # Bullish candle
                    obs.append({
                        "type":   "Bearish OB (resistance)",
                        "top":    highs[i],
                        "bottom": lows[i],
                        "index":  i,
                        "move":   round(next_move, 2),
                    })

        return sorted(obs, key=lambda x: x["index"], reverse=True)

    # ── Liquidity Sweep Detection ─────────────────────────────────────────────
    def _detect_liquidity_sweeps(self, highs, lows, swing_highs, swing_lows, closes) -> list:
        """
        Liquidity sweep = price briefly goes above a swing high (sweeping buy-side liquidity)
        or below a swing low (sweeping sell-side liquidity) then reverses.
        This is TJR's PRIMARY entry trigger.
        """
        sweeps  = []
        current = float(closes[-1])

        # Check if price swept above any recent swing high then reversed
        for sh in swing_highs[-5:]:
            sh_price = sh["price"]
            sh_idx   = sh["index"]

            # Was there a candle that went above this high recently?
            for i in range(sh_idx + 1, len(highs)):
                if float(highs[i]) > sh_price:
                    # Price swept above — did it reverse?
                    if float(closes[i]) < sh_price:
                        # Confirmed sweep and rejection
                        sweeps.append({
                            "type":       "Sell-side sweep (bearish)",
                            "swept_level": sh_price,
                            "sweep_high":  float(highs[i]),
                            "current":     current,
                            "index":       i,
                            "fresh":       i >= len(highs) - 10,
                        })
                    break

        # Check if price swept below any recent swing low then reversed
        for sl in swing_lows[-5:]:
            sl_price = sl["price"]
            sl_idx   = sl["index"]

            for i in range(sl_idx + 1, len(lows)):
                if float(lows[i]) < sl_price:
                    if float(closes[i]) > sl_price:
                        sweeps.append({
                            "type":       "Buy-side sweep (bullish)",
                            "swept_level": sl_price,
                            "sweep_low":   float(lows[i]),
                            "current":     current,
                            "index":       i,
                            "fresh":       i >= len(lows) - 10,
                        })
                    break

        return sorted(sweeps, key=lambda x: x["index"], reverse=True)

    # ── HTF Bias ──────────────────────────────────────────────────────────────
    def _get_htf_bias(self, timeframes: dict) -> str:
        """Determine higher timeframe bias from Daily and 4H."""
        daily = timeframes.get("daily", {})
        h4    = timeframes.get("h4", {})

        daily_str = daily.get("structure", "")
        h4_str    = h4.get("structure", "")

        if "Uptrend" in daily_str and "Uptrend" in h4_str:
            return "Strong Bullish — both Daily and 4H in uptrend"
        elif "Downtrend" in daily_str and "Downtrend" in h4_str:
            return "Strong Bearish — both Daily and 4H in downtrend"
        elif "Uptrend" in daily_str:
            return "Bullish (Daily uptrend, 4H mixed)"
        elif "Downtrend" in daily_str:
            return "Bearish (Daily downtrend, 4H mixed)"
        elif "Uptrend" in h4_str:
            return "Mildly Bullish (4H uptrend)"
        elif "Downtrend" in h4_str:
            return "Mildly Bearish (4H downtrend)"
        return "Neutral — no clear HTF bias, avoid trading"

    # ── Entry Signal ──────────────────────────────────────────────────────────
    def _get_entry_signal(self, timeframes: dict) -> str:
        """Check 1H, 15M and 5M for entry triggers."""
        h1  = timeframes.get("h1",  {})
        m15 = timeframes.get("m15", {})
        m5  = timeframes.get("m5",  {})
        m5  = timeframes.get("m5",  {})

        signals = []

        # Check for fresh liquidity sweeps on 1H
        h1_sweeps = h1.get("sweeps", [])
        for s in h1_sweeps:
            if s.get("fresh"):
                signals.append(f"1H: {s['type']} at ${s['swept_level']:,.4f}")

        # Check for BOS on 15M and 5M
        m15_bos = m15.get("bos", "None")
        if m15_bos != "None":
            signals.append(f"15M BOS: {m15_bos}")
        m5_bos = m5.get("bos", "None")
        if m5_bos != "None":
            signals.append(f"5M BOS: {m5_bos} (precise entry)")

        # Check for CHoCH on 1H
        h1_choch = h1.get("choch", "None")
        if h1_choch != "None":
            signals.append(f"1H CHoCH: {h1_choch}")

        # Check for unfilled FVGs near current price
        h1_fvgs = h1.get("fvgs", [])
        if h1_fvgs:
            fvg = h1_fvgs[0]
            signals.append(f"1H FVG: {fvg['type']} ${fvg['bottom']:,.4f}-${fvg['top']:,.4f}")

        return " | ".join(signals) if signals else "No clear entry trigger"

    # ── Combine All Signals ───────────────────────────────────────────────────
    def _combine_signals(self, result: dict) -> dict:
        """
        TJR only enters when:
        1. HTF bias is clear (not neutral)
        2. Liquidity sweep has occurred
        3. BOS or CHoCH confirms direction
        4. FVG or OB provides entry point
        """
        htf_bias     = result.get("htf_bias", "")
        entry_signal = result.get("entry_signal", "")
        h1           = result.get("timeframes", {}).get("h1",  {})
        m15          = result.get("timeframes", {}).get("m15", {})

        score        = 0
        reasons      = []
        direction    = "STAY OUT"

        # 1. HTF Bias (most important — 3 points)
        if "Strong Bullish" in htf_bias:
            score += 3; direction = "LONG"; reasons.append("Strong bullish HTF bias")
        elif "Strong Bearish" in htf_bias:
            score += 3; direction = "SHORT"; reasons.append("Strong bearish HTF bias")
        elif "Bullish" in htf_bias:
            score += 1; direction = "LONG"; reasons.append("Bullish HTF bias")
        elif "Bearish" in htf_bias:
            score += 1; direction = "SHORT"; reasons.append("Bearish HTF bias")
        else:
            reasons.append("No clear HTF bias — staying out")
            return {"signal": "STAY OUT", "score": 0, "reasons": reasons, "confidence": "Low"}

        # 2. Liquidity sweep (critical — 3 points)
        h1_sweeps  = h1.get("sweeps",  [])
        m15_sweeps = m15.get("sweeps", [])
        m5_sweeps  = m5.get("sweeps",  []) if "m5" in timeframes else []
        all_sweeps   = h1_sweeps + m15_sweeps + m5_sweeps
        fresh_sweeps = [s for s in all_sweeps if s.get("fresh")]
        if fresh_sweeps:
            sweep = fresh_sweeps[0]
            if direction == "LONG" and "bullish" in sweep["type"]:
                score += 3; reasons.append(f"Fresh bullish sweep at ${sweep['swept_level']:,.4f}")
            elif direction == "SHORT" and "bearish" in sweep["type"]:
                score += 3; reasons.append(f"Fresh bearish sweep at ${sweep['swept_level']:,.4f}")
            else:
                score -= 1; reasons.append("Sweep in opposite direction — caution")
        else:
            reasons.append("No fresh liquidity sweep — TJR would wait")

        # 3. BOS / CHoCH confirmation (2 points)
        m15_bos  = m15.get("bos",   "None")
        h1_choch = h1.get("choch",  "None")
        if direction == "LONG" and ("Bullish" in m15_bos or "Bullish" in m5_bos or "Bullish" in h1_choch):
            score += 2; reasons.append("Bullish BOS/CHoCH confirmed")
        elif direction == "SHORT" and ("Bearish" in m15_bos or "Bearish" in m5_bos or "Bearish" in h1_choch):
            score += 2; reasons.append("Bearish BOS/CHoCH confirmed")
        else:
            reasons.append("No BOS/CHoCH confirmation yet")

        # 4. FVG present (1 point)
        h1_fvgs = h1.get("fvgs", [])
        if h1_fvgs:
            fvg = h1_fvgs[0]
            if direction == "LONG" and "Bullish" in fvg["type"]:
                score += 1; reasons.append(f"Bullish FVG at ${fvg['bottom']:,.4f}-${fvg['top']:,.4f}")
            elif direction == "SHORT" and "Bearish" in fvg["type"]:
                score += 1; reasons.append(f"Bearish FVG at ${fvg['bottom']:,.4f}-${fvg['top']:,.4f}")

        # 5. Order Block (1 point)
        obs = h1.get("order_blocks", [])
        if obs:
            ob = obs[0]
            if direction == "LONG" and "Bullish" in ob["type"]:
                score += 1; reasons.append(f"Bullish OB support at ${ob['bottom']:,.4f}-${ob['top']:,.4f}")
            elif direction == "SHORT" and "Bearish" in ob["type"]:
                score += 1; reasons.append(f"Bearish OB resistance at ${ob['bottom']:,.4f}-${ob['top']:,.4f}")

        # Determine confidence and signal
        if score >= 7:
            confidence = "High"
        elif score >= 4:
            confidence = "Medium"
        else:
            confidence = "Low"
            direction  = "STAY OUT"  # TJR would not take this trade

        # TJR minimum: must have sweep + BOS + HTF bias (score >= 5)
        if score < 5:
            direction = "STAY OUT"
            reasons.append(f"Score {score}/10 — below TJR minimum threshold of 5")

        return {
            "signal":     direction,
            "score":      score,
            "confidence": confidence,
            "reasons":    reasons,
        }

    # ── Format for Claude ─────────────────────────────────────────────────────
    def format_for_claude(self, analysis: dict) -> str:
        """Format TJR analysis into a string for Claude's context."""
        tf    = analysis.get("timeframes", {})
        daily = tf.get("daily", {})
        h4    = tf.get("h4",    {})
        h1    = tf.get("h1",    {})
        m15   = tf.get("m15",   {})
        sig   = analysis.get("tjr_signal", {})

        lines = [
            f"TJR SMART MONEY ANALYSIS:",
            f"",
            f"HIGHER TIMEFRAME BIAS: {analysis.get('htf_bias', 'N/A')}",
            f"",
            f"DAILY: Structure={daily.get('structure','N/A')} | BOS={daily.get('bos','None')} | CHoCH={daily.get('choch','None')}",
            f"4H:    Structure={h4.get('structure','N/A')} | BOS={h4.get('bos','None')} | CHoCH={h4.get('choch','None')}",
            f"1H:    Structure={h1.get('structure','N/A')} | BOS={h1.get('bos','None')} | CHoCH={h1.get('choch','None')}",
            f"15M:   Structure={m15.get('structure','N/A')} | BOS={m15.get('bos','None')}",
        f"5M:    Structure={tf.get('m5',{}).get('structure','N/A')} | BOS={tf.get('m5',{}).get('bos','None')} (precise entry)",
            f"",
        ]

        # FVGs
        h1_fvgs = h1.get("fvgs", [])
        if h1_fvgs:
            lines.append("1H FAIR VALUE GAPS (unfilled):")
            for fvg in h1_fvgs[:2]:
                lines.append(f"  {fvg['type']}: ${fvg['bottom']:,.4f} - ${fvg['top']:,.4f} ({fvg['gap_pct']}%)")
        else:
            lines.append("1H FVGs: None detected")

        lines.append("")

        # Liquidity sweeps
        h1_sweeps = h1.get("sweeps", [])
        if h1_sweeps:
            lines.append("1H LIQUIDITY SWEEPS:")
            for s in h1_sweeps[:2]:
                fresh = "FRESH" if s.get("fresh") else "old"
                lines.append(f"  {s['type']} at ${s['swept_level']:,.4f} [{fresh}]")
        else:
            lines.append("1H Sweeps: None detected")

        lines.append("")

        # Order blocks
        h1_obs = h1.get("order_blocks", [])
        if h1_obs:
            lines.append("1H ORDER BLOCKS:")
            for ob in h1_obs[:2]:
                lines.append(f"  {ob['type']}: ${ob['bottom']:,.4f} - ${ob['top']:,.4f}")
        else:
            lines.append("1H Order Blocks: None detected")

        lines.append("")

        # TJR signal
        lines.append(f"TJR SIGNAL: {sig.get('signal','STAY OUT')} (Score: {sig.get('score',0)}/10, {sig.get('confidence','Low')} confidence)")
        lines.append("TJR Reasons:")
        for r in sig.get("reasons", []):
            lines.append(f"  - {r}")

        return "\n".join(lines)
