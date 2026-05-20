"""
auto_trader.py — Full auto-execution, sync version for pyTelegramBotAPI
"""

import os
import asyncio
import logging
from datetime import datetime, timezone
from enhancements import Enhancements
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

MAX_MARGIN_PER_TRADE = 20.0
MAX_OPEN_POSITIONS   = 1
DAILY_LOSS_LIMIT_PCT = 0.20
MIN_TRADE_INTERVAL   = 900
SCAN_INTERVAL        = 900
TRADE_SYMBOLS        = ["SOLUSDT", "AVAXUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT", "NEARUSDT", "INJUSDT"]


class AutoTrader:

    def __init__(self, market, brain, futures, bot, chat_id: int):
        self.market      = market
        self.brain       = brain
        self.futures     = futures
        self.bot         = bot
        self.chat_id     = chat_id
        self.enabled          = False
        self.enhance          = Enhancements(futures)
        self.day_start_balance = None
        self.paused_reason     = None
        self.last_trade_time   = {}
        self.trade_log         = []

    def enable(self):
        self.enabled       = True
        self.paused_reason = None
        bal = self.futures.get_futures_balance()
        usdt = bal.get("USDT", {})
        self.day_start_balance = usdt.get("total", 0) if isinstance(usdt, dict) else 0
        logger.info(f"Auto-trader enabled. Day-start: ${self.day_start_balance:.2f}")

    def disable(self):
        self.enabled = False
        logger.info("Auto-trader disabled.")

    def _alert(self, message: str):
        try:
            if self.bot:
                self.bot.send_message(self.chat_id, message, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Alert failed: {e}")

    async def run_loop(self):
        await asyncio.sleep(20)
        logger.info("Auto-trader loop started")
        while True:
            if self.enabled and not self.paused_reason:
                # Use shorter interval during London/NY sessions, longer off-hours
                from datetime import datetime, timezone, timedelta
                est_hour = (datetime.now(timezone.utc).hour - 5) % 24
                in_session = (3 <= est_hour < 7) or (8 <= est_hour < 12)
                interval   = 300 if in_session else 1800  # 5 min in session, 30 min off-hours
                logger.info(f"Auto-trader scanning ({'London/NY session' if in_session else 'off-hours'})...")
                try:
                    await self._scan_and_trade()
                except Exception as e:
                    logger.error(f"Auto-trader error: {e}")
                    await self._alert("Auto-trader error: " + str(e))
            else:
                logger.info(f"Auto-trader idle — enabled={self.enabled} paused={self.paused_reason}")
                interval = SCAN_INTERVAL
            logger.info(f"Next scan in {interval}s")
            await asyncio.sleep(interval)

    async def _scan_and_trade(self):
        if not await self._check_daily_limit():
            return

        open_positions = self.futures.get_open_positions()
        if len(open_positions) > 0:
            syms    = [p["symbol"] for p in open_positions]
            pnl_sum = sum(p["unrealized_pnl"] for p in open_positions)
            logger.info(f"Position open {syms} PnL ${pnl_sum:+.4f} — pausing scan, saving credits")
            # Update trailing stop on existing position instead
            try:
                self.futures.update_all_trailing_stops()
            except Exception:
                pass
            return

        bal        = self.futures.get_futures_balance()
        avail      = float(bal.get("_total_equity", 0))
        if avail == 0:
            usdt  = bal.get("USDT", {})
            avail = float(usdt.get("available", 0)) if isinstance(usdt, dict) else 0
        logger.info(f"Auto-trader available balance: ${avail:.2f}")

        if avail < 5:
            await self._alert("Auto-trader: balance under $5 — pausing.")
        open_symbols = {p["symbol"] for p in open_positions}

        for symbol in TRADE_SYMBOLS:
            if symbol in open_symbols:
                continue
            if not self._cooldown_ok(symbol):
                continue
            open_positions = self.futures.get_open_positions()
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            try:
                await self._evaluate_and_trade(symbol, avail)
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Error evaluating {symbol}: {e}")

    async def _evaluate_and_trade(self, symbol: str, available_balance: float):
        # Step 1: Run TJR pre-filter BEFORE calling Claude API
        tjr_score = 0
        tjr_dir   = "STAY OUT"
        try:
            from tjr_analysis import TJRAnalysis
            tjr      = TJRAnalysis(self.market)
            tjr_data = await tjr.full_analysis(symbol)
            tjr_sig  = tjr_data.get("tjr_signal", {})
            tjr_score = int(tjr_sig.get("score", 0))
            tjr_dir   = str(tjr_sig.get("signal", "STAY OUT"))

            logger.info(f"{symbol} TJR pre-filter: score={tjr_score}/10 signal={tjr_dir}")

            if tjr_score < 5 or tjr_dir == "STAY OUT":
                logger.info(f"{symbol}: TJR score {tjr_score}/10 — skipping Claude (saving credits)")
                return
        except Exception as e:
            logger.warning(f"TJR pre-filter error for {symbol}: {e} — proceeding to Claude")
            tjr_score = 0
            tjr_dir   = "UNKNOWN"

        # Step 1b: Volume spike check (bonus confirmation)
        vol = await self.enhance.check_volume_spike(symbol)
        logger.info(f"{symbol} volume: {vol['reason']}")

        # Step 1c: Long/Short ratio
        ls = await self.enhance.get_long_short_ratio(symbol)
        logger.info(f"{symbol} L/S ratio: {ls['long_pct']}% long / {ls['short_pct']}% short — {ls['signal']}")

        # Block if crowd is overcrowded in same direction as TJR signal
        if ls["sentiment"] == "CROWDED_LONG" and tjr_dir == "LONG":
            logger.info(f"{symbol}: Too many longs ({ls['long_pct']}%) — TJR would fade this")
        if ls["sentiment"] == "CROWDED_SHORT" and tjr_dir == "SHORT":
            logger.info(f"{symbol}: Too many shorts ({ls['short_pct']}%) — TJR would fade this")

        # Step 1d: Open interest
        oi = await self.enhance.get_open_interest_change(symbol)
        logger.info(f"{symbol} OI: {oi['signal']}")

        # Step 2: Only if TJR passes, call Claude API
        data   = await self.market.get_full_snapshot(symbol)
        result = await self.brain.analyze_futures(data, symbol, available_balance)

        signal     = result.get("signal", "STAY OUT")
        confidence = result.get("confidence", "Low")
        leverage   = result.get("leverage", 2)
        margin     = min(result.get("margin", 10.0), MAX_MARGIN_PER_TRADE)
        tp_pct     = result.get("take_profit_pct", 0.08)

        logger.info(f"{symbol}: {signal} / {confidence}")

        if signal == "STAY OUT" or confidence == "Low":
            logger.info(f"{symbol}: skipping — {signal} / {confidence}")
            return

        side = "Buy" if signal == "LONG" else "Sell"

        # Execute the trade FIRST
        logger.info(f"Executing {side} {symbol} {leverage}x ${margin} margin")
        execution_result = self.futures.open_position(symbol, side, margin, leverage, tp_pct)
        logger.info(f"Order result: {execution_result}")

        # Only log and alert if order succeeded
        if "failed" in execution_result.lower() or "error" in execution_result.lower() or "not connected" in execution_result.lower():
            logger.error(f"Order failed for {symbol}: {execution_result}")
            await self._alert("Order failed for " + symbol + ": " + execution_result)
            return

        # Set trailing stop (1.5% trail to lock in profits)
        try:
            trail_result = self.futures.set_trailing_stop(symbol, trail_pct=0.015)
            logger.info(f"Trailing stop: {trail_result}")
        except Exception as e:
            logger.warning(f"Trailing stop failed: {e}")

        # Set partial take profits (TJR style: 50% at 4%, 50% at 8%)
        try:
            import asyncio as _asyncio
            positions = self.futures.get_open_positions()
            pos       = next((p for p in positions if p["symbol"] == symbol), None)
            entry     = pos["entry_price"] if pos else 0
            if entry > 0:
                ptp = await self.enhance.set_partial_take_profits(symbol, entry, side, leverage)
                logger.info(f"Partial TP: {ptp}")
        except Exception as e:
            logger.warning(f"Partial TP failed: {e}")

        # Log successful trade
        self.last_trade_time[symbol] = datetime.now(timezone.utc)
        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "signal": signal,
            "leverage": leverage, "margin": margin, "confidence": confidence,
        })

        # Alert user
        await self._alert(
            "AUTO-TRADE EXECUTED\n\n"
            + "Symbol: " + symbol + "\n"
            + "Direction: " + ("LONG" if signal == "LONG" else "SHORT") + "\n"
            + "Leverage: " + str(leverage) + "x | Margin: $" + str(margin) + "\n"
            + "Confidence: " + confidence + "\n"
            + "TP: " + str(round(tp_pct*100)) + "% | SL: 5%\n\n"
            + execution_result + "\n\n"
            + "Use /fclose " + symbol + " to close manually."
        )

    async def _check_daily_limit(self) -> bool:
        if not self.day_start_balance or self.day_start_balance <= 0:
            return True
        bal      = self.futures.get_futures_balance()
        total    = float(bal.get("_total_equity", 0))
        if total == 0:
            usdt = bal.get("USDT", {})
            total = float(usdt.get("total", self.day_start_balance)) if isinstance(usdt, dict) else self.day_start_balance
        loss_pct = (self.day_start_balance - total) / self.day_start_balance
        if loss_pct >= DAILY_LOSS_LIMIT_PCT:
            self.paused_reason = f"Daily loss limit hit ({loss_pct*100:.1f}%)"
            self.enabled       = False
            await self._alert(
                f"🚨 *AUTO-TRADER PAUSED*\n\n"
                f"Daily loss limit reached: -{loss_pct*100:.1f}%\n"
                f"Start: ${self.day_start_balance:.2f} | Now: ${total:.2f}\n\n"
                f"Bot stopped for today. Use /autotrade on tomorrow."
            )
            return False
        return True

    def _cooldown_ok(self, symbol: str) -> bool:
        last = self.last_trade_time.get(symbol)
        if not last: return True
        return (datetime.now(timezone.utc) - last).total_seconds() >= MIN_TRADE_INTERVAL

    async def daily_reset(self):
        bal = self.futures.get_futures_balance()
        current_bal = float(bal.get("_total_equity", 0))
        if current_bal == 0:
            usdt = bal.get("USDT", {})
            current_bal = float(usdt.get("total", 0)) if isinstance(usdt, dict) else 0

        # Send daily report before resetting
        try:
            report = await self.enhance.generate_daily_report(
                self.trade_log, self.day_start_balance, current_bal
            )
            await self._alert(report)
        except Exception as e:
            logger.error(f"Daily report error: {e}")

        self.day_start_balance = current_bal
        usdt = bal.get("USDT", {})
        self.day_start_balance = self.day_start_balance
        self.trade_log = []
        if self.paused_reason and "Daily loss" in self.paused_reason:
            self.paused_reason = None
            self.enabled       = True
            self._alert(f"🌅 *New day — auto-trading resumed.*\nStart balance: ${self.day_start_balance:.2f}")
        logger.info(f"Daily reset. Balance: ${self.day_start_balance:.2f}")

    def status_text(self) -> str:
        trades = "\n".join(
            f"  • {t['symbol']} {t['signal']} {t['leverage']}x ${t['margin']}"
            for t in self.trade_log[-5:]
        ) or "  None yet"
        return (
            f"🤖 *Auto-Trader Status*\n"
            f"Enabled: {'✅ YES' if self.enabled else '❌ NO'}\n"
            f"Paused: {self.paused_reason or 'No'}\n"
            f"Day-start balance: ${self.day_start_balance or 0:.2f}\n"
            f"Trades today: {len(self.trade_log)}\n"
            f"Max margin/trade: ${MAX_MARGIN_PER_TRADE}\n"
            f"Max positions: {MAX_OPEN_POSITIONS}\n"
            f"Daily loss limit: {DAILY_LOSS_LIMIT_PCT*100:.0f}%\n\n"
            f"*Last trades:*\n{trades}"
        )
