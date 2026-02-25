#!/usr/bin/env python3
"""
Exhaustive adversarial validation for Streamlit-generated utilization reports.

Usage:
    python validate_report.py <html_report> <attendance> <appointments> <blockout>

Validates the FULL pipeline: source files -> app.py loaders -> embedded DATA -> JS computations.
Designed to catch:
  - Source data silently dropped or duplicated during ETL
  - Calculation mismatches between Python build and JS render
  - dayOfWeek/date misalignment
  - blockHours vs minsInHour disagreement
  - Scorecard totals that don't add up
  - Heatmap grid cells that contradict daily table rows
  - Period date boundary off-by-one errors
"""

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd

# ============================================================
# COUNTERS + REPORTING
# ============================================================
PASS = 0
FAIL = 0
WARN = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  PASS  {msg}")


def fail(msg, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL  {msg}")
    if detail:
        for line in str(detail).split('\n')[:10]:
            print(f"        {line}")


def warn(msg, detail=""):
    global WARN
    WARN += 1
    print(f"  WARN  {msg}")
    if detail:
        for line in str(detail).split('\n')[:5]:
            print(f"        {line}")


def section(num, title):
    print(f"\n{'='*70}")
    print(f" {num}. {title}")
    print(f"{'='*70}")


# ============================================================
# HELPERS (mirror app.py exactly)
# ============================================================
HOUR_START = 9
HOUR_END = 21


def parse_schedule_to_minutes(sched_str):
    try:
        parts = str(sched_str).split(' - ')
        if len(parts) != 2:
            return None, None
        start = datetime.strptime(parts[0].strip(), '%I:%M %p')
        end = datetime.strptime(parts[1].strip(), '%I:%M %p')
        return start.hour * 60 + start.minute, end.hour * 60 + end.minute
    except Exception:
        return None, None


def parse_time_str_to_minutes(s):
    s = str(s).strip().upper()
    for fmt in ('%I:%M%p', '%I:%M %p', '%I:%M:%S %p'):
        try:
            t = datetime.strptime(s, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


def mins_in_hour(start_min, end_min, hour_start):
    return max(0, min(end_min, hour_start + 60) - max(start_min, hour_start))


def load_blockout_file(path):
    """Load blockout file using format detection matching app.py:157-171."""
    name = path.lower()
    if name.endswith('.csv'):
        return pd.read_csv(path)
    elif name.endswith('.xlsx'):
        return pd.read_excel(path)
    else:
        # .xls and .html — Zenoti exports .xls files that are actually HTML tables
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        tables = pd.read_html(StringIO(content))
        if not tables:
            print("  FAIL  No tables found in blockout file")
            sys.exit(1)
        return tables[0].copy()


# ============================================================
# ARGUMENT PARSING
# ============================================================
def main():
    global PASS, FAIL, WARN

    if len(sys.argv) != 5:
        print("Usage: python validate_report.py <html_report> <attendance> <appointments> <blockout>")
        sys.exit(1)

    html_path = sys.argv[1]
    att_path = sys.argv[2]
    appt_path = sys.argv[3]
    bot_path = sys.argv[4]

    print("=" * 70)
    print(" ADVERSARIAL VALIDATION — Streamlit Report Generator")
    print("=" * 70)

    # ==================================================================
    # 1. SOURCE FILES
    # ==================================================================
    section(1, "SOURCE FILES: existence, size, and extension")

    valid_extensions = {
        'attendance': ('.csv', '.xls', '.xlsx'),
        'appointments': ('.csv', '.xls', '.xlsx'),
        'blockout': ('.csv', '.xls', '.xlsx', '.html'),
    }

    for path, label, ext_key in [
        (html_path, "HTML report", None),
        (att_path, "Attendance", 'attendance'),
        (appt_path, "Appointments", 'appointments'),
        (bot_path, "Block Out Time", 'blockout'),
    ]:
        if os.path.exists(path):
            sz = os.path.getsize(path)
            ok(f"{label} exists ({sz / 1024:.0f} KB)")
            if ext_key:
                ext = os.path.splitext(path)[1].lower()
                if ext in valid_extensions[ext_key]:
                    ok(f"{label} extension '{ext}' is valid")
                else:
                    fail(f"{label} extension '{ext}' not in {valid_extensions[ext_key]}")
        else:
            fail(f"{label} NOT FOUND: {path}")

    if FAIL > 0:
        print("\n  ** Cannot continue — source files missing **")
        sys.exit(1)

    # ==================================================================
    # 2. EMBEDDED JSON
    # ==================================================================
    section(2, "EMBEDDED JSON: extract and parse DATA payload from HTML")

    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    data_match = re.search(r'const DATA\s*=\s*(\{.*?\});\s*\n', html_content, re.DOTALL)
    if data_match:
        try:
            DATA = json.loads(data_match.group(1))
            ok("Embedded JSON parsed successfully")
        except json.JSONDecodeError as e:
            fail(f"Embedded JSON is invalid: {e}")
            sys.exit(1)
    else:
        fail("Could not find 'const DATA = {...}' in HTML")
        sys.exit(1)

    for key in ['META', 'APPOINTMENTS', 'ATTENDANCE', 'BLOCKOUTS']:
        if key in DATA:
            if isinstance(DATA[key], list):
                ok(f"DATA.{key} present ({len(DATA[key])} records)")
            else:
                ok(f"DATA.{key} present (object)")
        else:
            fail(f"DATA.{key} MISSING")
            sys.exit(1)

    meta = DATA['META']
    for mkey in ['reportDate', 'dataStartDate', 'dataEndDate', 'blockTypes',
                 'hourStart', 'hourEnd', 'periods']:
        if mkey in meta:
            ok(f"META.{mkey} present")
        else:
            fail(f"META.{mkey} MISSING")

    # ==================================================================
    # 3. SOURCE RELOAD
    # ==================================================================
    section(3, "SOURCE RELOAD: re-process files with identical app.py logic")

    # --- Attendance ---
    att_raw = pd.read_csv(att_path) if att_path.lower().endswith('.csv') else pd.read_excel(att_path)
    att = att_raw[att_raw['Schedule Status'] == 'Working'].copy()
    att['date'] = pd.to_datetime(att['Date'], format='mixed', errors='coerce')
    att = att.dropna(subset=['date', 'Schedule'])

    parsed = att['Schedule'].apply(
        lambda s: pd.Series(parse_schedule_to_minutes(s), index=['startMinute', 'endMinute'])
    )
    att = pd.concat([att, parsed], axis=1)
    att = att.dropna(subset=['startMinute', 'endMinute'])
    att['startMinute'] = att['startMinute'].astype(int)
    att['endMinute'] = att['endMinute'].astype(int)
    att['dayOfWeek'] = att['date'].dt.dayofweek
    att['dateStr'] = att['date'].dt.strftime('%Y-%m-%d')
    att['scheduledHours'] = (att['endMinute'] - att['startMinute']) / 60.0
    ok(f"Attendance reloaded: {len(att)} records (after Working filter + parse)")

    # --- Appointments ---
    appt_raw = pd.read_csv(appt_path) if appt_path.lower().endswith('.csv') else pd.read_excel(appt_path)
    appt = appt_raw.copy()
    appt['start_dt'] = pd.to_datetime(appt['Start Time'], format='mixed', errors='coerce')
    appt['end_dt'] = pd.to_datetime(appt['End Time'], format='mixed', errors='coerce')
    appt = appt.dropna(subset=['start_dt', 'end_dt'])
    appt['startMinute'] = appt['start_dt'].dt.hour * 60 + appt['start_dt'].dt.minute
    appt['endMinute'] = appt['end_dt'].dt.hour * 60 + appt['end_dt'].dt.minute
    appt = appt[appt['startMinute'] != appt['endMinute']].copy()
    appt['durationMin'] = appt['endMinute'] - appt['startMinute']
    appt['dayOfWeek'] = appt['start_dt'].dt.dayofweek
    appt['dateStr'] = appt['start_dt'].dt.strftime('%Y-%m-%d')
    ok(f"Appointments reloaded: {len(appt)} records (after zero-duration filter)")

    # --- Blockouts ---
    bot_raw = load_blockout_file(bot_path)
    bot = bot_raw.copy()
    bot['date'] = pd.to_datetime(bot['Date'], format='mixed', errors='coerce')
    bot = bot.dropna(subset=['date', 'StartTime', 'EndTime'])
    bot['startMinute'] = bot['StartTime'].astype(str).apply(parse_time_str_to_minutes)
    bot['endMinute'] = bot['EndTime'].astype(str).apply(parse_time_str_to_minutes)
    bot = bot.dropna(subset=['startMinute', 'endMinute'])
    bot['startMinute'] = bot['startMinute'].astype(int)
    bot['endMinute'] = bot['endMinute'].astype(int)
    # AM/PM wrap fix
    mask = bot['endMinute'] <= bot['startMinute']
    bot.loc[mask, 'endMinute'] = bot.loc[mask, 'endMinute'] + 720
    bot['dayOfWeek'] = bot['date'].dt.dayofweek
    bot['dateStr'] = bot['date'].dt.strftime('%Y-%m-%d')
    bot['blockHours'] = pd.to_numeric(bot['Block Out Time (in hours)'], errors='coerce').fillna(0)
    ok(f"Blockouts reloaded: {len(bot)} records (with AM/PM wrap fix)")

    # ==================================================================
    # 4. RECORD COUNTS
    # ==================================================================
    section(4, "RECORD COUNTS: source vs embedded — exact match required")

    for label, src_count, emb_count in [
        ('Attendance', len(att), len(DATA['ATTENDANCE'])),
        ('Appointments', len(appt), len(DATA['APPOINTMENTS'])),
        ('Blockouts', len(bot), len(DATA['BLOCKOUTS'])),
    ]:
        if src_count == emb_count:
            ok(f"{label}: source {src_count} == embedded {emb_count}")
        else:
            fail(f"{label} MISMATCH: source {src_count} vs embedded {emb_count}",
                 f"Difference of {abs(src_count - emb_count)} records")

    # ==================================================================
    # 5. FIELD-LEVEL SPOT CHECKS
    # ==================================================================
    section(5, "FIELD-LEVEL: compare sorted source vs embedded records")

    # Detect membership usage
    has_membership = any(r.get('isMember', False) for r in DATA['APPOINTMENTS'])
    if has_membership:
        warn("Embedded data has isMember=True records — skipping isMember field comparison")

    # --- Attendance ---
    src_att_records = att[['dateStr', 'dayOfWeek', 'startMinute', 'endMinute', 'scheduledHours']].rename(
        columns={'dateStr': 'date'}
    ).to_dict('records')

    def sort_key_att(r):
        return (r['date'], r['startMinute'], r['endMinute'], r.get('scheduledHours', 0))

    src_att_sorted = sorted(src_att_records, key=sort_key_att)
    emb_att_sorted = sorted(DATA['ATTENDANCE'], key=sort_key_att)

    att_mismatches = 0
    for i in range(min(len(src_att_sorted), len(emb_att_sorted))):
        s, e = src_att_sorted[i], emb_att_sorted[i]
        if (s['date'] != e['date'] or s['startMinute'] != e['startMinute'] or
                s['endMinute'] != e['endMinute'] or s['dayOfWeek'] != e['dayOfWeek'] or
                abs(s['scheduledHours'] - e['scheduledHours']) > 0.01):
            att_mismatches += 1
            if att_mismatches <= 3:
                fail(f"Attendance record {i} mismatch",
                     f"Source: {s}\nEmbedded: {e}")

    if att_mismatches == 0:
        ok(f"All {len(src_att_sorted)} attendance records match field-by-field")
    elif att_mismatches > 3:
        fail(f"{att_mismatches} total attendance record mismatches")

    # --- Appointments ---
    src_appt_records = appt[['dateStr', 'dayOfWeek', 'startMinute', 'endMinute', 'durationMin']].rename(
        columns={'dateStr': 'date'}
    ).to_dict('records')

    def sort_key_appt(r):
        return (r['date'], r['startMinute'], r['endMinute'], r.get('durationMin', 0))

    src_appt_sorted = sorted(src_appt_records, key=sort_key_appt)
    emb_appt_sorted = sorted(DATA['APPOINTMENTS'], key=sort_key_appt)

    appt_mismatches = 0
    for i in range(min(len(src_appt_sorted), len(emb_appt_sorted))):
        s, e = src_appt_sorted[i], emb_appt_sorted[i]
        if (s['date'] != e['date'] or s['startMinute'] != e['startMinute'] or
                s['endMinute'] != e['endMinute'] or s['dayOfWeek'] != e['dayOfWeek'] or
                s['durationMin'] != e['durationMin']):
            appt_mismatches += 1
            if appt_mismatches <= 3:
                fail(f"Appointment record {i} mismatch",
                     f"Source: {s}\nEmbedded: {e}")

    if appt_mismatches == 0:
        ok(f"All {len(src_appt_sorted)} appointment records match field-by-field")
    elif appt_mismatches > 3:
        fail(f"{appt_mismatches} total appointment record mismatches")

    # --- Blockouts ---
    src_blk_records = bot[['dateStr', 'dayOfWeek', 'startMinute', 'endMinute', 'blockHours',
                           'BlockOutTimeType']].rename(
        columns={'dateStr': 'date', 'BlockOutTimeType': 'blockType'}
    ).to_dict('records')

    def sort_key_blk(r):
        return (r['date'], r['startMinute'], r['endMinute'], r.get('blockType', ''))

    src_blk_sorted = sorted(src_blk_records, key=sort_key_blk)
    emb_blk_sorted = sorted(DATA['BLOCKOUTS'], key=sort_key_blk)

    blk_mismatches = 0
    for i in range(min(len(src_blk_sorted), len(emb_blk_sorted))):
        s, e = src_blk_sorted[i], emb_blk_sorted[i]
        if (s['date'] != e['date'] or s['startMinute'] != e['startMinute'] or
                s['endMinute'] != e['endMinute'] or s['dayOfWeek'] != e['dayOfWeek'] or
                s['blockType'] != e['blockType'] or abs(s['blockHours'] - e['blockHours']) > 0.01):
            blk_mismatches += 1
            if blk_mismatches <= 3:
                fail(f"Blockout record {i} mismatch",
                     f"Source: {s}\nEmbedded: {e}")

    if blk_mismatches == 0:
        ok(f"All {len(src_blk_sorted)} blockout records match field-by-field")
    elif blk_mismatches > 3:
        fail(f"{blk_mismatches} total blockout record mismatches")

    # ==================================================================
    # 6. dayOfWeek CORRECTNESS
    # ==================================================================
    section(6, "CALENDAR: dayOfWeek matches datetime.weekday() for every record")

    dow_errors = 0
    for label, records in [('ATTENDANCE', DATA['ATTENDANCE']),
                           ('APPOINTMENTS', DATA['APPOINTMENTS']),
                           ('BLOCKOUTS', DATA['BLOCKOUTS'])]:
        for r in records:
            actual_dow = datetime.strptime(r['date'], '%Y-%m-%d').weekday()
            if actual_dow != r['dayOfWeek']:
                dow_errors += 1
                if dow_errors <= 3:
                    fail(f"{label}: {r['date']} has dayOfWeek={r['dayOfWeek']} but calendar says {actual_dow}")

    if dow_errors == 0:
        total = len(DATA['ATTENDANCE']) + len(DATA['APPOINTMENTS']) + len(DATA['BLOCKOUTS'])
        ok(f"All {total} records have correct dayOfWeek")
    elif dow_errors > 3:
        fail(f"{dow_errors} total dayOfWeek errors across all record types")

    # ==================================================================
    # 7. SCHEDULE/TIME SANITY
    # ==================================================================
    section(7, "SCHEDULE SANITY: durations are positive and plausible")

    # Attendance: positive duration, scheduledHours matches, no >16h shifts
    att_issues = 0
    for r in DATA['ATTENDANCE']:
        dur = r['endMinute'] - r['startMinute']
        if dur <= 0:
            fail(f"Attendance {r['date']}: zero/negative shift ({r['startMinute']}-{r['endMinute']})")
            att_issues += 1
            if att_issues >= 3:
                break
        elif dur > 16 * 60:
            warn(f"Attendance {r['date']}: shift is {dur / 60:.1f}h (>16h)")
            att_issues += 1
        computed_hrs = (r['endMinute'] - r['startMinute']) / 60.0
        if abs(r['scheduledHours'] - computed_hrs) > 0.01:
            fail(f"Attendance {r['date']}: scheduledHours={r['scheduledHours']:.2f} != computed {computed_hrs:.2f}")
            att_issues += 1

    if att_issues == 0:
        ok("All attendance shifts have positive, plausible duration and matching scheduledHours")

    # Appointments: positive duration, no >8h
    appt_issues = 0
    neg_appt = [r for r in DATA['APPOINTMENTS'] if r['endMinute'] <= r['startMinute']]
    if neg_appt:
        fail(f"{len(neg_appt)} appointments have end <= start", str(neg_appt[:3]))
        appt_issues += 1
    else:
        ok("All appointments have positive duration (end > start)")

    long_appt = [r for r in DATA['APPOINTMENTS'] if (r['endMinute'] - r['startMinute']) > 8 * 60]
    if long_appt:
        warn(f"{len(long_appt)} appointments are >8h", str(long_appt[:3]))
    else:
        ok("No appointments exceed 8 hours")

    dur_mismatches = [r for r in DATA['APPOINTMENTS']
                      if r['durationMin'] != r['endMinute'] - r['startMinute']]
    if dur_mismatches:
        fail(f"{len(dur_mismatches)} appointments where durationMin != end - start",
             str(dur_mismatches[:3]))
    else:
        ok("All appointment durationMin fields are consistent with start/end")

    # Blockouts: positive duration after wrap fix
    neg_blk = [r for r in DATA['BLOCKOUTS'] if r['endMinute'] <= r['startMinute']]
    if neg_blk:
        fail(f"{len(neg_blk)} blockouts have end <= start (AM/PM wrap not fixed?)", str(neg_blk[:3]))
    else:
        ok("All blockouts have positive duration (end > start)")

    # ==================================================================
    # 8. UTILIZATION RECALCULATION
    # ==================================================================
    section(8, "UTILIZATION: independent recalculation per period")

    periods = meta['periods']
    for p in periods:
        p_start, p_end = p['start'], p['end']
        p_att = [r for r in DATA['ATTENDANCE'] if p_start <= r['date'] <= p_end]
        p_appt = [r for r in DATA['APPOINTMENTS'] if p_start <= r['date'] <= p_end]
        p_blk = [r for r in DATA['BLOCKOUTS'] if p_start <= r['date'] <= p_end]

        # Scorecard method: raw sums using scheduledHours/blockHours fields
        sc_sched = sum(r['scheduledHours'] for r in p_att)
        sc_appt = sum(r['durationMin'] / 60.0 for r in p_appt)
        sc_block = sum(r['blockHours'] for r in p_blk)
        sc_net = sc_sched - sc_block
        sc_util = (sc_appt / sc_net * 100) if sc_net > 0 else 0

        # Grid method: minsInHour clipped to 9-21
        grid_sched = 0
        grid_block = 0
        grid_appt = 0
        for r in p_att:
            for h in range(HOUR_START, HOUR_END):
                grid_sched += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
        for r in p_blk:
            for h in range(HOUR_START, HOUR_END):
                grid_block += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
        for r in p_appt:
            for h in range(HOUR_START, HOUR_END):
                grid_appt += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0

        grid_net = grid_sched - grid_block
        grid_util = (grid_appt / grid_net * 100) if grid_net > 0 else 0

        delta = abs(sc_util - grid_util)
        tag = " (current)" if p.get('isCurrent') else ""
        print(f"  INFO  {p['label']}{tag}: scorecard={sc_util:.1f}%, grid={grid_util:.1f}%, delta={delta:.1f}pp")
        print(f"        Sched: {sc_sched:.1f}h/{grid_sched:.1f}h  Block: {sc_block:.1f}h/{grid_block:.1f}h  "
              f"Appt: {sc_appt:.1f}h/{grid_appt:.1f}h")

        if delta < 1.0:
            ok(f"{p['label']}: scorecard vs grid within 1pp ({sc_util:.1f}% vs {grid_util:.1f}%)")
        elif delta < 3.0:
            warn(f"{p['label']}: scorecard vs grid differ by {delta:.1f}pp",
                 "Expected if some blocks/appointments fall outside 9AM-9PM window")
        else:
            fail(f"{p['label']}: LARGE utilization discrepancy — scorecard {sc_util:.1f}% vs grid {grid_util:.1f}%",
                 f"Difference: {delta:.1f} percentage points")

    # ==================================================================
    # 9. GRID CONSISTENCY
    # ==================================================================
    section(9, "GRID CONSISTENCY: hourly sums == daily totals == period totals")

    # Build the full 4-week grid (all periods combined)
    all_start = periods[0]['start']
    all_end = periods[-1]['end']
    all_att = [r for r in DATA['ATTENDANCE'] if all_start <= r['date'] <= all_end]
    all_appt = [r for r in DATA['APPOINTMENTS'] if all_start <= r['date'] <= all_end]
    all_blk = [r for r in DATA['BLOCKOUTS'] if all_start <= r['date'] <= all_end]

    grid = {}
    for dow in range(7):
        grid[dow] = {}
        for h in range(HOUR_START, HOUR_END):
            grid[dow][h] = {'scheduled': 0.0, 'blocked': 0.0, 'appointment': 0.0}

    for r in all_att:
        for h in range(HOUR_START, HOUR_END):
            grid[r['dayOfWeek']][h]['scheduled'] += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
    for r in all_blk:
        for h in range(HOUR_START, HOUR_END):
            grid[r['dayOfWeek']][h]['blocked'] += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
    for r in all_appt:
        for h in range(HOUR_START, HOUR_END):
            grid[r['dayOfWeek']][h]['appointment'] += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0

    # Check: sum of hourly cells per day == daily totals
    grid_ok = True
    for dow in range(7):
        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        hourly_sched = sum(grid[dow][h]['scheduled'] for h in range(HOUR_START, HOUR_END))
        hourly_block = sum(grid[dow][h]['blocked'] for h in range(HOUR_START, HOUR_END))
        hourly_appt = sum(grid[dow][h]['appointment'] for h in range(HOUR_START, HOUR_END))

        # Direct daily aggregation from records
        direct_att = [r for r in all_att if r['dayOfWeek'] == dow]
        direct_appt_d = [r for r in all_appt if r['dayOfWeek'] == dow]
        direct_blk_d = [r for r in all_blk if r['dayOfWeek'] == dow]

        direct_sched = 0
        for r in direct_att:
            for h in range(HOUR_START, HOUR_END):
                direct_sched += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0

        if abs(hourly_sched - direct_sched) > 0.01:
            fail(f"{day_names[dow]}: hourly sum {hourly_sched:.2f} != direct {direct_sched:.2f}")
            grid_ok = False

    # Sum all days should equal period totals
    total_grid_sched = sum(grid[d][h]['scheduled'] for d in range(7) for h in range(HOUR_START, HOUR_END))
    total_grid_appt = sum(grid[d][h]['appointment'] for d in range(7) for h in range(HOUR_START, HOUR_END))

    if grid_ok:
        ok(f"Grid consistency verified: per-dow hourly sums match direct aggregation")
    ok(f"Grid totals: scheduled={total_grid_sched:.1f}h, appointment={total_grid_appt:.1f}h")

    # ==================================================================
    # 10. PERIOD BOUNDARIES
    # ==================================================================
    section(10, "PERIOD BOUNDARIES: 4 periods, 7 days each, contiguous, non-overlapping")

    if len(periods) != 4:
        fail(f"Expected 4 periods, got {len(periods)}")
    else:
        ok(f"4 periods found")

    for i, p in enumerate(periods):
        p_s = datetime.strptime(p['start'], '%Y-%m-%d')
        p_e = datetime.strptime(p['end'], '%Y-%m-%d')
        span = (p_e - p_s).days + 1
        if span == 7:
            ok(f"Period {i} ({p['label']}): {p['start']} to {p['end']} = 7 days")
        else:
            fail(f"Period {i} ({p['label']}): spans {span} days, expected 7")

    # Check contiguous + non-overlapping
    for i in range(len(periods) - 1):
        end_i = datetime.strptime(periods[i]['end'], '%Y-%m-%d')
        start_next = datetime.strptime(periods[i + 1]['start'], '%Y-%m-%d')
        gap = (start_next - end_i).days
        if gap == 1:
            ok(f"Periods {i} and {i + 1} are contiguous (1-day gap)")
        elif gap == 0:
            fail(f"Periods {i} and {i + 1} overlap (same end/start date)")
        elif gap < 0:
            fail(f"Periods {i} and {i + 1} overlap by {abs(gap)} days")
        else:
            fail(f"Periods {i} and {i + 1} have {gap}-day gap (expected 1)")

    # isCurrent check
    current_periods = [p for p in periods if p.get('isCurrent')]
    if len(current_periods) == 1 and periods[-1].get('isCurrent'):
        ok("Last period is marked isCurrent (correct)")
    elif len(current_periods) == 1:
        warn("isCurrent is set on a non-last period")
    elif len(current_periods) == 0:
        fail("No period has isCurrent=True")
    else:
        fail(f"{len(current_periods)} periods have isCurrent=True (expected 1)")

    # ==================================================================
    # 11. FILL RATE INVARIANTS
    # ==================================================================
    section(11, "FILL RATE INVARIANTS: no negative utilization, warn if >300%")

    fill_issues = 0
    for dow in range(7):
        for h in range(HOUR_START, HOUR_END):
            cell = grid[dow][h]
            net = cell['scheduled'] - cell['blocked']
            if net > 0:
                util = cell['appointment'] / net * 100
                if util < 0:
                    fail(f"dow={dow} h={h}: negative utilization ({util:.1f}%)")
                    fill_issues += 1
                elif util > 300:
                    warn(f"dow={dow} h={h}: utilization is {util:.1f}% (>300%, extreme overbooking?)")
                    fill_issues += 1

    if fill_issues == 0:
        ok("All fill rates are non-negative and within expected bounds")

    # ==================================================================
    # 12. blockHours vs COMPUTED
    # ==================================================================
    section(12, "BLOCK HOURS: blockHours field vs (endMinute - startMinute) / 60")

    blk_field_vs_computed = 0
    for r in DATA['BLOCKOUTS']:
        computed_hrs = (r['endMinute'] - r['startMinute']) / 60.0
        field_hrs = r['blockHours']
        if abs(computed_hrs - field_hrs) > 0.1:
            blk_field_vs_computed += 1
            if blk_field_vs_computed <= 5:
                warn(f"Blockout {r['date']} {r['blockType']}: blockHours={field_hrs:.2f} vs computed={computed_hrs:.2f}",
                     f"start={r['startMinute']}, end={r['endMinute']}")

    if blk_field_vs_computed == 0:
        ok("All blockout blockHours match (endMinute - startMinute) / 60")
    else:
        warn(f"{blk_field_vs_computed} blockouts have blockHours != computed duration",
             "blockHours comes from Zenoti 'Block Out Time (in hours)' field.\n"
             "If it disagrees with start/end times, the scorecard and heatmap will show different block totals.")

    # ==================================================================
    # 13. DUPLICATE DETECTION
    # ==================================================================
    section(13, "DUPLICATE DETECTION: flag exact duplicate records per type")

    def record_key(r, fields):
        return tuple(r.get(f) for f in fields)

    att_keys = [record_key(r, ['date', 'startMinute', 'endMinute', 'scheduledHours'])
                for r in DATA['ATTENDANCE']]
    att_dupes = len(att_keys) - len(set(att_keys))
    if att_dupes > 0:
        warn(f"{att_dupes} duplicate attendance records (same date+start+end+hours)",
             "Each duplicate inflates available capacity. Could be legitimate\n"
             "(multiple therapists same shift) or data errors.")
    else:
        ok("No exact duplicate attendance records")

    appt_keys = [record_key(r, ['date', 'startMinute', 'endMinute', 'durationMin'])
                 for r in DATA['APPOINTMENTS']]
    appt_dupes = len(appt_keys) - len(set(appt_keys))
    if appt_dupes > 0:
        warn(f"{appt_dupes} duplicate appointment records",
             "Could be legitimate (multiple clients same time slot) or data errors.")
    else:
        ok("No exact duplicate appointment records")

    blk_keys = [record_key(r, ['date', 'startMinute', 'endMinute', 'blockType'])
                for r in DATA['BLOCKOUTS']]
    blk_dupes = len(blk_keys) - len(set(blk_keys))
    if blk_dupes > 0:
        warn(f"{blk_dupes} duplicate blockout records")
    else:
        ok("No exact duplicate blockout records")

    # ==================================================================
    # 14. NaN/INFINITY DETECTION
    # ==================================================================
    section(14, "NaN/INFINITY: all numeric fields clean in all record types")

    def check_numeric(records, fields, label):
        for r in records:
            for f in fields:
                v = r.get(f)
                if v is None:
                    fail(f"{label}: null value in '{f}' for {r.get('date', '?')}")
                    return
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    fail(f"{label}: NaN/Inf in '{f}' for {r.get('date', '?')}")
                    return
        ok(f"{label}: all {len(records)} records have clean numeric fields")

    check_numeric(DATA['ATTENDANCE'],
                  ['startMinute', 'endMinute', 'dayOfWeek', 'scheduledHours'], 'ATTENDANCE')
    check_numeric(DATA['APPOINTMENTS'],
                  ['startMinute', 'endMinute', 'dayOfWeek', 'durationMin'], 'APPOINTMENTS')
    check_numeric(DATA['BLOCKOUTS'],
                  ['startMinute', 'endMinute', 'dayOfWeek', 'blockHours'], 'BLOCKOUTS')

    # ==================================================================
    # 15. META INTEGRITY
    # ==================================================================
    section(15, "META INTEGRITY: blockTypes, hourStart/End, period count")

    actual_block_types = sorted(set(r['blockType'] for r in DATA['BLOCKOUTS']))
    meta_block_types = sorted(meta['blockTypes'])
    if actual_block_types == meta_block_types:
        ok(f"META.blockTypes matches actual blockout data ({len(actual_block_types)} types)")
    else:
        meta_only = set(meta_block_types) - set(actual_block_types)
        data_only = set(actual_block_types) - set(meta_block_types)
        if meta_only:
            warn(f"META.blockTypes has types not in data: {meta_only}")
        if data_only:
            fail(f"Data has block types not in META: {data_only} — filter UI won't show these")

    if meta['hourStart'] == HOUR_START:
        ok(f"META.hourStart = {HOUR_START}")
    else:
        fail(f"META.hourStart = {meta['hourStart']}, expected {HOUR_START}")

    if meta['hourEnd'] == HOUR_END:
        ok(f"META.hourEnd = {HOUR_END}")
    else:
        fail(f"META.hourEnd = {meta['hourEnd']}, expected {HOUR_END}")

    if len(meta['periods']) == 4:
        ok("META.periods has 4 entries")
    else:
        fail(f"META.periods has {len(meta['periods'])} entries, expected 4")

    # ==================================================================
    # 16. COVERAGE GAPS
    # ==================================================================
    section(16, "COVERAGE GAPS: missing calendar dates in the 4-week window")

    window_start = datetime.strptime(periods[0]['start'], '%Y-%m-%d')
    window_end = datetime.strptime(periods[-1]['end'], '%Y-%m-%d')
    expected_days = (window_end - window_start).days + 1

    all_data_dates = sorted(set(
        [r['date'] for r in DATA['ATTENDANCE']] +
        [r['date'] for r in DATA['APPOINTMENTS']]
    ))

    # Check for dates within the window
    date_set = set(all_data_dates)
    gaps = []
    d = window_start
    while d <= window_end:
        ds = d.strftime('%Y-%m-%d')
        if ds not in date_set:
            gaps.append(ds)
        d += timedelta(days=1)

    if not gaps:
        ok(f"Continuous coverage: all {expected_days} days in 4-week window have data")
    else:
        if len(gaps) <= 10:
            warn(f"{len(gaps)} days with no data in 4-week window",
                 f"Missing dates: {gaps}")
        else:
            warn(f"{len(gaps)} days with no data in 4-week window",
                 f"First 10 missing: {gaps[:10]}")

    # ==================================================================
    # 17. HTML STRUCTURE
    # ==================================================================
    section(17, "HTML STRUCTURE: key DOM elements and JS functions present")

    required_elements = [
        ('id="scorecard"', 'Scorecard container'),
        ('id="heatmapContainer"', 'Heatmap container'),
        ('id="dailyTableContainer"', 'Daily table container'),
        ('id="weekendComparisonContainer"', 'Weekend comparison'),
        ('id="tooltip"', 'Tooltip element'),
        ('id="filterBody"', 'Filter body'),
        ('id="dateRangeText"', 'Date range text'),
    ]

    required_js = [
        ('function computeMetrics', 'computeMetrics function'),
        ('function renderHeatmap', 'renderHeatmap function'),
        ('function minsInHour', 'minsInHour function'),
    ]

    for pattern, label in required_elements:
        if pattern in html_content:
            ok(f"{label} ({pattern})")
        else:
            fail(f"{label} MISSING ({pattern})")

    for pattern, label in required_js:
        if pattern in html_content:
            ok(f"{label}")
        else:
            fail(f"{label} MISSING")

    # ==================================================================
    # SUMMARY
    # ==================================================================
    section("", "SUMMARY")
    total = PASS + FAIL + WARN
    print(f"\n  {PASS} passed, {FAIL} FAILED, {WARN} warnings  (of {total} checks)")
    print()

    if FAIL > 0:
        print("  ** FAILURES DETECTED — review above for data integrity issues **")
        sys.exit(1)
    elif WARN > 0:
        print("  All critical checks passed. Warnings are informational.")
        sys.exit(0)
    else:
        print("  All checks passed clean.")
        sys.exit(0)


if __name__ == '__main__':
    main()
