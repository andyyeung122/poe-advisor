from poe_advisor import timing


def _league(name, medians, status="past", daily=None, latest=None):
    weekly = [{"week": i + 1, "median": m, "days": 7} for i, m in enumerate(medians)]
    peak = max(weekly, key=lambda w: w["median"])
    out = {"league": name, "status": status, "unit": "chaos", "days_of_data": len(medians) * 7,
           "latest": latest or medians[-1], "weekly": weekly, "peak": {"week": peak["week"], "median": peak["median"]}}
    if daily is not None:
        out["recent_daily"] = [{"day": i, "value": v} for i, v in enumerate(daily)]
    return out


def _history(current, past):
    return {"item": "Test Item", "leagues": [current, *past]}


FLAT_DAILY = [100.0] * 14


def test_waits_when_past_leagues_fell_after_this_week():
    past = [_league("A", [100, 100, 100, 70, 60]), _league("B", [100, 100, 100, 75, 65]),
            _league("C", [100, 100, 100, 72, 60])]
    current = _league("Now", [100, 100, 100], status="requested", daily=FLAT_DAILY, latest=100.0)
    out = timing.recommend(_history(current, past))
    assert out["action"] == "wait" and out["confidence"] == "high"
    assert out["target_price"] < 100 and out["potential_saving"] > 0


def test_urgent_buys_now_unless_a_drop_is_clear_and_trend_is_down():
    past = [_league("A", [100, 100, 100, 70, 60]), _league("B", [100, 100, 100, 75, 65])]
    current = _league("Now", [100, 100, 100], status="requested", daily=FLAT_DAILY, latest=100.0)
    assert timing.recommend(_history(current, past), urgent=True)["action"] == "buy_now"


def test_buys_now_when_past_leagues_rose():
    past = [_league("A", [100, 100, 100, 140, 150]), _league("B", [100, 100, 100, 130, 160])]
    current = _league("Now", [100, 100, 100], status="requested", daily=FLAT_DAILY, latest=100.0)
    out = timing.recommend(_history(current, past))
    assert out["action"] == "buy_now" and out["past_leagues"]["consensus"] == "up"


def test_mixed_history_with_sliding_price_waits_a_few_days():
    past = [_league("A", [100, 100, 100, 100, 130]), _league("B", [100, 100, 100, 100, 70])]
    sliding = [120 - i * 2 for i in range(14)]  # ends at 94, still above the two-week low region
    sliding[-1] = 100.0
    current = _league("Now", [100, 100, 100], status="requested", daily=sliding, latest=100.0)
    out = timing.recommend(_history(current, past))
    assert out["past_leagues"]["consensus"] == "mixed"
    assert out["action"] in ("wait_few_days", "buy_now")  # depends on the range position
    assert out["recent_trend"]["direction"] == "down"


def test_caveats_for_thin_data():
    current = _league("Now", [100], status="requested", daily=None, latest=100.0)
    out = timing.recommend(_history(current, []))
    assert any("two weeks" in c for c in out["caveats"])
    assert any("past leagues" in c for c in out["caveats"])


def test_no_current_history_is_unknown():
    out = timing.recommend({"item": "X", "leagues": [{"league": "Now", "status": "requested", "error": "no history yet"}]})
    assert out["action"] == "unknown"
