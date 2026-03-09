# OpenInsider Telegram Alert

Polls [openinsider.com](https://openinsider.com) every 4 hours for insider transactions. When 3+ executives at a company each buy or sell shares worth at least $200,000, you get a Telegram alert with company details, executive names, and amounts.

## Setup

### 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token you receive

### 2. Get Your Chat ID

1. Send a message to your new bot
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id":123456789}` in the JSON response
4. Or use [@userinfobot](https://t.me/userinfobot) to get your ID

### 3. Configure

```bash
cp .env.example .env
# Edit .env and add your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

### 4. Run with Docker

```bash
docker compose up -d
```

Or run locally:

```bash
pip install -r requirements.txt
python main.py
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_HOURS` | 4 | Hours between scrapes |
| `MIN_EXECUTIVES` | 3 | Minimum executives for alert |
| `MIN_TRANSACTION_VALUE` | 200000 | Min value per transaction (USD) |
| `LOOKBACK_DAYS` | 7 | Days of data to fetch per poll |
| `TELEGRAM_BOT_TOKEN` | (required) | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (required) | Your chat ID |

## Alert Format

```
🔔 Insider Cluster Alert

Company: Apple Inc. (AAPL)
Executives: 4

• Tim Cook (CEO): $2.45M
• Luca Maestri (CFO): $890,000
• Jeff Williams (COO): $1.20M
• Kate Adams (General Counsel): $320,000

Total: $4.86M
```

## Deduplication

Alerts are deduplicated: the same cluster (company + executives) won't trigger another notification for 24 hours.

## Disclaimer

This tool is for educational purposes. Ensure you comply with openinsider.com's terms of service and local regulations when using scraped data.
