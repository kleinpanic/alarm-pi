"""Shared day-of-week and time parsing/formatting (used by CLI and web)."""

from datetime import datetime
from typing import List

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_FULL_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_NAME_MAP = {
    "mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def parse_days(days_str: str) -> List[int]:
    """Parse '0,1,2' / 'mon,tue' / 'weekdays'|'weekends'|'daily' into [int]."""
    s = days_str.lower().strip()
    if s == "weekdays":
        return [0, 1, 2, 3, 4]
    if s == "weekends":
        return [5, 6]
    if s in ("daily", "everyday", "all"):
        return [0, 1, 2, 3, 4, 5, 6]

    days = []
    for part in (p.strip() for p in s.split(",")):
        if not part:
            continue
        if part in _NAME_MAP:
            days.append(_NAME_MAP[part])
        elif part.isdigit() and 0 <= int(part) <= 6:
            days.append(int(part))
        else:
            raise ValueError(f"Unknown day: {part}")
    if not days:
        raise ValueError("No valid days given")
    return sorted(set(days))


def format_days(days: List[int]) -> str:
    """Format day list as a readable string."""
    if days == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if days == [5, 6]:
        return "Weekends"
    if days == [0, 1, 2, 3, 4, 5, 6]:
        return "Daily"
    return ",".join(DAY_NAMES[d] for d in sorted(days))


def validate_time(time_str: str) -> str:
    """Validate/normalize a time string to HH:MM (24h). Raises ValueError."""
    try:
        return datetime.strptime(time_str.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(f"Invalid time format: {time_str}. Use HH:MM (24-hour)")
