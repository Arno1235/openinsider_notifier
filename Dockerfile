FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py scraper.py analyzer.py telegram_notifier.py main.py ./

# Persist dedup state
RUN mkdir -p /data

CMD ["python", "main.py"]
