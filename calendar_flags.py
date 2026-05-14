"""
calendar_flags.py — Day-context labeling for commute_v2
========================================================
Returns a CalendarContext for any given date, covering:
  - US federal holidays (via Nager.Date API, free, no key)
  - CPS Chicago no-school days (hardcoded from official calendar, update annually)
  - Holiday-eve detection (day before a federal holiday)
  - DST transition days (spring forward / fall back)

Usage:
    from calendar_flags import get_day_context
    ctx = get_day_context(datetime.date(2026, 3, 17))
    ctx.day_type          # "cps_off"
    ctx.exclude_from_model  # True
"""

import datetime
import requests
from dataclasses import dataclass, field

# ─────────────────────────────────────────
#  DST dates (America/Chicago)
#  Spring forward = 2nd Sunday March
#  Fall back      = 1st Sunday November
#  Mark the transition day + the day after (traffic adjusts ~1-2 days)
# ─────────────────────────────────────────
DST_TRANSITIONS = {
    # (year, month, day): "spring_forward" | "fall_back"
    datetime.date(2025, 3,  9): "spring_forward",
    datetime.date(2025, 3, 10): "spring_forward",   # day after
    datetime.date(2025, 11, 2): "fall_back",
    datetime.date(2025, 11, 3): "fall_back",
    datetime.date(2026, 3,  8): "spring_forward",
    datetime.date(2026, 3,  9): "spring_forward",
    datetime.date(2026, 11, 1): "fall_back",
    datetime.date(2026, 11, 2): "fall_back",
}

# ─────────────────────────────────────────
#  CPS Chicago no-school days 2025-26
#  Source: cps.edu official family & staff calendar
#  Update this dict each August for the new school year.
# ─────────────────────────────────────────
#
# Key: datetime.date → reason string
# Reasons: "summer" | "winter_break" | "spring_break" |
#          "pd_day" | "parent_conference" | "election_day" | "thanksgiving"
#
_CPS_NO_SCHOOL: dict[datetime.date, str] = {}

def _d(y, m, day): return datetime.date(y, m, day)
def _range(start: datetime.date, end: datetime.date, reason: str):
    cur = start
    while cur <= end:
        _CPS_NO_SCHOOL[cur] = reason
        cur += datetime.timedelta(days=1)

# Summer (before school starts Aug 18)
# Already weekends/pre-data so not critical, but mark for completeness
_range(_d(2025, 8, 11), _d(2025, 8, 15), "pd_day")   # teacher PD before school

# Standalone no-school days (students off)
_CPS_NO_SCHOOL[_d(2025, 9, 1)]  = "labor_day"           # Federal + CPS
_CPS_NO_SCHOOL[_d(2025, 9, 26)] = "pd_day"              # Professional development
_CPS_NO_SCHOOL[_d(2025, 10, 13)]= "indigenous_day"      # Indigenous Peoples Day
_CPS_NO_SCHOOL[_d(2025, 11, 4)] = "parent_conference"   # Elementary P-T conf
_CPS_NO_SCHOOL[_d(2025, 11, 5)] = "election_day"        # General election
_range(_d(2025, 11, 24), _d(2025, 11, 28), "thanksgiving")
_range(_d(2025, 12, 22), _d(2026, 1,  2),  "winter_break")
_CPS_NO_SCHOOL[_d(2026, 1,  5)]  = "pd_day"
_CPS_NO_SCHOOL[_d(2026, 1, 19)]  = "mlk_day"
_CPS_NO_SCHOOL[_d(2026, 2, 16)]  = "presidents_day"
_CPS_NO_SCHOOL[_d(2026, 2, 17)]  = "pd_day"
_CPS_NO_SCHOOL[_d(2026, 3, 16)]  = "pd_day"             # principal-directed
_CPS_NO_SCHOOL[_d(2026, 3, 17)]  = "election_day"       # IL primary election
_range(_d(2026, 3, 23), _d(2026, 3, 27), "spring_break")
_CPS_NO_SCHOOL[_d(2026, 4,  3)]  = "pd_day"
_CPS_NO_SCHOOL[_d(2026, 4, 24)]  = "pd_day"
_CPS_NO_SCHOOL[_d(2026, 5, 25)]  = "memorial_day"
_CPS_NO_SCHOOL[_d(2026, 6,  4)]  = "last_day_students"  # last day = full day, not off, but mark
_CPS_NO_SCHOOL[_d(2026, 6,  5)]  = "pd_day"
_CPS_NO_SCHOOL[_d(2026, 6,  8)]  = "pd_day"

# ─────────────────────────────────────────
#  Federal holidays cache (Nager.Date API)
# ─────────────────────────────────────────
_federal_cache: dict[int, dict[datetime.date, str]] = {}  # year → {date: name}

