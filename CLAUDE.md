# Utilization Report Generator

Streamlit app that turns Zenoti data exports into interactive 4-week therapist utilization HTML reports.

## Quick Start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Upload three Zenoti exports (Attendance, Appointments, Block Out Time) and optionally a Membership file, then click **Generate Report** to produce a self-contained HTML report with:

- Scorecard (utilization, scheduled hours, block time, net available, appointment hours)
- Hourly utilization heatmap (day-of-week x hour grid, 9AM-9PM)
- Daily utilization table with week-over-week trend
- Weekday vs weekend comparison

## Data Sources

| Upload | Format | Key Columns |
|---|---|---|
| Attendance | CSV / XLS / XLSX | `Schedule Status`, `Date`, `Schedule` (e.g. `10:00 AM - 03:00 PM`) |
| Appointments | CSV / XLS / XLSX | `Start Time`, `End Time` |
| Block Out Time | XLS (HTML table) / CSV / XLSX | `Date`, `StartTime`, `EndTime`, `BlockOutTimeType`, `Block Out Time (in hours)` |
| Membership (optional) | CSV / XLS / XLSX | `GuestCode`, `StartDate`, `EndDate` |

### Parsing Notes

- Attendance schedules use `'%I:%M %p'` format (space before AM/PM)
- Block-out times use `'%I:%M%p'` format (no space) — multiple formats tried
- Block-out end < start triggers AM/PM wrap fix (+720 minutes)
- Zero-duration appointments (start == end) are enhancement add-ons and are filtered out

## Architecture

**Python ETL -> Embedded JSON -> Self-Contained HTML**

1. Streamlit collects uploaded files
2. Python loaders parse and validate each file into DataFrames
3. `build_data_payload()` assembles a JSON payload with ATTENDANCE, APPOINTMENTS, BLOCKOUTS, and META
4. `generate_html_report()` injects the payload into an HTML template via `__DATA_PAYLOAD__` replacement
5. The HTML file contains all CSS, JS, and data inline — no external dependencies

### Core Calculation

All utilization is computed via minute-level overlap:

```python
max(0, min(end_min, hour_start + 60) - max(start_min, hour_start))
```

This clips any time range to a specific clock-hour bucket (9AM-9PM operating window).

### Key Metrics

- **Utilization** = appointment minutes / net available minutes per (day-of-week, hour) cell
- **Net Available** = scheduled hours - block time hours
- **Member %** = member appointment hours / total appointment hours (requires membership file)

## Validation Scripts

After generating a report, validate it against the source data:

```bash
# Quick sanity check (~2 seconds)
python scripts/quick_check.py <html_report> <attendance> <appointments> <blockout>

# Exhaustive adversarial validation
python scripts/validate_report.py <html_report> <attendance> <appointments> <blockout>
```

Both scripts now use a shared validation core with:

- mirror parsing (matches app.py behavior) and independent reference parsing
- reason-coded row-loss ledgers per source
- critical-vs-warning severity policy (critical failures control exit code)
- machine-readable JSON output for automation

### Optional Validation Flags

```bash
# Include membership end-to-end checks
--membership <membership_file>

# Emit machine-readable report
--json-out <output.json>

# Summary-only terminal output
--quiet
```

Examples:

```bash
python scripts/quick_check.py report.html attendance.csv appointments.csv blockout.csv \
  --json-out quick_report.json

python scripts/validate_report.py report.html attendance.csv appointments.csv blockout.csv \
  --membership membership.csv --json-out validate_report.json
```

### Severity and Exit Policy

- **Critical failures**: correctness/integrity violations (for example parse drift, hidden block types, membership mismatch)
- **Warnings**: suspicious but potentially expected conditions (for example zero-duration appointment filtering, duplicates, coverage gaps)
- **Info/pass**: successful invariants and metadata checks

Exit code policy:

- `0` when no critical failures
- `1` when one or more critical failures

### Triage Guide

When a critical check fails:

1. Review the row-loss ledger first (`schedule_parse_fail`, `datetime_parse_fail`, `time_parse_fail`).
2. If membership checks fail, verify guest-code normalization and membership date boundaries.
3. For invariant failures, compare `scorecard_util` vs `grid_util` in the period metrics section.
4. Use `--json-out` and inspect `checks[].id` plus `checks[].details` for exact mismatches.

### Local Test Suite

Install test dependencies and run:

```bash
pip install -r requirements-dev.txt
pytest
```

The tests are synthetic-only and include deterministic scenarios plus Hypothesis property tests for interval math, row-loss accounting, and period-boundary detection.

## Constants

- Operating hours: 9AM-9PM (`HOUR_START=9`, `HOUR_END=21`)
- Report periods: 4 weeks, rolling back from the latest date in the data
- Each period spans exactly 7 days

## Dependencies

- `streamlit` — web UI
- `pandas` — data loading and manipulation
- `openpyxl` — Excel file support
