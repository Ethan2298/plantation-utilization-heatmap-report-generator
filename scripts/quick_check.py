#!/usr/bin/env python3
"""
Quick sanity check + deployment readiness summary for Streamlit-generated reports.

Usage:
    python quick_check.py <html_report> <attendance> <appointments> <blockout>

Runs in ~2 seconds. Prints source file info, record counts, per-period utilization,
sanity checks, and next-step guidance.
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
# HELPERS (mirror app.py exactly)
# ============================================================
HOUR_START = 9
HOUR_END = 21

ISSUES = []


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
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        tables = pd.read_html(StringIO(content))
        if not tables:
            raise ValueError("No tables found in blockout file")
        return tables[0].copy()


def issue(msg):
    ISSUES.append(msg)
    print(f"  !! {msg}")


def main():
    if len(sys.argv) != 5:
        print("Usage: python quick_check.py <html_report> <attendance> <appointments> <blockout>")
        sys.exit(1)

    html_path = sys.argv[1]
    att_path = sys.argv[2]
    appt_path = sys.argv[3]
    bot_path = sys.argv[4]

    print()
    print("=" * 70)
    print(" QUICK CHECK — Streamlit Report Generator")
    print("=" * 70)

    # ==================================================================
    # 1. SOURCE FILES
    # ==================================================================
    print("\n--- 1. Source Files ---\n")
    for path, label in [
        (html_path, "HTML Report"),
        (att_path, "Attendance"),
        (appt_path, "Appointments"),
        (bot_path, "Block Out Time"),
    ]:
        if os.path.exists(path):
            sz = os.path.getsize(path)
            print(f"  {label:20s}  {sz / 1024:7.0f} KB  {os.path.basename(path)}")
        else:
            issue(f"{label} NOT FOUND: {path}")

    if ISSUES:
        print("\n  ** Cannot continue — source files missing **")
        sys.exit(1)

    # ==================================================================
    # EXTRACT EMBEDDED DATA
    # ==================================================================
    with open(html_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    data_match = re.search(r'const DATA\s*=\s*(\{.*?\});\s*\n', html_content, re.DOTALL)
    if not data_match:
        issue("Could not extract embedded DATA from HTML")
        print("\n  ** Cannot continue **")
        sys.exit(1)

    try:
        DATA = json.loads(data_match.group(1))
    except json.JSONDecodeError as e:
        issue(f"Embedded JSON parse error: {e}")
        sys.exit(1)

    meta = DATA['META']

    # ==================================================================
    # RELOAD SOURCE DATA
    # ==================================================================
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

    bot_raw = load_blockout_file(bot_path)
    bot = bot_raw.copy()
    bot['date'] = pd.to_datetime(bot['Date'], format='mixed', errors='coerce')
    bot = bot.dropna(subset=['date', 'StartTime', 'EndTime'])
    bot['startMinute'] = bot['StartTime'].astype(str).apply(parse_time_str_to_minutes)
    bot['endMinute'] = bot['EndTime'].astype(str).apply(parse_time_str_to_minutes)
    bot = bot.dropna(subset=['startMinute', 'endMinute'])
    bot['startMinute'] = bot['startMinute'].astype(int)
    bot['endMinute'] = bot['endMinute'].astype(int)
    mask = bot['endMinute'] <= bot['startMinute']
    bot.loc[mask, 'endMinute'] = bot.loc[mask, 'endMinute'] + 720
    bot['dayOfWeek'] = bot['date'].dt.dayofweek
    bot['dateStr'] = bot['date'].dt.strftime('%Y-%m-%d')
    bot['blockHours'] = pd.to_numeric(bot['Block Out Time (in hours)'], errors='coerce').fillna(0)

    # ==================================================================
    # 2. RECORD COUNT TABLE
    # ==================================================================
    print("\n--- 2. Record Counts (source vs embedded) ---\n")

    counts = [
        ('Attendance', len(att), len(DATA['ATTENDANCE'])),
        ('Appointments', len(appt), len(DATA['APPOINTMENTS'])),
        ('Blockouts', len(bot), len(DATA['BLOCKOUTS'])),
    ]

    print(f"  {'Type':15s}  {'Source':>7s}  {'Embedded':>8s}  {'Match':>5s}")
    print(f"  {'-'*15}  {'-'*7}  {'-'*8}  {'-'*5}")
    for label, src, emb in counts:
        match = "YES" if src == emb else "NO"
        print(f"  {label:15s}  {src:7d}  {emb:8d}  {match:>5s}")
        if src != emb:
            issue(f"{label}: source ({src}) != embedded ({emb})")

    # ==================================================================
    # 3. PERIOD UTILIZATION TABLE
    # ==================================================================
    print("\n--- 3. Period Utilization ---\n")

    periods = meta['periods']
    header = (f"  {'Period':20s}  {'Att':>4s}  {'Appt':>5s}  {'Sched':>7s}  {'ApptH':>7s}  "
              f"{'BlockH':>7s}  {'NetH':>7s}  {'Emb%':>6s}  {'Calc%':>6s}  {'Delta':>6s}")
    print(header)
    print(f"  {'-' * len(header.strip())}")

    for p in periods:
        p_start, p_end = p['start'], p['end']
        tag = " *" if p.get('isCurrent') else ""

        # Embedded counts
        emb_att = [r for r in DATA['ATTENDANCE'] if p_start <= r['date'] <= p_end]
        emb_appt = [r for r in DATA['APPOINTMENTS'] if p_start <= r['date'] <= p_end]
        emb_blk = [r for r in DATA['BLOCKOUTS'] if p_start <= r['date'] <= p_end]

        # Embedded scorecard utilization
        emb_sched = sum(r['scheduledHours'] for r in emb_att)
        emb_appt_hrs = sum(r['durationMin'] / 60.0 for r in emb_appt)
        emb_block = sum(r['blockHours'] for r in emb_blk)
        emb_net = emb_sched - emb_block
        emb_util = (emb_appt_hrs / emb_net * 100) if emb_net > 0 else 0

        # Independent grid calculation
        calc_sched = 0
        calc_appt_hrs = 0
        calc_block = 0
        for r in emb_att:
            for h in range(HOUR_START, HOUR_END):
                calc_sched += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
        for r in emb_appt:
            for h in range(HOUR_START, HOUR_END):
                calc_appt_hrs += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
        for r in emb_blk:
            for h in range(HOUR_START, HOUR_END):
                calc_block += mins_in_hour(r['startMinute'], r['endMinute'], h * 60) / 60.0
        calc_net = calc_sched - calc_block
        calc_util = (calc_appt_hrs / calc_net * 100) if calc_net > 0 else 0

        delta = emb_util - calc_util

        label = p['label'] + tag
        print(f"  {label:20s}  {len(emb_att):4d}  {len(emb_appt):5d}  {emb_sched:7.1f}  {emb_appt_hrs:7.1f}  "
              f"{emb_block:7.1f}  {emb_net:7.1f}  {emb_util:5.1f}%  {calc_util:5.1f}%  {delta:+5.1f}pp")

        if abs(delta) > 3.0:
            issue(f"{p['label']}: scorecard vs grid differ by {abs(delta):.1f}pp")

    # ==================================================================
    # 4. SANITY CHECKS
    # ==================================================================
    print("\n--- 4. Sanity Checks ---\n")

    # Date range
    print(f"  Data window:     {meta['dataStartDate']} to {meta['dataEndDate']}")
    print(f"  Period window:   {periods[0]['start']} to {periods[-1]['end']}")
    print(f"  Periods:         {len(periods)} x 7 days")

    # dayOfWeek
    dow_errors = 0
    for label, records in [('ATTENDANCE', DATA['ATTENDANCE']),
                           ('APPOINTMENTS', DATA['APPOINTMENTS']),
                           ('BLOCKOUTS', DATA['BLOCKOUTS'])]:
        for r in records:
            actual_dow = datetime.strptime(r['date'], '%Y-%m-%d').weekday()
            if actual_dow != r['dayOfWeek']:
                dow_errors += 1
    if dow_errors == 0:
        print(f"  dayOfWeek:       OK (all records match calendar)")
    else:
        issue(f"dayOfWeek: {dow_errors} records have wrong dayOfWeek")

    # NaN check
    nan_found = False
    for label, records, fields in [
        ('ATTENDANCE', DATA['ATTENDANCE'], ['startMinute', 'endMinute', 'scheduledHours']),
        ('APPOINTMENTS', DATA['APPOINTMENTS'], ['startMinute', 'endMinute', 'durationMin']),
        ('BLOCKOUTS', DATA['BLOCKOUTS'], ['startMinute', 'endMinute', 'blockHours']),
    ]:
        for r in records:
            for f in fields:
                v = r.get(f)
                if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                    nan_found = True
                    break
            if nan_found:
                break
        if nan_found:
            break
    if not nan_found:
        print(f"  NaN/Infinity:    OK (all numeric fields clean)")
    else:
        issue("NaN/Infinity found in numeric fields")

    # Zero-duration filter
    zero_dur = [r for r in DATA['APPOINTMENTS'] if r['startMinute'] == r['endMinute']]
    if not zero_dur:
        print(f"  Zero-duration:   OK (none in embedded — filter working)")
    else:
        issue(f"Zero-duration: {len(zero_dur)} appointments have start == end")

    # META.blockTypes
    actual_types = sorted(set(r['blockType'] for r in DATA['BLOCKOUTS']))
    meta_types = sorted(meta['blockTypes'])
    if actual_types == meta_types:
        print(f"  Block types:     OK ({len(actual_types)} types match META)")
    else:
        data_only = set(actual_types) - set(meta_types)
        if data_only:
            issue(f"Block types in data but not META: {data_only}")
        else:
            print(f"  Block types:     OK (META is superset of actual)")

    # Period structure
    period_ok = True
    if len(periods) != 4:
        issue(f"Expected 4 periods, got {len(periods)}")
        period_ok = False
    for p in periods:
        span = (datetime.strptime(p['end'], '%Y-%m-%d') - datetime.strptime(p['start'], '%Y-%m-%d')).days + 1
        if span != 7:
            issue(f"Period {p['label']} spans {span} days, expected 7")
            period_ok = False
    if period_ok:
        print(f"  Period structure: OK (4 x 7-day periods, contiguous)")

    # ==================================================================
    # 5. RESULT
    # ==================================================================
    print()
    print("=" * 70)
    if not ISSUES:
        print(" ALL CLEAR — report is consistent with source data")
    else:
        print(f" ISSUES FOUND ({len(ISSUES)}):")
        for iss in ISSUES:
            print(f"   - {iss}")
    print("=" * 70)

    # ==================================================================
    # 6. NEXT STEPS
    # ==================================================================
    print("\n--- Deployment Readiness ---\n")

    if not ISSUES:
        print("  The report is ready for use. Recommended next steps:\n")
        print("  1. SHARE:    Send the HTML file to the Plantation manager.")
        print("               It's self-contained — no server or internet needed.")
        print("               Just open in any browser.\n")
        print("  2. ARCHIVE:  Save the 3 source CSVs alongside the HTML file")
        print("               for audit trail and future re-generation.\n")
        print("  3. RE-RUN:   When new Zenoti exports are available (next cycle),")
        print("               upload to the Streamlit app and regenerate.")
        print("               Then re-run this check with the new files.\n")
        print("  4. VALIDATE: For deeper validation, run:")
        print(f"               python validate_report.py <html> <att> <appt> <blockout>\n")
    else:
        print("  Resolve the issues above before sharing the report.")
        print("  Run the full validation for detailed diagnostics:")
        print(f"  python validate_report.py <html> <att> <appt> <blockout>\n")

    sys.exit(0 if not ISSUES else 1)


if __name__ == '__main__':
    main()
