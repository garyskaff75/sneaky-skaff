"""
Trading Bot - main.py (Bybit edition)
"""

import os
import time
import requests as _req

try:
    print(f"SERVER IP: {_req.get('https://api.ipify.org', timeout=5).text}", flush=True)
except Exception:
    pass

import asyncio
import logging
import threading
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from market_data import MarketData
from claude_brain import ClaudeBrain
from bybit_futures import BybitFutures
from auto_trader import AutoTrader
from enhancements import Enhancements

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "0"))

# Clear any existing Telegram session
for attempt in range(5):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true"
        r = _req.get(url, timeout=10)
        logger.info(f"Session cleared (attempt {attempt+1}): {r.json().get('description','ok')}")
        time.sleep(5)
        break
    except Exception as e:
        logger.warning(f"Clear attempt {attempt+1} failed: {e}")
        time.sleep(3)

market  = MarketData()
brain   = ClaudeBrain(market_data=market)
futures = BybitFutures()
auto    = AutoTrader(market, brain, futures, None, ALLOWED_USER_ID)
enhance = Enhancements(futures)
bot     = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

def authorized(message) -> bool:
    return message.from_user.id == ALLOWED_USER_ID

def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

def main_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("Market Snapshot",    callback_data="snapshot"))
    kb.row(InlineKeyboardButton("Spot Signal",        callback_data="signal"))
    kb.row(InlineKeyboardButton("Futures Signal",     callback_data="futures_signal"))
    kb.row(InlineKeyboardButton("Open Positions",     callback_data="positions"))
    kb.row(InlineKeyboardButton("Balances",           callback_data="balance"))
    kb.row(InlineKeyboardButton("Auto-Trader Status", callback_data="auto_status"))
    kb.row(InlineKeyboardButton("Crypto News",        callback_data="news"))
    return kb

@bot.message_handler(commands=["start"])
def start(message):
    if not authorized(message): return
    bot.send_message(message.chat.id, "Trading Bot Online\nChoose an action:", reply_markup=main_keyboard())

@bot.message_handler(commands=["signal"])
def signal_cmd(message):
    if not authorized(message): return
    parts  = message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    bot.send_message(message.chat.id, f"Analyzing {symbol}...")
    try:
        data = run_async(market.get_full_snapshot(symbol))
        rec  = run_async(brain.analyze(data, symbol))
        bot.send_message(message.chat.id, rec)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")

@bot.message_handler(commands=["fsignal"])
def fsignal_cmd(message):
    if not authorized(message): return
    parts  = message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    bot.send_message(message.chat.id, f"Analyzing {symbol} for futures...")
    try:
        data   = run_async(market.get_full_snapshot(symbol))
        bal    = futures.get_futures_balance()
        avail  = bal.get("USDT", {}).get("available", 100) if isinstance(bal.get("USDT"), dict) else 100
        result = run_async(brain.analyze_futures(data, symbol, float(avail)))
        bot.send_message(message.chat.id, result["text"])
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")

