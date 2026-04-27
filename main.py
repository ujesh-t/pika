#!/usr/bin/env python3
"""
CLI alternative for the Trading Bot.
Runs the bot without the web interface.
"""

import os
import re
import asyncio
import threading
import time
import logging
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events

from trade_manager import (
    init_db, create_trade, update_trade_entry, update_trade_exit,
    cancel_trade, get_active_trade, log_event,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        init_db()
        self.fyers = None
        self.telegram_client = None
        self.running = False
        self.loop = None

    def get_fyers(self):
        from trade_manager import load_token
        token = load_token()
        if not token:
            logger.error("No Fyers token available. Run generate_token.py first.")
            return None
        try:
            from fyers_apiv3 import fyersModel
            client_id = os.getenv("FYERS_CLIENT_ID")
            self.fyers = fyersModel.FyersModel(client_id=client_id, token=token, log_path="")
            return self.fyers
        except Exception as e:
            logger.error(f"Fyers init error: {e}")
            return None

    def get_ltp(self, symbol: str) -> float:
        try:
            resp = self.fyers.quotes({"symbols": symbol})
            if resp.get("s") == "ok":
                for item in resp.get("d", []):
                    return float(item["v"]["lp"])
        except Exception as e:
            logger.error(f"LTP error for {symbol}: {e}")
        return 0.0

    def parse_signal(self, message: str) -> dict:
        lines = [l.strip() for l in message.strip().split("\n") if l.strip()]
        result = {}
        symbol_line = lines[0] if lines else ""
        strike_match = re.search(r"(\d{5,})", symbol_line)
        ce_pe_match = re.search(r"\b(CE|PE)\b", symbol_line, re.I)
        index_match = re.search(r"(NIFTY|BANKNIFTY|FINNIFTY|SENSEX)", symbol_line, re.I)

        index_name = index_match.group(1).upper() if index_match else "NIFTY"
        strike = strike_match.group(1) if strike_match else ""
        option_type = ce_pe_match.group(1).upper() if ce_pe_match else "CE"

        for line in lines[1:]:
            lower = line.upper()
            if "ABOVE" in lower or "BUY" in lower or "ENTRY" in lower:
                nums = re.findall(r"[\d.]+", line)
                if nums:
                    result["trigger_price"] = float(nums[0])
            elif "SL" in lower or "STOPLOSS" in lower:
                nums = re.findall(r"[\d.]+", line)
                if nums:
                    result["sl_price"] = float(nums[0])
            elif "TARGET" in lower or "TP" in lower:
                nums = re.findall(r"[\d.]+", line)
                if nums:
                    result["tp_price"] = float(nums[-1])

        result["symbol"] = f"{index_name}{strike}{option_type}"
        result["side"] = "BUY"
        result.setdefault("trigger_price", 0)
        result.setdefault("sl_price", 0)
        result.setdefault("tp_price", 0)
        return result

    def lookup_lot_size(self, symbol: str) -> int:
        return 30 if "BANKNIFTY" in symbol.upper() else 65

    def process_signal(self, text: str):
        signal = self.parse_signal(text)
        if not signal.get("trigger_price") or signal["trigger_price"] == 0:
            logger.warning(f"Incomplete signal, skipping")
            return

        if get_active_trade():
            logger.info("Active trade in progress, ignoring signal")
            return

        lot_size = self.lookup_lot_size(signal["symbol"])
        trade_id = create_trade(
            symbol=signal["symbol"],
            trigger_price=signal["trigger_price"],
            sl_price=signal["sl_price"],
            tp_price=signal["tp_price"],
            quantity=lot_size,
            side=signal["side"],
            raw_signal=text,
        )
        logger.info(f"Trade #{trade_id} created: {signal['symbol']} @ {signal['trigger_price']}")
        threading.Thread(target=self.monitor_entry, args=(trade_id,), daemon=True).start()

    def monitor_entry(self, trade_id: int):
        from trade_manager import get_trade
        trade = get_trade(trade_id)
        if not trade:
            return
        trigger = trade["trigger_price"]
        symbol = trade["symbol"]
        logger.info(f"Monitoring entry: {symbol} >= {trigger}")

        while self.running:
            price = self.get_ltp(symbol)
            if price and price >= trigger:
                self.place_entry(trade_id)
                return
            time.sleep(2)

        cancel_trade(trade_id)
        logger.info(f"Trade #{trade_id} cancelled")

    def place_entry(self, trade_id: int):
        from trade_manager import get_trade
        trade = get_trade(trade_id)
        if not trade:
            return

        try:
            order = self.fyers.place_order({
                "symbol": trade["symbol"],
                "qty": trade["quantity"],
                "type": 2,
                "side": 1,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "offlineOrder": False,
            })
            if order.get("s") == "ok":
                order_id = order.get("id", "")
                price = self.get_ltp(trade["symbol"])
                update_trade_entry(trade_id, price, trade["quantity"], order_id)
                logger.info(f"Entry filled #{trade_id} @ {price}")
                threading.Thread(target=self.monitor_exit, args=(trade_id,), daemon=True).start()
            else:
                logger.error(f"Entry order failed: {order}")
        except Exception as e:
            logger.error(f"Entry error: {e}")

    def monitor_exit(self, trade_id: int):
        from trade_manager import get_trade
        trade = get_trade(trade_id)
        if not trade:
            return
        sl, tp, symbol = trade["sl_price"], trade["tp_price"], trade["symbol"]
        logger.info(f"Monitoring exit: {symbol} SL={sl} TP={tp}")

        while self.running:
            price = self.get_ltp(symbol)
            if price:
                if sl and price <= sl:
                    self.place_exit(trade_id, "SL_HIT")
                    return
                if tp and price >= tp:
                    self.place_exit(trade_id, "TP_HIT")
                    return
            time.sleep(2)

    def place_exit(self, trade_id: int, reason: str):
        from trade_manager import get_trade
        trade = get_trade(trade_id)
        if not trade:
            return

        try:
            order = self.fyers.place_order({
                "symbol": trade["symbol"],
                "qty": trade["quantity"],
                "type": 2,
                "side": -1,
                "productType": "INTRADAY",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "offlineOrder": False,
            })
            if order.get("s") == "ok":
                order_id = order.get("id", "")
                price = self.get_ltp(trade["symbol"])
                update_trade_exit(trade_id, price, order_id, reason)
                logger.info(f"Exit filled #{trade_id} @ {price} ({reason})")
            else:
                logger.error(f"Exit order failed: {order}")
        except Exception as e:
            logger.error(f"Exit error: {e}")

    async def run_telegram(self):
        api_id = int(os.getenv("TG_API_ID", 0))
        api_hash = os.getenv("TG_API_HASH", "")
        target_chat = int(os.getenv("TARGET_CHAT_ID", 0))

        self.telegram_client = TelegramClient("coders_bot_session", api_id, api_hash)
        await self.telegram_client.start()

        me = await self.telegram_client.get_me()
        logger.info(f"Logged in as: {me.username or me.first_name}")
        log_event(None, "BOT_STARTED", f"Bot started as {me.username or me.first_name}")

        async for message in self.telegram_client.iter_messages(target_chat, limit=10):
            if not self.running:
                break
            if message.text:
                self.process_signal(message.text)

        if self.running:
            @self.telegram_client.on(events.NewMessage(chats=target_chat))
            async def handler(event):
                if self.running and event.text:
                    self.process_signal(event.text)

            logger.info("Listening for signals...")
            await self.telegram_client.run_until_disconnected()

        await self.telegram_client.disconnect()
        self.running = False
        log_event(None, "BOT_STOPPED", "Bot stopped")
        logger.info("Bot stopped")

    def start(self):
        if not self.get_fyers():
            logger.error("Cannot start: No valid Fyers token")
            return

        self.running = True
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.loop.run_until_complete(self.run_telegram())
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        except Exception as e:
            logger.error(f"Bot error: {e}")
        finally:
            self.running = False

    def stop(self):
        self.running = False
        if self.telegram_client and self.loop:
            asyncio.run_coroutine_threadsafe(self.telegram_client.disconnect(), self.loop)


if __name__ == "__main__":
    bot = TradingBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop()
        logger.info("Bot terminated by user")
