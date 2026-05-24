import io
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

# ── ReportLab (PDF generation) ────────────────────────────────
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image as RLImage, Flowable,
)


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

COLOR_RANK = {
    "green":  0,
    "yellow": 1,
    "orange": 2,
    "red":    3,
}

# Severity label displayed in cards / badges
SEVERITY_LABEL = {
    "green":  "No Warning",
    "yellow": "Watch",
    "orange": "Alert",
    "red":    "Warning",
}

# Badge CSS per color
COLOR_BADGE_CSS = {
    "red":    "background:#cc0000;color:#fff;",
    "orange": "background:#e07000;color:#fff;",
    "yellow": "background:#b8b800;color:#fff;",
    "green":  "background:#2e7d32;color:#fff;",
}

# ── ReportLab color constants (match email palette) ──────────
RL_RED    = colors.HexColor("#cc0000")
RL_ORANGE = colors.HexColor("#e07000")
RL_YELLOW = colors.HexColor("#b8b800")
RL_GREEN  = colors.HexColor("#2e7d32")
RL_NAVY   = colors.HexColor("#1B3A6B")
RL_LTBLUE = colors.HexColor("#e8f0fe")
RL_GREY   = colors.HexColor("#f5f7fb")
RL_BORD   = colors.HexColor("#d8dce8")

# ── TPCODL District → Circle & Division mapping ───────────────
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

IMD_NAME_NORMALIZE = {
    "KENDRAPARHA": "KENDRAPARA",
}

TPCODL_DISTRICTS_IMD = [
    "ANUGUL", "CUTTACK", "DHENKANAL", "JAGATSINGHPUR",
    "KENDRAPARHA", "KHORDHA", "NAYAGARH", "PURI",
]


# ─────────────────────────────────────────────────────────────
# CLEAN OLD FILES
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
    return datetime.now(IST).strftime("%H:%M")


def extract_time_only(issued_at_str: str) -> str:
    if not issued_at_str:
        return "—"
    m = re.search(r"(\d{4})$", issued_at_str.strip())
    if m:
        t = m.group(1)
        return f"{t[:2]}:{t[2:]}"
    m2 = re.search(r"(\d{1,2}:\d{2})", issued_at_str)
    if m2:
        return m2.group(1)
    return issued_at_str.strip()


def format_valid_upto(valid_upto_str: str) -> str:
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
# WEATHER VALUE EXTRACTOR
# Pulls short numeric key figures from raw balloon text fields.
# e.g. "Moderate rain: 5-15 mm/hr"          → "5–15 mm/hr"
#      "wind speed between 41 – 61 kmph..."  → "41–61 kmph (In gusts)"
#      "< 30% probability of lightning..."   → "< 30%"
# ─────────────────────────────────────────────────────────────

