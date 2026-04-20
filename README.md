# Plantation Utilization Heatmap — Report Generator

A Streamlit app that turns raw Zenoti exports into a single self-contained HTML report showing **therapist utilization** over the last 4 weeks.

You upload 3 files (optionally a 4th), click a button, and download an HTML report you can open in any browser, email to anyone, or stash anywhere. No server, no internet, no dependencies once the file exists.

---

## What the Report Shows

- **Scorecard** — utilization %, scheduled hours, block time, net available hours, appointment hours (with week-over-week deltas)
- **Hourly heatmap** — day-of-week × hour grid, 9 AM – 9 PM, color-coded by utilization
- **Daily table** — same numbers rolled up per day, with week-over-week trend per weekday
- **Weekend vs. weekday card** — how the two halves of the week compare
- **Member %** — share of appointment hours booked by members (only if a membership file is uploaded)
- **Block-type filters** — toggle which block-out categories (Training, Meeting, etc.) count against availability; everything recomputes in-browser

---

## Quick Start

```bash
pip install -r requirements.txt
streamlit run app.py
```

In the browser window that opens:

1. Upload **Attendance**, **Appointments**, and **Block Out Time** exports from Zenoti
2. (Optional) Upload a **Membership** export to unlock the Member % metric
3. Click **Generate Report**
4. Download the HTML file

---

## Input Files

All files are Zenoti exports. CSV, XLS (HTML table), and XLSX are all accepted.

| File                  | Must contain columns                                                            |
| --------------------- | ------------------------------------------------------------------------------- |
| Attendance            | `Schedule Status`, `Date`, `Schedule` (e.g. `10:00 AM - 03:00 PM`)              |
| Appointments          | `Start Time`, `End Time`                                                        |
| Block Out Time        | `Date`, `StartTime`, `EndTime`, `BlockOutTimeType`, `Block Out Time (in hours)` |
| Membership (optional) | `GuestCode`, `StartDate`, `EndDate`                                             |

All three (or four) files must come from the **same center** for the same date range. If the app detects a mismatch it warns you.

---

## Validating a Report

Two scripts independently re-parse the source files and compare against the JSON embedded in the HTML report:

```bash
# Fast sanity check (~2 seconds)
python scripts/quick_check.py <report.html> <attendance> <appointments> <blockout>

# Exhaustive adversarial validation (17 test sections)
python scripts/validate_report.py <report.html> <attendance> <appointments> <blockout>
```

If either script passes, the numbers in the report are arithmetically consistent with what was uploaded.

---

## Documentation

- **[HOW_IT_WORKS.md](HOW_IT_WORKS.md)** — plain-English walkthrough of every number in the report and the math behind it. Start here if you want to understand the logic.
- **[CLAUDE.md](CLAUDE.md)** — short architecture + parsing-quirks reference (originally written for AI coding agents; still useful as a terse overview).

---

## Tech Stack

- **Streamlit** — upload UI and file handling
- **pandas** + **openpyxl** + **lxml** — parses CSV / XLSX / XLS-as-HTML
- **Vanilla JS** (embedded in the generated HTML) — filtering, heatmap rendering, recalculation

All logic lives in a single file: `app.py` (~1,300 lines). The HTML template with its JS lives inside that same file as a string constant.
