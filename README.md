# IMD Warning Scraper

Automated scraper for [IMD Nowcast Warnings](https://mausam.imd.gov.in/) (Odisha — State ID 10).  
Runs every 3 hours via GitHub Actions, saves data to CSV/JSON, uploads to Supabase, and sends a Gmail alert when a **red (Warning)** alert is detected for monitored locations.

---

## 📁 Repository Structure

```
imd-warning-scraper/
├── .github/
│   └── workflows/
│       └── scrape_imd.yml          # GitHub Actions workflow
├── data/                           # Auto-generated output (committed each run)
│   ├── district_warnings_latest.csv
│   ├── district_warnings_latest.json
│   ├── station_warnings_latest.csv
│   ├── station_warnings_latest.json
│   ├── warnings_latest.csv         # Combined (district + station)
│   ├── warnings_latest.json
│   ├── warnings_history.jsonl      # Append-only history log
│   ├── district_warning_10.png     # Screenshot — district map
│   └── station_warning_10.png      # Screenshot — station map
├── scripts/
│   └── scrape_imd.py               # Main scraper
├── requirements.txt
└── README.md
```

---

## 📊 Data Schema

Both CSVs share the same column structure:

| Column | Description |
|---|---|
| `scraped_at` | UTC timestamp of the scrape run, e.g. `2026-05-11 22:15 UTC` |
| `state_id` | IMD state ID (10 = Odisha) |
| `type` | `district` or `station` |
| `name` | District or station name |
| `warning_color` | Human-readable colour: `green`, `yellow`, `orange`, `red` |
| `severity` | `No Warning`, `Watch`, `Alert`, `Warning` |
| `issued_at` | When the warning was issued, e.g. `2026-05-11 2200` (stations only) |
| `valid_upto` | Valid until time in Hrs, e.g. `0100` (stations only) |

### Severity / Colour mapping

| Color | Severity | Meaning |
|---|---|---|
| 🟢 `green` | No Warning | No adverse weather |
| 🟡 `yellow` | Watch | Be updated |
| 🟠 `orange` | Alert | Be prepared |
| 🔴 `red` | Warning | Take action |

---

## ⏰ Schedule

IMD updates its nowcast every 3 hours. The scraper runs **15 minutes after each update** to ensure fresh data is available:

| IMD Update (IST) | Scraper Runs (IST) | Cron (UTC) |
|---|---|---|
| 22:00 | 22:15 | `45 16 * * *` |
| 01:00 | 01:15 | `45 19 * * *` |
| 04:00 | 04:15 | `45 22 * * *` |
| 07:00 | 07:15 | `45 1 * * *` |
| 10:00 | 10:15 | `45 4 * * *` |
| 13:00 | 13:15 | `45 7 * * *` |
| 16:00 | 16:15 | `45 10 * * *` |
| 19:00 | 19:15 | `45 13 * * *` |

You can also trigger a manual run anytime from **Actions → Run workflow**.

---

## 🗄️ Supabase Integration

Each scrape run clears and repopulates two Supabase tables:

| Table | Contents |
|---|---|
| `district_warnings` | District-wise nowcast warnings |
| `station_warnings` | Station-wise nowcast warnings |

### One-time Supabase setup

**1. Create the tables** — run in Supabase SQL Editor:

```sql
CREATE TABLE district_warnings (
    id            bigserial PRIMARY KEY,
    scraped_at    text,
    state_id      text,
    type          text,
    name          text,
    warning_color text,
    severity      text,
    issued_at     text,
    valid_upto    text
);

CREATE TABLE station_warnings (
    id            bigserial PRIMARY KEY,
    scraped_at    text,
    state_id      text,
    type          text,
    name          text,
    warning_color text,
    severity      text,
    issued_at     text,
    valid_upto    text
);
```

**2. Disable RLS** (allows the anon key to insert/delete):

```sql
ALTER TABLE district_warnings DISABLE ROW LEVEL SECURITY;
ALTER TABLE station_warnings  DISABLE ROW LEVEL SECURITY;
```

---

## 🚨 Email Alerts

A Gmail alert is sent when a **red (Warning)** severity is detected for any of these monitored locations:

**District:** KHORDHA  
**Stations:** Bhubaneshwar AP, Cuttack, Khordha, Bhubaneshwar OUAT

The email includes:
- Which district/station triggered the alert
- Severity level and warning colour
- Issued at / Valid upto times
- Both map screenshots attached as PNG

---

## 🔐 GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add these secrets:

| Secret | Description |
|---|---|
| `GMAIL_FROM` | Your Gmail address, e.g. `you@gmail.com` |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password (not your login password) |
| `ALERT_EMAIL_TO` | Recipient(s), comma-separated, e.g. `a@x.com,b@y.com` |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon/publishable key |

### How to create a Gmail App Password

1. Enable **2-Step Verification** at [myaccount.google.com/security](https://myaccount.google.com/security)
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. App name: `IMD Scraper` → click **Create**
4. Copy the 16-character password and save it as `GMAIL_APP_PASSWORD`

---

## 📦 Dependencies

```
playwright>=1.53.0
beautifulsoup4==4.12.3
lxml==5.2.2
supabase==2.10.0
```

Install locally:
```bash
pip install -r requirements.txt
python -m playwright install chromium
```

---

## 🚀 Running Locally

```bash
# Clone the repo
git clone https://github.com/sonalmahapatra63-maker/imd-warning-scraper.git
cd imd-warning-scraper

# Install dependencies
pip install -r requirements.txt
python -m playwright install chromium

# Set environment variables
export IMD_STATE_ID=10
export GMAIL_FROM=you@gmail.com
export GMAIL_APP_PASSWORD=your_app_password
export ALERT_EMAIL_TO=recipient@example.com
export SUPABASE_URL=https://odrvhelastdyozjejqss.supabase.co
export SUPABASE_KEY=your_supabase_key

# Run
python scripts/scrape_imd.py
```

Output files will be saved to the `data/` folder.

---

## 📝 Notes

- The `data/` folder is committed to the repo after every successful scrape so you have a version-controlled history of warning states.
- `warnings_history.jsonl` is append-only and never deleted — it builds up a full timeline across all runs.
- All other files in `data/` are deleted and rewritten fresh each run.
- District `issued_at` and `valid_upto` are empty because the IMD district map page does not expose time information (unlike the station page).
