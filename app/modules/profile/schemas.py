import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SETTINGS_TYPES = {
    "calm_mode": bool,
    "sound_enabled": bool,
    "streak_visible": bool,
    "reminders_enabled": bool,
    "daily_reminder_cap": int,
    "quiet_hours_start": str,
    "quiet_hours_end": str,
    "show_task_in_notification": bool,
    "detail_level_default": int,
}
QUIET_HOURS_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ProfileValidationError(Exception):
    def __init__(self, fields):
        super().__init__("Profile validation failed")
        self.fields = fields


def validate_profile_patch(payload, supported_languages):
    if not isinstance(payload, dict):
        raise ProfileValidationError([{"field": "body", "code": "object_required"}])

    allowed = {"display_name", "language", "timezone", "settings"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProfileValidationError([
            {"field": field, "code": "unknown_field"} for field in unknown
        ])

    result = {}
    problems = []

    if "display_name" in payload:
        value = payload["display_name"]
        if value is not None and (
            not isinstance(value, str) or not 1 <= len(value) <= 60
        ):
            problems.append({"field": "display_name", "code": "invalid_value"})
        else:
            result["display_name"] = value

    if "language" in payload:
        value = payload["language"]
        if not isinstance(value, str) or value not in supported_languages:
            problems.append({"field": "language", "code": "unsupported_language"})
        else:
            result["language"] = value

    if "timezone" in payload:
        value = payload["timezone"]
        if not isinstance(value, str):
            problems.append({"field": "timezone", "code": "invalid_timezone"})
        else:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                problems.append({"field": "timezone", "code": "invalid_timezone"})
            else:
                result["timezone"] = value

    if "settings" in payload:
        settings = payload["settings"]
        if not isinstance(settings, dict):
            problems.append({"field": "settings", "code": "object_required"})
        else:
            validated_settings = {}
            for field, value in settings.items():
                expected_type = SETTINGS_TYPES.get(field)
                if expected_type is None:
                    problems.append({"field": f"settings.{field}", "code": "unknown_key"})
                    continue
                if expected_type is int:
                    valid = isinstance(value, int) and not isinstance(value, bool)
                else:
                    valid = isinstance(value, expected_type)
                if field == "daily_reminder_cap" and valid and not 0 <= value <= 6:
                    valid = False
                elif field == "detail_level_default" and valid and not 1 <= value <= 5:
                    valid = False
                elif field in {"quiet_hours_start", "quiet_hours_end"}:
                    valid = isinstance(value, str) and bool(QUIET_HOURS_PATTERN.fullmatch(value))
                if not valid:
                    problems.append({"field": f"settings.{field}", "code": "invalid_value"})
                else:
                    validated_settings[field] = value
            result["settings"] = validated_settings

    if problems:
        raise ProfileValidationError(problems)
    return result