def _load_federal_holidays(year: int) -> dict[datetime.date, str]:
    if year in _federal_cache:
        return _federal_cache[year]
    try:
        url = f"https://date.nager.at/api/v3/publicholidays/{year}/US"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        result = {}
        for h in resp.json():
            # Only keep national ("global") holidays; skip state-only ones
            if h.get("global", True):
                d = datetime.date.fromisoformat(h["date"])
                result[d] = h["name"]
        _federal_cache[year] = result
        return result
    except Exception as e:
        print(f"[calendar_flags] Warning: could not fetch federal holidays for {year}: {e}")
        # Hardcoded fallback for the most impactful US federal holidays
        fallback = {
            _d(year, 1,  1): "New Year's Day",
            _d(year, 6, 19): "Juneteenth",
            _d(year, 7,  4): "Independence Day",
            _d(year, 11,11): "Veterans Day",
            _d(year, 12,25): "Christmas Day",
        }
        _federal_cache[year] = fallback
        return fallback


# ─────────────────────────────────────────
#  Main interface
# ─────────────────────────────────────────
@dataclass
class CalendarContext:
    date: datetime.date

    # Federal holiday
    is_federal_holiday: bool = False
    federal_holiday_name: str = ""

    # CPS
    is_cps_off: bool = False
    cps_off_reason: str = ""

    # Derived flags
    is_holiday_eve: bool = False    # day before a federal holiday (traffic lighter)
    dst_transition: str = ""        # "spring_forward" | "fall_back" | ""

    # Rolled-up
    day_type: str = "normal"        # normal | federal_holiday | cps_off | holiday_eve | dst
    exclude_from_model: bool = False


def get_day_context(date: datetime.date) -> CalendarContext:
    ctx = CalendarContext(date=date)

    # Federal holidays
    fed = _load_federal_holidays(date.year)
    if date in fed:
        ctx.is_federal_holiday = True
        ctx.federal_holiday_name = fed[date]

    # Holiday eve: tomorrow is a federal holiday
    tomorrow = date + datetime.timedelta(days=1)
    if tomorrow in _load_federal_holidays(tomorrow.year):
        ctx.is_holiday_eve = True

    # CPS no-school
    if date in _CPS_NO_SCHOOL:
        ctx.is_cps_off = True
        ctx.cps_off_reason = _CPS_NO_SCHOOL[date]

    # DST
    if date in DST_TRANSITIONS:
        ctx.dst_transition = DST_TRANSITIONS[date]

    # Rolled-up day_type (priority: holiday > cps_off > holiday_eve > dst > normal)
    if ctx.is_federal_holiday:
        ctx.day_type = "federal_holiday"
    elif ctx.is_cps_off:
        ctx.day_type = "cps_off"
    elif ctx.is_holiday_eve:
        ctx.day_type = "holiday_eve"
    elif ctx.dst_transition:
        ctx.day_type = "dst_transition"
    else:
        ctx.day_type = "normal"

    # Exclude from model: holidays and DST transitions are structurally different
    # Keep holiday_eve IN the model (it's a real pattern worth learning)
    ctx.exclude_from_model = ctx.is_federal_holiday or bool(ctx.dst_transition)

    return ctx


def context_as_row_fields(ctx: CalendarContext) -> dict:
    """Returns a flat dict ready to merge into a CSV row."""
    return {
        "is_federal_holiday": int(ctx.is_federal_holiday),
        "federal_holiday_name": ctx.federal_holiday_name,
        "is_cps_off": int(ctx.is_cps_off),
        "cps_off_reason": ctx.cps_off_reason,
        "is_holiday_eve": int(ctx.is_holiday_eve),
        "dst_transition": ctx.dst_transition,
        "day_type": ctx.day_type,
        "exclude_from_model": int(ctx.exclude_from_model),
    }


if __name__ == "__main__":
    # Quick smoke-test
    test_dates = [
        datetime.date(2026, 1, 1),    # New Year's Day
        datetime.date(2026, 1, 2),    # CPS winter break
        datetime.date(2026, 3, 8),    # DST spring forward
        datetime.date(2026, 3, 17),   # CPS election day off
        datetime.date(2026, 3, 23),   # CPS spring break
        datetime.date(2026, 5, 22),   # day before Memorial Day eve... no, let's pick
        datetime.date(2026, 5, 25),   # Memorial Day
        datetime.date(2026, 7, 3),    # holiday eve (July 4th)
        datetime.date(2026, 7, 4),    # Independence Day
        datetime.date(2026, 4, 7),    # normal Tuesday
    ]
    for d in test_dates:
        ctx = get_day_context(d)
        print(f"{d} {d.strftime('%a'):3s}  {ctx.day_type:18s}  exclude={ctx.exclude_from_model}  "
              f"fed={ctx.federal_holiday_name or '-':25s}  "
              f"cps={ctx.cps_off_reason or '-':20s}  "
              f"dst={ctx.dst_transition or '-'}")
