"""
commute_v2.py — I-290 commute tracker, V2
==========================================
Target: arrive 08:25–08:30 daily.

Modes (set via SAMPLE_MODE env var):
  collect   — record 10 drive-time samples (07:30–08:15, every 5 min)
  recommend — run once at ~07:00, email today's suggested departure window
  both      — recommend first, then collect (default for GitHub Actions)

New in V2 vs V1:
  - Window shifted to 07:30–08:15 (10 samples)
  - Adds estimated_arrival_local + arrival_delta_min columns
  - recommend mode with cold-start fallback on V1 data
  - Email via SMTP (Gmail app-password or any SMTP relay)
  - Clean separation of concerns for easy extension later
"""

import os, csv, time, smtplib, datetime as dt
import requests, pytz
from calendar_flags import get_day_context, context_as_row_fields
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────
#  Config
# ─────────────────────────────────────────
LOCAL_TZ    = pytz.timezone("America/Chicago")
SAMPLE_MODE = os.getenv("SAMPLE_MODE", "both").lower()   # collect | recommend | both
FORCE_RUN   = os.getenv("FORCE_RUN") == "1"
FORCE_DATE  = os.getenv("FORCE_DATE")                    # YYYY-MM-DD override

OD = {
    "origin_name":        "Home",
    "origin_address":     "1001 S State St, Chicago, IL 60605",
    "destination_name":   "Office",
    "destination_address":"5911 Butterfield Rd, Hillside, IL 60162",
}

# Collection window
WINDOW = {"start": "07:30", "end": "08:15", "step_min": 5}   # 10 slots: 07:30..08:10
WEEKDAYS_ONLY    = True
RUN_END_DATE     = "2026-12-31"

# Arrival target
TARGET_ARRIVE_HM = "08:28"   # midpoint of 08:25–08:30; used for delta calc & recommend

# Recommend mode settings
RECOMMEND_PERCENTILE = 80    # use P80 drive time so 80% of days you arrive on time
COLD_START_V1_GLOB   = "data/commute_2025*.csv,data/commute_2026*.csv"  # V1 files to bootstrap

# API keys
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
if not SERPAPI_KEY:
    raise RuntimeError("Missing env SERPAPI_KEY")

# Email (optional — skip if not configured)
SMTP_HOST    = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT    = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER    = os.getenv("SMTP_USER", "")      # sender Gmail address
SMTP_PASS    = os.getenv("SMTP_PASS", "")      # Gmail app-password
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")   # recipient (can be same as SMTP_USER)
EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASS and NOTIFY_EMAIL)

SLEEP_BETWEEN = 0.6   # seconds between forecast API calls

# External endpoints
SERP_ENDPOINT       = "https://serpapi.com/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODE  = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_HOURLY      = [
    "temperature_2m", "precipitation", "precipitation_probability",
    "weather_code", "wind_speed_10m",
]

# ─────────────────────────────────────────
#  CSV schema
# ─────────────────────────────────────────
CSV_HEADERS = [
    "schema_version",
    "run_timestamp_local", "date_local", "weekday",
    "origin_name", "origin_address", "destination_name", "destination_address",
    "depart_time_local", "depart_time_unix",
    "distance_meters", "drive_duration_seconds", "drive_duration_minutes",
    "estimated_arrival_local", "arrival_delta_min",   # NEW: + = late, - = early
    "route_via", "route_summary",
    "weather_hour_local", "weather_lat", "weather_lon",
    "weather_temp_c", "weather_precip_mm", "weather_precip_prob_pct",
    "weather_code", "weather_wind_kmh", "weather_class",
    # Calendar context (from calendar_flags.py)
    "is_federal_holiday", "federal_holiday_name",
    "is_cps_off", "cps_off_reason",
    "is_holiday_eve", "dst_transition",
    "day_type", "exclude_from_model",
]


def csv_path(date: dt.date) -> str:
    return f"data/commute_v2_{date.strftime('%Y%m')}.csv"


