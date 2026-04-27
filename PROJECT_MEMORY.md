# WealthCreation Project Reference

## Project Overview

**Telegram to Fyers Copy Trading Bot** - A web-based trading bot that monitors Telegram channels for trade signals and automatically executes them on the Fyers brokerage platform.

## Core Components

| File | Purpose |
|------|---------|
| `app.py` | Main Flask web app with bot logic, UI routes, and Fyers/Telegram integration |
| `main.py` | CLI alternative version with TradingBot class |
| `trade_manager.py` | Trade tracking, P&L calculations, SQLite database management |
| `generate_token.py` | Standalone token generator |
| `templates/` | HTML templates for web UI (home, reports, orders/positions, login) |
| `trades.db` | SQLite database storing trade history |

## Key Features

- **Telegram Signal Parsing**: Parses messages like `NIFTY 22000 CE`, `ABOVE 225`, `SL 210`, `TARGET 240`
- **Fyers API Integration**: v3 API for order placement, positions, orders, funds
- **WebSocket Price Monitoring**: Real-time LTP monitoring for entry/SL/TP triggers
- **Web Dashboard**: Login-protected UI with real-time logs, P&L reports, position management
- **Token Management**: OAuth-based Fyers auth with 24-hour token expiry
- **Trade Tracking**: SQLite with trades and trade_logs tables

## Signal Format Expected

```
NIFTY 22000 CE
ABOVE 225
SL 210
TARGET 240
05 MARCH
```

Parsed to Fyers symbol Dynamically.

## Lot Sizes (2026)

- NIFTY: 65
- BANKNIFTY: 30

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Dashboard home |
| `/login` | GET/POST | User authentication |
| `/reports` | GET | P&L reports page |
| `/orders-positions` | GET | Fyers orders/positions page |
| `/generate-token` | GET | Token generation page |
| `/api/start-bot` | GET | Start the trading bot |
| `/api/stop-bot` | GET | Stop the bot |
| `/api/save-token` | POST | Save Fyers access token |
| `/api/close-position` | POST | Close an active position |
| `/api/exit-position` | POST | Square off a position |
| `/api/cancel-order` | POST | Cancel an open order |
| `/api/trades` | GET | Get all trades |
| `/api/open-trades` | GET | Get open trades with real-time P&L |
| `/api/pending-signals` | GET | Get pending signals waiting for entry |
| `/api/subscribed-symbols` | GET | Get monitored symbols with LTP |
| `/api/orders` | GET | Get Fyers orders |
| `/api/open-orders` | GET | Get open/pending orders |
| `/api/positions` | GET | Get Fyers positions |
| `/api/pnl/daily` | GET | Daily P&L |
| `/api/pnl/weekly` | GET | Weekly P&L |
| `/api/pnl/monthly` | GET | Monthly P&L |
| `/api/pnl/total` | GET | All-time P&L stats |
| `/api/account-balance` | GET | Get account funds |

## Environment Variables (.env)

```bash
# Telegram
TG_API_ID=           # From my.telegram.org
TG_API_HASH=         # From my.telegram.org
TARGET_CHAT_ID=      # Telegram channel/group ID (negative for channels)

# Fyers
FYERS_CLIENT_ID=     # Full ID like "5N4IWBSUA0-100"
FYERS_SECRET_KEY=    # App secret key
FYERS_REDIRECT_URI=  # e.g., "https://trader.silver-screen.stream"
```

## Database Schema

### trades table
- id, symbol, trigger_price, entry_price, exit_price, quantity, side
- status: PENDING, OPEN, CLOSED, CANCELLED
- pnl, sl_price, tp_price
- entry_order_id, sl_order_id, tp_order_id, exit_order_id
- signal_time, entry_time, exit_time, exit_reason

### trade_logs table
- id, trade_id, event_type, message, price, timestamp

## Dependencies

```
telethon>=1.34.0          # Telegram client
fyers-apiv3>=3.0.0        # Fyers API v3
requests>=2.31.0
python-dotenv>=1.0.0
flask>=3.0.0
werkzeug>=3.0.0
gunicorn>=21.0.0
flask-login>=0.6.3
```

## Architecture Flow

1. **Bot Start**: Initialize Telegram client, Fyers instance, WebSocket
2. **Signal Received**: Parse Telegram message → Create PENDING trade
3. **Entry Monitoring**: WebSocket monitors LTP until >= trigger_price
4. **Order Execution**: Place market BUY order on trigger hit
5. **SL/TP Monitoring**: Monitor until SL (<=) or TP (>=) hit
6. **Exit Execution**: Place market SELL order, calculate P&L
7. **Trade Closed**: Update DB, log event, ready for next signal

## Important Notes

- Tokens expire in 24 hours - requires daily regeneration
- Bot only works while Flask server is running
- Single trade mode: only one open trade at a time
- All orders are MARKET orders (bot-level price monitoring)
- INTRADAY product type for all trades