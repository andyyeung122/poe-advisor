"""Buy-timing read of one item: where the current league is versus how past leagues behaved, plus the last weeks' trend.

Pure functions over the ``ninja.item_history(..., daily=True)`` result, so they are easy to test. This is a
statistical pattern across a few leagues, never a forecast: patches, new builds and league mechanics move prices.
"""

from __future__ import annotations

import statistics
from typing import Any

FLAT_PCT = 5.0        # a past-league move smaller than this counts as flat
STRONG_PCT = 15.0     # a consensus move at least this big justifies waiting (or buying early)


def _week_medians(league: dict[str, Any]) -> dict[int, float]:
    return {w["week"]: w["median"] for w in league.get("weekly") or [] if w.get("median")}


def past_pattern(current_week: int, past: list[dict[str, Any]], horizon: int = 2) -> dict[str, Any]:
    """What each past league did over the ``horizon`` weeks after the same week of the league."""
    rows = []
    for league in past:
        weeks = _week_medians(league)
        base, later = weeks.get(current_week), weeks.get(current_week + horizon)
        if base is None or later is None:
            continue
        change = (later / base - 1) * 100
        peak_week = (league.get("peak") or {}).get("week")
        rows.append({
            "league": league.get("league"), "change_pct": round(change, 1),
            "direction": "up" if change > FLAT_PCT else "down" if change < -FLAT_PCT else "flat",
            "weeks_to_peak": (peak_week - current_week) if peak_week is not None else None,
        })
    out: dict[str, Any] = {"leagues_compared": len(rows), "horizon_weeks": horizon, "by_league": rows}
    if rows:
        directions = [r["direction"] for r in rows]
        top = max(set(directions), key=directions.count)
        out["agreeing"] = directions.count(top)
        out["consensus"] = top if directions.count(top) / len(rows) >= 2 / 3 and len(rows) >= 2 else "mixed"
        out["median_change_pct"] = round(statistics.median(r["change_pct"] for r in rows), 1)
    return out


def recent_trend(daily: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Short-horizon read from the last two weeks of daily prices."""
    values = [d["value"] for d in daily if d.get("value")][-14:]
    if len(values) < 7:
        return None
    latest = values[-1]
    now = statistics.median(values[-3:])
    before = statistics.median(values[:3])
    change = (now / before - 1) * 100 if before else 0.0
    low, high = min(values), max(values)
    ordered = sorted(values)
    return {
        "days": len(values), "latest": latest, "median": round(statistics.median(values), 3),
        "low": low, "high": high,
        "position_in_range": round((latest - low) / (high - low), 2) if high > low else 0.5,  # 0 = at the low
        "change_pct": round(change, 1),
        "direction": "up" if change > FLAT_PCT else "down" if change < -FLAT_PCT else "flat",
        "volatility_pct": round(statistics.pstdev(values) / statistics.mean(values) * 100, 1),
        "p25": ordered[len(ordered) // 4],
    }


def recommend(history: dict[str, Any], urgent: bool = False) -> dict[str, Any]:
    leagues = history.get("leagues") or []
    current = next((l for l in leagues if l.get("status") == "requested" and l.get("weekly")), None)
    if current is None:
        return {"item": history.get("item"), "action": "unknown", "reason": "No price history for the current league."}
    past = [l for l in leagues if l.get("status") == "past" and l.get("weekly")]
    week = current["weekly"][-1]["week"]
    pattern = past_pattern(week, past)
    trend = recent_trend(current.get("recent_daily") or [])
    unit = current.get("unit")
    latest = current.get("latest")

    caveats = []
    if current.get("days_of_data", 0) < 14:
        caveats.append("Under two weeks of data this league: prices are thin and noisy.")
    if pattern["leagues_compared"] < 2:
        caveats.append("Fewer than two past leagues cover this week: the cross-league signal is weak.")
    if trend is None:
        caveats.append("Not enough daily points for a short-term trend.")

    consensus = pattern.get("consensus")
    move = pattern.get("median_change_pct", 0.0)
    recent_down = trend and trend["direction"] == "down"
    recent_up = trend and trend["direction"] == "up"
    near_low = bool(trend and trend["position_in_range"] <= 0.33)
    agreeing = pattern.get("agreeing", 0)
    confidence = "high" if agreeing >= 3 else "medium" if agreeing == 2 else "low"

    target = None
    if consensus == "down" and move <= -STRONG_PCT and not (urgent and not recent_down):
        action = "wait"
        reason = (f"{agreeing}/{pattern['leagues_compared']} past leagues fell (median {move}%) over the next "
                  f"{pattern['horizon_weeks']} weeks from week {week}.")
        if urgent:
            reason += " Urgent: only wait if you can live without it; a few days is the most the data supports."
        target = round(latest * (1 + move / 100), 3) if latest else None
    elif consensus == "up" and move >= STRONG_PCT:
        action = "buy_now"
        reason = (f"{agreeing}/{pattern['leagues_compared']} past leagues rose (median +{move}%) over the next "
                  f"{pattern['horizon_weeks']} weeks from week {week}: waiting tends to cost more.")
    elif urgent:
        action = "buy_now"
        reason = "Urgent and the data shows no clear drop coming: the saving from waiting is not worth the delay."
    elif recent_down and not near_low:
        action = "wait_few_days"
        reason = "Price has been sliding over the last two weeks and is not yet near its low."
        target = trend["p25"]
    elif near_low or recent_up:
        action = "buy_now"
        reason = ("Price is near its two-week low." if near_low else "Price is climbing; no sign of a dip.")
    else:
        action = "wait_few_days"
        reason = "No clear signal; the price is mid-range, so a dip toward the lower quartile is likely within days."
        target = trend["p25"] if trend else None

    result = {
        "item": history.get("item"), "league": current.get("league"), "unit": unit, "latest": latest,
        "league_week": week, "days_of_data": current.get("days_of_data"),
        "action": action, "reason": reason, "confidence": confidence,
        "past_leagues": pattern, "recent_trend": trend, "caveats": caveats,
        "note": "A pattern across a few leagues, not a forecast; patches and new builds can break it. "
                "Past-league units differ, so only percentages are compared.",
    }
    if target is not None:
        result["target_price"] = target
        if latest:
            result["potential_saving"] = round(latest - target, 3)
    return result