def _parse_weather_values(r: dict) -> dict:
    """
    Returns {"rain": str, "wind": str, "lightning": str} with short display values.
    Falls back to "—" if field is empty or unparseable.
    """
    def _extract_rain(text: str) -> str:
        if not text:
            return "—"
        # e.g. "< 5 mm/hr" or "5-15 mm/hr" or "5 – 15 mm/hr"
        m = re.search(r"([\d\s\-–<>\.]+mm/hr)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip().replace(" - ", "–").replace(" – ", "–")
        # fallback: strip prefix label
        for prefix in ["Extremely heavy rain:", "Very heavy rain:", "Heavy rain:",
                        "Moderate rain:", "Light rain:"]:
            if text.lower().startswith(prefix.lower()):
                return text[len(prefix):].strip()
        return text

    def _extract_wind(text: str) -> str:
        if not text:
            return "—"
        # "between 41 – 61 kmph (In gusts)"  → "41–61 kmph (In gusts)"
        m = re.search(
            r"(?:between\s*)?([\d]+)\s*[–\-]\s*([\d]+)\s*km(?:ph|/h)",
            text, re.IGNORECASE,
        )
        if m:
            lo, hi = m.group(1), m.group(2)
            gusts = " (In gusts)" if "gust" in text.lower() else ""
            return f"{lo}–{hi} kmph{gusts}"
        # "less than 40 kmph"
        m2 = re.search(r"less\s+than\s+([\d]+)\s*km(?:ph|/h)", text, re.IGNORECASE)
        if m2:
            return f"< {m2.group(1)} kmph"
        # generic number + kmph
        m3 = re.search(r"([\d]+)\s*km(?:ph|/h)", text, re.IGNORECASE)
        if m3:
            return f"{m3.group(1)} kmph"
        return text

    def _extract_lightning(text: str) -> str:
        if not text:
            return "—"
        # "> 60%" / "30 - 60%" / "< 30%"
        m = re.search(
            r"([<>]?\s*[\d]+\s*(?:[–\-]\s*[\d]+)?\s*%)",
            text,
        )
        if m:
            return m.group(1).strip().replace(" - ", "–").replace(" – ", "–")
        return text

    return {
        "rain":      _extract_rain(r.get("rain_description") or ""),
        "wind":      _extract_wind(r.get("thunderstorm_desc") or ""),
        "lightning": _extract_lightning(r.get("lightning_probability") or ""),
    }


# ─────────────────────────────────────────────────────────────
# STATION ARIA-LABEL PARSER  (kept for reference, not used)
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
        "name":         name,
        "issued_at":    issued_at,
        "valid_upto":   valid_upto,
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
    """
    result = {
        "issued_at":             "",
        "valid_upto":            "",
        "rain_description":      None,
        "thunderstorm_desc":     None,
        "lightning_probability": None,
        "balloon_text":          text,
    }

    lines = [l.strip() for l in text.splitlines()]

    for i, line in enumerate(lines):
        if line.lower().startswith("time of issue:"):
            date_part = line.split(":", 1)[1].strip()
            for j in range(i + 1, len(lines)):
                next_l = lines[j]
                m = re.match(r"^(\d{3,4})\s*Hrs?$", next_l, re.IGNORECASE)
                if m:
                    result["issued_at"] = f"{date_part} {m.group(1)}"
                    break
            break

    for line in lines:
        m = re.match(r"Valid upto:\s*(\d{3,4})\s*Hrs?", line, re.IGNORECASE)
        if m:
            result["valid_upto"] = m.group(1)
            break

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
    Load district page, screenshot, hover 8 TPCODL districts for balloon data.
    Returns (html, balloon_data_by_canonical_name).
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

        print("[scraper] Hovering TPCODL districts for balloon data...")
        for imd_name in TPCODL_DISTRICTS_IMD:
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
    Re-open district page, hover each escalated district, save full-page screenshot.
    Only called when escalations exist. Returns list of saved PNG paths.
    """
    if not escalated_names:
        return []

    print(f"\n[scraper] Taking hover screenshots for: {escalated_names}")
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
# DISTRICT EXTRACTION (scoped to 8 TPCODL districts)
# ─────────────────────────────────────────────────────────────

def extract_district_records(html: str) -> list:
    soup    = BeautifulSoup(html, "html.parser")
    records = []
    seen    = set()
    TPCODL_CANONICAL = set(TPCODL_MAP.keys())

    def _make_record(label: str, fill: str) -> dict | None:
        raw_name  = label.strip().upper()
        canonical = IMD_NAME_NORMALIZE.get(raw_name, raw_name)
        if canonical not in TPCODL_CANONICAL:
            return None
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

    for path in soup.select("svg path.amcharts-map-area"):
        label = (path.get("aria-label") or "").strip()
        fill  = (path.get("fill") or "").strip()
        canonical = IMD_NAME_NORMALIZE.get(label.upper(), label.upper())
        if label and fill and canonical not in seen:
            rec = _make_record(label, fill)
            if rec:
                seen.add(canonical)
                records.append(rec)

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
# BALLOON ENRICHMENT
# ─────────────────────────────────────────────────────────────

def enrich_district_from_balloons(district_records: list, balloon_data: dict) -> list:
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
    Returns { DISTRICT_NAME: color_rank_int } for all 8 TPCODL districts.
    Missing entries default to rank 0 (green) — safe cold-start behaviour.
    """
    try:
        resp = (
            sb.table(DISTRICT_TABLE)
            .select("name, warning_color")
            .eq("state_id", int(STATE_ID))
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
# ESCALATION CHECK
# Condition: current rank >= 2 (orange/red) AND strictly > previous rank
# ─────────────────────────────────────────────────────────────

def check_tpcodl_escalation(district_records: list, prev_state: dict) -> list:
    escalated = []
    for r in district_records:
        name      = r["name"].upper()
        new_rank  = COLOR_RANK.get(r["warning_color"].lower(), 0)
        prev_rank = prev_state.get(name, 0)
        if new_rank >= 2 and new_rank > prev_rank:
            escalated.append(r)
            print(
                f"[scraper] ESCALATION: {name}  "
                f"rank {prev_rank} → {new_rank}  ({r['warning_color']})"
            )
    if not escalated:
        print("[scraper] No TPCODL escalations this scan")
    return escalated


# ─────────────────────────────────────────────────────────────
# NORMALISATION CHECK
# Condition: previous rank >= 2 AND current rank < 2
# Fires for drops to Watch (yellow) AND No Warning (green)
# ─────────────────────────────────────────────────────────────

def check_tpcodl_normalisation(district_records: list, prev_state: dict) -> list:
    """
    Returns list of districts that dropped FROM orange/red TO yellow/green.
    Each entry is the current district record, augmented with 'prev_color' key.
    """
    normalised = []
    for r in district_records:
        name      = r["name"].upper()
        new_rank  = COLOR_RANK.get(r["warning_color"].lower(), 0)
        prev_rank = prev_state.get(name, 0)
        if prev_rank >= 2 and new_rank < 2:
            entry = dict(r)
            entry["prev_color"] = list(COLOR_RANK.keys())[prev_rank]
            normalised.append(entry)
            print(
                f"[scraper] NORMALISATION: {name}  "
                f"rank {prev_rank} → {new_rank}  ({entry['prev_color']} → {r['warning_color']})"
            )
    if not normalised:
        print("[scraper] No TPCODL normalisations this scan")
    return normalised


# ─────────────────────────────────────────────────────────────
# EMAIL HELPERS
# ─────────────────────────────────────────────────────────────

def _badge(color: str, label: str, extra_css: str = "") -> str:
    style = COLOR_BADGE_CSS.get(color.lower(), "background:#ccc;color:#000;")
    return (
        f'<span style="{style}padding:3px 10px;border-radius:4px;'
        f'font-weight:700;font-size:11px;letter-spacing:.5px;{extra_css}">'
        f'{label.upper()}</span>'
    )


def _imd_header(bg_color: str, title: str, subtitle: str,
                badge_label: str, valid_range: str) -> str:
    """
    Full-width IMD-style header block.
    Matches the reference image: warning triangle left, status badge right.
    """
    return f"""
    <div style="background:{bg_color};padding:0;">
      <!-- Top accent bar -->
      <div style="background:rgba(0,0,0,.15);height:4px;"></div>
      <table style="border-collapse:collapse;width:100%;padding:20px 28px;">
        <tr>
          <!-- Left: triangle icon + title + subtitle -->
          <td style="vertical-align:middle;padding:20px 0 20px 28px;">
            <table style="border-collapse:collapse;">
              <tr>
                <td style="vertical-align:middle;padding-right:14px;">
                  <!-- Warning triangle SVG — inline, no external dep -->
                  <svg width="40" height="40" viewBox="0 0 40 40"
                       xmlns="http://www.w3.org/2000/svg">
                    <polygon points="20,4 38,36 2,36"
                             fill="white" fill-opacity="0.25"
                             stroke="white" stroke-width="2"/>
                    <text x="20" y="30" text-anchor="middle"
                          font-size="18" font-weight="bold"
                          fill="white">!</text>
                  </svg>
                </td>
                <td style="vertical-align:middle;">
                  <div style="font-size:9px;color:rgba(255,255,255,.7);
                              text-transform:uppercase;letter-spacing:1px;
                              margin-bottom:3px;">
                    India Meteorological Department
                  </div>
                  <div style="font-size:21px;font-weight:900;color:#fff;
                              letter-spacing:.5px;line-height:1.15;">
                    {title}
                  </div>
                  <div style="font-size:12px;color:rgba(255,255,255,.85);
                              margin-top:5px;">
                    {subtitle}
                  </div>
                </td>
              </tr>
            </table>
          </td>
          <!-- Right: status badge box -->
          <td style="vertical-align:middle;text-align:right;
                     padding:20px 28px 20px 16px;white-space:nowrap;width:1%;">
            <div style="display:inline-block;
                        background:rgba(0,0,0,.30);
                        border:2px solid rgba(255,255,255,.5);
                        border-radius:8px;padding:10px 18px;text-align:center;">
              <div style="font-size:11px;font-weight:700;color:#fff;
                          text-transform:uppercase;letter-spacing:.8px;">
                {badge_label}
              </div>
              <div style="font-size:11px;color:rgba(255,255,255,.8);
                          margin-top:4px;white-space:nowrap;">
                🕐 {valid_range}
              </div>
            </div>
          </td>
        </tr>
      </table>
      <!-- Bottom accent bar -->
      <div style="background:rgba(0,0,0,.10);height:3px;"></div>
    </div>"""


def _imd_footer(reported_at_ist: str) -> str:
    today_str = date.today().strftime("%d %b %Y")
    return f"""
    <div style="background:#1B3A6B;padding:16px 28px;">
      <table style="border-collapse:collapse;width:100%;">
        <tr>
          <td style="vertical-align:top;width:33%;padding-right:16px;">
            <div style="font-size:10px;color:rgba(255,255,255,.5);
                        text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;">
              Source
            </div>
            <div style="font-size:12px;color:rgba(255,255,255,.85);font-weight:600;">
              India Meteorological Department
            </div>
            <div style="font-size:11px;color:rgba(255,255,255,.5);">
              mausam.imd.gov.in
            </div>
          </td>
          <td style="vertical-align:top;width:33%;padding:0 16px;
                     border-left:1px solid rgba(255,255,255,.15);
                     border-right:1px solid rgba(255,255,255,.15);">
            <div style="font-size:10px;color:rgba(255,255,255,.5);
                        text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;">
              Monitoring
            </div>
            <div style="font-size:12px;color:rgba(255,255,255,.85);font-weight:600;">
              TPCODL Auto-Scraper
            </div>
            <div style="font-size:11px;color:rgba(255,255,255,.5);">
              Scans every 15 min via GitHub Actions
            </div>
          </td>
          <td style="vertical-align:top;width:33%;padding-left:16px;">
            <div style="font-size:10px;color:rgba(255,255,255,.5);
                        text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px;">
              Generated
            </div>
            <div style="font-size:12px;color:rgba(255,255,255,.85);font-weight:600;">
              {reported_at_ist} Hrs IST · {today_str}
            </div>
            <div style="font-size:11px;color:rgba(255,255,255,.5);">
              Email on escalation to orange/red only
            </div>
          </td>
        </tr>
      </table>
    </div>"""


def _scan_summary_bar(reported_at_ist: str, n_active: int, n_new: int) -> str:
    """Thin info strip between header and cards."""
    next_scan = (
        datetime.now(IST) + timedelta(minutes=15)
    ).strftime("%H:%M")
    return f"""
    <div style="background:#f0f2f7;border-bottom:1px solid #dde1ea;
                padding:8px 28px;">
      <table style="border-collapse:collapse;width:100%;font-size:11px;color:#666;">
        <tr>
          <td>🕐 Scanned at <strong>{reported_at_ist} Hrs IST</strong></td>
          <td style="text-align:center;">
            🔴 <strong>{n_active}</strong> district{'s' if n_active != 1 else ''} active
            &nbsp;·&nbsp;
            ⬆ <strong>{n_new}</strong> newly escalated
          </td>
          <td style="text-align:right;">
            Next refresh ~<strong>{next_scan}</strong> Hrs
          </td>
        </tr>
      </table>
    </div>"""


def _build_district_card(r: dict, is_new: bool) -> str:
    """
    Single district card matching the reference image style.
    Card header color matches severity. is_new adds a NEW badge.
    """
    color      = r["warning_color"].lower()
    sev        = r["severity"]
    name       = r["name"].title()
    issued     = extract_time_only(r.get("issued_at", ""))
    valid      = format_valid_upto(r.get("valid_upto", ""))
    mapping    = TPCODL_MAP.get(r["name"].upper(), {})
    circles    = ", ".join(mapping.get("circles",   ["—"]))
    divisions  = ", ".join(mapping.get("divisions", ["—"]))
    wv         = _parse_weather_values(r)

    header_bg  = "#cc0000" if color == "red" else "#e07000"
    dot_color  = "#cc0000" if color == "red" else "#e07000"

    new_badge = (
        '&nbsp;<span style="background:#fff;color:#cc0000;font-size:9px;'
        'font-weight:900;padding:1px 5px;border-radius:3px;'
        'letter-spacing:.5px;vertical-align:middle;">↑ NEW</span>'
        if is_new else ""
    )

    has_weather = wv["rain"] != "—" or wv["wind"] != "—" or wv["lightning"] != "—"

    weather_rows = ""
    if has_weather:
        def _wrow(icon, label, value):
            if value == "—":
                return ""
            return (
                f'<tr>'
                f'<td style="padding:6px 14px;border-bottom:1px solid #f0f0f0;'
                f'font-size:12px;color:#555;width:28px;">{icon}</td>'
                f'<td style="padding:6px 4px 6px 0;border-bottom:1px solid #f0f0f0;'
                f'font-size:12px;color:#333;font-weight:600;white-space:nowrap;">'
                f'{label}:</td>'
                f'<td style="padding:6px 14px 6px 8px;border-bottom:1px solid #f0f0f0;'
                f'font-size:12px;color:#333;">{value}</td>'
                f'</tr>'
            )
        weather_rows = (
            _wrow("🌧", "Rain",      wv["rain"])
            + _wrow("💨", "Winds",   wv["wind"])
            + _wrow("⚡", "Lightning", wv["lightning"])
        )

    return f"""
    <div style="background:#fff;border-radius:10px;overflow:hidden;
                box-shadow:0 2px 8px rgba(0,0,0,.10);
                border:1px solid #e8e8e8;margin:6px;">
      <!-- Card header -->
      <div style="background:{header_bg};padding:10px 14px;">
        <div style="font-size:14px;font-weight:900;color:#fff;
                    letter-spacing:.3px;text-align:center;">
          {name} District
        </div>
      </div>
      <!-- Card body -->
      <table style="border-collapse:collapse;width:100%;">
        <!-- Time -->
        <tr>
          <td style="padding:8px 14px 4px;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#555;width:28px;">🕐</td>
          <td style="padding:8px 4px 4px 0;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#333;font-weight:600;white-space:nowrap;">
                     Time:</td>
          <td style="padding:8px 14px 4px 8px;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#333;">
                     {issued} – {valid} Hrs</td>
        </tr>
        <!-- Severity -->
        <tr style="background:rgba(0,0,0,.02);">
          <td style="padding:6px 14px;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#555;">⚠</td>
          <td style="padding:6px 4px 6px 0;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#333;font-weight:600;">Severity:</td>
          <td style="padding:6px 14px 6px 8px;border-bottom:1px solid #f0f0f0;">
            <span style="background:{dot_color};color:#fff;font-size:10px;
                         font-weight:700;padding:2px 8px;border-radius:3px;
                         letter-spacing:.5px;">{sev.upper()}</span>
            {new_badge}
          </td>
        </tr>
        <!-- Weather rows -->
        {weather_rows}
        <!-- Circle -->
        <tr>
          <td style="padding:6px 14px;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#555;">📍</td>
          <td style="padding:6px 4px 6px 0;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#333;font-weight:600;white-space:nowrap;">
                     Circle:</td>
          <td style="padding:6px 14px 6px 8px;border-bottom:1px solid #f0f0f0;
                     font-size:12px;color:#333;">{circles}</td>
        </tr>
        <!-- Divisions -->
        <tr>
          <td style="padding:6px 14px 10px;font-size:12px;color:#555;">🏢</td>
          <td style="padding:6px 4px 10px 0;font-size:12px;color:#333;
                     font-weight:600;white-space:nowrap;vertical-align:top;">
                     Divisions:</td>
          <td style="padding:6px 14px 10px 8px;font-size:12px;color:#333;
                     line-height:1.5;">{divisions}</td>
        </tr>
      </table>
    </div>"""


def _build_card_grid(currently_active: list, escalated_names: set) -> str:
    """
    3-column email-safe table grid of district cards.
    Newly escalated cards get the NEW badge; already-active cards do not.
    Districts sorted: red first, then orange; within each color newly escalated first.
    """
    # Sort: red before orange, new before existing within each color
    def _sort_key(r):
        rank   = COLOR_RANK.get(r["warning_color"].lower(), 0)
        is_new = 0 if r["name"].upper() in escalated_names else 1
        return (-rank, is_new)

    sorted_records = sorted(currently_active, key=_sort_key)
    cards = [
        _build_district_card(r, r["name"].upper() in escalated_names)
        for r in sorted_records
    ]

    # Build 3-column table rows
    cols = 3
    rows_html = ""
    for i in range(0, len(cards), cols):
        chunk = cards[i:i + cols]
        # Pad to full row
        while len(chunk) < cols:
            chunk.append("<td></td>")
        cells = "".join(
            f'<td style="vertical-align:top;width:33%;">{c}</td>'
            for c in chunk
        )
        rows_html += f"<tr>{cells}</tr>"

    return f"""
    <table style="border-collapse:collapse;width:100%;table-layout:fixed;">
      {rows_html}
    </table>"""


def _build_kalabaisakhi_summary(active: list) -> str:
    """Kalabaisakhi timing cards — one per active district."""
    cards = []
    for idx, r in enumerate(active):
        name      = r["name"].upper()
        mapping   = TPCODL_MAP.get(name, {})
        divisions = ", ".join(mapping.get("divisions", ["—"]))
        issued    = extract_time_only(r.get("issued_at", ""))
        valid     = format_valid_upto(r.get("valid_upto", ""))
        divider   = (
            "border-bottom:1px dashed #f0d060;padding-bottom:14px;margin-bottom:14px;"
            if idx < len(active) - 1 else ""
        )
        cards.append(f"""
        <div style="{divider}">
          <table style="border-collapse:collapse;width:100%;">
            <tr>
              <td style="vertical-align:middle;padding-right:16px;width:65%;">
                <span style="font-size:13px;color:#5a3800;line-height:1.7;">
                  Start time of <strong>KALABAISAKHI</strong> :
                  <strong>{issued} Hrs</strong> and expected stop time is
                  <strong>{valid} Hrs</strong> in
                  <strong>{r['name'].title()} District</strong>
                  and associated Divisions ({divisions}).
                </span>
              </td>
              <td style="vertical-align:middle;width:35%;
                         border-left:1px solid #f0d060;padding-left:16px;">
                <div style="margin-bottom:8px;">
                  <div style="font-size:10px;color:#8a6000;text-transform:uppercase;
                               letter-spacing:.4px;">Start Time</div>
                  <div style="font-size:18px;font-weight:700;color:#c05000;">
                    {issued} Hrs</div>
                </div>
                <div style="border-top:1px dashed #e0c070;padding-top:8px;">
                  <div style="font-size:10px;color:#8a6000;text-transform:uppercase;
                               letter-spacing:.4px;">Expected End Time</div>
                  <div style="font-size:18px;font-weight:700;color:#c05000;">
                    {valid} Hrs</div>
                </div>
              </td>
            </tr>
          </table>
        </div>""")
    return "".join(cards)


def _build_kalabaisakhi_summary_plain(active: list) -> str:
    lines = []
    for r in active:
        name      = r["name"].upper()
        mapping   = TPCODL_MAP.get(name, {})
        divisions = ", ".join(mapping.get("divisions", ["—"]))
        issued    = extract_time_only(r.get("issued_at", ""))
        valid     = format_valid_upto(r.get("valid_upto", ""))
        lines.append(
            f"  • Start time of KALABAISAKHI : {issued} Hrs and expected stop time "
            f"is {valid} Hrs in {r['name'].title()} District "
            f"and associated Divisions ({divisions})."
        )
    return "\n".join(lines)


def _subject_counts(records: list) -> tuple[int, int]:
    circles   = set()
    divisions = set()
    for r in records:
        mapping = TPCODL_MAP.get(r["name"].upper(), {})
        for c in mapping.get("circles",   []):
            circles.add(c)
        for d in mapping.get("divisions", []):
            divisions.add(d)
    return len(circles), len(divisions)


# ─────────────────────────────────────────────────────────────
# PDF GENERATION
# ─────────────────────────────────────────────────────────────

class _ColoredRect(Flowable):
    """Solid colored rectangle with text — used as section headers in PDF."""
    def __init__(self, width, height, bg_color, text,
                 text_color=colors.white, font_size=8, font_name="Helvetica-Bold"):
        super().__init__()
        self.width      = width
        self.height     = height
        self.bg_color   = bg_color
        self.text       = text
        self.text_color = text_color
        self.font_size  = font_size
        self.font_name  = font_name

    def draw(self):
        self.canv.setFillColor(self.bg_color)
        self.canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        self.canv.setFillColor(self.text_color)
        self.canv.setFont(self.font_name, self.font_size)
        self.canv.drawString(8, self.height / 2 - self.font_size / 3, self.text)


def _rl_color_for(color_name: str):
    return {
        "red":    RL_RED, "orange": RL_ORANGE,
        "yellow": RL_YELLOW, "green": RL_GREEN,
    }.get(color_name.lower(), colors.grey)


def _rl_row_bg(color_name: str):
    return {
        "red":    colors.HexColor("#fff5f5"),
        "orange": colors.HexColor("#fff8f0"),
        "yellow": colors.HexColor("#fffef0"),
        "green":  colors.HexColor("#f5fff6"),
    }.get(color_name.lower(), colors.white)


def build_report_pdf(
    all_records: list,
    escalated: list,
    reported_at_ist: str,
    overview_png: Path,
    hover_pngs: list,
    is_normalisation: bool = False,
) -> bytes:
    """
    Build the IMD TPCODL report PDF and return raw bytes.

    Page 1  : Summary (header + counts + 3 tables)
    Page 2  : district overview map (full page, no crop)
    Page 3+ : hover PNGs for escalated districts (alert email only)

    Parameters
    ----------
    all_records      : all 8 district records (current scan)
    escalated        : newly escalated records ([] for normalisation)
    reported_at_ist  : "HH:MM" IST scan time string
    overview_png     : Path to district_warning_10.png
    hover_pngs       : hover PNG paths ([] for normalisation email)
    is_normalisation : True → green header; False → red/orange header
    """
    buf      = io.BytesIO()
    page_w, page_h = A4          # 595 x 842 pt
    margin   = 18 * mm
    usable_w = page_w - 2 * margin

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin,  bottomMargin=14 * mm,
        title="IMD TPCODL District Warning Report",
        author="TPCODL - IMD Nowcast Smart Alert Engine",
    )

    FONT_REG  = "Helvetica"
    FONT_BOLD = "Helvetica-Bold"

    counts          = Counter(r["warning_color"].lower() for r in all_records)
    active_records  = [r for r in all_records
                       if COLOR_RANK.get(r["warning_color"].lower(), 0) >= 2]
    escalated_names = {r["name"].upper() for r in escalated}
    sorted_all      = sorted(all_records,
                             key=lambda r: -COLOR_RANK.get(r["warning_color"].lower(), 0))

    has_red      = any(r["warning_color"].lower() == "red" for r in active_records)
    hdr_color    = RL_GREEN if is_normalisation else (RL_RED if has_red else RL_ORANGE)
    today_str    = date.today().strftime("%d %b %Y")
    active_names = ", ".join(r["name"].title() for r in active_records) or "None"

    issued_times = [extract_time_only(r.get("issued_at", ""))
                    for r in active_records if r.get("issued_at")]
    valid_times  = [format_valid_upto(r.get("valid_upto", ""))
                    for r in active_records if r.get("valid_upto")]
    valid_range  = (f"{min(issued_times)} - {max(valid_times)} Hrs IST"
                    if issued_times and valid_times else f"{reported_at_ist} Hrs IST")

    badge_label = ("ALL CLEAR" if is_normalisation
                   else ("WARNING ACTIVE" if has_red else "ALERT ACTIVE"))

    story = []

    # ── Header Flowable (replicates email header exactly) ─────
    HDR_H = 52 * mm

    class _HeaderFlowable(Flowable):
        def __init__(self, w, bg, title, subtitle, badge, time_range, today, fb, fr):
            super().__init__()
            self.width = w;  self.height = HDR_H
            self.bg = bg;    self.title = title;  self.subtitle = subtitle
            self.badge = badge;  self.time_range = time_range;  self.today = today
            self.fb = fb;    self.fr = fr

        def draw(self):
            c = self.canv
            w, h = self.width, self.height
            # Background
            c.setFillColor(self.bg)
            c.rect(0, 0, w, h, fill=1, stroke=0)
            # Top accent
            c.setFillColor(colors.HexColor("#00000026"))
            c.rect(0, h - 4, w, 4, fill=1, stroke=0)
            # Bottom accent
            c.setFillColor(colors.HexColor("#0000001a"))
            c.rect(0, 0, w, 3, fill=1, stroke=0)
            # Triangle icon
            tx, ty, ts = 22, h / 2, 13
            c.setFillColor(colors.HexColor("#ffffff40"))
            c.setStrokeColor(colors.white)
            c.setLineWidth(1.5)
            c.polygon([tx, ty + ts,
                        tx - ts * 0.87, ty - ts * 0.5,
                        tx + ts * 0.87, ty - ts * 0.5], fill=1, stroke=1)
            c.setFillColor(colors.white)
            c.setFont(self.fb, 10)
            c.drawCentredString(tx, ty - 4, "!")
            # Left text block
            lx = tx * 2 + 10
            c.setFillColor(colors.HexColor("#ffffffb3"))
            c.setFont(self.fr, 6.5)
            c.drawString(lx, h - 13, "INDIA METEOROLOGICAL DEPARTMENT")
            c.setFillColor(colors.white)
            c.setFont(self.fb, 16)
            c.drawString(lx, h - 27, self.title)
            c.setFont(self.fr, 8.5)
            c.setFillColor(colors.HexColor("#ffffffd9"))
            c.drawString(lx, h - 39, self.subtitle)
            c.setFont(self.fr, 7.5)
            c.setFillColor(colors.HexColor("#ffffff99"))
            c.drawString(lx, 8, self.today)
            # Badge box (right)
            bw, bh = 115, 32
            bx = w - bw - 12
            by = (h - bh) / 2
            c.setFillColor(colors.HexColor("#00000048"))
            c.setStrokeColor(colors.HexColor("#ffffff80"))
            c.setLineWidth(1.2)
            c.roundRect(bx, by, bw, bh, 5, fill=1, stroke=1)
            c.setFillColor(colors.white)
            c.setFont(self.fb, 8.5)
            c.drawCentredString(bx + bw / 2, by + bh - 13, self.badge)
            c.setFont(self.fr, 7.5)
            c.setFillColor(colors.HexColor("#ffffffcc"))
            c.drawCentredString(bx + bw / 2, by + 8, self.time_range)

    story.append(_HeaderFlowable(
        usable_w, hdr_color,
        "IMD NOWCAST WARNING", f"Districts: {active_names}",
        badge_label, valid_range, today_str, FONT_BOLD, FONT_REG,
    ))

    # ── Count strip ───────────────────────────────────────────
    story.append(Spacer(1, 3 * mm))
    c_ps = ParagraphStyle("c", alignment=TA_CENTER)
    count_data = [[
        Paragraph(f'<font name="{FONT_BOLD}" size="17" color="#cc0000">{counts.get("red",0)}</font><br/>'
                  f'<font name="{FONT_REG}" size="7" color="#6680aa">WARNING  RED</font>', c_ps),
        Paragraph(f'<font name="{FONT_BOLD}" size="17" color="#e07000">{counts.get("orange",0)}</font><br/>'
                  f'<font name="{FONT_REG}" size="7" color="#6680aa">ALERT  ORANGE</font>', c_ps),
        Paragraph(f'<font name="{FONT_BOLD}" size="17" color="#b8b800">{counts.get("yellow",0)}</font><br/>'
                  f'<font name="{FONT_REG}" size="7" color="#6680aa">WATCH  YELLOW</font>', c_ps),
        Paragraph(f'<font name="{FONT_BOLD}" size="17" color="#2e7d32">{counts.get("green",0)}</font><br/>'
                  f'<font name="{FONT_REG}" size="7" color="#6680aa">NO WARNING  GREEN</font>', c_ps),
        Paragraph(f'<font name="{FONT_BOLD}" size="17" color="#1B3A6B">{len(escalated)}</font><br/>'
                  f'<font name="{FONT_REG}" size="7" color="#6680aa">NEW ESCALATIONS</font>', c_ps),
    ]]
    ct = Table(count_data, colWidths=[usable_w / 5] * 5, rowHeights=[14 * mm])
    ct.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), RL_LTBLUE),
        ("BOX",           (0,0),(-1,-1), 0.5, RL_BORD),
        ("LINEBEFORE",    (1,0),(-1,-1), 0.5, RL_BORD),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("ALIGN",         (0,0),(-1,-1), "CENTRE"),
        ("TOPPADDING",    (0,0),(-1,-1), 4),
        ("BOTTOMPADDING", (0,0),(-1,-1), 4),
    ]))
    story.append(ct)

    # Reusable paragraph styles
    th_ps    = ParagraphStyle("th",   fontSize=7, fontName=FONT_BOLD,
                               textColor=colors.HexColor("#555555"))
    cell_ps  = ParagraphStyle("cell", fontSize=8, fontName=FONT_REG)
    cell_mut = ParagraphStyle("cmut", fontSize=8, fontName=FONT_REG,
                               textColor=colors.HexColor("#aaaaaa"))

    def _row_table(data, col_widths, row_bg, min_h=8*mm):
        rt = Table(data, colWidths=col_widths)
        rt.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), row_bg),
            ("LINEBELOW",     (0,0),(-1,-1), 0.3, RL_BORD),
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
            ("LEFTPADDING",   (0,0),(-1,-1), 6),
            ("RIGHTPADDING",  (0,0),(-1,-1), 4),
            ("TOPPADDING",    (0,0),(-1,-1), 4),
            ("BOTTOMPADDING", (0,0),(-1,-1), 4),
        ]))
        return rt

    def _hdr_table(cells, col_widths):
        ht = Table([cells], colWidths=col_widths, rowHeights=[7*mm])
        ht.setStyle(TableStyle([
            ("BACKGROUND",  (0,0),(-1,-1), RL_GREY),
            ("LINEBELOW",   (0,0),(-1,-1), 1.0, RL_BORD),
            ("VALIGN",      (0,0),(-1,-1), "MIDDLE"),
            ("LEFTPADDING", (0,0),(-1,-1), 6),
        ]))
        return ht

    # ── TABLE 1: IMD Nowcast Status — all 8 districts ─────────
    story.append(Spacer(1, 4 * mm))
    story.append(_ColoredRect(
        usable_w, 9*mm, RL_NAVY,
        "  IMD Nowcast Status  -  TPCODL Coverage Districts"
        "                    (sorted by severity, highest first)",
        font_size=8, font_name=FONT_BOLD,
    ))

    # Legend bar
    leg_ps = ParagraphStyle("leg", fontSize=7, fontName=FONT_REG)
    leg_t  = Table([[
        Paragraph('<font color="#cc0000">&#9632;</font>  Warning', leg_ps),
        Paragraph('<font color="#e07000">&#9632;</font>  Alert',   leg_ps),
        Paragraph('<font color="#b8b800">&#9632;</font>  Watch',   leg_ps),
        Paragraph('<font color="#2e7d32">&#9632;</font>  No Warning', leg_ps),
        Paragraph("", leg_ps),
    ]], colWidths=[usable_w/5]*5, rowHeights=[6*mm])
    leg_t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0),(-1,-1), RL_GREY),
        ("LINEBELOW",   (0,0),(-1,-1), 0.4, RL_BORD),
        ("VALIGN",      (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0),(-1,-1), 8),
    ]))
    story.append(leg_t)

    cw1 = [usable_w*0.22, usable_w*0.14, usable_w*0.18, usable_w*0.46]
    story.append(_hdr_table(
        [Paragraph("DISTRICT",th_ps), Paragraph("SEVERITY",th_ps),
         Paragraph("ISSUED / VALID (HRS)",th_ps), Paragraph("WEATHER SUMMARY",th_ps)], cw1))

    for r in sorted_all:
        cn     = r["warning_color"].lower()
        rank   = COLOR_RANK.get(cn, 0)
        is_new = r["name"].upper() in escalated_names
        nm     = "  [NEW]" if is_new else ""
        sc_hex = {"red":"#cc0000","orange":"#e07000","yellow":"#b8b800"}.get(cn,"#2e7d32")

        issued = extract_time_only(r.get("issued_at", ""))
        valid  = format_valid_upto(r.get("valid_upto", ""))

        if rank >= 2:
            wv    = _parse_weather_values(r)
            parts = []
            if wv["rain"]      != "—": parts.append(f"Rain: {wv['rain']}")
            if wv["wind"]      != "—": parts.append(f"Wind: {wv['wind']}")
            if wv["lightning"] != "—": parts.append(f"Lightning: {wv['lightning']}")
            wx_p  = Paragraph("  |  ".join(parts) or "—", cell_ps)
            time_p = Paragraph(f"{issued} to {valid}", cell_ps)
        else:
            wx_p   = Paragraph("—", cell_mut)
            time_p = Paragraph("—", cell_mut)

        story.append(_row_table([[
            Paragraph(f'<font name="{FONT_BOLD}" color="#1B3A6B">{r["name"]}</font>'
                      f'<font name="{FONT_REG}" size="7" color="#cc0000">{nm}</font>', cell_ps),
            Paragraph(f'<font name="{FONT_BOLD}" color="{sc_hex}">{r["severity"].upper()}</font>',
                      cell_ps),
            time_p,
            wx_p,
        ]], cw1, _rl_row_bg(cn)))

    # ── TABLE 2: Affected Circles & Divisions (active only) ────
    if active_records:
        sorted_active = sorted(active_records,
                               key=lambda r: -COLOR_RANK.get(r["warning_color"].lower(), 0))
        story.append(Spacer(1, 4*mm))
        story.append(_ColoredRect(
            usable_w, 9*mm, RL_ORANGE,
            "  Affected TPCODL Circles & Divisions"
            "                    (active districts only - orange / red)",
            font_size=8, font_name=FONT_BOLD,
        ))
        cw2 = [usable_w*0.22, usable_w*0.22, usable_w*0.56]
        story.append(_hdr_table(
            [Paragraph("DISTRICT",th_ps), Paragraph("CIRCLE(S)",th_ps),
             Paragraph("AFFECTED DIVISIONS",th_ps)], cw2))
        for r in sorted_active:
            mapping = TPCODL_MAP.get(r["name"].upper(), {})
            circles = ", ".join(mapping.get("circles", ["—"]))
            divs    = "  |  ".join(mapping.get("divisions", ["—"]))
            story.append(_row_table([[
                Paragraph(f'<font name="{FONT_BOLD}" color="#1B3A6B">{r["name"]}</font>',
                          cell_ps),
                Paragraph(circles, cell_ps),
                Paragraph(divs,    cell_ps),
            ]], cw2, _rl_row_bg(r["warning_color"].lower())))

        # ── TABLE 3: District Weather Details (active only) ────
        story.append(Spacer(1, 4*mm))
        story.append(_ColoredRect(
            usable_w, 9*mm, RL_NAVY,
            "  District Weather Details"
            "                    (active districts only - based on IMD nowcast data)",
            font_size=8, font_name=FONT_BOLD,
        ))
        cw3 = [usable_w*0.18, usable_w*0.26, usable_w*0.30, usable_w*0.26]
        story.append(_hdr_table(
            [Paragraph("DISTRICT",th_ps), Paragraph("RAINFALL",th_ps),
             Paragraph("WIND / THUNDERSTORM",th_ps), Paragraph("LIGHTNING RISK",th_ps)], cw3))
        for r in sorted_active:
            wv      = _parse_weather_values(r)
            rain_f  = r.get("rain_description")     or wv["rain"]
            wind_f  = r.get("thunderstorm_desc")     or wv["wind"]
            light_f = r.get("lightning_probability") or wv["lightning"]
            story.append(_row_table([[
                Paragraph(f'<font name="{FONT_BOLD}" color="#1B3A6B">{r["name"]}</font>',
                          cell_ps),
                Paragraph(rain_f  or "—", cell_ps),
                Paragraph(wind_f  or "—", cell_ps),
                Paragraph(light_f or "—", cell_ps),
            ]], cw3, _rl_row_bg(r["warning_color"].lower())))

    # ── Page 1 footer strip ───────────────────────────────────
    story.append(Spacer(1, 5*mm))
    total_pages = 2 + len(hover_pngs)
    fp_l = ParagraphStyle("fpl", fontSize=7, fontName=FONT_REG,
                           textColor=colors.HexColor("#dddddd"))
    fp_r = ParagraphStyle("fpr", fontSize=7, fontName=FONT_REG,
                           textColor=colors.HexColor("#aaaaaa"), alignment=TA_RIGHT)
    footer_t = Table([[
        Paragraph(
            f'Auto Generated by  <font name="{FONT_BOLD}">'
            f'TPCODL - IMD Nowcast Smart Alert Engine</font>'
            f'  |  Data source: mausam.imd.gov.in', fp_l),
        Paragraph(
            f'Page 1 of {total_pages}  |  '
            f'Pages 2-{total_pages}: District maps & IMD warning snapshots', fp_r),
    ]], colWidths=[usable_w*0.60, usable_w*0.40], rowHeights=[8*mm])
    footer_t.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), RL_NAVY),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0),(-1,-1), 8),
        ("RIGHTPADDING", (0,0),(-1,-1), 8),
    ]))
    story.append(footer_t)

    # ── Screenshot pages ──────────────────────────────────────
    cap_ps = ParagraphStyle("cap", fontSize=8, fontName=FONT_BOLD,
                             textColor=RL_NAVY, spaceAfter=4)

    def _add_image_page(png_path: Path, caption: str):
        story.append(PageBreak())
        story.append(Paragraph(caption, cap_ps))
        if png_path.exists():
            img    = RLImage(str(png_path))
            scale  = usable_w / img.imageWidth
            img.drawWidth  = usable_w
            img.drawHeight = img.imageHeight * scale
            max_h = page_h - 2 * margin - 20 * mm
            if img.drawHeight > max_h:
                s2 = max_h / img.drawHeight
                img.drawWidth  *= s2
                img.drawHeight  = max_h
            story.append(img)
        else:
            story.append(Paragraph(
                f"[Screenshot not available: {png_path.name}]",
                ParagraphStyle("miss", fontSize=9, fontName=FONT_REG,
                               textColor=colors.grey)))

    # Page 2 — district overview map (always present)
    _add_image_page(
        overview_png,
        f"IMD District Warning Map - All TPCODL Districts  |  Scanned {reported_at_ist} Hrs IST",
    )
    # Pages 3+ — hover PNGs (alert only; empty list for normalisation)
    for hp in hover_pngs:
        dname = hp.stem.replace("district_hover_", "").replace(f"_{STATE_ID}", "").upper()
        _add_image_page(
            hp,
            f"IMD Hover Detail - {dname} District  |  Scanned {reported_at_ist} Hrs IST",
        )

    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# ALERT EMAIL
# ─────────────────────────────────────────────────────────────

def build_email_html(escalated: list, currently_active: list,
                     reported_at_ist: str) -> str:
    has_red        = any(r["warning_color"].lower() == "red" for r in currently_active)
    header_bg      = "#cc0000" if has_red else "#e07000"
    sev_label      = "WARNING" if has_red else "ALERT"
    badge_label    = "WARNING ACTIVE" if has_red else "ALERT ACTIVE"
    escalated_names = {r["name"].upper() for r in escalated}

    district_names = ", ".join(r["name"].title() for r in currently_active)
    n_districts    = len(currently_active)

    # Valid time range for header badge — use widest window across active districts
    issued_times = [extract_time_only(r.get("issued_at","")) for r in currently_active
                    if r.get("issued_at")]
    valid_times  = [format_valid_upto(r.get("valid_upto","")) for r in currently_active
                    if r.get("valid_upto")]
    if issued_times and valid_times:
        valid_range = f"{min(issued_times)} – {max(valid_times)} Hrs (IST)"
    else:
        valid_range = "—"

    header     = _imd_header(
        header_bg,
        "IMD NOWCAST WARNING",
        f"Districts: {district_names}",
        badge_label,
        valid_range,
    )
    scan_bar   = _scan_summary_bar(reported_at_ist, len(currently_active), len(escalated))
    card_grid  = _build_card_grid(currently_active, escalated_names)
    kala       = _build_kalabaisakhi_summary(currently_active)
    footer     = _imd_footer(reported_at_ist)

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#e8eaf0;
             margin:0;padding:0;">
  <div style="max-width:760px;margin:20px auto;background:#fff;border-radius:12px;
              box-shadow:0 4px 20px rgba(0,0,0,.12);overflow:hidden;">

    {header}
    {scan_bar}

    <!-- District Cards -->
    <div style="padding:16px 22px 8px;">
      <div style="font-size:11px;font-weight:700;color:#1B3A6B;
                  text-transform:uppercase;letter-spacing:.6px;
                  margin-bottom:10px;display:flex;align-items:center;gap:8px;">
        <span style="display:inline-block;width:3px;height:12px;
                     background:#e07000;border-radius:2px;"></span>
        DISTRICT STATUS
      </div>
      {card_grid}
    </div>

    <!-- Kalabaisakhi -->
    <div style="padding:0 22px 16px;">
      <div style="background:#fffbee;border:1px solid #f0d060;
                  border-left:4px solid #e07000;border-radius:8px;padding:16px 18px;">
        <div style="font-size:11px;font-weight:700;color:#8a5000;
                    text-transform:uppercase;letter-spacing:.6px;margin-bottom:14px;">
          🕐 KALABAISAKHI TIMING SUMMARY
        </div>
        {kala}
      </div>
    </div>

    {footer}
  </div>
</body>
</html>"""


def build_email_plain(escalated: list, currently_active: list,
                      reported_at_ist: str) -> str:
    lines = [
        "IMD TPCODL WARNING ALERT",
        f"Reported at: {reported_at_ist} Hrs (IST)",
        "=" * 60,
        f"  Newly escalated : {len(escalated)} district(s)",
        f"  Currently active: {len(currently_active)} district(s)",
        "",
        "DISTRICT STATUS",
        "-" * 40,
    ]
    escalated_names = {r["name"].upper() for r in escalated}
    for r in currently_active:
        issued = extract_time_only(r.get("issued_at", ""))
        valid  = format_valid_upto(r.get("valid_upto", ""))
        new_tag = " [NEW]" if r["name"].upper() in escalated_names else ""
        wv = _parse_weather_values(r)
        lines.append(
            f"  {r['name']}{new_tag}  |  {r['severity'].upper()}"
            f"  |  {issued} – {valid} Hrs"
        )
        if wv["rain"]      != "—": lines.append(f"    🌧 Rain      : {wv['rain']}")
        if wv["wind"]      != "—": lines.append(f"    💨 Winds     : {wv['wind']}")
        if wv["lightning"] != "—": lines.append(f"    ⚡ Lightning : {wv['lightning']}")
    lines += ["", "KALABAISAKHI TIMING SUMMARY", "-" * 40]
    lines.append(_build_kalabaisakhi_summary_plain(currently_active))
    lines += ["", "=" * 60, "Source: India Meteorological Department (IMD)"]
    return "\n".join(lines)


def send_alert_email(escalated: list, currently_active: list, all_records: list,
                     reported_at_ist: str, hover_pngs: list = None):
    if not GMAIL_FROM or not GMAIL_PASS or not EMAIL_TO:
        print("[scraper] Email env vars not set — skipping alert email")
        return

    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]

    has_red       = any(r["warning_color"].lower() == "red" for r in currently_active)
    sev_label     = "WARNING" if has_red else "ALERT"
    n_circles, n_divs = _subject_counts(currently_active)
    new_names     = ", ".join(r["name"].title() for r in escalated)
    subject = (
        f"IMD [{'WARNING' if has_red else 'ALERT'}] {sev_label} -- {new_names} -- "
        f"{n_circles} Circle{'s' if n_circles != 1 else ''}, "
        f"{n_divs} Division{'s' if n_divs != 1 else ''} Affected"
    )

    msg            = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(
        build_email_plain(escalated, currently_active, reported_at_ist), "plain"))
    alt_part.attach(MIMEText(
        build_email_html(escalated, currently_active, reported_at_ist), "html"))
    msg.attach(alt_part)

    # ── Attach PNG screenshots (overview + hover, same as before) ──
    overview_png = DATA_DIR / f"district_warning_{STATE_ID}.png"
    attach_paths = [overview_png] + (hover_pngs or [])
    for attach_path in attach_paths:
        if not attach_path.exists():
            print(f"[scraper] Attachment not found (skipping): {attach_path.name}")
            continue
        with open(attach_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{attach_path.name}"')
        msg.attach(part)
        print(f"[scraper] Attached PNG: {attach_path.name}")

    # ── Build PDF and attach alongside the PNGs ───────────────
    try:
        pdf_bytes    = build_report_pdf(
            all_records=all_records,
            escalated=escalated,
            reported_at_ist=reported_at_ist,
            overview_png=overview_png,
            hover_pngs=hover_pngs or [],
            is_normalisation=False,
        )
        pdf_filename = f"IMD_TPCODL_Report_{reported_at_ist.replace(':','')}IST.pdf"
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{pdf_filename}"')
        msg.attach(part)
        print(f"[scraper] Alert PDF attached: {pdf_filename} ({len(pdf_bytes)//1024} KB)")
    except Exception as e:
        print(f"[scraper] WARNING: could not build alert PDF — {e}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, GMAIL_PASS)
            server.sendmail(GMAIL_FROM, recipients, msg.as_string())
        print(f"[scraper] Alert email sent → {', '.join(recipients)}")
        print(f"[scraper] Subject: {subject}")
    except Exception as e:
        print(f"[scraper] ERROR sending alert email: {e}")


# ─────────────────────────────────────────────────────────────
# NORMALISATION EMAIL
# ─────────────────────────────────────────────────────────────

def build_normalisation_html(normalised: list, reported_at_ist: str) -> str:
    today_str = date.today().strftime("%d %b %Y")

    # Build downgrade cards
    cards_html = ""
    for r in normalised:
        prev_color  = r.get("prev_color", "orange")
        new_color   = r["warning_color"].lower()
        prev_sev    = SEVERITY_LABEL.get(prev_color, prev_color.title())
        new_sev     = SEVERITY_LABEL.get(new_color, new_color.title())
        prev_badge  = COLOR_BADGE_CSS.get(prev_color, "background:#ccc;color:#000;")
        new_badge   = COLOR_BADGE_CSS.get(new_color, "background:#ccc;color:#000;")
        issued      = extract_time_only(r.get("issued_at", ""))
        valid       = format_valid_upto(r.get("valid_upto", ""))
        name        = r["name"].title()

        cards_html += f"""
        <div style="background:#fff;border-radius:10px;overflow:hidden;
                    box-shadow:0 2px 8px rgba(0,0,0,.08);
                    border:1px solid #e0e0e0;margin:6px;display:inline-block;
                    width:calc(33% - 12px);min-width:180px;vertical-align:top;">
          <div style="background:#2e7d32;padding:10px 14px;text-align:center;">
            <div style="font-size:13px;font-weight:900;color:#fff;">
              ✅ {name} District
            </div>
          </div>
          <table style="border-collapse:collapse;width:100%;">
            <tr>
              <td style="padding:8px 14px;border-bottom:1px solid #f0f0f0;
                         font-size:12px;color:#555;width:28px;">⬇</td>
              <td style="padding:8px 4px 8px 0;border-bottom:1px solid #f0f0f0;
                         font-size:12px;font-weight:600;color:#333;">Was:</td>
              <td style="padding:8px 14px 8px 8px;border-bottom:1px solid #f0f0f0;">
                <span style="{prev_badge}padding:2px 8px;border-radius:3px;
                             font-size:10px;font-weight:700;">
                  {prev_sev.upper()}
                </span>
              </td>
            </tr>
            <tr style="background:rgba(46,125,50,.04);">
              <td style="padding:8px 14px;border-bottom:1px solid #f0f0f0;
                         font-size:12px;color:#555;">✅</td>
              <td style="padding:8px 4px 8px 0;border-bottom:1px solid #f0f0f0;
                         font-size:12px;font-weight:600;color:#333;">Now:</td>
              <td style="padding:8px 14px 8px 8px;border-bottom:1px solid #f0f0f0;">
                <span style="{new_badge}padding:2px 8px;border-radius:3px;
                             font-size:10px;font-weight:700;">
                  {new_sev.upper()}
                </span>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 14px 10px;font-size:12px;color:#555;">🕐</td>
              <td style="padding:8px 4px 10px 0;font-size:12px;
                         font-weight:600;color:#333;">Scanned:</td>
              <td style="padding:8px 14px 10px 8px;font-size:12px;color:#333;">
                {reported_at_ist} Hrs IST
              </td>
            </tr>
          </table>
        </div>"""

    district_list = ", ".join(r["name"].title() for r in normalised)

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#e8eaf0;
             margin:0;padding:0;">
  <div style="max-width:760px;margin:20px auto;background:#fff;border-radius:12px;
              box-shadow:0 4px 20px rgba(0,0,0,.12);overflow:hidden;">

    <!-- Green header -->
    <div style="background:#2e7d32;padding:0;">
      <div style="background:rgba(0,0,0,.10);height:4px;"></div>
      <table style="border-collapse:collapse;width:100%;padding:20px 28px;">
        <tr>
          <td style="vertical-align:middle;padding:20px 0 20px 28px;">
            <table style="border-collapse:collapse;">
              <tr>
                <td style="vertical-align:middle;padding-right:14px;">
                  <svg width="36" height="36" viewBox="0 0 36 36"
                       xmlns="http://www.w3.org/2000/svg">
                    <circle cx="18" cy="18" r="16"
                            fill="white" fill-opacity="0.2"
                            stroke="white" stroke-width="2"/>
                    <text x="18" y="24" text-anchor="middle"
                          font-size="18" fill="white">✓</text>
                  </svg>
                </td>
                <td style="vertical-align:middle;">
                  <div style="font-size:9px;color:rgba(255,255,255,.7);
                              text-transform:uppercase;letter-spacing:1px;
                              margin-bottom:3px;">
                    India Meteorological Department
                  </div>
                  <div style="font-size:20px;font-weight:900;color:#fff;">
                    IMD NOWCAST — ALL CLEAR (TPCODL)
                  </div>
                  <div style="font-size:12px;color:rgba(255,255,255,.85);margin-top:5px;">
                    Districts downgraded: {district_list}
                  </div>
                </td>
              </tr>
            </table>
          </td>
          <td style="vertical-align:middle;text-align:right;
                     padding:20px 28px 20px 16px;white-space:nowrap;width:1%;">
            <div style="display:inline-block;background:rgba(0,0,0,.20);
                        border:2px solid rgba(255,255,255,.5);border-radius:8px;
                        padding:10px 18px;text-align:center;">
              <div style="font-size:13px;font-weight:700;color:#fff;
                          letter-spacing:.5px;">✅ ALL CLEAR</div>
              <div style="font-size:11px;color:rgba(255,255,255,.7);margin-top:4px;">
                🕐 {reported_at_ist} Hrs IST
              </div>
            </div>
          </td>
        </tr>
      </table>
      <div style="background:rgba(0,0,0,.10);height:3px;"></div>
    </div>

    <!-- Body -->
    <div style="padding:20px 22px;">

      <!-- Explanation paragraph -->
      <div style="background:#f1f8e9;border-left:4px solid #2e7d32;border-radius:6px;
                  padding:12px 16px;margin-bottom:18px;font-size:13px;color:#2e5e2e;
                  line-height:1.6;">
        The following TPCODL districts have been <strong>downgraded</strong> from
        Alert/Warning status as per the latest IMD Nowcast scan at
        <strong>{reported_at_ist} Hrs IST</strong>.
        Operations teams in these districts may stand down from heightened readiness.
      </div>

      <!-- Downgrade cards -->
      <div style="font-size:11px;font-weight:700;color:#1B3A6B;
                  text-transform:uppercase;letter-spacing:.6px;
                  margin-bottom:10px;">
        <span style="display:inline-block;width:3px;height:12px;
                     background:#2e7d32;border-radius:2px;
                     vertical-align:middle;margin-right:6px;"></span>
        DISTRICT STATUS UPDATE
      </div>
      <div>
        {cards_html}
      </div>

    </div>

    <!-- Footer -->
    {_imd_footer(reported_at_ist)}

  </div>
</body>
</html>"""


def build_normalisation_plain(normalised: list, reported_at_ist: str) -> str:
    lines = [
        "IMD TPCODL — ALL CLEAR NOTIFICATION",
        f"Scanned at: {reported_at_ist} Hrs (IST)",
        "=" * 60,
        "",
        "The following TPCODL districts have been downgraded from",
        "Alert/Warning status as per the latest IMD Nowcast scan.",
        "",
        "DISTRICT STATUS UPDATE",
        "-" * 40,
    ]
    for r in normalised:
        prev_color = r.get("prev_color", "orange")
        prev_sev   = SEVERITY_LABEL.get(prev_color, prev_color.title())
        new_sev    = r["severity"]
        issued     = extract_time_only(r.get("issued_at", ""))
        valid      = format_valid_upto(r.get("valid_upto", ""))
        lines.append(
            f"  {r['name']}  |  {prev_sev.upper()} → {new_sev.upper()}"
            f"  |  Scanned at {reported_at_ist} Hrs"
        )
    lines += ["", "=" * 60, "Source: India Meteorological Department (IMD)"]
    return "\n".join(lines)


def send_normalisation_email(normalised: list, all_records: list,
                             reported_at_ist: str):
    if not GMAIL_FROM or not GMAIL_PASS or not EMAIL_TO:
        print("[scraper] Email env vars not set — skipping normalisation email")
        return

    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]

    # Subject distinguishes full clear vs downgrade to watch
    all_green = all(r["warning_color"].lower() == "green" for r in normalised)
    prefix    = "RESOLVED" if all_green else "DOWNGRADED"
    changes = []
    for r in normalised:
        prev_sev = SEVERITY_LABEL.get(r.get("prev_color","orange"), "Alert")
        new_sev  = r["severity"]
        changes.append(f"{r['name'].title()} ({prev_sev}->{new_sev})")
    subject = f"IMD [{prefix}] -- {', '.join(changes)}"

    msg = MIMEMultipart("mixed")
    msg["From"]    = GMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = subject

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(
        build_normalisation_plain(normalised, reported_at_ist), "plain"))
    alt_part.attach(MIMEText(
        build_normalisation_html(normalised, reported_at_ist), "html"))
    msg.attach(alt_part)

    # ── Build PDF and attach (overview map only — no hover PNGs for normalisation) ──
    overview_png = DATA_DIR / f"district_warning_{STATE_ID}.png"
    try:
        pdf_bytes    = build_report_pdf(
            all_records=all_records,
            escalated=[],                  # no new escalations on normalisation
            reported_at_ist=reported_at_ist,
            overview_png=overview_png,
            hover_pngs=[],                 # no hover screenshots for normalisation
            is_normalisation=True,
        )
        pdf_filename = f"IMD_TPCODL_AllClear_{reported_at_ist.replace(':','')}IST.pdf"
        part = MIMEBase("application", "pdf")
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{pdf_filename}"')
        msg.attach(part)
        print(f"[scraper] Normalisation PDF attached: {pdf_filename} ({len(pdf_bytes)//1024} KB)")
    except Exception as e:
        print(f"[scraper] WARNING: could not build normalisation PDF — {e}")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, GMAIL_PASS)
            server.sendmail(GMAIL_FROM, recipients, msg.as_string())
        print(f"[scraper] Normalisation email sent → {', '.join(recipients)}")
        print(f"[scraper] Subject: {subject}")
    except Exception as e:
        print(f"[scraper] ERROR sending normalisation email: {e}")


# ─────────────────────────────────────────────────────────────
# SUPABASE — UPSERT ALL 8 ROWS EVERY RUN
# ─────────────────────────────────────────────────────────────

def upsert_district_warnings(district_records: list, meta: dict, sb: Client):
    rows_to_upsert = []
    for r in district_records:
        rows_to_upsert.append({
            "scraped_at":            meta["scraped_at"],
            "state_id":              int(meta["state_id"]),
            "type":                  r.get("type", "district"),
            "name":                  r["name"],
            "warning_color":         r.get("warning_color", ""),
            "severity":              r.get("severity", ""),
            "issued_at":             r.get("issued_at") or None,
            "valid_upto":            r.get("valid_upto") or None,
            "balloon_text":          r.get("balloon_text") or None,
            "rain_description":      r.get("rain_description") or None,
            "thunderstorm_desc":     r.get("thunderstorm_desc") or None,
            "lightning_probability": r.get("lightning_probability") or None,
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

    reported_at_ist = ist_now_human()
    scraped_at_utc  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meta = {"scraped_at": scraped_at_utc, "state_id": STATE_ID}

    # ── District page + balloon hovers ───────────────────────
    district_html, balloon_data = load_district_page(
        DISTRICT_URL, f"district_warning_{STATE_ID}.png"
    )
    district_records = extract_district_records(district_html)
    district_records = enrich_district_from_balloons(district_records, balloon_data)

    print(f"\n[scraper] District records: {len(district_records)}")
    if not district_records:
        print("\n[scraper] ERROR: no district data extracted — aborting")
        sys.exit(1)

    # ── Supabase client + previous state ─────────────────────
    sb         = None
    prev_state: dict = {}
    if SUPABASE_KEY:
        try:
            sb         = create_client(SUPABASE_URL, SUPABASE_KEY)
            prev_state = read_previous_state(sb)
        except Exception as e:
            print(f"[scraper] Supabase client error: {e}")
    else:
        print("[scraper] SUPABASE_KEY not set — skipping Supabase operations")

    # ── Severity summary ──────────────────────────────────────
    print("\n[scraper] District severity summary:")
    counts = Counter(r["severity"] for r in district_records)
    print("  " + "  ".join(f"{s}={c}" for s, c in sorted(counts.items())))

    # ── Escalation & normalisation checks ────────────────────
    escalated   = check_tpcodl_escalation(district_records, prev_state)
    normalised  = check_tpcodl_normalisation(district_records, prev_state)

    # All currently orange/red districts (for full email body)
    currently_active = [
        r for r in district_records
        if COLOR_RANK.get(r["warning_color"].lower(), 0) >= 2
    ]

    # ── Alert email (escalation) ──────────────────────────────
    if escalated:
        print(f"\n[scraper] {len(escalated)} escalation(s) — "
              f"{len(currently_active)} total active — sending alert email...")
        escalated_names = [r["name"].upper() for r in escalated]
        hover_pngs      = take_escalated_hover_screenshots(escalated_names)
        send_alert_email(escalated, currently_active, district_records,
                         reported_at_ist, hover_pngs)
    else:
        print("\n[scraper] No TPCODL escalations — no alert email sent")

    # ── Normalisation email (downgrade) ───────────────────────
    if normalised:
        print(f"\n[scraper] {len(normalised)} normalisation(s) — sending all-clear email...")
        send_normalisation_email(normalised, district_records, reported_at_ist)
    else:
        print("[scraper] No TPCODL normalisations — no all-clear email sent")

    # ── Upsert ALL 8 rows to Supabase every run ───────────────
    if sb:
        upsert_district_warnings(district_records, meta, sb)

    # ── Save CSV ──────────────────────────────────────────────
    save_csv(
        "district_warnings_latest.csv",
        district_records,
        meta,
        extra_fields=["issued_at", "valid_upto", "rain_description",
                      "thunderstorm_desc", "lightning_probability"],
    )
    print(f"[scraper] Saved district_warnings_latest.csv ({len(district_records)} rows)")
    print("\n[scraper] SUCCESS")


if __name__ == "__main__":
    main()
