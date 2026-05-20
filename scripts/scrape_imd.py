import os
import re
import csv
import sys
import smtplib

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone, timedelta, date

from bs4 import BeautifulSoup
from supabase import create_client, Client

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

STATE_ID = os.getenv("IMD_STATE_ID", "10")

DISTRICT_URL = (
    "https://mausam.imd.gov.in/imd_latest/"
    f"contents/districtwisewarnings_mc.php?id={STATE_ID}"
)


DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Email ─────────────────────────────────────────────────────
GMAIL_FROM = os.getenv("GMAIL_FROM", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_TO   = os.getenv("ALERT_EMAIL_TO", "")

# ── Supabase ──────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://odrvhelastdyozjejqss.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

DISTRICT_TABLE = "district_warnings"

# ── IST timezone (UTC+5:30) ───────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ── Colour maps ───────────────────────────────────────────────
HEX_TO_COLOR_NAME = {
    "#008000": "green",
    "#ffff00": "yellow",
    "#ffa500": "orange",
    "#ff0000": "red",
}

WARNING_COLOR_MAP = {
    "#008000": "No Warning",
    "#ffff00": "Watch",
    "#ffa500": "Alert",
    "#ff0000": "Warning",
}

# Severity rank for escalation comparison
COLOR_RANK = {
    "green":  0,
    "yellow": 1,
    "orange": 2,
    "red":    3,
}

# ── TPCODL District → Circle & Division mapping ───────────────
# Exact names from district_name_Circle_Division.csv
# IMD name KENDRAPARHA is normalized to KENDRAPARA at extraction time
TPCODL_MAP = {
    "ANUGUL": {
        "circles":   ["DHENKANAL"],
        "divisions": ["ANED, ANGUL", "TED, CHAINPAL"],
    },
    "CUTTACK": {
        "circles":   ["CUTTACK", "BBSR-1", "BBSR-2"],
        "divisions": ["AED, ATHAGARH", "BCDD-II, BBSR", "CDD-I, Cuttack",
                      "CDD-II, Cuttack", "CED, Cuttack", "KHED, Khurda", "SED, SALIPUR"],
    },
    "DHENKANAL": {
        "circles":   ["DHENKANAL"],
        "divisions": ["DED, DHENKANAL", "TED, CHAINPAL"],
    },
    "JAGATSINGHPUR": {
        "circles":   ["PARADEEP"],
        "divisions": ["JED, JAGATSINGHPUR", "PAED, PARADEEP"],
    },
    "KENDRAPARA": {
        "circles":   ["PARADEEP"],
        "divisions": ["KED-I, KENDRAPARA", "KED-II, MARSHAGHAI"],
    },
    "KHORDHA": {
        "circles":   ["BBSR1", "BBSR2"],
        "divisions": ["BCDD-I, BBSR", "BCDD-II, BBSR", "BED, BBSR",
                      "KHED, KHORDHA", "NYED, NAYAGARH", "NED, NIMAPARA", "BAED, BALUGAON"],
    },
    "NAYAGARH": {
        "circles":   ["BBSR2"],
        "divisions": ["NYED, NAYAGARH", "KHED, KHORDHA", "BAED, BALUGAON"],
    },
    "PURI": {
        "circles":   ["BBSR2", "BBSR1"],
        "divisions": ["PED, PURI", "BED, BBSR", "KHED, KHORDHA", "NED, NIMAPARA"],
    },
}

# IMD district name normalization — raw scraped name → canonical name
IMD_NAME_NORMALIZE = {
    "KENDRAPARHA": "KENDRAPARA",
}

# IMD uses 2 trailing spaces in aria-label for these districts
TPCODL_DISTRICTS_IMD = [
    "ANUGUL", "CUTTACK", "DHENKANAL", "JAGATSINGHPUR",
    "KENDRAPARHA", "KHORDHA", "NAYAGARH", "PURI",
]


# ─────────────────────────────────────────────────────────────
# CLEAN OLD FILES
# PNGs excluded — overwritten in-place each run.
# ─────────────────────────────────────────────────────────────

def clean_old_files():
    print("\n[scraper] Cleaning old files...")
    keep    = {".gitkeep"}
    deleted = 0
    for pattern in ["*.html", "*.csv", "*.json"]:
        for f in DATA_DIR.glob(pattern):
            if f.name in keep:
                continue
            try:
                f.unlink()
                deleted += 1
            except Exception as e:
                print(f"[scraper] Could not delete {f.name}: {e}")
    print(f"[scraper] Deleted {deleted} old file(s)")


# ─────────────────────────────────────────────────────────────
# COLOUR HELPERS
# ─────────────────────────────────────────────────────────────

def normalize_color(color: str) -> str:
    if not color:
        return ""
    color = color.strip().lower()
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color)
    if m:
        r, g, b = map(int, m.groups())
        return "#{:02x}{:02x}{:02x}".format(r, g, b)
    return color


def color_to_severity(color: str) -> str:
    c = normalize_color(color)
    for k, v in WARNING_COLOR_MAP.items():
        if c == k.lower():
            return v
    return f"Unknown ({c})"


def hex_to_color_name(hex_color: str) -> str:
    return HEX_TO_COLOR_NAME.get(normalize_color(hex_color), hex_color)


def marker_filename_to_color(href: str) -> str:
    m = re.search(r"map-marker-icon-png-(\w+)\.png", href, re.IGNORECASE)
    if not m:
        return ""
    color_map = {
        "green":  "#008000",
        "yellow": "#ffff00",
        "orange": "#ffa500",
        "red":    "#ff0000",
    }
    return color_map.get(m.group(1).lower(), "")


# ─────────────────────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────────────────────

def ist_now_human() -> str:
    """Return current IST time as 'Reported at HH:MM Hrs' string for email."""
    return datetime.now(IST).strftime("%H:%M")