def ensure_csv(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def already_collected(path: str, date_str: str) -> int:
    """Count rows already recorded for this date in the collect window."""
    if not os.path.exists(path):
        return 0
    expected = set()
    h, m = map(int, WINDOW["start"].split(":"))
    end_h, end_m = map(int, WINDOW["end"].split(":"))
    cur = dt.datetime(2000, 1, 1, h, m)
    end = dt.datetime(2000, 1, 1, end_h, end_m)
    while cur <= end:
        expected.add(cur.strftime("%H:%M"))
        cur += dt.timedelta(minutes=WINDOW["step_min"])
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return sum(
                1 for row in reader
                if row.get("date_local") == date_str
                and row.get("depart_time_local") in expected
            )
    except Exception:
        return 0


# ─────────────────────────────────────────
#  Time helpers
# ─────────────────────────────────────────
def window_times(local_date: dt.date):
    h, m = map(int, WINDOW["start"].split(":"))
    end_h, end_m = map(int, WINDOW["end"].split(":"))
    cur = LOCAL_TZ.localize(dt.datetime(local_date.year, local_date.month, local_date.day, h, m))
    end = LOCAL_TZ.localize(dt.datetime(local_date.year, local_date.month, local_date.day, end_h, end_m))
    step = dt.timedelta(minutes=WINDOW["step_min"])
    while cur <= end:
        yield cur
        cur += step


def target_arrival_dt(local_date: dt.date) -> dt.datetime:
    th, tm = map(int, TARGET_ARRIVE_HM.split(":"))
    return LOCAL_TZ.localize(dt.datetime(local_date.year, local_date.month, local_date.day, th, tm))


# ─────────────────────────────────────────
#  SerpAPI — Google Maps Directions
# ─────────────────────────────────────────
def fetch_directions(start_addr: str, end_addr: str,
                     depart_unix: int | None = None, leave_now: bool = False) -> dict:
    params = {
        "engine": "google_maps_directions",
        "api_key": SERPAPI_KEY,
        "hl": "en", "gl": "us",
        "start_addr": start_addr,
        "end_addr": end_addr,
        "travel_mode": 0,      # driving
        "distance_unit": 1,
        "no_cache": "true",
    }
    if not leave_now and depart_unix is not None:
        params["time"] = f"depart_at:{depart_unix}"
    r = requests.get(SERP_ENDPOINT, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("search_metadata", {}).get("status") != "Success":
        raise RuntimeError(f"SerpApi error: {data.get('error')}")
    return data


def parse_directions(data: dict) -> tuple[int | None, int | None, str, str]:
    """Returns (dur_sec, distance_m, via, summary)."""
    routes = data.get("directions") or []
    if routes:
        d0 = routes[0]
        return (
            d0.get("duration"),
            d0.get("distance"),
            d0.get("via", ""),
            d0.get("summary", ""),
        )
    durs = data.get("durations") or []
    if durs:
        return durs[0].get("duration"), None, "", ""
    return None, None, "", ""


# ─────────────────────────────────────────
#  Open-Meteo — Weather
# ─────────────────────────────────────────
_geocode_cache: dict[str, tuple[float | None, float | None]] = {}


def geocode(addr: str) -> tuple[float | None, float | None]:
    if addr in _geocode_cache:
        return _geocode_cache[addr]
    try:
        js = requests.get(
            OPEN_METEO_GEOCODE,
            params={"name": addr, "count": 1, "language": "en"},
            timeout=20,
        ).json()
        res = js.get("results") or []
        if res:
            lat, lon = float(res[0]["latitude"]), float(res[0]["longitude"])
            _geocode_cache[addr] = (lat, lon)
            return lat, lon
    except Exception:
        pass
    _geocode_cache[addr] = (None, None)
    return None, None


def coords_from_places(data: dict, prefer_addr: str) -> tuple[float | None, float | None]:
    places = data.get("places_info") or []
    prefer = prefer_addr.lower()
    cand = []
    for p in places:
        gps = p.get("gps_coordinates") or {}
        lat, lon = gps.get("latitude"), gps.get("longitude")
        if lat is None or lon is None:
            continue
        score = 2 if prefer.split(",")[0] in (p.get("address") or "").lower() else 0
        cand.append((score, float(lat), float(lon)))
    if not cand:
        return None, None
    cand.sort(reverse=True)
    return cand[0][1], cand[0][2]


def fetch_weather(lat: float, lon: float, dt_local: dt.datetime
                  ) -> tuple[str | None, float | None, float | None,
                              float | None, int | None, float | None]:
    dt_hour = dt_local.replace(minute=0, second=0, microsecond=0)
    date_str = dt_hour.strftime("%Y-%m-%d")
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(WEATHER_HOURLY),
        "timezone": "America/Chicago",
        "start_date": date_str, "end_date": date_str,
        "windspeed_unit": "kmh",
    }
    try:
        js = requests.get(OPEN_METEO_FORECAST, params=params, timeout=20).json()
        hourly = js.get("hourly") or {}
        times = hourly.get("time") or []
        target = dt_hour.strftime("%Y-%m-%dT%H:00")
        idx = times.index(target) if target in times else 0
        def pick(key):
            arr = hourly.get(key)
            return arr[idx] if isinstance(arr, list) and len(arr) > idx else None
        return (
            dt_hour.strftime("%Y-%m-%d %H:00"),
            pick("temperature_2m"),
            pick("precipitation"),
            pick("precipitation_probability"),
            pick("weather_code"),
            pick("wind_speed_10m"),
        )
    except Exception:
        return None, None, None, None, None, None


def classify_weather(wx_code, precip_mm) -> str:
    try:
        code = int(wx_code) if wx_code is not None else None
    except Exception:
        code = None
    try:
        p = float(precip_mm) if precip_mm is not None else 0.0
    except Exception:
        p = 0.0

    if code in {56, 57, 66, 67}: return "freezing_precip"
    if code in {0, 1}:           return "clear"
    if code in {2}:              return "partly_cloudy"
    if code in {3}:              return "overcast"
    if code in {45, 48}:         return "fog"
    if code in {71, 73, 75, 77, 85, 86}:
        return "heavy_snow" if p > 1.0 else "light_snow"
    if code in {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}:
        if p > 4.0:   return "heavy_rain"
        if p > 0.1:   return "light_rain"
        return "light_rain"
    if p > 4.0:  return "heavy_rain"
    if p > 0.1:  return "light_rain"
    return "overcast"


# ─────────────────────────────────────────
#  Email
# ─────────────────────────────────────────
def send_email(subject: str, body_text: str, body_html: str | None = None):
    if not EMAIL_ENABLED:
        print("[email] Not configured — printing instead:\n")
        print(subject)
        print(body_text)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
    print(f"[email] Sent: {subject}")


# ─────────────────────────────────────────
#  RECOMMEND mode
# ─────────────────────────────────────────
def load_all_history() -> list[dict]:
    """Load V2 data first, fall back to V1 CSVs for cold start."""
    import glob as _glob
    rows = []
    # V2 data (preferred)
    for path in sorted(_glob.glob("data/commute_v2_*.csv")):
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    rows.append(row)
        except Exception:
            pass
    # V1 cold-start if V2 has <20 rows in the new window
    v2_in_window = sum(
        1 for r in rows
        if r.get("depart_time_local", "") >= WINDOW["start"]
    )
    if v2_in_window < 20:
        for pattern in COLD_START_V1_GLOB.split(","):
            for path in sorted(_glob.glob(pattern.strip())):
                try:
                    with open(path, newline="", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            row["_source"] = "v1"
                            rows.append(row)
                except Exception:
                    pass
    return rows


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 36.0   # global fallback
    s = sorted(values)
    idx = (pct / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def recommend(today: dt.date, today_wx_class: str, today_temp_c: float | None):
    history = load_all_history()
    weekday = today.strftime("%a")   # Mon, Tue, …
    target  = target_arrival_dt(today)
    cal_ctx = get_day_context(today)

    # Federal holiday → skip collection entirely
    if cal_ctx.is_federal_holiday and not FORCE_RUN:
        send_email(
            f"🚗 No commute — {cal_ctx.federal_holiday_name} ({today.strftime('%b %-d')})",
            f"Today is {cal_ctx.federal_holiday_name}. Script will not collect data.",
        )
        print(f"[recommend] Skipping: federal holiday ({cal_ctx.federal_holiday_name})")
        return

    # Filter history: exclude structurally abnormal days from model training
    # (federal holidays, DST transitions) but keep holiday_eve and cps_off days
    # because those patterns are real and worth learning
    clean_history = [
        r for r in history
        if r.get("exclude_from_model", "0") not in ("1", 1, True)
        or r.get("_source") == "v1"   # V1 rows predate the flag; keep them
    ]

    # Candidate departure times: 07:30 to 08:15 in 5-min steps
    candidates = []
    h, m = map(int, WINDOW["start"].split(":"))
    end_h, end_m = map(int, WINDOW["end"].split(":"))
    cur = dt.datetime(2000, 1, 1, h, m)
    end = dt.datetime(2000, 1, 1, end_h, end_m)
    while cur <= end:
        candidates.append(cur.strftime("%H:%M"))
        cur += dt.timedelta(minutes=WINDOW["step_min"])

    # Weather penalty map (minutes added relative to "clear" baseline)
    # Derived from V1 analysis; refined once V2 data accumulates
    WX_PENALTY = {
        "clear": 0, "partly_cloudy": -4, "overcast": 1,
        "fog": 0, "light_rain": 8, "heavy_rain": 12,
        "light_snow": 1, "heavy_snow": 6, "freezing_precip": 4,
    }
    wx_adj = WX_PENALTY.get(today_wx_class, 0)

    results = []
    for slot in candidates:
        # Filter history to same weekday + same slot (or close slots for V1 data)
        # For V1 data the latest slot is 07:40; we extrapolate cautiously for later ones
        same_day_vals = [
            float(r["drive_duration_minutes"])
            for r in clean_history
            if r.get("weekday") == weekday
            and r.get("drive_duration_minutes", "") not in ("", None)
            and float(r["drive_duration_minutes"]) > 5
            and abs(_slot_diff(r.get("depart_time_local", ""), slot)) <= 10
        ]
        v1_only = all(r.get("_source") == "v1" for r in history
                      if r.get("weekday") == weekday
                      and r.get("depart_time_local", "") == slot)
        p_drive = percentile(same_day_vals, RECOMMEND_PERCENTILE) + wx_adj

        depart_dt = LOCAL_TZ.localize(
            dt.datetime(today.year, today.month, today.day,
                        int(slot[:2]), int(slot[3:]))
        )
        est_arrival = depart_dt + dt.timedelta(minutes=p_drive)
        delta = (est_arrival - target).total_seconds() / 60   # + = late

        results.append({
            "slot": slot,
            "p_drive": p_drive,
            "est_arrival": est_arrival.strftime("%H:%M"),
            "delta_min": delta,
            "n_samples": len(same_day_vals),
            "v1_only": v1_only,
        })

    # Recommended = latest slot where P80 arrival ≤ target
    on_time = [r for r in results if r["delta_min"] <= 0]
    rec = on_time[-1] if on_time else results[0]   # fallback: earliest slot

    # Safe = P90 (one extra margin step)
    for r in results:
        p90 = percentile(
            [float(x["drive_duration_minutes"])
             for x in history
             if x.get("weekday") == weekday
             and x.get("drive_duration_minutes", "") not in ("", None)
             and float(x["drive_duration_minutes"]) > 5
             and abs(_slot_diff(x.get("depart_time_local", ""), r["slot"])) <= 10],
            90,
        ) + wx_adj
        r["p90_drive"] = p90
        r["safe_arrival"] = (
            LOCAL_TZ.localize(dt.datetime(today.year, today.month, today.day,
                                          int(r["slot"][:2]), int(r["slot"][3:])))
            + dt.timedelta(minutes=p90)
        ).strftime("%H:%M")

    safe_on_time = [r for r in results if
                    (LOCAL_TZ.localize(dt.datetime(today.year, today.month, today.day,
                                                    int(r["slot"][:2]), int(r["slot"][3:])))
                     + dt.timedelta(minutes=r["p90_drive"]) <= target)]
    safe = safe_on_time[-1] if safe_on_time else results[0]

    _send_recommend_email(today, weekday, today_wx_class, today_temp_c,
                          wx_adj, rec, safe, results, cal_ctx)


def _slot_diff(slot_a: str, slot_b: str) -> int:
    """Difference in minutes between two HH:MM strings."""
    try:
        a = int(slot_a[:2]) * 60 + int(slot_a[3:])
        b = int(slot_b[:2]) * 60 + int(slot_b[3:])
        return a - b
    except Exception:
        return 999


def _send_recommend_email(today, weekday, wx_class, temp_c, wx_adj,
                          rec, safe, all_results, cal_ctx=None):
    date_str  = today.strftime("%a, %b %-d")
    temp_str  = f"{temp_c:.0f}°C" if temp_c is not None else "—"

    # Special day note
    special_note = ""
    special_html = ""
    if cal_ctx:
        if cal_ctx.is_cps_off:
            special_note = f"\n🏫 CPS no school today ({cal_ctx.cps_off_reason}) — expect lighter traffic than usual."
            special_html = f"<p style='color:#0F6E56;font-size:13px;'>🏫 CPS no school ({cal_ctx.cps_off_reason}) — likely lighter traffic.</p>"
        if cal_ctx.is_holiday_eve:
            special_note += f"\n🗓️  Holiday eve — tomorrow is a federal holiday. Traffic often lighter today."
            special_html += "<p style='color:#185FA5;font-size:13px;'>🗓️ Holiday eve — traffic often lighter than a typical {weekday}.</p>"
        if cal_ctx.dst_transition:
            special_note += f"\n⏰ DST {cal_ctx.dst_transition} weekend — commute patterns may be off for 1–2 days."
            special_html += f"<p style='color:#BA7517;font-size:13px;'>⏰ DST {cal_ctx.dst_transition} — patterns may be slightly off today.</p>"
    date_str  = today.strftime("%a, %b %-d")
    temp_str  = f"{temp_c:.0f}°C" if temp_c is not None else "—"
    cold_note = ""
    if rec.get("v1_only") or rec["n_samples"] < 10:
        cold_note = (
            f"\n⚠️  Estimate based on {'V1 historical data' if rec.get('v1_only') else 'limited V2 data'} "
            f"({rec['n_samples']} samples). Confidence grows as V2 data accumulates."
        )

    subject = (
        f"🚗 Commute {date_str} — leave by {rec['slot']} "
        f"(safe: {safe['slot']})"
    )

    table_rows = "\n".join(
        f"  {r['slot']}  →  est. {r['est_arrival']}  "
        f"({'on time ✓' if r['delta_min'] <= 0 else f\"+{r['delta_min']:.0f} min late\"})  "
        f"[P80: {r['p_drive']:.0f} min, n={r['n_samples']}]"
        for r in all_results
    )

    body = f"""Good morning! Here's your I-290 commute forecast for {date_str}.

TODAY
  Day:       {weekday}
  Weather:   {wx_class}  {temp_str}
  Target:    arrive by {TARGET_ARRIVE_HM}
{special_note}
RECOMMENDATION
  Comfortable  →  leave at {rec['slot']}  (P80 arrival {rec['est_arrival']}, Δ{rec['delta_min']:+.0f} min)
  Safe         →  leave at {safe['slot']}  (P90 arrival {safe['safe_arrival']})
{cold_note}

FULL WINDOW BREAKDOWN
{table_rows}

Weather adjustment applied: {wx_adj:+d} min ({wx_class})
Data source: {'V2 only' if not rec.get('v1_only') else 'V1 cold-start bootstrap'}
"""

    # HTML version (nicer in email clients)
    rec_color  = "#1D9E75"
    safe_color = "#378ADD"
    html_rows  = "".join(
        f"<tr style='background:{'#f0faf5' if r['slot']==rec['slot'] else '#edf6ff' if r['slot']==safe['slot'] else 'white'};'>"
        f"<td style='padding:6px 12px;font-weight:{'700' if r['slot'] in (rec['slot'],safe['slot']) else '400'};'>{r['slot']}</td>"
        f"<td style='padding:6px 12px;'>{r['est_arrival']}</td>"
        f"<td style='padding:6px 12px;color:{'#1D9E75' if r['delta_min']<=0 else '#E24B4A'};'>"
        f"{'on time ✓' if r['delta_min']<=0 else f'+{r[\"delta_min\"]:.0f} min late'}</td>"
        f"<td style='padding:6px 12px;color:#888;font-size:12px;'>{r['p_drive']:.0f} min  (n={r['n_samples']})</td>"
        f"</tr>"
        for r in all_results
    )
    cold_html = (
        f"<p style='color:#BA7517;font-size:13px;'>⚠️ {cold_note.strip()}</p>"
        if cold_note else ""
    )
    html = f"""<html><body style='font-family:sans-serif;color:#2c2c2a;max-width:560px;'>
<h2 style='margin-bottom:4px;'>🚗 I-290 commute — {date_str}</h2>
<p style='color:#888;margin-top:0;'>Target arrival: {TARGET_ARRIVE_HM} &nbsp;|&nbsp; {weekday} &nbsp;|&nbsp; {wx_class} {temp_str}</p>

{special_html}<div style='display:flex;gap:16px;margin-bottom:16px;'>
  <div style='border-radius:8px;background:#e1f5ee;padding:12px 20px;flex:1;'>
    <div style='font-size:12px;color:#0F6E56;margin-bottom:4px;'>Comfortable</div>
    <div style='font-size:28px;font-weight:700;color:{rec_color};'>{rec['slot']}</div>
    <div style='font-size:13px;color:#1D9E75;'>P80 arrival {rec['est_arrival']}</div>
  </div>
  <div style='border-radius:8px;background:#e6f1fb;padding:12px 20px;flex:1;'>
    <div style='font-size:12px;color:#185FA5;margin-bottom:4px;'>Safe (P90)</div>
    <div style='font-size:28px;font-weight:700;color:{safe_color};'>{safe['slot']}</div>
    <div style='font-size:13px;color:#378ADD;'>P90 arrival {safe['safe_arrival']}</div>
  </div>
</div>

{cold_html}

<table style='width:100%;border-collapse:collapse;font-size:14px;'>
  <thead><tr style='border-bottom:1px solid #ddd;color:#888;font-size:12px;'>
    <th style='padding:6px 12px;text-align:left;'>Depart</th>
    <th style='padding:6px 12px;text-align:left;'>Est. arrival</th>
    <th style='padding:6px 12px;text-align:left;'>vs target</th>
    <th style='padding:6px 12px;text-align:left;'>Drive time</th>
  </tr></thead>
  <tbody>{html_rows}</tbody>
</table>
<p style='color:#aaa;font-size:11px;margin-top:12px;'>
  Weather adj: {wx_adj:+d} min &nbsp;|&nbsp; P{RECOMMEND_PERCENTILE} threshold &nbsp;|&nbsp;
  commute_v2
</p>
</body></html>"""

    send_email(subject, body, html)


# ─────────────────────────────────────────
#  COLLECT mode — write one row per slot
# ─────────────────────────────────────────
def collect_row(path: str, run_ts: str, when_local: dt.datetime, data: dict):
    dur_sec, dist_m, via, summary = parse_directions(data)
    dur_min = round((dur_sec or 0) / 60)

    depart_dt = when_local
    if dur_sec is not None:
        est_arr = depart_dt + dt.timedelta(seconds=dur_sec)
    else:
        est_arr = None
    target = target_arrival_dt(when_local.date())
    delta  = round((est_arr - target).total_seconds() / 60) if est_arr else ""
    est_arr_str = est_arr.strftime("%H:%M") if est_arr else ""

    lat, lon = coords_from_places(data, OD["origin_address"])
    if lat is None or lon is None:
        lat, lon = geocode(OD["origin_address"])

    if lat is not None and lon is not None:
        wx_time, wx_temp, wx_precip, wx_prob, wx_code, wx_wind = fetch_weather(lat, lon, when_local)
        wx_class = classify_weather(wx_code, wx_precip)
    else:
        wx_time = wx_temp = wx_precip = wx_prob = wx_code = wx_wind = None
        wx_class = "overcast"

    # Calendar context
    cal = context_as_row_fields(get_day_context(when_local.date()))

    row = [
        "v2",
        run_ts,
        when_local.strftime("%Y-%m-%d"),
        when_local.strftime("%a"),
        OD["origin_name"], OD["origin_address"],
        OD["destination_name"], OD["destination_address"],
        when_local.strftime("%H:%M"),
        int(when_local.timestamp()),
        dist_m if dist_m is not None else "",
        dur_sec if dur_sec is not None else "",
        dur_min,
        est_arr_str, delta,
        via, summary,
        wx_time or "", lat or "", lon or "",
        wx_temp or "", wx_precip or "", wx_prob or "",
        wx_code or "", wx_wind or "",
        wx_class,
        # Calendar columns
        cal["is_federal_holiday"], cal["federal_holiday_name"],
        cal["is_cps_off"], cal["cps_off_reason"],
        cal["is_holiday_eve"], cal["dst_transition"],
        cal["day_type"], cal["exclude_from_model"],
    ]
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)
    print(f"  wrote: {when_local.strftime('%H:%M')} → {est_arr_str}  ({dur_min} min, Δ{delta} min)")


# ─────────────────────────────────────────
#  Main
# ─────────────────────────────────────────
def main():
    now_local = dt.datetime.now(LOCAL_TZ)
    target_date = (
        dt.datetime.strptime(FORCE_DATE, "%Y-%m-%d").date()
        if FORCE_DATE else now_local.date()
    )

    if WEEKDAYS_ONLY and target_date.weekday() >= 5 and not FORCE_RUN:
        print("Skip: weekend")
        return
    if target_date > dt.datetime.strptime(RUN_END_DATE, "%Y-%m-%d").date() and not FORCE_RUN:
        print("Reached end date")
        return

    # ── RECOMMEND ──────────────────────────────────────────────────
    if SAMPLE_MODE in ("recommend", "both"):
        print(f"[recommend] Generating forecast for {target_date} …")
        # Get weather at origin for the 08:00 hour (representative for the commute)
        lat, lon = geocode(OD["origin_address"])
        wx_hour_dt = LOCAL_TZ.localize(
            dt.datetime(target_date.year, target_date.month, target_date.day, 8, 0)
        )
        _, temp, precip, _, wx_code, _ = fetch_weather(lat, lon, wx_hour_dt) if lat else (None,)*6
        wx_class = classify_weather(wx_code, precip)
        recommend(target_date, wx_class, temp)

    # ── COLLECT ────────────────────────────────────────────────────
    if SAMPLE_MODE in ("collect", "both"):
        date_str = target_date.strftime("%Y-%m-%d")
        path = csv_path(target_date)
        ensure_csv(path)

        expected_slots = sum(1 for _ in window_times(target_date))
        done = already_collected(path, date_str)
        if done >= expected_slots and not FORCE_RUN:
            print(f"Skip collect: already have {done}/{expected_slots} rows for {date_str}")
            return

        run_ts = now_local.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[collect] {date_str} ({target_date.strftime('%a')}) — {expected_slots} slots")

        for when_local in window_times(target_date):
            # In realtime mode, wait until the actual wall-clock time
            if SAMPLE_MODE == "collect" or True:  # always align to real time
                sleep_s = (when_local - dt.datetime.now(LOCAL_TZ)).total_seconds()
                if sleep_s > 30:
                    print(f"  waiting {sleep_s/60:.1f} min until {when_local.strftime('%H:%M')} …")
                    time.sleep(max(0, sleep_s))

            print(f"  sampling {when_local.strftime('%H:%M')} …", end=" ", flush=True)
            try:
                data = fetch_directions(
                    OD["origin_address"], OD["destination_address"], leave_now=True
                )
                collect_row(path, run_ts, when_local, data)
            except Exception as e:
                print(f"ERROR: {e}")

            time.sleep(SLEEP_BETWEEN)

        print(f"[collect] Done → {path}")


if __name__ == "__main__":
    main()
