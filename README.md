# OpenInsider Telegram Alert + Backtester

Two tools that share one scraper:

1. **Telegram notifier** — polls [openinsider.com](https://openinsider.com) every few hours. When 3+ executives at a company each buy/sell shares worth at least $200k, you get a Telegram alert.
2. **Backtesting web app** — Streamlit UI that replays historical openinsider signals against configurable strategy rules and compares equity vs. the S&P 500.

---

## Telegram notifier

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

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_HOURS` | 4 | Hours between scrapes |
| `MIN_EXECUTIVES` | 3 | Minimum executives for alert |
| `MIN_TRANSACTION_VALUE` | 200000 | Min value per transaction (USD) |
| `LOOKBACK_DAYS` | 7 | Days of data to fetch per poll |
| `TELEGRAM_BOT_TOKEN` | (required) | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | (required) | Your chat ID |

### Alert Format

```
 Insider Cluster Alert

Company: Apple Inc. (AAPL)
Executives: 4

 Tim Cook (CEO): $2.45M
 Luca Maestri (CFO): $890,000
 Jeff Williams (COO): $1.20M
 Kate Adams (General Counsel): $320,000

Total: $4.86M
```

### Deduplication

Alerts are deduplicated: the same cluster (company + executives) won't trigger another notification for 24 hours.

---

## Backtesting web app

An interactive Streamlit app that:

- Pulls historical insider transactions from openinsider (chunked, cached to `.cache/insiders/`).
- Derives configurable signals (cluster buys/sells, single large buys, CEO/CFO/Chair buys).
- Simulates a portfolio with realistic fills, commissions, slippage, stops, and holding periods.
- Pulls historical OHLCV via `yfinance` (cached to `.cache/prices/`).
- Benchmarks results against SPY (or any ticker you pick) with a full set of risk metrics.

### Run locally

```bash
pip install -r requirements.txt
streamlit run backtest_app.py
```

Then open http://localhost:8501.

### Run with Docker

```bash
docker compose --profile backtest up -d backtest
```

App will be on http://localhost:8501. Caches persist in the `backtest_cache` Docker volume.

### What you can configure

| Group | Setting | Notes |
|-------|---------|-------|
| **Range** | Start / End date, initial capital, benchmark ticker, risk-free rate | Default window is the last 3 years. |
| **Signal filters** | Signal types, min executives, min transaction value, cluster window, single-large-buy threshold | Mix any combination of signal types. |
| **Entry** | Entry delay (days after signal), fill at open/close, slippage bps, commission bps | Entry delay defaults to 1 day to model "you see it after close". |
| **Sizing** | Equal-weight / fixed $ / percent-of-equity, max concurrent positions, overflow (skip or replace-oldest) | Equal-weight splits capital across `max_positions` slots. |
| **Exits** | Holding period (trading days), stop-loss %, take-profit %, trailing stop % | Any combination; first trigger wins. |
| **Advanced** | Short on cluster-sell signals | Simulated only, no borrow fees modeled. |

### Results

Six tabs:

- **Overview** — Total return, CAGR, Sharpe, Sortino, max DD, alpha/beta, trade stats, etc., side-by-side with the benchmark.
- **Equity curve** — Strategy vs benchmark (growth of $1), plus an exposure chart.
- **Drawdown** — Underwater plot for both.
- **Monthly returns** — Year-by-month heatmap for both.
- **Trades** — Interactive table of every round-trip with entry/exit prices, P&L, hold days, exit reason; CSV download; PnL histogram; exit-reason bar chart.
- **Signals** — Every derived signal and any skipped signals (with reasons).

### Caching

- Insider transactions are cached per month to `.cache/insiders/<start>_<end>_<minvalue>.parquet`. Only fully-historical chunks are cached.
- Prices are cached per ticker to `.cache/prices/<TICKER>.parquet` and extended on demand.
- Use **Clear cache** in the sidebar to wipe both.

---

## Disclaimer

For educational purposes. Ensure you comply with openinsider.com's terms of service and local regulations when using scraped data. Backtest results are not predictive of live performance; insider-trade timestamps on openinsider reflect *filing* dates, not necessarily realistic entry timing.