def extract_time_only(issued_at_str: str) -> str:
    """
    Extract time part only from issued_at string.
    Input formats seen: '2026-05-17 1600', '2026-05-17 16:00', '1600'
    Returns: '16:00' or original string if unparseable.
    """
    if not issued_at_str:
        return "—"
    # Try extracting trailing 4-digit time e.g. '1600' or '0130'
    m = re.search(r"(\d{4})$", issued_at_str.strip())
    if m:
        t = m.group(1)
        return f"{t[:2]}:{t[2:]}"
    # Try HH:MM format already present
    m2 = re.search(r"(\d{1,2}:\d{2})", issued_at_str)
    if m2:
        return m2.group(1)
    return issued_at_str.strip()


def format_valid_upto(valid_upto_str: str) -> str:
    """
    Format valid_upto to HH:MM.
    Input formats: '1900', '19:00', '19'
    Returns: '19:00'
    """
    if not valid_upto_str:
        return "—"
    s = valid_upto_str.strip()
    if re.match(r"^\d{4}$", s):
        return f"{s[:2]}:{s[2:]}"
    if re.match(r"^\d{1,2}:\d{2}$", s):
        return s
    if re.match(r"^\d{1,2}$", s):
        return f"{int(s):02d}:00"
    return s


# ─────────────────────────────────────────────────────────────
# STATION ARIA-LABEL PARSER
# ─────────────────────────────────────────────────────────────

def parse_station_aria_label(aria_label: str) -> dict:
    soup_label = BeautifulSoup(aria_label, "html.parser")
    lines = [
        line.strip()
        for line in soup_label.get_text(separator="\n").splitlines()
        if line.strip()
    ]

    name         = lines[0] if lines else aria_label.strip()
    warn_m       = re.search(r"\b(No Warning|Watch|Alert|Warning)\b", aria_label)
    warning_text = warn_m.group(1) if warn_m else "Unknown"

    time_m   = re.search(
        r"Time of issue:\s*([\d-]+)\s*(?:</br>|<br\s*/?>|\s)([\d]+)\s*Hrs",
        aria_label, re.IGNORECASE,
    )
    issued_at  = f"{time_m.group(1)} {time_m.group(2)}" if time_m else ""
    valid_m    = re.search(r"Valid upto:\s*([\d]+)\s*Hrs", aria_label, re.IGNORECASE)
    valid_upto = valid_m.group(1) if valid_m else ""

    return {
        "name":      name,
        "issued_at": issued_at,
        "valid_upto": valid_upto,
        "warning_text": warning_text,
    }


# ─────────────────────────────────────────────────────────────
# PAGE LOADERS
# ─────────────────────────────────────────────────────────────

def _launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
        ],
    )


def parse_balloon_text(text: str) -> dict:
    """
    Parse balloon hover text from IMD district map into structured fields.

    Expected format (non-green districts):
        ANUGUL

        Light rain: < 5 mm/hr
        Light Thunderstorms with maximum surface wind speed less than 40 kmph
        Low cloud to ground Lightning probability ( < 30% probability of lightning occurrence)

        Time of issue: 2026-05-18
        2037 Hrs
        Valid upto: 2337 Hrs

    Green districts:
        CUTTACK

        No Warning

        Time of issue: 2026-05-18
        1900 Hrs
        Valid upto: 2200 Hrs
    """
    result = {
        "issued_at":            "",
        "valid_upto":           "",
        "rain_description":     None,
        "thunderstorm_desc":    None,
        "lightning_probability": None,
        "balloon_text":         text,
    }

    lines = [l.strip() for l in text.splitlines()]

    # issued_at: "Time of issue: 2026-05-18" on one line, then "2037 Hrs" on next
    for i, line in enumerate(lines):
        if line.lower().startswith("time of issue:"):
            date_part = line.split(":", 1)[1].strip()   # "2026-05-18"
            # Next non-empty line should be "HHMM Hrs"
            for j in range(i + 1, len(lines)):
                next_l = lines[j]
                m = re.match(r"^(\d{3,4})\s*Hrs?$", next_l, re.IGNORECASE)
                if m:
                    result["issued_at"] = f"{date_part} {m.group(1)}"
                    break
            break

    # valid_upto: "Valid upto: 2337 Hrs"
    for line in lines:
        m = re.match(r"Valid upto:\s*(\d{3,4})\s*Hrs?", line, re.IGNORECASE)
        if m:
            result["valid_upto"] = m.group(1)
            break

    # weather description lines
    for line in lines:
        ll = line.lower()
        if ("rain:" in ll or "mm/hr" in ll) and result["rain_description"] is None:
            result["rain_description"] = line
        elif ("thunderstorm" in ll or "wind speed" in ll) and result["thunderstorm_desc"] is None:
            result["thunderstorm_desc"] = line
        elif "lightning probability" in ll and result["lightning_probability"] is None:
            result["lightning_probability"] = line

    return result


