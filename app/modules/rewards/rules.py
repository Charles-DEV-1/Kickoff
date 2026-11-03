"""Deterministic reference rules for reward policy and event-log checks.

Database RPCs remain authoritative for production writes. Keeping these small
pure functions makes the policy easy to test without a live Supabase project.
"""

from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo


def local_date_for(instant: datetime, timezone_name: str) -> date:
    """Convert a UTC-aware action instant to the user's IANA local date."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must include a timezone")
    return instant.astimezone(ZoneInfo(timezone_name)).date()


def plant_stage_for(points_total: int, thresholds: list[int]) -> int:
    if points_total < 0 or not thresholds or thresholds[0] != 0:
        raise ValueError("invalid reward configuration")
    if any(value < 0 for value in thresholds) or thresholds != sorted(set(thresholds)):
        raise ValueError("plant thresholds must be strictly increasing")
    return max(0, sum(points_total >= value for value in thresholds) - 1)


def advance_streak(state: dict, active_date: date) -> tuple[dict, list[dict]]:
    """Apply one newly observed active local day and return freeze/streak events."""
    result = deepcopy(state)
    events = []
    last_date = result.get("last_active_local_date")
    if isinstance(last_date, str):
        last_date = date.fromisoformat(last_date)
    if last_date is not None and active_date <= last_date:
        return result, events

    if last_date is not None:
        gap = max(0, (active_date - last_date).days - 1)
        covered = min(gap, result["freezes_held"])
        for offset in range(1, covered + 1):
            freeze_date = last_date.fromordinal(last_date.toordinal() + offset)
            result["freezes_held"] -= 1
            events.append({"type": "freeze_used", "local_date": freeze_date})
        if gap > covered:
            result["best_streak"] = max(result["best_streak"], result["current_streak"])
            result["current_streak"] = 0
            result["streak_status"] = "ended"
            events.append({"type": "streak_ended", "local_date": active_date})
        elif gap > 0:
            result["streak_status"] = "frozen"

    if last_date is None or result["streak_status"] in {"none", "ended"}:
        result["current_streak"] = 1
    else:
        result["current_streak"] += 1
    result["best_streak"] = max(result["best_streak"], result["current_streak"])
    result["streak_status"] = "active"
    result["last_active_local_date"] = active_date
    if result["current_streak"] % 7 == 0 and result["freezes_held"] < 3:
        result["freezes_held"] += 1
        events.append({"type": "freeze_earned", "local_date": active_date})
    return result, events


def rebuild_state_from_events(events: list[dict]) -> dict:
    """Rebuild the cached state from immutable event snapshots in event order."""
    if not events:
        return {
            "points_total": 0, "plant_stage": 0, "current_streak": 0,
            "best_streak": 0, "freezes_held": 0, "streak_status": "none",
            "last_active_local_date": None,
        }
    latest = max(events, key=lambda event: event["event_order"])
    data = latest["event_data"]
    return {
        "points_total": data["points_after"],
        "plant_stage": data["plant_stage_after"],
        "current_streak": data["current_streak_after"],
        "best_streak": data["best_streak_after"],
        "freezes_held": data["freezes_after"],
        "streak_status": data["streak_status_after"],
        "last_active_local_date": data["last_active_local_date_after"],
    }