@bot.message_handler(commands=["ftrade"])
def ftrade_cmd(message):
    if not authorized(message): return
    parts  = message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    bot.send_message(message.chat.id, f"Getting futures signal for {symbol}...")
    try:
        data   = run_async(market.get_full_snapshot(symbol))
        bal    = futures.get_futures_balance()
        avail  = bal.get("USDT", {}).get("available", 100) if isinstance(bal.get("USDT"), dict) else 100
        result = run_async(brain.analyze_futures(data, symbol, float(avail)))
        if result["signal"] == "STAY OUT":
            bot.send_message(message.chat.id, f"STAY OUT of {symbol}\n\n{result['text']}")
            return
        side = "Buy" if result["signal"] == "LONG" else "Sell"
        kb   = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("Execute", callback_data=f"fexec_{symbol}_{side}_{result['margin']}_{result['leverage']}_{result['take_profit_pct']}"),
            InlineKeyboardButton("Cancel",  callback_data="cancel")
        )
        bot.send_message(message.chat.id,
            f"{result['text']}\n\nReady: {'LONG' if side=='Buy' else 'SHORT'} {symbol} {result['leverage']}x ${result['margin']} margin",
            reply_markup=kb
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")

@bot.message_handler(commands=["trailstop"])
def trailstop_cmd(message):
    if not authorized(message): return
    parts = message.text.split()
    if len(parts) < 2:
        results = futures.update_all_trailing_stops()
        msg = "Trailing stops updated:" if results else "No open positions"
        if results: msg += chr(10) + chr(10).join(results)
        bot.send_message(message.chat.id, msg)
    else:
        symbol = parts[1].upper()
        pct = float(parts[2]) / 100 if len(parts) > 2 else 0.015
        bot.send_message(message.chat.id, futures.set_trailing_stop(symbol, pct))

@bot.message_handler(commands=["fclose"])
def fclose_cmd(message):
    if not authorized(message): return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Usage: /fclose BTCUSDT")
        return
    bot.send_message(message.chat.id, futures.close_position(parts[1].upper()))

@bot.message_handler(commands=["fpositions"])
def fpositions_cmd(message):
    if not authorized(message): return
    bot.send_message(message.chat.id, futures.format_positions())

@bot.message_handler(commands=["balance"])
def balance_cmd(message):
    if not authorized(message): return
    bal      = futures.get_futures_balance()
    lines    = ""
    for asset, info in bal.items():
        if asset.startswith("_"): continue
        if isinstance(info, dict):
            lines += f"  {asset}: ${info.get('available',0):.2f} avail | PnL: ${info.get('unrealizedPnL',0):+.4f}\n"
    mode = bal.get("_mode", "")
    bot.send_message(message.chat.id, f"Bybit Futures ({mode}):\n{lines or 'No balance found'}")

@bot.message_handler(commands=["autotrade"])
def autotrade_cmd(message):
    if not authorized(message): return
    parts = message.text.split()
    arg   = parts[1].lower() if len(parts) > 1 else "status"
    if arg == "on":
        auto.bot = bot
        auto.enable()
        bot.send_message(message.chat.id,
            "Auto-Trading ENABLED\n"
            "Scans BTC/ETH/SOL every 15 min and executes automatically.\n"
            "Limits: max $20/trade | 2 positions | pause if down 20%\n"
            "Use /autotrade off to stop."
        )
    elif arg == "off":
        auto.disable()
        bot.send_message(message.chat.id, "Auto-trading DISABLED.")
    else:
        bot.send_message(message.chat.id, auto.status_text())

@bot.message_handler(commands=["autostatus"])
def autostatus_cmd(message):
    if not authorized(message): return
    bot.send_message(message.chat.id, auto.status_text())

@bot.message_handler(commands=["pnl"])
def pnl_cmd(message):
    if not authorized(message): return
    try:
        bal          = futures.get_futures_balance()
        total_equity = float(bal.get("_total_equity", 0))
        if total_equity == 0:
            usdt = bal.get("USDT", {})
            total_equity = float(usdt.get("total", 0)) if isinstance(usdt, dict) else 0
        positions    = futures.get_open_positions()
        open_pnl     = sum(p["unrealized_pnl"] for p in positions)
        trades_today = len(auto.trade_log)
        start_bal    = auto.day_start_balance or 0
        day_pnl      = total_equity - start_bal if start_bal > 0 else 0
        day_pnl_pct  = (day_pnl / start_bal * 100) if start_bal > 0 else 0
        pos_lines = ""
        for p in positions:
            pos_lines += "  " + p["symbol"] + " " + p["side"] + " " + str(p["leverage"]) + "x: $" + "{:+.4f}".format(p["unrealized_pnl"]) + "\n"
        sep = "=" * 25
        msg = (
            "PnL Summary\n" + sep + "\n" +
            "Equity:        $" + "{:.2f}".format(total_equity) + " USDT\n" +
            "Day start:     $" + "{:.2f}".format(start_bal) + " USDT\n" +
            "Today PnL:     $" + "{:+.4f}".format(day_pnl) + " (" + "{:+.2f}".format(day_pnl_pct) + "%)\n" +
            "Open PnL:      $" + "{:+.4f}".format(open_pnl) + " USDT\n" +
            "Trades today:  " + str(trades_today) + "\n\n" +
            "Open Positions:\n" + (pos_lines if pos_lines else "  None\n") +
            sep + "\n" +
            "Loss limit:    $" + "{:.2f}".format(start_bal * 0.2)
        )
        bot.send_message(message.chat.id, msg)
    except Exception as e:
        bot.send_message(message.chat.id, "Error getting PnL: " + str(e))


@bot.message_handler(commands=["funding"])
def funding_cmd(message):
    if not authorized(message): return
    async def _get():
        opps = await enhance.check_funding_opportunities()
        if not opps:
            return "No funding opportunities above 0.1% threshold right now."
        lines = ["Funding Rate Opportunities:", ""]
        for o in opps[:5]:
            lines.append(
                o["symbol"] + ": " + str(o["funding_rate"]) + "% per 8h" +
                " -> " + o["direction"] + " (" + str(o["annualized"]) + "% annualized)"
            )
        return chr(10).join(lines)
    bot.send_message(message.chat.id, run_async(_get()))

@bot.message_handler(commands=["market"])
def market_cmd(message):
    if not authorized(message): return
    async def _get():
        btc = await enhance.check_btc_correlation()
        cal = await enhance.check_economic_calendar()
        lines = [
            "Market Overview:",
            "",
            "BTC Filter: " + btc["filter"],
            btc["reason"],
            "",
            "Calendar: " + ("AVOID TRADING" if cal["avoid"] else "Clear"),
            cal["reason"],
        ]
        return chr(10).join(lines)
    bot.send_message(message.chat.id, run_async(_get()))

@bot.message_handler(commands=["report"])
def report_cmd(message):
    if not authorized(message): return
    async def _get():
        bal = futures.get_futures_balance()
        current = float(bal.get("_total_equity", 0))
        return await enhance.generate_daily_report(
            auto.trade_log, auto.day_start_balance or current, current
        )
    bot.send_message(message.chat.id, run_async(_get()))

@bot.message_handler(commands=["lsratio"])
def lsratio_cmd(message):
    if not authorized(message): return
    parts  = message.text.split()
    symbol = parts[1].upper() if len(parts) > 1 else "BTCUSDT"
    async def _get():
        ls = await enhance.get_long_short_ratio(symbol)
        oi = await enhance.get_open_interest_change(symbol)
        return (
            symbol + " Market Positioning:" + chr(10) +
            "Longs: " + str(ls["long_pct"]) + "% | Shorts: " + str(ls["short_pct"]) + "%" + chr(10) +
            "Signal: " + ls["signal"] + chr(10) + chr(10) +
            "Open Interest: " + oi["signal"] + " (" + str(oi["change_pct"]) + "%)"
        )
    bot.send_message(message.chat.id, run_async(_get()))

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.from_user.id != ALLOWED_USER_ID: return
    bot.answer_callback_query(call.id)
    data = call.data
    cid  = call.message.chat.id
    mid  = call.message.message_id
    try:
        if data == "snapshot":
            snap = run_async(market.get_full_snapshot("BTCUSDT"))
            bot.edit_message_text(
                f"Market Snapshot\n"
                f"BTC: ${snap.get('price',0):,.2f} ({snap.get('change_24h',0):+.2f}%)\n"
                f"RSI: {snap.get('rsi','N/A')} | MACD: {snap.get('macd_signal','N/A')}\n"
                f"Stoch RSI: {snap.get('stoch_rsi_k','N/A')} | EMA: {snap.get('ema_trend','N/A')}\n"
                f"Volatility: {snap.get('volatility','N/A')}\n"
                f"OBV: {snap.get('obv_trend','N/A')}\n"
                f"Fear & Greed: {snap.get('fear_greed','N/A')}\n"
                f"Oil: ${snap.get('oil_price','N/A')}",
                cid, mid
            )
        elif data == "signal":
            bot.edit_message_text("Getting spot signals...", cid, mid)
            results = []
            for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                d = run_async(market.get_full_snapshot(sym))
                results.append(run_async(brain.analyze(d, sym)))
            bot.edit_message_text("\n\n---\n\n".join(results), cid, mid)
        elif data == "futures_signal":
            bot.edit_message_text("Getting futures signals...", cid, mid)
            results = []
            for sym in ["BTCUSDT", "ETHUSDT"]:
                d = run_async(market.get_full_snapshot(sym))
                results.append(run_async(brain.analyze_futures(d, sym))["text"])
            bot.edit_message_text("\n\n---\n\n".join(results), cid, mid)
        elif data == "positions":
            bot.edit_message_text(futures.format_positions(), cid, mid)
        elif data == "balance":
            bal   = futures.get_futures_balance()
            lines = ""
            for asset, info in bal.items():
                if asset.startswith("_"): continue
                if isinstance(info, dict):
                    lines += f"  {asset}: ${info.get('available',0):.2f}\n"
            bot.edit_message_text(f"Bybit Futures:\n{lines or 'No balance'}", cid, mid)
        elif data == "auto_status":
            bot.edit_message_text(auto.status_text(), cid, mid)
        elif data == "news":
            news = run_async(market.get_news())
            msg  = "Crypto News:\n\n" + "\n\n".join(f"{n['title']}\n{n['url']}" for n in news[:5])
            bot.edit_message_text(msg, cid, mid)
        elif data.startswith("fexec_"):
            _, symbol, side, margin, lev, tp_pct = data.split("_")
            result = futures.open_position(symbol, side, float(margin), int(lev), float(tp_pct))
            bot.edit_message_text(result, cid, mid)
        elif data == "cancel":
            bot.edit_message_text("Cancelled.", cid, mid)
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            bot.send_message(cid, f"Error: {e}")
        except Exception:
            pass

def auto_trader_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    auto.bot = bot
    # Auto-enable trading on startup
    auto.enable()
    logger.info("Auto-trader enabled on startup")
    loop.run_until_complete(auto.run_loop())

if __name__ == "__main__":
    logger.info("Bot starting...")
    threading.Thread(target=auto_trader_thread, daemon=True).start()
    logger.info("Bot started — polling!")
    while True:
        try:
            bot.polling(none_stop=True, timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(10)
            try:
                _req.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
            except Exception:
                pass
            time.sleep(5)
