"""Dates for mail lists and headers, in fixed English (no locale)."""

import datetime

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def clock_time(moment: datetime.datetime) -> str:
    """Format a time of day, like ``3:07 PM``.

    Args:
        moment: The time.

    Returns:
        The text.
    """
    half = "AM" if moment.hour < 12 else "PM"
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {half}"


def month_day(moment: datetime.datetime) -> str:
    """Format a month and day, like ``Nov 5``.

    Args:
        moment: The time.

    Returns:
        The text.
    """
    return f"{MONTHS[moment.month - 1]} {moment.day}"


def when(stamp: int, now: datetime.datetime, tz: datetime.tzinfo) -> str:
    """Format a short date: the time today, the weekday this week, else the date.

    Args:
        stamp: Epoch seconds.
        now: The current time.
        tz: The time zone to show.

    Returns:
        The text.
    """
    moment = datetime.datetime.fromtimestamp(stamp, tz)
    days = (now.astimezone(tz).date() - moment.date()).days
    if days == 0:
        return clock_time(moment)
    if 0 < days < 7:
        return DAYS[moment.weekday()]
    if moment.year == now.astimezone(tz).year:
        return month_day(moment)
    return f"{month_day(moment)}, {moment.year}"


def full_date(stamp: int, tz: datetime.tzinfo) -> str:
    """Format a full date, like ``Thu Jan 1, 2026, 4:00 PM``.

    Args:
        stamp: Epoch seconds.
        tz: The time zone to show.

    Returns:
        The text.
    """
    moment = datetime.datetime.fromtimestamp(stamp, tz)
    day = DAYS[moment.weekday()]
    return f"{day} {month_day(moment)}, {moment.year}, {clock_time(moment)}"
