FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py scraper.py analyzer.py telegram_notifier.py main.py backtest_app.py ./
COPY backtesting ./backtesting

# Persist dedup state and backtest caches
RUN mkdir -p /data /app/.cache

EXPOSE 8501

CMD ["python", "main.py"]
