"""Fetch and parse insider transactions from openinsider.com screener."""

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

BASE_URL = "http://openinsider.com/screener"
SCREENER_PARAMS = (
    "s=&o=&pl=&ph=&ll=&lh=&fd=-1&fdr={start_date}+-+{end_date}&td=0&tdr="
    "&fdlyl=&fdlyh=&daysago=&xp=1&xs=1&vl={vl}&vh=&ocl=&och=&sic1=-1"
    "&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nol=&noh=&v2l=&v2h="
    "&oc2l=&oc2h=&sortcol=0&cnt=5000&page=1"
)


def _parse_value(value_str: str) -> float:
    """Parse value string like '$1,234' or '$1.23M' into float."""
    if not value_str or value_str.lower() in ("n/a", "new", ""):
        return 0.0
    clean = value_str.replace("$", "").replace(",", "").strip()
    if not clean:
        return 0.0
    match = re.match(r"^([\d.]+)\s*[MmKk]?$", clean)
    if match:
        num = float(match.group(1))
        if "M" in value_str.upper() or "m" in value_str:
            num *= 1_000_000
        elif "K" in value_str.upper() or "k" in value_str:
            num *= 1_000
        return num
    try:
        return float(clean)
    except ValueError:
        return 0.0


def build_screener_url(
    start_date: date | datetime,
    end_date: date | datetime,
    min_transaction_value: int = 200000,
) -> str:
    """Build the openinsider screener URL for an arbitrary date range."""
    start_str = start_date.strftime("%m/%d/%Y")
    end_str = end_date.strftime("%m/%d/%Y")
    vl = min_transaction_value // 1000
    return f"{BASE_URL}?{SCREENER_PARAMS.format(start_date=start_str, end_date=end_str, vl=vl)}"


def _fetch_with_retry(
    url: str,
    timeout: int = 30,
    max_retries: int = 3,
) -> requests.Response:
    """Fetch URL with exponential backoff on 503/network errors."""
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
            if response.status_code == 503:
                wait = 2 ** attempt
                logger.warning(
                    "Got 503, retrying in %ds (attempt %d/%d)",
                    wait,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            wait = 2 ** attempt
            logger.warning(
                "Request failed: %s, retrying in %ds (attempt %d/%d)",
                e,
                wait,
                attempt + 1,
                max_retries,
            )
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                raise
    raise requests.RequestException("Max retries exceeded")


def parse_screener_html(html: str, min_transaction_value: float = 0.0) -> list[dict[str, Any]]:
    """Parse an openinsider screener HTML page into transaction dicts."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"class": "tinytable"})
    if not table:
        logger.error("No tinytable found in response")
        return []

    tbody = table.find("tbody")
    if not tbody:
        logger.error("No tbody in tinytable")
        return []

    rows = tbody.find_all("tr")
    transactions = []
    keys = [
        "filing_date",
        "trade_date",
        "ticker",
        "company_name",
        "owner_name",
        "title",
        "transaction_type",
        "last_price",
        "qty",
        "owned",
        "delta_owned",
        "value",
    ]

    for row in rows:
        cols = row.find_all("td")
        if not cols:
            continue
        # Remove first column (D indicator) if present
        if len(cols) > len(keys):
            cols = cols[1:]
        if len(cols) < len(keys):
            continue

        data = {k: cols[i].get_text(strip=True) for i, k in enumerate(keys)}

        # Normalize transaction type: "P - Purchase" -> "P"
        if data.get("transaction_type"):
            data["transaction_type"] = data["transaction_type"].split(" - ")[0].strip()

        value = _parse_value(data.get("value", ""))
        if value < min_transaction_value:
            continue

        # Parse last_price and qty for richer downstream use
        last_price = _parse_value(data.get("last_price", ""))
        qty_raw = data.get("qty", "").replace(",", "").replace("+", "").strip()
        try:
            qty = int(float(qty_raw)) if qty_raw else 0
        except ValueError:
            qty = 0

        transactions.append({
            "filing_date": data.get("filing_date", ""),
            "trade_date": data.get("trade_date", ""),
            "ticker": data.get("ticker", ""),
            "company_name": data.get("company_name", ""),
            "owner_name": data.get("owner_name", ""),
            "title": data.get("title", ""),
            "transaction_type": data.get("transaction_type", ""),
            "last_price": last_price,
            "qty": qty,
            "value": value,
        })

    return transactions


def scrape_range(
    start_date: date | datetime,
    end_date: date | datetime,
    min_transaction_value: int = 200000,
) -> list[dict[str, Any]]:
    """Scrape a specific date range from openinsider."""
    url = build_screener_url(start_date, end_date, min_transaction_value)
    logger.info("Fetching screener: %s", url)
    response = _fetch_with_retry(url)
    transactions = parse_screener_html(response.text, min_transaction_value)
    logger.info(
        "Scraped %d transactions for %s -> %s",
        len(transactions),
        start_date,
        end_date,
    )
    return transactions


def scrape(
    lookback_days: int = 7,
    min_transaction_value: int = 200000,
) -> list[dict[str, Any]]:
    """Scrape recent insider transactions (last `lookback_days` days)."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback_days)
    return scrape_range(start_date, end_date, min_transaction_value)
