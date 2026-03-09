"""Cluster detection: find companies with 3+ executives trading 200k+."""

import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

PURCHASE_SALE_TYPES = {"P", "S"}  # Purchase, Sale


def analyze(
    transactions: list[dict[str, Any]],
    min_executives: int = 3,
    min_transaction_value: float = 200000,
) -> list[dict[str, Any]]:
    """
    Find companies where at least min_executives each traded >= min_transaction_value.

    Returns list of alerts: {ticker, company_name, executives: [{name, title, total_value}], total_value}
    """
    # Group by (ticker, company_name)
    by_company: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in transactions:
        if t.get("transaction_type") not in PURCHASE_SALE_TYPES:
            continue
        value = t.get("value", 0)
        if value < min_transaction_value:
            continue
        key = (t.get("ticker", ""), t.get("company_name", ""))
        if not key[0]:
            continue
        by_company[key].append(t)

    alerts = []
    for (ticker, company_name), txs in by_company.items():
        # Sum per executive (owner_name)
        by_executive: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"name": "", "title": "", "total_value": 0.0}
        )
        for t in txs:
            name = t.get("owner_name", "").strip() or "Unknown"
            by_executive[name]["name"] = name
            by_executive[name]["title"] = t.get("title", "")
            by_executive[name]["total_value"] += t.get("value", 0)

        executives = list(by_executive.values())
        if len(executives) < min_executives:
            continue

        total_value = sum(e["total_value"] for e in executives)
        alerts.append({
            "ticker": ticker,
            "company_name": company_name,
            "executives": executives,
            "total_value": total_value,
        })

    logger.info("Found %d cluster alerts", len(alerts))
    return alerts
