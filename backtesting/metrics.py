"""Performance metrics for equity curves and trades."""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def total_return(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def cagr(equity: pd.Series) -> float:
    if equity.empty or len(equity) < 2:
        return 0.0
    days = (equity.index[-1] - equity.index[0]).days
    if days <= 0:
        return 0.0
    years = days / 365.25
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1)


def volatility(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    return float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))


def sharpe(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    if returns.empty:
        return 0.0
    daily_rf = risk_free_rate / TRADING_DAYS
    excess = returns - daily_rf
    sd = excess.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return 0.0
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS))


def sortino(returns: pd.Series, risk_free_rate: float = 0.0) -> float:
    if returns.empty:
        return 0.0
    daily_rf = risk_free_rate / TRADING_DAYS
    excess = returns - daily_rf
    downside = excess[excess < 0]
    sd = downside.std(ddof=1)
    if sd == 0 or np.isnan(sd) or downside.empty:
        return 0.0
    return float(excess.mean() / sd * np.sqrt(TRADING_DAYS))


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def drawdown_series(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return equity
    peak = equity.cummax()
    return equity / peak - 1.0


def calmar(equity: pd.Series) -> float:
    dd = abs(max_drawdown(equity))
    if dd == 0:
        return 0.0
    return cagr(equity) / dd


def exposure(exposure_series: pd.Series) -> float:
    if exposure_series.empty:
        return 0.0
    return float(exposure_series.mean())


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "avg_trade_pct": 0.0,
            "profit_factor": 0.0,
            "avg_hold_days": 0.0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
        }
    wins = trades[trades["pnl_pct"] > 0]
    losses = trades[trades["pnl_pct"] <= 0]
    gross_win = float(wins["pnl_dollars"].sum())
    gross_loss = float(-losses["pnl_dollars"].sum())
    return {
        "num_trades": int(len(trades)),
        "win_rate": float(len(wins) / len(trades)),
        "avg_win_pct": float(wins["pnl_pct"].mean()) if not wins.empty else 0.0,
        "avg_loss_pct": float(losses["pnl_pct"].mean()) if not losses.empty else 0.0,
        "avg_trade_pct": float(trades["pnl_pct"].mean()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "avg_hold_days": float(trades["hold_days"].mean()),
        "best_trade_pct": float(trades["pnl_pct"].max()),
        "worst_trade_pct": float(trades["pnl_pct"].min()),
    }


def alpha_beta(strat_returns: pd.Series, bench_returns: pd.Series) -> tuple[float, float]:
    """OLS regression: strat = alpha + beta * bench. Alpha annualized."""
    df = pd.concat([strat_returns, bench_returns], axis=1).dropna()
    if len(df) < 2:
        return 0.0, 0.0
    y = df.iloc[:, 0].values
    x = df.iloc[:, 1].values
    xm = x.mean()
    ym = y.mean()
    denom = ((x - xm) ** 2).sum()
    if denom == 0:
        return 0.0, 0.0
    beta = float(((x - xm) * (y - ym)).sum() / denom)
    alpha_daily = float(ym - beta * xm)
    alpha_annual = alpha_daily * TRADING_DAYS
    return alpha_annual, beta


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    """Return a year x month pivot of monthly returns (%)."""
    if equity.empty:
        return pd.DataFrame()
    monthly = equity.resample("ME").last().pct_change().dropna()
    pivot = (
        pd.DataFrame(
            {
                "year": monthly.index.year,
                "month": monthly.index.month,
                "ret": monthly.values * 100,
            }
        )
        .pivot(index="year", columns="month", values="ret")
    )
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    pivot = pivot.rename(columns={i + 1: month_names[i] for i in range(12)})
    return pivot


def summarize(
    equity: pd.Series,
    returns: pd.Series,
    bench_equity: pd.Series,
    bench_returns: pd.Series,
    trades: pd.DataFrame,
    exposure_series: pd.Series,
    risk_free_rate: float = 0.0,
) -> dict:
    a, b = alpha_beta(returns, bench_returns)
    return {
        "total_return": total_return(equity),
        "cagr": cagr(equity),
        "volatility": volatility(returns),
        "sharpe": sharpe(returns, risk_free_rate),
        "sortino": sortino(returns, risk_free_rate),
        "max_drawdown": max_drawdown(equity),
        "calmar": calmar(equity),
        "alpha": a,
        "beta": b,
        "exposure": exposure(exposure_series),
        "bench_total_return": total_return(bench_equity),
        "bench_cagr": cagr(bench_equity),
        "bench_volatility": volatility(bench_returns),
        "bench_sharpe": sharpe(bench_returns, risk_free_rate),
        "bench_max_drawdown": max_drawdown(bench_equity),
        **trade_stats(trades),
    }
