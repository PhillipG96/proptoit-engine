"""Market context: data models, seed loading, rules, scoring, and the /context router."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter()

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED_PATH = DATA_DIR / "housing_cycle_seed_v1.csv"
SEED_VERSION = "housing-cycle-seed-v1"
SEED_SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "14s9RLZKXQdz5UkkdzVDMnVoEjyREkND7"
)
LIVE_CONTEXT_PATH = DATA_DIR / "market_context_latest.json"


# ── Data models and loading ──


@dataclass(frozen=True)
class MarketIndicator:
    year: int
    median_price: float
    total_transactions: int
    nominal_appreciation_pct: float
    real_appreciation_pct: float
    mortgage_rate_pct: float
    affordability_index: float
    price_income_ratio: float
    real_wage_growth_pct: float
    payment_income_pct: float
    down_payment_income_pct: float
    unemployment_pct: float
    consumer_sentiment: float

    def to_api(self) -> dict:
        return asdict(self)


def _to_float(row: dict, key: str) -> float:
    raw = row.get(key)
    if raw in (None, ""):
        return 0.0
    return float(str(raw).replace(",", "").replace("$", "").replace("%", ""))


def _to_indicator(row: dict) -> MarketIndicator:
    return MarketIndicator(
        year=int(row["year"]),
        median_price=_to_float(row, "median_price"),
        total_transactions=int(_to_float(row, "total_transactions")),
        nominal_appreciation_pct=_to_float(row, "nominal_appreciation_pct"),
        real_appreciation_pct=_to_float(row, "real_appreciation_pct"),
        mortgage_rate_pct=_to_float(row, "mortgage_rate_pct"),
        affordability_index=_to_float(row, "affordability_index"),
        price_income_ratio=_to_float(row, "price_income_ratio"),
        real_wage_growth_pct=_to_float(row, "real_wage_growth_pct"),
        payment_income_pct=_to_float(row, "payment_income_pct"),
        down_payment_income_pct=_to_float(row, "down_payment_income_pct"),
        unemployment_pct=_to_float(row, "unemployment_pct"),
        consumer_sentiment=_to_float(row, "consumer_sentiment"),
    )


@lru_cache(maxsize=1)
def load_seed_indicators() -> dict[int, MarketIndicator]:
    with SEED_PATH.open(newline="") as fh:
        return {item.year: item for item in map(_to_indicator, csv.DictReader(fh))}


def resolve_seed_year(year: Optional[str | int] = "latest") -> int:
    years = sorted(load_seed_indicators())
    if year in (None, "", "latest"):
        return years[-1]
    requested = int(year)
    if requested in load_seed_indicators():
        return requested
    earlier = [candidate for candidate in years if candidate <= requested]
    return earlier[-1] if earlier else years[0]


def load_live_context() -> Optional[dict]:
    if not LIVE_CONTEXT_PATH.exists():
        return None
    with LIVE_CONTEXT_PATH.open() as fh:
        return json.load(fh)


# ── Rules: thresholds and market conditions ──

TRANSACTION_PEAK = 8_355_000
ADJUSTMENT_MIN = -10
ADJUSTMENT_MAX = 5


def clamp_score(value: int) -> int:
    return max(0, min(100, int(value)))


def transaction_share(indicator: MarketIndicator) -> float:
    return indicator.total_transactions / TRANSACTION_PEAK


def classify_market(indicator: MarketIndicator) -> str:
    pressure = (
        indicator.affordability_index < 100
        or indicator.price_income_ratio >= 3.84
        or indicator.payment_income_pct >= 25
        or indicator.down_payment_income_pct >= 79.09
    )
    if indicator.real_appreciation_pct <= -5 or indicator.nominal_appreciation_pct <= -5:
        return "correction_risk"
    if pressure:
        return "affordability_pressure"
    if transaction_share(indicator) <= 0.60:
        return "low_liquidity"
    if indicator.consumer_sentiment <= 74.9 and indicator.real_appreciation_pct > 0:
        return "recovery_watch"
    if (
        indicator.affordability_index >= 120
        and indicator.price_income_ratio <= 3.35
        and indicator.payment_income_pct <= 20
        and indicator.unemployment_pct <= 5.5
    ):
        return "favorable"
    return "balanced"


def investment_posture(label: str, adjustment: int, indicator: MarketIndicator) -> str:
    if label == "correction_risk" or (
        label == "low_liquidity" and indicator.unemployment_pct >= 7
    ):
        return "defensive"
    if adjustment <= -6:
        return "margin_required"
    if adjustment < 0 or label in {"low_liquidity", "recovery_watch"}:
        return "selective"
    return "normal"


# ── Adjustment factors ──


def score_adjustment(indicator: MarketIndicator) -> tuple[int, list[str]]:
    adjustment = 0
    reasons: list[str] = []
    if indicator.affordability_index < 100:
        adjustment -= 3
        reasons.append("Affordability index is below 100")
    if indicator.price_income_ratio >= 3.84:
        adjustment -= 3
        reasons.append("Price-income ratio is in stress territory")

    share = transaction_share(indicator)
    if share <= 0.60:
        adjustment -= 3
        reasons.append("Transaction volume is far below the prior peak")
    elif share < 0.75:
        adjustment -= 2
        reasons.append("Transaction volume is below the long-run peak")
    elif share < 0.85:
        adjustment -= 1
        reasons.append("Transaction volume is moderately below peak")

    if indicator.payment_income_pct >= 28.1:
        adjustment -= 3
        reasons.append("Payment burden is above the front-end stress band")
    elif indicator.payment_income_pct >= 25:
        adjustment -= 2
        reasons.append("Payment burden is elevated")
    if indicator.down_payment_income_pct >= 79.09:
        adjustment -= 1
        reasons.append("Down payment burden is elevated")

    if indicator.real_appreciation_pct <= -5:
        adjustment -= 2
        reasons.append("Real home prices are contracting")
    elif indicator.nominal_appreciation_pct >= 10 and indicator.price_income_ratio >= 3.84:
        adjustment -= 1
        reasons.append("Rapid price growth adds affordability caution")
    if indicator.consumer_sentiment <= 74.9:
        adjustment -= 2
        reasons.append("Consumer sentiment is weak")

    if indicator.real_wage_growth_pct >= 4:
        adjustment += 2
        reasons.append("Strong real wage growth partly offsets pressure")
    if indicator.unemployment_pct <= 4.9:
        adjustment += 1
        reasons.append("Low unemployment partly offsets pressure")
    elif indicator.unemployment_pct >= 7:
        adjustment -= 1
        reasons.append("Labor market stress is elevated")

    capped = max(ADJUSTMENT_MIN, min(ADJUSTMENT_MAX, adjustment))
    return capped, reasons[:4]


# ── Scoring / context ──


def get_market_context(year: Optional[str | int] = "latest") -> dict:
    if year in (None, "", "latest"):
        live = load_live_context()
        if live:
            return live
    resolved_year = resolve_seed_year(year)
    indicator = load_seed_indicators()[resolved_year]
    adjustment, reasons = score_adjustment(indicator)
    label = classify_market(indicator)
    return {
        "year": indicator.year,
        "requestedYear": year or "latest",
        "source": "static_seed",
        "sourceVersion": SEED_VERSION,
        "sourceUrl": SEED_SOURCE_URL,
        "observationDate": f"{indicator.year}-12-31",
        "fetchedAt": None,
        "isStale": False,
        "fallbackReason": None,
        "conditionLabel": label,
        "cycleState": label,
        "investmentPosture": investment_posture(label, adjustment, indicator),
        "marketScoreAdjustment": adjustment,
        "marketAdjustmentReasons": reasons,
        "nationalContextNote": "National annual context; not a substitute for local comps.",
        "indicators": indicator.to_api(),
    }


def market_fields_for_score(
    deal_score: Optional[int | float],
    context: Optional[dict] = None,
    include_context: bool = False,
) -> dict:
    local_score = clamp_score(int(deal_score or 0))
    market_context = context or get_market_context("latest")
    adjustment = int(market_context.get("marketScoreAdjustment") or 0)
    reasons = list(market_context.get("marketAdjustmentReasons") or [])
    if adjustment > 0 and local_score < 40:
        adjustment = 0
        reasons = ["Positive national adjustment withheld for local scores below 40"] + reasons
    adjusted = clamp_score(local_score + adjustment)
    fields = {
        "marketScoreAdjustment": adjustment,
        "marketAdjustedDealScore": adjusted,
        "marketAdjustmentReasons": reasons[:4],
        "marketConditionLabel": market_context.get("conditionLabel"),
        "marketInvestmentPosture": market_context.get("investmentPosture"),
        "marketContextYear": market_context.get("year"),
        "marketContextIsStale": bool(market_context.get("isStale")),
    }
    if include_context:
        fields["marketContext"] = market_context
    return fields


def local_floor_for_adjusted_min(min_adjusted_score: int, context: Optional[dict] = None) -> int:
    market_context = context or get_market_context("latest")
    adjustment = int(market_context.get("marketScoreAdjustment") or 0)
    if adjustment > 0:
        if min_adjusted_score <= 40:
            return max(0, min(100, min_adjusted_score))
        return max(0, min(100, max(40, min_adjusted_score - adjustment)))
    return max(0, min(100, min_adjusted_score - adjustment))


# ── Router ──


@router.get("/context")
def market_context(year: Optional[str] = Query(default="latest")) -> Dict[str, Any]:
    try:
        return {"success": True, "data": get_market_context(year)}
    except Exception:
        logger.error("Market context error", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
