"""
bybit_futures.py - Bybit USDT perpetual futures
"""

import os
import math
import time
import json
import hmac
import hashlib
import logging
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET          = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BASE_URL         = "https://api-testnet.bybit.com" if TESTNET else "https://api.bybit.com"

STOP_LOSS_PCT  = 0.02
MAX_LEVERAGE   = 10
MIN_LEVERAGE   = 5
MAX_TRADE_USDT = 15


class BybitFutures:

    def __init__(self):
        self.connected = False
        self.testnet   = TESTNET

        if not BYBIT_API_KEY or not BYBIT_API_SECRET:
            logger.warning("Bybit API keys not set in Railway variables.")
            return

        try:
            result = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            if result and result.get("retCode") == 0:
                self.connected = True
                logger.info(f"Bybit connected ({'TESTNET' if TESTNET else 'LIVE'})")
            else:
                logger.error(f"Bybit auth failed: {result.get('retMsg', 'Unknown')}")
        except Exception as e:
            logger.error(f"Bybit connection error: {e}")

    def _sign_request(self, timestamp, params_str):
        msg = f"{timestamp}{BYBIT_API_KEY}5000{params_str}"
        return hmac.new(
            BYBIT_API_SECRET.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _get(self, path, params={}):
        ts        = str(int(time.time() * 1000))
        query_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = self._sign_request(ts, query_str)
        headers   = {
            "X-BAPI-API-KEY":     BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP":   ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN":        signature,
        }
        try:
            r = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=10)
            logger.info(f"Bybit {path} status={r.status_code} body={r.text[:300]}")
            return r.json()
        except Exception as e:
            logger.error(f"Bybit GET {path}: {e}")
            return {}

    def _post(self, path, body={}):
        ts        = str(int(time.time() * 1000))
        body_str  = json.dumps(body)
        signature = self._sign_request(ts, body_str)
        headers   = {
            "X-BAPI-API-KEY":     BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP":   ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN":        signature,
            "Content-Type":       "application/json",
        }
        try:
            r = requests.post(f"{BASE_URL}{path}", headers=headers, data=body_str, timeout=10)
            return r.json()
        except Exception as e:
            logger.error(f"Bybit POST {path}: {e}")
            return {}

    def get_futures_balance(self):
        if not self.connected:
            return {"_status": "Not connected"}
        try:
            data  = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            if data.get("retCode") != 0:
                return {"error": data.get("retMsg", "Unknown")}
            account = data["result"]["list"][0]
            coins   = account.get("coin", [])
            result  = {
                c["coin"]: {
                    "available":     round(float(c.get("availableToWithdraw") or c.get("walletBalance") or 0), 2),
                    "total":         round(float(c.get("walletBalance") or 0), 2),
                    "unrealizedPnL": round(float(c.get("unrealisedPnl") or 0), 4),
                }
                for c in coins if float(c.get("walletBalance") or 0) > 0
            }
            # Show total equity if no individual coins listed
            if not result:
                total_equity = float(account.get("totalEquity") or 0)
                if total_equity > 0:
                    result["USDT"] = {
                        "available": round(total_equity, 2),
                        "total":     round(total_equity, 2),
                        "unrealizedPnL": 0.0,
                    }
            result["_mode"]         = "TESTNET" if self.testnet else "LIVE"
            result["_total_equity"] = round(float(account.get("totalEquity") or 0), 2)
            return result
        except Exception as e:
            return {"error": str(e)}

    def get_open_positions(self):
        if not self.connected:
            return []
        try:
            data = self._get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
            if data.get("retCode") != 0:
                return []
            return [
                {
                    "symbol":         p["symbol"],
                    "side":           p["side"],
                    "size":           float(p["size"]),
                    "entry_price":    float(p["avgPrice"]),
                    "mark_price":     float(p["markPrice"]),
                    "unrealized_pnl": round(float(p["unrealisedPnl"]), 4),
                    "leverage":       int(float(p["leverage"])),
                    "liquidation":    float(p.get("liqPrice") or 0),
                }
                for p in data["result"]["list"]
                if float(p["size"]) > 0
            ]
        except Exception as e:
            logger.error(f"get_open_positions: {e}")
            return []

    def open_position(self, symbol, side, usdt_margin, leverage, take_profit_pct=0.08):
        if not self.connected:
            return "Bybit not connected. Check API keys in Railway vars."

        leverage    = max(MIN_LEVERAGE, min(leverage, MAX_LEVERAGE))
        usdt_margin = min(usdt_margin, MAX_TRADE_USDT)
        mode_tag    = "TESTNET" if self.testnet else "LIVE"

        try:
            self._post("/v5/position/set-leverage", {
                "category": "linear", "symbol": symbol,
                "buyLeverage": str(leverage), "sellLeverage": str(leverage),
            })

            ticker = self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
            price  = float(ticker["result"]["list"][0]["lastPrice"])
            qty    = self._round_qty(symbol, (usdt_margin * leverage) / price)

            # Enforce minimum notional ($10) to avoid "contracts exceeds minimum" error
            notional = qty * price
            if notional < 50:
                needed_qty = self._round_qty(symbol, 51 / price)
                logger.info(f"Qty {qty} too small (${notional:.2f} notional), scaling to {needed_qty}")
                qty = needed_qty


            if side == "Buy":
                sl_price = round(price * (1 - STOP_LOSS_PCT), 2)
                tp_price = round(price * (1 + take_profit_pct), 2)
            else:
                sl_price = round(price * (1 + STOP_LOSS_PCT), 2)
                tp_price = round(price * (1 - take_profit_pct), 2)

            order = self._post("/v5/order/create", {
                "category": "linear", "symbol": symbol,
                "side": side, "orderType": "Market",
                "qty": str(qty),
                "stopLoss": str(sl_price),
                "takeProfit": str(tp_price),
                "slTriggerBy": "MarkPrice",
                "tpTriggerBy": "MarkPrice",
                "timeInForce": "IOC",
            })

            if order.get("retCode") != 0:
                return f"Order failed: {order.get('retMsg', 'Unknown')}"

            return (
                f"{mode_tag} Position opened\n"
                f"{'LONG' if side == 'Buy' else 'SHORT'} {symbol} {leverage}x\n"
                f"Margin: ${usdt_margin} | Entry: ~${price:,.4f}\n"
                f"SL: ${sl_price:,.4f} | TP: ${tp_price:,.4f}\n"
                f"Order: {order['result']['orderId']}"
            )
        except Exception as e:
            return f"Order failed: {e}"

    def close_position(self, symbol):
        if not self.connected:
            return "Not connected"
        try:
            positions = self.get_open_positions()
            pos = next((p for p in positions if p["symbol"] == symbol), None)
            if not pos:
                return f"No open position for {symbol}"
            side  = "Sell" if pos["side"] == "Buy" else "Buy"
            order = self._post("/v5/order/create", {
                "category": "linear", "symbol": symbol,
                "side": side, "orderType": "Market",
                "qty": str(pos["size"]),
                "timeInForce": "IOC",
                "reduceOnly": True,
            })
            if order.get("retCode") != 0:
                return f"Close failed: {order.get('retMsg')}"
            return f"Closed {symbol}\nPnL: ${pos['unrealized_pnl']:+.4f}"
        except Exception as e:
            return f"Close failed: {e}"

    def format_positions(self):
        if not self.connected:
            return "Bybit not connected."
        positions = self.get_open_positions()
        if not positions:
            return "No open futures positions."
        lines = [f"{'TESTNET' if self.testnet else 'LIVE'} Open Positions:\n"]
        for p in positions:
            lines.append(
                f"{p['symbol']} {p['side']} {p['leverage']}x\n"
                f"Entry: ${p['entry_price']:,.4f} | Mark: ${p['mark_price']:,.4f}\n"
                f"PnL: ${p['unrealized_pnl']:+.4f} | Liq: ${p['liquidation']:,.4f}"
            )
        return "\n\n".join(lines)


    def set_trailing_stop(self, symbol: str, trail_pct: float = 0.015) -> str:
        """Set trailing stop on open position (default 1.5% trail)."""
        if not self.connected:
            return "Not connected"
        try:
            positions = self.get_open_positions()
            pos = next((p for p in positions if p["symbol"] == symbol), None)
            if not pos:
                return f"No open position for {symbol}"
            trailing_stop = round(pos["mark_price"] * trail_pct, 4)
            result = self._post("/v5/position/trading-stop", {
                "category":     "linear",
                "symbol":       symbol,
                "trailingStop": str(trailing_stop),
                "positionIdx":  0,
            })
            if result.get("retCode") == 0:
                return f"Trailing stop set: ${trailing_stop} ({trail_pct*100:.1f}% trail) on {symbol}"
            return f"Trailing stop failed: {result.get('retMsg','Unknown')}"
        except Exception as e:
            return f"Trailing stop error: {e}"

    def update_all_trailing_stops(self) -> list:
        """Update trailing stops on all open positions."""
        results = []
        for pos in self.get_open_positions():
            r = self.set_trailing_stop(pos["symbol"])
            logger.info(f"Trailing stop: {r}")
            results.append(r)
        return results

    def _round_qty(self, symbol, qty):
        try:
            info = self._get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
            step = float(info["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
            qty  = math.floor(qty / step) * step
            prec = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
            return round(qty, prec)
        except Exception:
            return round(qty, 3)
