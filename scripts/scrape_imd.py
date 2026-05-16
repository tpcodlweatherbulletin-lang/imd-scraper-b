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
from datetime import datetime, timezone

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

STATION_URL = (
    "https://mausam.imd.gov.in/imd_latest/"
    f"contents/stationwise-nowcast-warning_mc.php?id={STATE_ID}"
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


def load_district_page(url: str, screenshot_name: str) -> str:
    """
    Load the district warnings page.
    No balloon-click capture — those fields (rain_description etc.)
    are JS-rendered and unreliable. We only need fill colours.
    """
    print(f"\n[scraper] Loading district page: {url}")

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

        html = page.content()
        browser.close()

    return html


def load_station_page(url: str, screenshot_name: str) -> str:
    """Load station page — used only for issued_at / valid_upto enrichment."""
    print(f"\n[scraper] Loading station page: {url}")

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
            print("[scraper] WARNING: station page load timed out — continuing")

        try:
            page.wait_for_selector("svg", timeout=20000)
            page.wait_for_timeout(5000)
        except PlaywrightTimeout:
            print("[scraper] WARNING: SVG not detected on station page")

        screenshot_path = DATA_DIR / screenshot_name
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[scraper] Screenshot saved → {screenshot_path.name}")

        html = page.content()
        browser.close()

    return html


# ─────────────────────────────────────────────────────────────
# EXTRACTION — DISTRICTS
# ─────────────────────────────────────────────────────────────

def extract_district_records(html: str) -> list:
    """
    Extract district warning records from the SVG map HTML.
    Only extracts: name, warning_color, severity.
    issued_at / valid_upto are filled later by enrich_district_issued_at().
    Applies IMD_NAME_NORMALIZE to fix known name mismatches.
    """
    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()

    def _make_record(label: str, fill: str) -> dict:
        raw_name  = label.strip()
        # Normalize known IMD name mismatches
        name      = IMD_NAME_NORMALIZE.get(raw_name.upper(), raw_name.upper())
        hex_color = normalize_color(fill)
        return {
            "type":          "district",
            "name":          name,
            "warning_color": hex_to_color_name(hex_color),
            "severity":      color_to_severity(fill),
            "issued_at":     "",
            "valid_upto":    "",
        }

    # Primary: amcharts-map-area SVG paths
    for path in soup.select("svg path.amcharts-map-area"):
        label = (path.get("aria-label") or "").strip()
        fill  = (path.get("fill") or "").strip()
        if label and fill and label.upper() not in seen:
            seen.add(label.upper())
            records.append(_make_record(label, fill))

    # Fallback: any SVG path with aria-label + fill
    if not records:
        for path in soup.select("svg path"):
            label = (path.get("aria-label") or path.get("title") or "").strip()
            fill  = (path.get("fill") or path.get("stroke") or "").strip()
            if label and fill and label.upper() not in seen:
                seen.add(label.upper())
                records.append(_make_record(label, fill))

    print(f"[scraper] District records extracted: {len(records)}")
    return records


# ─────────────────────────────────────────────────────────────
# EXTRACTION — STATIONS  (for time enrichment only)
# ─────────────────────────────────────────────────────────────

def extract_station_records(html: str) -> list:
    """
    Extract station records. Used ONLY to derive consensus issued_at / valid_upto
    for district enrichment. Station records are NOT uploaded to Supabase.
    """
    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()

    TEXT_TO_COLOR = {
        "No Warning": "#008000",
        "Watch":      "#ffff00",
        "Alert":      "#ffa500",
        "Warning":    "#ff0000",
    }

    for img_el in soup.select("svg image.amcharts-map-image"):
        href   = (img_el.get("xlink:href") or img_el.get("href") or "").strip()
        parent = img_el.parent
        aria   = (parent.get("aria-label") or "").strip() if parent else ""
        if not aria:
            continue
        parsed = parse_station_aria_label(aria)
        name   = parsed["name"]
        if not name or name in seen:
            continue
        seen.add(name)
        color_hex = marker_filename_to_color(href)
        if not color_hex:
            color_hex = TEXT_TO_COLOR.get(parsed["warning_text"], "")
        records.append({
            "name":      name,
            "issued_at": parsed["issued_at"],
            "valid_upto": parsed["valid_upto"],
        })

    print(f"[scraper] Station records extracted: {len(records)} (time enrichment only)")
    return records


# ─────────────────────────────────────────────────────────────
# ENRICH DISTRICTS with consensus issued_at / valid_upto
# ─────────────────────────────────────────────────────────────

def enrich_district_issued_at(district_records: list, station_records: list) -> list:
    issued_vals = [r["issued_at"]  for r in station_records if r.get("issued_at")]
    valid_vals  = [r["valid_upto"] for r in station_records if r.get("valid_upto")]

    if not issued_vals:
        print("[scraper] No station issued_at found — district time columns stay empty")
        return district_records

    common_issued = Counter(issued_vals).most_common(1)[0][0]
    common_valid  = Counter(valid_vals).most_common(1)[0][0] if valid_vals else ""
    print(f"[scraper] Station consensus → issued_at={common_issued}  valid_upto={common_valid}")

    for r in district_records:
        if not r.get("issued_at"):
            r["issued_at"] = common_issued
        if not r.get("valid_upto"):
            r["valid_upto"] = common_valid

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
    """
    try:
        resp = (
            sb.table(DISTRICT_TABLE)
            .select("name, warning_color")
            .eq("state_id", STATE_ID)
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
    rows = ""
    for r in escalated:
        badge = _badge(r["warning_color"], r["severity"])
        rows += (
            f"<tr>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"font-weight:600;font-size:13px;'>{r['name'].title()}</td>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"text-align:center;'>{badge}</td>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"font-size:12px;color:#555;'>{r.get('issued_at', '—')}</td>"
            f"<td style='padding:7px 12px;border-bottom:1px solid #e8e8e8;"
            f"font-size:12px;color:#555;'>{r.get('valid_upto', '—')} Hrs</td>"
            f"</tr>"
        )
    return f"""
    <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;">
      <thead>
        <tr style="background:#1B3A6B;color:#fff;">
          <th style="padding:8px 12px;text-align:left;font-size:13px;">District</th>
          <th style="padding:8px 12px;font-size:13px;">Severity</th>
          <th style="padding:8px 12px;text-align:left;font-size:13px;">Issued At</th>
          <th style="padding:8px 12px;text-align:left;font-size:13px;">Valid Upto</th>
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


def build_email_html(escalated: list, timestamp_human: str) -> str:
    # Determine highest severity label for header colour
    has_red    = any(r["warning_color"].lower() == "red"    for r in escalated)
    has_orange = any(r["warning_color"].lower() == "orange" for r in escalated)
    header_bg  = "#cc0000" if has_red else "#e07000"
    sev_label  = "⛔ WARNING" if has_red else "🚨 ALERT"

    district_table = _build_district_table(escalated)
    ops_table      = _build_ops_table(escalated)

    district_names = ", ".join(r["name"].title() for r in escalated)

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;">
  <div style="max-width:760px;margin:24px auto;background:#fff;border-radius:8px;
              box-shadow:0 2px 8px rgba(0,0,0,.1);overflow:hidden;">

    <!-- Header -->
    <div style="background:{header_bg};padding:20px 28px;">
      <h2 style="margin:0;color:#fff;font-size:18px;">
        {sev_label} — IMD Nowcast Warning (TPCODL)
      </h2>
      <p style="margin:6px 0 0;color:rgba(255,255,255,.85);font-size:12px;">
        Scraped at {timestamp_human} &nbsp;|&nbsp; Districts: {district_names}
      </p>
    </div>

    <div style="padding:24px 28px;">

      <!-- Table 1 -->
      <h3 style="margin:0 0 10px;color:#1B3A6B;font-size:14px;letter-spacing:.3px;">
        DISTRICT WARNING STATUS
      </h3>
      {district_table}

      <!-- Table 2 -->
      <h3 style="margin:24px 0 10px;color:#1B3A6B;font-size:14px;letter-spacing:.3px;">
        AFFECTED TPCODL CIRCLES &amp; DIVISIONS
      </h3>
      {ops_table}

      <!-- Footer -->
      <hr style="margin:28px 0 16px;border:none;border-top:1px solid #eee;">
      <p style="font-size:11px;color:#999;margin:0;line-height:1.6;">
        Source: IMD Nowcast Warning system &nbsp;|&nbsp;
        Auto-scraped by GitHub Actions (15-min interval)<br>
        Attachments: district map PNG &bull; station map PNG<br>
        Email triggered on severity escalation to orange/red only.
      </p>
    </div>
  </div>
</body>
</html>"""


def build_email_plain(escalated: list, timestamp_human: str) -> str:
    lines = [
        "IMD TPCODL WARNING ALERT",
        f"Scraped at: {timestamp_human}",
        "=" * 60,
        "",
        "DISTRICT WARNING STATUS",
        "-" * 40,
    ]
    for r in escalated:
        lines.append(
            f"  {r['name']}  |  {r['severity'].upper()}  ({r['warning_color']})"
            f"  |  Issued: {r.get('issued_at','—')}  Valid upto: {r.get('valid_upto','—')} Hrs"
        )
    lines += ["", "AFFECTED TPCODL CIRCLES & DIVISIONS", "-" * 40]
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


def send_alert_email(escalated: list, timestamp_human: str):
    if not GMAIL_FROM or not GMAIL_PASS or not EMAIL_TO:
        print("[scraper] Email env vars not set — skipping alert email")
        return

    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]

    # Subject: severity label + circle count + division count
    has_red       = any(r["warning_color"].lower() == "red" for r in escalated)
    sev_label     = "⛔ WARNING" if has_red else "🚨 ALERT"
    n_circles, n_divs = _subject_counts(escalated)
    subject = (
        f"IMD {sev_label} — "
        f"{n_circles} Circle{'s' if n_circles != 1 else ''}, "
        f"{n_divs} Division{'s' if n_divs != 1 else ''} Affected"
    )

    msg = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(build_email_plain(escalated, timestamp_human), "plain"))
    alt_part.attach(MIMEText(build_email_html(escalated,  timestamp_human), "html"))
    msg.attach(alt_part)

    # Attach district + station map PNGs
    for attach_path in [
        DATA_DIR / f"district_warning_{STATE_ID}.png",
        DATA_DIR / f"station_warning_{STATE_ID}.png",
    ]:
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
# SUPABASE — UPSERT (only changed rows)
# ─────────────────────────────────────────────────────────────

def upsert_district_warnings(
    district_records: list,
    prev_state: dict,
    meta: dict,
    sb: Client,
):
    """
    Upsert district_warnings rows where warning_color has changed since
    the last scan. On cold start (prev_state empty), upserts all rows.

    Columns deliberately NOT written:
        balloon_text, rain_description, thunderstorm_desc, lightning_probability
    These are JS-rendered and unreliable — columns remain in DB untouched.
    """
    rows_to_upsert = []
    for r in district_records:
        name      = r["name"].upper()
        new_rank  = COLOR_RANK.get(r["warning_color"].lower(), 0)
        prev_rank = prev_state.get(name, -1)   # -1 = never seen → always upsert
        if new_rank != prev_rank:
            rows_to_upsert.append({
                "scraped_at":    meta["scraped_at"],
                "state_id":      meta["state_id"],
                "type":          r.get("type", "district"),
                "name":          r["name"],
                "warning_color": r.get("warning_color", ""),
                "severity":      r.get("severity", ""),
                "issued_at":     r.get("issued_at") or None,
                "valid_upto":    r.get("valid_upto") or None,
            })

    if not rows_to_upsert:
        print("[scraper] Supabase: no color changes — 0 rows upserted")
        return

    try:
        sb.table(DISTRICT_TABLE).upsert(
            rows_to_upsert,
            on_conflict="state_id,name",
        ).execute()
        print(f"[scraper] Supabase: upserted {len(rows_to_upsert)} changed row(s) → '{DISTRICT_TABLE}'")
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

    timestamp_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta = {
        "scraped_at": timestamp_human,
        "state_id":   STATE_ID,
    }

    # ── DISTRICT PAGE ─────────────────────────────────────────
    district_html    = load_district_page(DISTRICT_URL, f"district_warning_{STATE_ID}.png")
    district_records = extract_district_records(district_html)

    # ── STATION PAGE (time enrichment only) ───────────────────
    station_html    = load_station_page(STATION_URL, f"station_warning_{STATE_ID}.png")
    station_records = extract_station_records(station_html)

    # ── Enrich districts with consensus issued_at / valid_upto ─
    district_records = enrich_district_issued_at(district_records, station_records)

    print(f"\n[scraper] District : {len(district_records)} records")
    print(f"[scraper] Station  : {len(station_records)} records (time enrichment only, not uploaded)")

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
        send_alert_email(escalated, timestamp_human)
    else:
        print("\n[scraper] No TPCODL escalations — no email sent")

    # ── Upsert to Supabase (only changed rows) ────────────────
    if sb:
        upsert_district_warnings(district_records, prev_state, meta, sb)
        # station_warnings: not written

    # ── Save district CSV (audit trail in git) ────────────────
    save_csv(
        "district_warnings_latest.csv",
        district_records,
        meta,
        extra_fields=["issued_at", "valid_upto"],
    )
    print(f"[scraper] Saved district_warnings_latest.csv ({len(district_records)} rows)")

    # Station CSV: intentionally not saved — no longer needed
    # warnings_history.jsonl: removed — Supabase is the source of truth

    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
