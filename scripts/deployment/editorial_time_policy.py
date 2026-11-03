"""Offline Beijing-time admission for scheduled editorial workers."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def load_policy(path):
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if policy.get("schema") != "editorial-time-policy/v1":
        raise ValueError("unsupported editorial time policy")
    if policy.get("timezone") != "Asia/Shanghai":
        raise ValueError("policy must use Beijing time")
    workers = policy.get("workers")
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("workers must be between 1 and 32")
    for start, end in policy["weekday_blackout_minutes"]:
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 1440:
            raise ValueError("invalid blackout interval")
    for year, calendar in policy["holiday_calendars"].items():
        if not year.isdecimal() or not calendar.get("source"):
            raise ValueError("holiday calendar needs year and source")
        for day in calendar["off_dates"]:
            parsed = date.fromisoformat(day)
            if str(parsed.year) != year:
                raise ValueError("holiday belongs to another year")
    return policy


def time_admission(policy, now):
    if now.tzinfo is None:
        raise ValueError("schedule requires an aware timestamp")
    local = now.astimezone(ZoneInfo(policy["timezone"]))
    calendar = policy["holiday_calendars"].get(str(local.year))
    known = calendar is not None
    holiday = known and local.date().isoformat() in calendar["off_dates"]
    minute = local.hour * 60 + local.minute
    blackout = local.weekday() < 5 and not holiday and any(
        start <= minute < end for start, end in policy["weekday_blackout_minutes"])
    return {"allowed": not blackout, "beijing_time": local.isoformat(),
            "reason": "weekday_blackout" if blackout else "allowed",
            "holiday": bool(holiday), "calendar_year_known": known}


def gate(policy_path, health_path, archive_root, *, now=None, require_network=True):
    """Recheck time immediately before claims and every physical model call."""
    import shutil

    try:
        moment = now or datetime.now(UTC)
        policy = load_policy(policy_path)
        result = time_admission(policy, moment)
        if not result["allowed"]:
            return result
        if shutil.disk_usage(archive_root).free < 6 * 1024 ** 3:
            return {**result, "allowed": False, "reason": "disk_below_6GiB"}
        if require_network:
            health = json.loads(Path(health_path).read_text(encoding="utf-8"))
            age = moment.timestamp() - health["checked_at_epoch"]
            if (not health.get("ready") or not 0 <= age <= 120
                    or health.get("provider_base_url") != policy["provider_base_url"]
                    or health.get("model") != policy["model"]):
                return {**result, "allowed": False, "reason": "provider_unavailable"}
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {"allowed": False, "reason": "invalid_or_missing_policy_health"}