def load_district_page(url: str, screenshot_name: str) -> tuple[str, dict]:
    """
    Load the district warnings page, take a screenshot, then hover each of
    the 8 TPCODL districts to capture balloon data.

    Returns:
        (html_content, balloon_data_by_canonical_name)
        balloon_data keys: canonical district name (e.g. "KENDRAPARA")
        balloon_data values: dict from parse_balloon_text()
    """
    print(f"\n[scraper] Loading district page: {url}")
    balloon_data: dict = {}

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000},
            locale="en-IN",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: district page load timed out — continuing")

        try:
            page.wait_for_selector("svg", timeout=20000)
            page.wait_for_timeout(5000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: SVG not detected on district page")

        screenshot_path = DATA_DIR / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[scraper] Screenshot saved → {screenshot_path.name}")

        # ── Balloon hover for 8 TPCODL districts ─────────────────
        print("[scraper] Hovering TPCODL districts for balloon data...")
        for imd_name in TPCODL_DISTRICTS_IMD:
            # IMD appends 2 trailing spaces to aria-label
            selector = f"path[aria-label='{imd_name}  ']"
            try:
                path_el = page.query_selector(selector)
                if not path_el:
                    print(f"[scraper]   {imd_name}: path element not found — skipping")
                    continue
                path_el.scroll_into_view_if_needed()
                path_el.hover()
                page.wait_for_timeout(800)

                balloon_el = page.query_selector(".amcharts-balloon-div")
                if balloon_el and balloon_el.is_visible():
                    raw_text = balloon_el.inner_text().strip()
                    parsed   = parse_balloon_text(raw_text)
                    # Store under canonical name
                    canonical = IMD_NAME_NORMALIZE.get(imd_name, imd_name)
                    balloon_data[canonical] = parsed
                    print(
                        f"[scraper]   {canonical}: issued={parsed['issued_at']} "
                        f"valid={parsed['valid_upto']} "
                        f"rain={bool(parsed['rain_description'])} "
                        f"thunder={bool(parsed['thunderstorm_desc'])} "
                        f"lightning={bool(parsed['lightning_probability'])}"
                    )
                else:
                    print(f"[scraper]   {imd_name}: balloon not visible after hover")
            except Exception as e:
                print(f"[scraper]   {imd_name}: hover error — {e}")

        html = page.content()
        browser.close()

    print(f"[scraper] Balloon data captured for {len(balloon_data)} district(s)")
    return html, balloon_data


def take_escalated_hover_screenshots(escalated_names: list) -> list[Path]:
    """
    Re-open the district page, hover each escalated district, and save a full-page
    screenshot with the balloon visible. Only called when there are escalations.

    Returns list of Path objects for the saved PNGs (to attach to email).
    """
    if not escalated_names:
        return []

    print(f"\n[scraper] Taking hover screenshots for escalated districts: {escalated_names}")
    saved: list[Path] = []

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000},
            locale="en-IN",
        )
        page = context.new_page()

        try:
            page.goto(DISTRICT_URL, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: district page reload timed out for hover screenshots")

        try:
            page.wait_for_selector("svg", timeout=20000)
            page.wait_for_timeout(5000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: SVG not ready for hover screenshots")

        for canonical_name in escalated_names:
            # Convert canonical back to IMD name for the aria-label selector
            imd_name = next(
                (k for k, v in IMD_NAME_NORMALIZE.items() if v == canonical_name),
                canonical_name,
            )
            selector = f"path[aria-label='{imd_name}  ']"
            try:
                path_el = page.query_selector(selector)
                if not path_el:
                    print(f"[scraper]   {canonical_name}: path not found for screenshot")
                    continue
                path_el.scroll_into_view_if_needed()
                path_el.hover()
                page.wait_for_timeout(800)

                balloon_el = page.query_selector(".amcharts-balloon-div")
                if not (balloon_el and balloon_el.is_visible()):
                    print(f"[scraper]   {canonical_name}: balloon not visible — skipping screenshot")
                    continue

                fname = DATA_DIR / f"district_hover_{canonical_name.lower()}_{STATE_ID}.png"
                page.screenshot(path=str(fname), full_page=True)
                saved.append(fname)
                print(f"[scraper]   Saved hover screenshot → {fname.name}")

            except Exception as e:
                print(f"[scraper]   {canonical_name}: hover screenshot error — {e}")

        browser.close()

    return saved


# ─────────────────────────────────────────────────────────────
# EXTRACTION — DISTRICTS
# ─────────────────────────────────────────────────────────────

def extract_district_records(html: str) -> list:
    """
    Extract district warning records from the SVG map HTML.
    Scoped to only the 8 TPCODL districts — non-TPCODL districts are ignored.
    Only extracts: name, warning_color, severity.
    issued_at / valid_upto + weather fields are filled later by enrich_district_from_balloons().
    Applies IMD_NAME_NORMALIZE to fix known name mismatches (e.g. KENDRAPARHA → KENDRAPARA).
    """
    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()

    # Canonical names we care about (8 TPCODL districts)
    TPCODL_CANONICAL = set(TPCODL_MAP.keys())

    def _make_record(label: str, fill: str) -> dict | None:
        raw_name  = label.strip().upper()
        canonical = IMD_NAME_NORMALIZE.get(raw_name, raw_name)
        if canonical not in TPCODL_CANONICAL:
            return None                         # skip non-TPCODL district
        hex_color = normalize_color(fill)
        return {
            "type":                  "district",
            "name":                  canonical,
            "warning_color":         hex_to_color_name(hex_color),
            "severity":              color_to_severity(fill),
            "issued_at":             "",
            "valid_upto":            "",
            "balloon_text":          None,
            "rain_description":      None,
            "thunderstorm_desc":     None,
            "lightning_probability": None,
        }

    # Primary: amcharts-map-area SVG paths
    for path in soup.select("svg path.amcharts-map-area"):
        label = (path.get("aria-label") or "").strip()
        fill  = (path.get("fill") or "").strip()
        canonical = IMD_NAME_NORMALIZE.get(label.upper(), label.upper())
        if label and fill and canonical not in seen:
            rec = _make_record(label, fill)
            if rec:
                seen.add(canonical)
                records.append(rec)

    # Fallback: any SVG path with aria-label + fill
    if not records:
        for path in soup.select("svg path"):
            label = (path.get("aria-label") or path.get("title") or "").strip()
            fill  = (path.get("fill") or path.get("stroke") or "").strip()
            canonical = IMD_NAME_NORMALIZE.get(label.upper(), label.upper())
            if label and fill and canonical not in seen:
                rec = _make_record(label, fill)
                if rec:
                    seen.add(canonical)
                    records.append(rec)

    print(f"[scraper] TPCODL district records extracted: {len(records)}/8")
    return records


# ─────────────────────────────────────────────────────────────
# ENRICH DISTRICTS with balloon hover data (issued_at, valid_upto,
# rain_description, thunderstorm_desc, lightning_probability)
# ─────────────────────────────────────────────────────────────

def enrich_district_from_balloons(district_records: list, balloon_data: dict) -> list:
    """
    Apply balloon-extracted data directly to the 8 TPCODL district records.
    Since district_records is now scoped to TPCODL only, every record should
    have a matching balloon entry. Missing entries leave times empty.
    """
    for r in district_records:
        name = r["name"].upper()
        b    = balloon_data.get(name)
        if b:
            r["issued_at"]             = b.get("issued_at", "")
            r["valid_upto"]            = b.get("valid_upto", "")
            r["balloon_text"]          = b.get("balloon_text")
            r["rain_description"]      = b.get("rain_description")
            r["thunderstorm_desc"]     = b.get("thunderstorm_desc")
            r["lightning_probability"] = b.get("lightning_probability")
        else:
            print(f"[scraper]   WARNING: no balloon data for {name} — times will be empty")

    enriched = sum(1 for r in district_records if r.get("issued_at"))
    print(f"[scraper] Balloon enrichment: {enriched}/{len(district_records)} districts have times")
    return district_records


# ─────────────────────────────────────────────────────────────
# SUPABASE — READ PREVIOUS STATE
# ─────────────────────────────────────────────────────────────

def read_previous_state(sb: Client) -> dict:
    """
    Returns { district_name_upper: color_rank_int } for all rows in
    district_warnings for this state_id.
    If table is empty (cold start), returns {} — callers treat missing
    entries as rank 0 (green).
    FIX: cast STATE_ID to int to match int4 column type.
    """
    try:
        resp = (
            sb.table(DISTRICT_TABLE)
            .select("name, warning_color")
            .eq("state_id", int(STATE_ID))      # ← FIX: int cast matches int4 column
            .execute()
        )
        prev = {}
        for row in (resp.data or []):
            name  = (row.get("name") or "").strip().upper()
            color = (row.get("warning_color") or "green").strip().lower()
            prev[name] = COLOR_RANK.get(color, 0)
        print(f"[scraper] Previous state loaded: {len(prev)} district(s)")
        return prev
    except Exception as e:
        print(f"[scraper] WARNING: could not read previous state: {e}")
        return {}


# ─────────────────────────────────────────────────────────────
# TPCODL ESCALATION CHECK
# ─────────────────────────────────────────────────────────────

def check_tpcodl_escalation(district_records: list, prev_state: dict) -> list:
    """
    Returns list of district records that:
      1. Are in TPCODL_MAP (one of the 8 TPCODL districts)
      2. Have warning_color of orange or red (rank >= 2)
      3. Have a HIGHER rank than the previous scan (strict escalation)

    Cold-start behaviour: missing prev_state entry treated as rank 0 (green),
    so any orange/red on first run correctly triggers an email.
    """
    escalated = []
    for r in district_records:
        name = r["name"].upper()
        if name not in TPCODL_MAP:
            continue
        new_rank  = COLOR_RANK.get(r["warning_color"].lower(), 0)
        prev_rank = prev_state.get(name, 0)  # default green if unseen
        if new_rank >= 2 and new_rank > prev_rank:
            escalated.append(r)
            print(
                f"[scraper] ESCALATION: {name}  "
                f"{list(COLOR_RANK.keys())[prev_rank]} → {r['warning_color']}  "
                f"(rank {prev_rank} → {new_rank})"
            )

    if not escalated:
        print("[scraper] No TPCODL escalations this scan")

    return escalated


# ─────────────────────────────────────────────────────────────
# EMAIL BUILDERS
# ─────────────────────────────────────────────────────────────

COLOR_BADGE_CSS = {
    "red":    ("background:#cc0000;color:#fff;",  ),
    "orange": ("background:#e07000;color:#fff;",  ),
    "yellow": ("background:#b8b800;color:#fff;",  ),
    "green":  ("background:#008000;color:#fff;",  ),
}


def _badge(color: str, label: str) -> str:
    style = COLOR_BADGE_CSS.get(color.lower(), ("background:#ccc;color:#000;",))[0]
    return (
        f'<span style="{style}padding:2px 10px;border-radius:4px;'
        f'font-weight:bold;font-size:12px;letter-spacing:.5px;">'
        f'{label.upper()}</span>'
    )


def _build_district_table(escalated: list) -> str:
    """Table 1: District, Severity, Issued At, Valid Upto — clean, no weather details."""
    rows = ""
    for r in escalated:
        badge          = _badge(r["warning_color"], r["severity"])
        issued_display = extract_time_only(r.get("issued_at", ""))
        valid_display  = format_valid_upto(r.get("valid_upto", ""))
        rows += (
            f"<tr>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-weight:700;font-size:13px;color:#1B3A6B;'>"
            f"<span style='display:inline-block;width:8px;height:8px;border-radius:50%;"
            f"background:#e07000;margin-right:7px;vertical-align:middle;'></span>"
            f"{r['name'].title()}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"text-align:center;'>{badge}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-size:13px;color:#444;text-align:center;'>{issued_display} Hrs</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-size:13px;color:#444;text-align:center;'>{valid_display} Hrs</td>"
            f"</tr>"
        )
    return f"""
    <table style="border-collapse:collapse;width:100%;font-family:'Segoe UI',Arial,sans-serif;
                  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);">
      <thead>
        <tr style="background:#1B3A6B;color:#fff;">
          <th style="padding:10px 14px;text-align:left;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">District</th>
          <th style="padding:10px 14px;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">Severity</th>
          <th style="padding:10px 14px;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">Issued At</th>
          <th style="padding:10px 14px;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">Valid Upto</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_weather_table(escalated: list) -> str:
    """
    Table 3 (new): District Weather Details — Rain, Wind Speed, Lightning Probability.
    Only included for districts that have at least one weather field populated.
    """
    # Filter to districts that actually have weather data
    weather_rows = [
        r for r in escalated
        if r.get("rain_description") or r.get("thunderstorm_desc") or r.get("lightning_probability")
    ]
    if not weather_rows:
        return ""

    WEATHER_ICON = {
        "rain":        "🌧",
        "thunderstorm": "⛈",
        "lightning":   "⚡",
    }

    rows = ""
    for r in weather_rows:
        rain   = r.get("rain_description")     or "—"
        thunder = r.get("thunderstorm_desc")   or "—"
        light  = r.get("lightning_probability") or "—"

        # Extract just the key figure from each field for cleaner display
        # e.g. "Moderate rain: 5-15 mm/hr" → keep as-is but strip leading label
        def _clean(text: str) -> str:
            if text == "—":
                return text
            # Remove duplicate prefix patterns like "Light rain: " keeping rest
            for prefix in ["Light rain:", "Moderate rain:", "Heavy rain:",
                            "Very heavy rain:", "Extremely heavy rain:"]:
                if text.lower().startswith(prefix.lower()):
                    return text[len(prefix):].strip()
            return text

        rain_clean    = _clean(rain)
        thunder_clean = _clean(thunder)
        light_clean   = _clean(light)

        rows += (
            f"<tr>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-weight:700;font-size:13px;color:#1B3A6B;vertical-align:top;'>"
            f"<span style='display:inline-block;width:8px;height:8px;border-radius:50%;"
            f"background:#e07000;margin-right:7px;vertical-align:middle;'></span>"
            f"{r['name'].title()}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-size:12px;color:#333;vertical-align:top;'>"
            f"{WEATHER_ICON['rain']} {rain_clean}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-size:12px;color:#333;vertical-align:top;'>"
            f"{WEATHER_ICON['thunderstorm']} {thunder_clean}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef0f4;"
            f"font-size:12px;color:#333;vertical-align:top;'>"
            f"{WEATHER_ICON['lightning']} {light_clean}</td>"
            f"</tr>"
        )

    return f"""
    <table style="border-collapse:collapse;width:100%;font-family:'Segoe UI',Arial,sans-serif;
                  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);">
      <thead>
        <tr style="background:#2a5298;color:#fff;">
          <th style="padding:10px 14px;text-align:left;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">District</th>
          <th style="padding:10px 14px;text-align:left;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">🌧 Rainfall</th>
          <th style="padding:10px 14px;text-align:left;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">⛈ Wind / Thunderstorm</th>
          <th style="padding:10px 14px;text-align:left;font-size:12px;font-weight:600;
                     letter-spacing:.5px;text-transform:uppercase;">⚡ Lightning Risk</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_ops_table(escalated: list) -> str:
    """
    Table 2: Affected Circles & Divisions for each escalated district.
    Uses exact names from TPCODL_MAP.
    """
    rows = ""
    for r in escalated:
        name    = r["name"].upper()
        mapping = TPCODL_MAP.get(name, {})
        circles   = ", ".join(mapping.get("circles",   ["-"]))
        divisions = ", ".join(mapping.get("divisions", ["-"]))
        rows += (
            f"<tr>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"font-weight:600;font-size:13px;'>{r['name'].title()}</td>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"font-size:12px;'>{circles}</td>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"font-size:12px;'>{divisions}</td>"
            f"</tr>"
        )
    return f"""
    <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;">
      <thead>
        <tr style="background:#1B3A6B;color:#fff;">
          <th style="padding:8px 12px;text-align:left;font-size:13px;">District</th>
          <th style="padding:8px 12px;text-align:left;font-size:13px;">Circles Affected</th>
          <th style="padding:8px 12px;text-align:left;font-size:13px;">Divisions Affected</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def _build_kalabaisakhi_summary(escalated: list) -> str:
    """
    Kalabaisakhi timing cards — one card per escalated district.
    Shows narrative text on left, start/end times on right (inspired by reference design).
    """
    cards = []
    for r in escalated:
        name      = r["name"].upper()
        mapping   = TPCODL_MAP.get(name, {})
        divisions = ", ".join(mapping.get("divisions", ["-"]))
        issued    = extract_time_only(r.get("issued_at", ""))
        valid     = format_valid_upto(r.get("valid_upto", ""))
        cards.append(f"""
        <div style="display:table;width:100%;border-collapse:collapse;margin-bottom:8px;">
          <div style="display:table-row;">
            <div style="display:table-cell;vertical-align:middle;padding:2px 16px 2px 0;width:65%;">
              <span style="font-size:13px;color:#5a3800;line-height:1.6;">
                Start time of <strong>KALABAISAKHI</strong> : <strong>{issued} Hrs</strong>
                and expected stop time is <strong>{valid} Hrs</strong>
                in <strong>{r['name'].title()} District</strong>
                and associated Divisions ({divisions}).
              </span>
            </div>
            <div style="display:table-cell;vertical-align:middle;padding:2px 0;width:35%;">
              <table style="border-collapse:collapse;width:100%;">
                <tr>
                  <td style="padding:4px 10px;border-bottom:1px dashed #e0c070;">
                    <span style="font-size:10px;color:#8a6000;text-transform:uppercase;
                                 letter-spacing:.4px;">Start Time</span><br>
                    <strong style="font-size:16px;color:#c05000;">{issued} Hrs</strong>
                  </td>
                </tr>
                <tr>
                  <td style="padding:4px 10px;">
                    <span style="font-size:10px;color:#8a6000;text-transform:uppercase;
                                 letter-spacing:.4px;">Expected End Time</span><br>
                    <strong style="font-size:16px;color:#c05000;">{valid} Hrs</strong>
                  </td>
                </tr>
              </table>
            </div>
          </div>
        </div>""")
    return "".join(cards)


def _build_kalabaisakhi_summary_plain(escalated: list) -> str:
    """Plain-text version of Kalabaisakhi summary for email plain part."""
    lines = []
    for r in escalated:
        name      = r["name"].upper()
        mapping   = TPCODL_MAP.get(name, {})
        divisions = ", ".join(mapping.get("divisions", ["-"]))
        issued    = extract_time_only(r.get("issued_at", ""))
        valid     = format_valid_upto(r.get("valid_upto", ""))
        lines.append(
            f"  • Start time of KALABAISAKHI : {issued} Hrs and expected stop time "
            f"is {valid} Hrs in {r['name'].title()} District "
            f"and associated Divisions ({divisions})."
        )
    return "\n".join(lines)


def _subject_counts(escalated: list) -> tuple[int, int]:
    """Return (unique_circle_count, unique_division_count) across all escalated districts."""
    circles   = set()
    divisions = set()
    for r in escalated:
        name    = r["name"].upper()
        mapping = TPCODL_MAP.get(name, {})
        for c in mapping.get("circles",   []):
            circles.add(c)
        for d in mapping.get("divisions", []):
            divisions.add(d)
    return len(circles), len(divisions)


def build_email_plain(escalated: list, reported_at_ist: str) -> str:
    kalabaisakhi_plain = _build_kalabaisakhi_summary_plain(escalated)
    lines = [
        "IMD TPCODL WARNING ALERT",
        f"Reported at: {reported_at_ist} Hrs (IST)",
        "=" * 60,
        "",
        "DISTRICT WARNING STATUS",
        "-" * 40,
    ]
    for r in escalated:
        issued = extract_time_only(r.get("issued_at", ""))
        valid  = format_valid_upto(r.get("valid_upto", ""))
        lines.append(
            f"  {r['name']}  |  {r['severity'].upper()}  ({r['warning_color']})"
            f"  |  Issued: {issued} Hrs  Valid upto: {valid} Hrs"
        )
    lines += [
        "",
        "KALABAISAKHI TIMING SUMMARY",
        "-" * 40,
        kalabaisakhi_plain,
        "",
        "DISTRICT WEATHER DETAILS",
        "-" * 40,
    ]
    for r in escalated:
        if r.get("rain_description") or r.get("thunderstorm_desc") or r.get("lightning_probability"):
            lines.append(f"  {r['name']}")
            if r.get("rain_description"):
                lines.append(f"    🌧 Rain     : {r['rain_description']}")
            if r.get("thunderstorm_desc"):
                lines.append(f"    ⛈ Wind/Storm: {r['thunderstorm_desc']}")
            if r.get("lightning_probability"):
                lines.append(f"    ⚡ Lightning : {r['lightning_probability']}")
    lines += [
        "",
        "AFFECTED TPCODL CIRCLES & DIVISIONS",
        "-" * 40,
    ]
    for r in escalated:
        name    = r["name"].upper()
        mapping = TPCODL_MAP.get(name, {})
        circles   = ", ".join(mapping.get("circles",   ["-"]))
        divisions = ", ".join(mapping.get("divisions", ["-"]))
        lines.append(f"  {r['name']}")
        lines.append(f"    Circles  : {circles}")
        lines.append(f"    Divisions: {divisions}")
    lines += ["", "=" * 60, "Source: IMD Nowcast Warning system"]
    return "\n".join(lines)


def build_email_html(escalated: list, reported_at_ist: str) -> str:
    has_red     = any(r["warning_color"].lower() == "red"    for r in escalated)
    header_bg   = "#cc0000" if has_red else "#e07000"
    sev_label   = "WARNING" if has_red else "ALERT"
    sev_emoji   = "⛔" if has_red else "🚨"
    alert_badge_bg = "#cc0000" if has_red else "#e07000"

    district_table   = _build_district_table(escalated)
    weather_table    = _build_weather_table(escalated)
    kala_summary     = _build_kalabaisakhi_summary(escalated)
    ops_table        = _build_ops_table(escalated)

    district_names   = ", ".join(r["name"].title() for r in escalated)
    n_districts      = len(escalated)

    # ── Weather summary stat cards (top of email, like reference image) ──────
    # Aggregate: take first escalated district that has data for each field
    first_weather = next((r for r in escalated if r.get("rain_description")), None)
    stat_cards = ""
    if first_weather:
        rain_val    = first_weather.get("rain_description",     "") or ""
        thunder_val = first_weather.get("thunderstorm_desc",    "") or ""
        light_val   = first_weather.get("lightning_probability","") or ""

        def _stat_card(icon, label, value, color):
            return f"""
            <td style="padding:0 8px;text-align:center;vertical-align:top;width:25%;">
              <div style="background:#fff;border-radius:8px;padding:12px 8px;
                          border-top:3px solid {color};box-shadow:0 1px 4px rgba(0,0,0,.07);">
                <div style="font-size:22px;margin-bottom:4px;">{icon}</div>
                <div style="font-size:10px;color:{color};font-weight:700;
                            text-transform:uppercase;letter-spacing:.5px;">{label}</div>
                <div style="font-size:12px;color:#333;margin-top:4px;line-height:1.4;">{value}</div>
              </div>
            </td>"""

        # extract valid_upto from first escalated district for the "Valid Till" card
        valid_display = format_valid_upto(escalated[0].get("valid_upto",""))
        today_str = date.today().strftime("%-d %b %Y")

        stat_cards = f"""
      <div style="margin-bottom:20px;">
        <table style="border-collapse:separate;border-spacing:0;width:100%;">
          <tr>
            {_stat_card("🌧","Rainfall", rain_val or "—", "#1a7abf")}
            {_stat_card("⛈","Wind / Storm", thunder_val or "—", "#e07000")}
            {_stat_card("⚡","Lightning Risk", light_val or "—", "#e07000")}
            {_stat_card("🕐","Valid Till", f"<strong style='font-size:15px;'>{valid_display} Hrs</strong><br><span style='font-size:10px;color:#888;'>{today_str}</span>", "#555")}
          </tr>
        </table>
      </div>"""

    weather_section = ""
    if weather_table:
        weather_section = f"""
      <h3 style="margin:24px 0 10px;color:#1B3A6B;font-size:13px;font-weight:700;
                 letter-spacing:.5px;text-transform:uppercase;
                 display:flex;align-items:center;gap:6px;">
        <span style="display:inline-block;width:3px;height:14px;background:#e07000;
                     border-radius:2px;margin-right:6px;vertical-align:middle;"></span>
        DISTRICT WEATHER DETAILS
      </h3>
      {weather_table}"""

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f7;margin:0;padding:0;">
  <div style="max-width:780px;margin:24px auto;background:#fff;border-radius:10px;
              box-shadow:0 4px 16px rgba(0,0,0,.10);overflow:hidden;">

    <!-- Header -->
    <div style="background:{header_bg};padding:22px 28px;position:relative;">
      <table style="border-collapse:collapse;width:100%;">
        <tr>
          <td style="vertical-align:middle;">
            <div style="font-size:11px;color:rgba(255,255,255,.7);
                        text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;">
              {sev_emoji} IMD Nowcast Warning (TPCODL)
            </div>
            <div style="font-size:20px;font-weight:700;color:#fff;line-height:1.2;">
              {sev_label} — IMD Nowcast Warning (TPCODL)
            </div>
            <div style="margin-top:8px;font-size:12px;color:rgba(255,255,255,.85);">
              <span>📍 District{'s' if n_districts > 1 else ''}: {district_names}</span>
              &nbsp;&nbsp;
              <span>🕐 Reported at: {reported_at_ist} Hrs (IST)</span>
            </div>
          </td>
          <td style="vertical-align:middle;text-align:right;padding-left:16px;white-space:nowrap;">
            <div style="display:inline-block;background:rgba(255,255,255,.2);
                        border:2px solid rgba(255,255,255,.6);border-radius:8px;
                        padding:8px 14px;text-align:center;">
              <div style="font-size:9px;color:rgba(255,255,255,.8);
                          text-transform:uppercase;letter-spacing:.6px;">⚠</div>
              <div style="font-size:13px;font-weight:800;color:#fff;
                          letter-spacing:.5px;">{sev_label}</div>
            </div>
          </td>
        </tr>
      </table>
    </div>

    <div style="padding:24px 28px;background:#f8f9fc;">

      <!-- Stat Cards -->
      {stat_cards}

      <!-- Table 1: District Warning Status -->
      <h3 style="margin:0 0 10px;color:#1B3A6B;font-size:13px;font-weight:700;
                 letter-spacing:.5px;text-transform:uppercase;">
        <span style="display:inline-block;width:3px;height:14px;background:#1B3A6B;
                     border-radius:2px;margin-right:8px;vertical-align:middle;"></span>
        DISTRICT WARNING STATUS
      </h3>
      {district_table}

      <!-- Kalabaisakhi Box -->
      <div style="margin-top:20px;padding:16px 18px;background:#fffbee;
                  border:1px solid #f0d060;border-left:4px solid #e07000;border-radius:6px;">
        <div style="font-size:12px;font-weight:700;color:#8a5000;
                    text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;">
          🕐 KALABAISAKHI TIMING SUMMARY
        </div>
        {kala_summary}
      </div>

      <!-- Table 3: District Weather Details (new) -->
      {weather_section}

      <!-- Table 2: Affected Circles & Divisions -->
      <h3 style="margin:24px 0 10px;color:#1B3A6B;font-size:13px;font-weight:700;
                 letter-spacing:.5px;text-transform:uppercase;">
        <span style="display:inline-block;width:3px;height:14px;background:#1B3A6B;
                     border-radius:2px;margin-right:8px;vertical-align:middle;"></span>
        AFFECTED TPCODL CIRCLES &amp; DIVISIONS
      </h3>
      {ops_table}

      <!-- Footer -->
      <div style="margin-top:28px;padding-top:16px;border-top:1px solid #e4e8f0;">
        <table style="border-collapse:collapse;width:100%;">
          <tr>
            <td style="vertical-align:top;padding:0 12px 0 0;width:33%;font-size:11px;color:#888;">
              <div style="font-weight:600;color:#555;margin-bottom:3px;">📄 Source</div>
              IMD Nowcast Warning system
            </td>
            <td style="vertical-align:top;padding:0 12px;width:33%;font-size:11px;color:#888;
                       border-left:1px solid #eee;border-right:1px solid #eee;">
              <div style="font-weight:600;color:#555;margin-bottom:3px;">🔄 Auto-updated</div>
              Every 15 min via GitHub Actions
            </td>
            <td style="vertical-align:top;padding:0 0 0 12px;width:33%;font-size:11px;color:#888;">
              <div style="font-weight:600;color:#555;margin-bottom:3px;">📢 Generated on</div>
              Alert / Warning raised by IMD
            </td>
          </tr>
        </table>
        <div style="margin-top:12px;font-size:10px;color:#bbb;text-align:center;">
          Generated At: {reported_at_ist} IST &nbsp;|&nbsp;
          Next Refresh: ~15 min &nbsp;|&nbsp;
          Email triggered on escalation to orange/red only
        </div>
      </div>

    </div>
  </div>
</body>
</html>"""


def send_alert_email(escalated: list, reported_at_ist: str, hover_pngs: list[Path] = None):
    if not GMAIL_FROM or not GMAIL_PASS or not EMAIL_TO:
        print("[scraper] Email env vars not set — skipping alert email")
        return

    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]

    # Subject: severity + district names + circle/division counts
    has_red       = any(r["warning_color"].lower() == "red" for r in escalated)
    sev_label     = "⛔ WARNING" if has_red else "🚨 ALERT"
    n_circles, n_divs = _subject_counts(escalated)
    district_names = ", ".join(r["name"].title() for r in escalated)
    subject = (
        f"IMD {sev_label} — {district_names} — "
        f"{n_circles} Circle{'s' if n_circles != 1 else ''}, "
        f"{n_divs} Division{'s' if n_divs != 1 else ''} Affected"
    )

    msg = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(build_email_plain(escalated, reported_at_ist), "plain"))
    alt_part.attach(MIMEText(build_email_html(escalated,  reported_at_ist), "html"))
    msg.attach(alt_part)

    # Attachments: district overview PNG + per-escalated-district hover PNGs
    attach_paths = [DATA_DIR / f"district_warning_{STATE_ID}.png"] + (hover_pngs or [])
    for attach_path in attach_paths:
        if not attach_path.exists():
            print(f"[scraper] Attachment not found (skipping): {attach_path.name}")
            continue
        with open(attach_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attach_path.name}"',
        )
        msg.attach(part)
        print(f"[scraper] Attached: {attach_path.name}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, GMAIL_PASS)
            server.sendmail(GMAIL_FROM, recipients, msg.as_string())
        print(f"[scraper] Alert email sent → {', '.join(recipients)}")
        print(f"[scraper] Subject: {subject}")
    except Exception as e:
        print(f"[scraper] ERROR sending email: {e}")


# ─────────────────────────────────────────────────────────────
# SUPABASE — UPSERT ALL ROWS EVERY RUN
# ─────────────────────────────────────────────────────────────

def upsert_district_warnings(
    district_records: list,
    meta: dict,
    sb: Client,
):
    """
    Always upserts ALL 30 district rows every run.
    This ensures the table always reflects current live IMD state,
    including green phases — preventing false re-escalation emails
    when a district cycles orange → green → orange.

    FIX vs previous version:
      - Removed the 'only upsert changed rows' filter
      - Cast state_id to int to match int4 column
    """
    rows_to_upsert = []
    for r in district_records:
        rows_to_upsert.append({
            "scraped_at":             meta["scraped_at"],
            "state_id":               int(meta["state_id"]),   # ← FIX: int cast matches int4
            "type":                   r.get("type", "district"),
            "name":                   r["name"],
            "warning_color":          r.get("warning_color", ""),
            "severity":               r.get("severity", ""),
            "issued_at":              r.get("issued_at") or None,
            "valid_upto":             r.get("valid_upto") or None,
            "balloon_text":           r.get("balloon_text") or None,
            "rain_description":       r.get("rain_description") or None,
            "thunderstorm_desc":      r.get("thunderstorm_desc") or None,
            "lightning_probability":  r.get("lightning_probability") or None,
        })

    try:
        sb.table(DISTRICT_TABLE).upsert(
            rows_to_upsert,
            on_conflict="state_id,name",
        ).execute()
        print(f"[scraper] Supabase: upserted {len(rows_to_upsert)} rows → '{DISTRICT_TABLE}'")
    except Exception as e:
        print(f"[scraper] Supabase ERROR on upsert: {e}")


# ─────────────────────────────────────────────────────────────
# FILE SAVERS
# ─────────────────────────────────────────────────────────────

def save_csv(filename: str, records: list, meta: dict, extra_fields: list = None):
    fieldnames = [
        "scraped_at", "state_id", "type", "name",
        "warning_color", "severity",
    ] + (extra_fields or [])
    with open(DATA_DIR / filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            writer.writerow({
                "scraped_at": meta["scraped_at"],
                "state_id":   meta["state_id"],
                **r,
            })


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    clean_old_files()

    # IST time for email display — taken from system clock, no UTC conversion
    reported_at_ist = ist_now_human()   # e.g. "18:33"

    # UTC timestamp for database/CSV audit trail
    scraped_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    meta = {
        "scraped_at": scraped_at_utc,
        "state_id":   STATE_ID,
    }

    # ── DISTRICT PAGE + BALLOON HOVER ────────────────────────
    district_html, balloon_data = load_district_page(DISTRICT_URL, f"district_warning_{STATE_ID}.png")
    district_records = extract_district_records(district_html)

    # ── Enrich districts with balloon-extracted times + weather data ─
    district_records = enrich_district_from_balloons(district_records, balloon_data)

    print(f"\n[scraper] District : {len(district_records)} records")

    if not district_records:
        print("\n[scraper] ERROR: no district data extracted — aborting")
        sys.exit(1)

    # ── Supabase client ───────────────────────────────────────
    sb = None
    prev_state: dict = {}
    if SUPABASE_KEY:
        try:
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            # READ previous state BEFORE writing — needed for escalation check
            prev_state = read_previous_state(sb)
        except Exception as e:
            print(f"[scraper] Supabase client error: {e}")
    else:
        print("[scraper] SUPABASE_KEY not set — skipping Supabase operations")

    # ── Severity summary ──────────────────────────────────────
    print("\n[scraper] District severity summary:")
    counts = Counter(r["severity"] for r in district_records)
    print("  " + "  ".join(f"{s}={c}" for s, c in sorted(counts.items())))

    # ── TPCODL escalation check ───────────────────────────────
    escalated = check_tpcodl_escalation(district_records, prev_state)

    # ── Send alert email if escalation detected ───────────────
    if escalated:
        print(f"\n[scraper] {len(escalated)} TPCODL district(s) escalated — sending email...")
        # Take hover screenshots only for escalated (orange/red) districts
        escalated_names = [r["name"].upper() for r in escalated]
        hover_pngs = take_escalated_hover_screenshots(escalated_names)
        send_alert_email(escalated, reported_at_ist, hover_pngs)
    else:
        print("\n[scraper] No TPCODL escalations — no email sent")

    # ── Upsert ALL rows to Supabase every run ─────────────────
    if sb:
        upsert_district_warnings(district_records, meta, sb)

    # ── Save district CSV (audit trail in git) ────────────────
    save_csv(
        "district_warnings_latest.csv",
        district_records,
        meta,
        extra_fields=["issued_at", "valid_upto", "rain_description", "thunderstorm_desc", "lightning_probability"],
    )
    print(f"[scraper] Saved district_warnings_latest.csv ({len(district_records)} rows)")

    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
