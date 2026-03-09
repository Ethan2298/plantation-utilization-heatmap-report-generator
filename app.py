#!/usr/bin/env python3
"""
Streamlit app — upload Zenoti exports, generate a 4-week utilization HTML report.
"""

import html as html_lib
import io
import json
import traceback
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta


# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(
    page_title="Utilization Report Generator",
    page_icon=":bar_chart:",
    layout="wide",
)

st.markdown("""
<style>
    /* tighten top padding */
    .block-container { padding-top: 2rem; }
    /* file uploader labels */
    .stFileUploader label { font-weight: 600; }
</style>
""", unsafe_allow_html=True)

HOUR_START = 9
HOUR_END = 21
NUM_WEEKS = 4


# ============================================================
# EXCEPTION
# ============================================================
class DataLoadError(Exception):
    pass


def read_tabular_file(file):
    """Read an uploaded file as a DataFrame, supporting CSV and Excel formats."""
    name = file.name.lower()
    if name.endswith('.csv'):
        return pd.read_csv(file)
    elif name.endswith(('.xls', '.xlsx')):
        return pd.read_excel(file)
    else:
        raise DataLoadError(f"Unsupported file type: {file.name}. Upload a .csv, .xls, or .xlsx file.")


# ============================================================
# HELPERS  (from build_7day_compare.py lines 27-64)
# ============================================================
def parse_schedule_to_minutes(sched_str):
    try:
        parts = str(sched_str).split(' - ')
        if len(parts) != 2:
            return None, None
        start = datetime.strptime(parts[0].strip(), '%I:%M %p')
        end   = datetime.strptime(parts[1].strip(), '%I:%M %p')
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


def fmt_period_label(start_dt, end_dt):
    sm = start_dt.strftime('%b')
    em = end_dt.strftime('%b')
    if sm == em:
        return f"{sm} {start_dt.day}\u2013{end_dt.day}"
    return f"{sm} {start_dt.day}\u2013{em} {end_dt.day}"


# ============================================================
# DATA LOADERS  (refactored for UploadedFile)
# ============================================================
def load_attendance(file):
    """Parse attendance CSV, return processed DataFrame."""
    try:
        att = read_tabular_file(file)
    except DataLoadError:
        raise
    except Exception as e:
        raise DataLoadError(f"Could not read Attendance file: {e}")

    required = ['Schedule Status', 'Date', 'Schedule']
    missing = [c for c in required if c not in att.columns]
    if missing:
        raise DataLoadError(f"Attendance file missing columns: {', '.join(missing)}")

    att = att[att['Schedule Status'] == 'Working'].copy()
    if att.empty:
        raise DataLoadError("No 'Working' records found in Attendance data.")

    att['date'] = pd.to_datetime(att['Date'], format='mixed', errors='coerce')
    att = att.dropna(subset=['date', 'Schedule'])

    parsed = att['Schedule'].apply(
        lambda s: pd.Series(parse_schedule_to_minutes(s), index=['startMinute', 'endMinute'])
    )
    att = pd.concat([att, parsed], axis=1)
    att = att.dropna(subset=['startMinute', 'endMinute'])
    att['startMinute']    = att['startMinute'].astype(int)
    att['endMinute']      = att['endMinute'].astype(int)
    att['dayOfWeek']      = att['date'].dt.dayofweek
    att['dateStr']        = att['date'].dt.strftime('%Y-%m-%d')
    att['scheduledHours'] = (att['endMinute'] - att['startMinute']) / 60.0
    return att


def load_appointments(file):
    """Parse appointments CSV, return processed DataFrame."""
    try:
        appt = read_tabular_file(file)
    except DataLoadError:
        raise
    except Exception as e:
        raise DataLoadError(f"Could not read Appointments file: {e}")

    required = ['Start Time', 'End Time']
    missing = [c for c in required if c not in appt.columns]
    if missing:
        raise DataLoadError(f"Appointments file missing columns: {', '.join(missing)}")

    appt['start_dt'] = pd.to_datetime(appt['Start Time'], format='mixed', errors='coerce')
    appt['end_dt']   = pd.to_datetime(appt['End Time'],   format='mixed', errors='coerce')
    appt = appt.dropna(subset=['start_dt', 'end_dt'])
    appt['startMinute'] = appt['start_dt'].dt.hour * 60 + appt['start_dt'].dt.minute
    appt['endMinute']   = appt['end_dt'].dt.hour   * 60 + appt['end_dt'].dt.minute
    # Filter zero-duration (enhancement add-ons) and negative-duration (midnight-spanning)
    appt = appt[appt['endMinute'] > appt['startMinute']].copy()
    if appt.empty:
        raise DataLoadError("No valid appointments found after filtering zero-duration entries.")
    appt['durationMin'] = appt['endMinute'] - appt['startMinute']
    appt['dayOfWeek']   = appt['start_dt'].dt.dayofweek
    appt['dateStr']     = appt['start_dt'].dt.strftime('%Y-%m-%d')
    return appt


def load_blockouts(file):
    """Parse block-out file (XLS/HTML table, CSV, or XLSX), return processed DataFrame."""
    name = file.name.lower()
    try:
        if name.endswith('.csv') or name.endswith('.xlsx'):
            bot = read_tabular_file(file)
        else:
            # .xls and .html — Zenoti exports .xls files that are actually HTML tables
            content = file.read().decode('utf-8', errors='replace')
            tables = pd.read_html(io.StringIO(content))
            if not tables:
                raise DataLoadError("No tables found in Block Out Time file.")
            bot = tables[0].copy()
    except DataLoadError:
        raise
    except Exception as e:
        raise DataLoadError(f"Could not read Block Out Time file: {e}")
    required = ['Date', 'StartTime', 'EndTime', 'BlockOutTimeType', 'Block Out Time (in hours)']
    missing = [c for c in required if c not in bot.columns]
    if missing:
        raise DataLoadError(f"Block Out Time file missing columns: {', '.join(missing)}")

    bot['date'] = pd.to_datetime(bot['Date'], format='mixed', errors='coerce')
    bot = bot.dropna(subset=['date', 'StartTime', 'EndTime'])
    bot['startMinute'] = bot['StartTime'].astype(str).apply(parse_time_str_to_minutes)
    bot['endMinute']   = bot['EndTime'].astype(str).apply(parse_time_str_to_minutes)
    bot = bot.dropna(subset=['startMinute', 'endMinute'])
    bot['startMinute'] = bot['startMinute'].astype(int)
    bot['endMinute']   = bot['endMinute'].astype(int)

    # AM/PM wrap fix (strict < to avoid inflating zero-duration blockouts)
    mask = bot['endMinute'] < bot['startMinute']
    bot.loc[mask, 'endMinute'] = bot.loc[mask, 'endMinute'] + 720

    bot['dayOfWeek']  = bot['date'].dt.dayofweek
    bot['dateStr']    = bot['date'].dt.strftime('%Y-%m-%d')
    bot['blockHours'] = pd.to_numeric(bot['Block Out Time (in hours)'], errors='coerce').fillna(0)
    return bot


def load_membership(file):
    """Parse membership CSV, return GuestCode -> [(start, end), ...] lookup."""
    try:
        mem = read_tabular_file(file)
    except DataLoadError:
        raise
    except Exception as e:
        raise DataLoadError(f"Could not read Membership file: {e}")

    required = ['GuestCode', 'StartDate', 'EndDate']
    missing = [c for c in required if c not in mem.columns]
    if missing:
        raise DataLoadError(f"Membership file missing columns: {', '.join(missing)}")

    mem['StartDate'] = pd.to_datetime(mem['StartDate'], format='mixed', errors='coerce')
    mem['EndDate']   = pd.to_datetime(mem['EndDate'],   format='mixed', errors='coerce')

    lookup = {}
    for _, row in mem.iterrows():
        code = str(row['GuestCode']).strip()
        s, e = row['StartDate'], row['EndDate']
        if pd.notna(s) and code and code != 'nan':
            lookup.setdefault(code, []).append((s, e))
    return lookup


def tag_members(appt_df, lookup):
    """Add isMember column to appointments DataFrame."""
    if not lookup:
        appt_df['isMember'] = False
        return appt_df

    if 'Guest Code' not in appt_df.columns:
        raise DataLoadError(
            "Membership file provided but Appointments CSV has no 'Guest Code' column. "
            "Cannot match members to appointments."
        )

    def is_member_on_date(guest_code, appt_date):
        code = str(guest_code).strip() if pd.notna(guest_code) else ''
        if not code or code not in lookup:
            return False
        for (s, e) in lookup[code]:
            if appt_date >= s and (pd.isna(e) or appt_date <= e):
                return True
        return False

    appt_df['isMember'] = appt_df.apply(
        lambda r: is_member_on_date(r['Guest Code'], r['start_dt']), axis=1
    )
    return appt_df


# ============================================================
# PAYLOAD BUILDER
# ============================================================
def build_data_payload(att_df, appt_df, bot_df, has_membership=False):
    """Assemble the JSON data payload for the HTML report."""
    attendance_records = att_df[
        ['dateStr', 'dayOfWeek', 'startMinute', 'endMinute', 'scheduledHours']
    ].rename(columns={'dateStr': 'date'}).to_dict('records')

    appt_cols = ['dateStr', 'dayOfWeek', 'startMinute', 'endMinute', 'durationMin']
    if has_membership:
        appt_cols.append('isMember')
    appointment_records = appt_df[
        appt_cols
    ].rename(columns={'dateStr': 'date'}).to_dict('records')

    blockout_records = bot_df[
        ['dateStr', 'dayOfWeek', 'startMinute', 'endMinute', 'blockHours', 'BlockOutTimeType']
    ].rename(columns={'dateStr': 'date', 'BlockOutTimeType': 'blockType'}).to_dict('records')

    # Build 4-week periods rolling back from data end date
    all_dates = sorted(set(
        [r['date'] for r in attendance_records] +
        [r['date'] for r in appointment_records] +
        [r['date'] for r in blockout_records]
    ))

    if not all_dates:
        raise DataLoadError("No valid dates found across uploaded files. Check that files contain parseable date values.")

    data_end = datetime.strptime(all_dates[-1], '%Y-%m-%d')

    periods = []
    p_end = data_end
    for i in range(NUM_WEEKS):
        p_start = p_end - timedelta(days=6)
        periods.insert(0, {
            'label':     fmt_period_label(p_start, p_end),
            'start':     p_start.strftime('%Y-%m-%d'),
            'end':       p_end.strftime('%Y-%m-%d'),
            'isCurrent': i == 0,
        })
        p_end = p_start - timedelta(days=1)

    meta = {
        'reportDate':    all_dates[-1],
        'dataStartDate': all_dates[0],
        'dataEndDate':   all_dates[-1],
        'blockTypes':    sorted(bot_df['BlockOutTimeType'].dropna().unique().tolist()),
        'hasMembership': has_membership,
        'hourStart': HOUR_START,
        'hourEnd':   HOUR_END,
        'periods':   periods,
    }

    return {
        'META':         meta,
        'APPOINTMENTS': appointment_records,
        'ATTENDANCE':   attendance_records,
        'BLOCKOUTS':    blockout_records,
    }


# ============================================================
# HTML TEMPLATE  (verbatim from build_7day_compare.py lines 254-817)
# ============================================================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__REPORT_TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,Cantarell,sans-serif;background:#fff;color:#1a1a1a;line-height:1.6}
.container{max-width:1280px;margin:0 auto;padding:40px 24px}
.header{border-bottom:1px solid #e5e5e5;padding-bottom:24px;margin-bottom:32px}
.header h1{font-size:2em;font-weight:600;margin-bottom:4px}
.header .subtitle{color:#666;font-size:1.05em}
.header .date-range{color:#999;font-size:0.85em;margin-top:6px}

/* Filter Bar */
.filter-bar{background:#f8f9fa;border:1px solid #e5e5e5;border-radius:4px;padding:16px 20px;margin-bottom:24px}
.filter-bar-header{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.filter-bar-header h3{font-size:14px;font-weight:600;color:#333}
.filter-bar-toggle{font-size:12px;color:#666;cursor:pointer;border:none;background:none;font-family:inherit;text-decoration:underline}
.filter-bar-body{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.filter-group{display:flex;flex-direction:column;gap:6px}
.filter-group-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:#888}
.filter-options{display:flex;flex-wrap:wrap;gap:8px}
.filter-checkbox{display:flex;align-items:center;gap:4px;cursor:pointer;user-select:none}
.filter-checkbox input{width:14px;height:14px;cursor:pointer;accent-color:#1e3a8a}
.filter-checkbox span{font-size:12px;color:#444}

/* Stats Cards */
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:32px}
.stat-card{background:#fafafa;border:1px solid #e5e5e5;padding:18px;border-radius:4px}
.stat-card .value{font-size:1.8em;font-weight:600;margin-bottom:1px;line-height:1.2}
.stat-card .label{color:#666;font-size:0.8em}
.stat-card .delta{font-size:0.75em;margin-top:4px;font-weight:500}
.delta-label{font-size:0.75em;color:#999;margin-top:8px;text-align:right}
.delta-positive{color:#5cb86e}
.delta-negative{color:#e47272}
.delta-neutral{color:#999}

/* Sections */
.section{margin-bottom:40px}
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid #eee}
.section-header h2{font-size:1.4em;font-weight:600}

/* Heatmap View Toggles */
.heatmap-controls{display:inline-flex;gap:0;flex-wrap:wrap}
.hm-view-btn{padding:5px 14px;border:1px solid #e5e5e5;background:#fff;font-size:12px;cursor:pointer;color:#666;font-family:inherit;transition:all .15s}
.hm-view-btn:first-child{border-radius:4px 0 0 4px}
.hm-view-btn:last-child{border-radius:0 4px 4px 0}
.hm-view-btn:not(:first-child){border-left:none}
.hm-view-btn.active{background:#1e3a8a;color:#fff;border-color:#1e3a8a}
.hm-view-btn:hover:not(.active){background:#f0f4ff}

/* Heatmap Table */
.heatmap-wrap{overflow-x:auto;border:1px solid #e5e5e5;border-radius:4px}
.hm-table{border-collapse:separate;border-spacing:2px;width:100%;font-size:13px;background:#fff}
.hm-table th{padding:8px 2px;text-align:center;font-weight:600;font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.3px}
.hm-table td{padding:6px 2px 5px;text-align:center;min-width:110px;border-radius:3px;font-weight:500;transition:opacity .15s}
.hm-cell-sub{font-size:10px;opacity:0.85;margin-top:1px;font-weight:400}
.hm-table td:hover{opacity:0.85}
.hm-table .hour-label{text-align:left;font-weight:600;color:#222;padding-left:10px;min-width:60px;background:transparent!important;font-size:13px}
.hm-table .row-total{font-weight:600;background:transparent!important;color:#333;min-width:55px}
.hm-table .avg-row td{font-weight:600;border-top:2px solid #ccc}

/* Tooltip */
.tooltip{position:absolute;background:#1a1a1a;color:#fff;padding:8px 12px;border-radius:4px;font-size:11px;z-index:100;white-space:pre-line;pointer-events:none;display:none;line-height:1.6;box-shadow:0 2px 8px rgba(0,0,0,.15)}

/* Daily Table */
.table-wrap{overflow-x:auto;border:1px solid #e5e5e5;border-radius:4px}
.daily-table{width:100%;border-collapse:collapse;font-size:13px}
.daily-table thead{background:#fafafa;border-bottom:1px solid #e5e5e5}
.daily-table th{padding:10px 14px;text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:0.4px;color:#888}
.daily-table td{padding:10px 14px;border-bottom:1px solid #f0f0f0}
.daily-table tbody tr:last-child td{border-bottom:none}
.daily-table tbody tr:hover{background:#fafafa}
.daily-table .num{text-align:right;font-variant-numeric:tabular-nums}
.daily-table .total-row{background:#f8f9fa;font-weight:600}
.daily-table .total-row td{border-top:2px solid #e5e5e5}
.current-col{background:#f0f4ff!important}

/* Weekend vs Weekday */
.comparison-cards{display:grid;grid-template-columns:1fr 1fr;gap:24px}
.comparison-card{background:#fafafa;border:1px solid #e5e5e5;border-radius:4px;padding:24px}
.comparison-card h3{font-size:1.1em;font-weight:600;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid #eee}
.comparison-card .big-util{font-size:2.2em;font-weight:600;margin-bottom:4px;line-height:1.1}
.comparison-card .util-delta{font-size:0.85em;margin-bottom:16px}
.comparison-metric{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid #eee;font-size:13px}
.comparison-metric:last-child{border-bottom:none}
.comparison-metric .m-label{color:#666}
.comparison-metric .m-value{font-weight:500;font-variant-numeric:tabular-nums}

/* Staffing View - Cell Colors */
.cell-add{background:#dcfce7;color:#15803d}
.cell-add .cell-sub{color:rgba(21,128,61,0.55)}
.cell-cut{background:#fee2e2;color:#b91c1c}
.cell-cut .cell-sub{color:rgba(185,28,28,0.55)}
.cell-ok{color:#555}
.cell-empty{background:#f3f4f6;color:#ccc;font-size:11px}

/* Staffing heatmap table overrides */
.hm-staffing{border-collapse:separate;border-spacing:3px;width:100%;background:#fafafa;padding:6px;font-size:12px}
.hm-staffing th{text-align:center;font-weight:600;font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.4px;padding:4px}
.hm-staffing td{border-radius:4px;min-width:110px;padding:6px 4px 5px;text-align:center;vertical-align:middle;cursor:default;transition:opacity .12s}
.hm-staffing td:hover{opacity:0.82}
.hm-staffing .hour-label{text-align:right!important;padding-right:10px!important;font-weight:600;color:#555;min-width:48px;font-size:11px;background:transparent!important}
.hm-staffing .row-total{font-weight:600;background:transparent!important;min-width:55px}
.hm-staffing .avg-row td{font-weight:600;border-top:2px solid #ccc}
.util-val{font-size:13px;font-weight:600}
.cell-sub{font-size:10px;color:rgba(0,0,0,0.45);margin-top:1px}

/* Staffing Action Labels */
.cell-action{font-size:10px;font-weight:600;margin-top:2px;line-height:1.2}
.cell-action-add{color:#15803d}
.cell-action-cut{color:#b91c1c}

/* Staffing Legend */
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px;font-size:12px;align-items:center}
.legend-item{display:flex;align-items:center;gap:6px}
.legend-dot{width:12px;height:12px;border-radius:3px}
.ld-add{background:#dcfce7;border:1px solid #86efac}
.ld-ok{background:#f3f4f6;border:1px solid #d1d5db}
.ld-cut{background:#fee2e2;border:1px solid #fca5a5}
.ld-na{background:#f3f4f6;border:1px solid #d1d5db}

/* Threshold Controls */
.thresh-input{width:58px;padding:4px 6px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;font-weight:600;text-align:center;font-family:inherit;outline:none}
.thresh-input:focus{border-color:#1e3a8a;box-shadow:0 0 0 2px rgba(30,58,138,.12)}
.thresh-input.add{color:#15803d;border-color:#86efac}
.thresh-input.cut{color:#b91c1c;border-color:#fca5a5}
.thresh-pct{font-size:13px;color:#888;font-weight:500}
.ctrl-group{display:flex;align-items:center;gap:8px}
.ctrl-group label{font-size:12px;font-weight:500;color:#555;white-space:nowrap}
.ctrl-divider{width:1px;height:26px;background:#e5e5e5}
.threshold-controls{display:flex;align-items:center;gap:16px;flex-wrap:wrap}

/* View Toggle */
.view-toggle{display:inline-flex;gap:0;margin-left:auto}
.view-btn{padding:5px 14px;border:1px solid #e5e5e5;background:#fff;font-size:12px;cursor:pointer;color:#666;font-family:inherit;transition:all .15s}
.view-btn:first-child{border-radius:4px 0 0 4px}
.view-btn:last-child{border-radius:0 4px 4px 0}
.view-btn:not(:first-child){border-left:none}
.view-btn.active{background:#1e3a8a;color:#fff;border-color:#1e3a8a}
.view-btn:hover:not(.active){background:#f0f4ff}

@media(max-width:900px){
  .stats{grid-template-columns:repeat(2,1fr)}
  .comparison-cards{grid-template-columns:1fr}
  .filter-bar-body{flex-direction:column}
  .action-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <h1>__REPORT_TITLE__</h1>
    <div class="subtitle">MT utilization by week, with trend</div>
    <div class="date-range" id="dateRangeText"></div>
  </div>

  <div class="filter-bar">
    <div class="filter-bar-header">
      <h3>Settings</h3>
      <button class="filter-bar-toggle" id="filterToggle">collapse</button>
    </div>
    <div class="filter-bar-body" id="filterBody">
      <div class="filter-group">
        <div class="filter-group-label">Block Time Type Filters</div>
        <div class="filter-options" id="blockTypeFilters"></div>
      </div>
      <div class="filter-group" id="thresholdGroup">
        <div class="filter-group-label">Thresholds</div>
        <div class="threshold-controls">
          <div class="ctrl-group">
            <label>&#9650; Increase above</label>
            <input type="number" id="addInput" min="1" max="100" step="1" value="90" class="thresh-input add">
            <span class="thresh-pct">%</span>
          </div>
          <div class="ctrl-divider"></div>
          <div class="ctrl-group">
            <label>&#9660; Decrease below</label>
            <input type="number" id="cutInput" min="0" max="99" step="1" value="85" class="thresh-input cut">
            <span class="thresh-pct">%</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="section-header" style="margin-top:24px"><h2 id="scorecardTitle">Overview</h2></div>
  <div class="stats" id="scorecard"></div>

  <div class="section">
    <div class="section-header">
      <h2 id="heatmapTitle">Utilization Heatmap</h2>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div class="heatmap-controls" id="heatmapTabs"></div>
        <div class="view-toggle" id="viewToggle"></div>
      </div>
    </div>
    <div id="staffingLegend" style="display:none">
      <div class="legend">
        <div class="legend-item"><div class="legend-dot ld-add"></div><span id="lgAdd"></span></div>
        <div class="legend-item"><div class="legend-dot ld-ok"></div><span id="lgOk"></span></div>
        <div class="legend-item"><div class="legend-dot ld-cut"></div><span id="lgCut"></span></div>
        <div class="legend-item"><div class="legend-dot ld-na"></div>No schedule</div>
      </div>
    </div>
    <div class="heatmap-wrap" id="heatmapContainer"></div>
  </div>

  <div class="section">
    <div class="section-header"><h2>Daily Utilization by Day of Week</h2></div>
    <div class="table-wrap" id="dailyTableContainer"></div>
  </div>

  <div class="section">
    <div class="section-header"><h2>Weekday vs Weekend</h2></div>
    <div id="weekendComparisonContainer"></div>
  </div>

</div>
<div class="tooltip" id="tooltip"></div>

<script>
const DATA = __DATA_PAYLOAD__;

const DAY_NAMES  = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const HOUR_START = DATA.META.hourStart;
const HOUR_END   = DATA.META.hourEnd;
const PERIODS    = DATA.META.periods; // [{label, start, end, isCurrent}, ...]

const state = {
  blockTypes:        new Set(DATA.META.blockTypes),
  heatmapIdx:        'avg',  // default = 4-week average
  viewMode:          'utilization',  // 'utilization' | 'staffing'
};

let ADD = 90;
let CUT = 85;

let allMetrics = [];  // one entry per period, index 0 = oldest
let avg2Metrics = null; // 2-week aggregate (last 2 periods)
let avg2EarlierMetrics = null; // 2-week aggregate (first 2 periods)
let avgMetrics = null; // 4-week aggregate

// ============================================================
// HELPERS
// ============================================================
function minsInHour(startMin, endMin, hourStart) {
  return Math.max(0, Math.min(endMin, hourStart + 60) - Math.max(startMin, hourStart));
}

function escAttr(s) { return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

function fmtDelta(curr, prev, suffix, isPP) {
  if (prev === null || prev === undefined) return '<span class="delta-neutral">N/A</span>';
  const d = curr - prev;
  if (isPP) {
    const cls = d > 0 ? 'delta-positive' : d < 0 ? 'delta-negative' : 'delta-neutral';
    const arrow = d > 0 ? '\u2191 ' : d < 0 ? '\u2193 ' : '';
    return '<span class="' + cls + '">' + arrow + Math.abs(d).toFixed(1) + '</span>';
  }
  const sign = d > 0 ? '+' : '';
  const pct  = prev !== 0 ? Math.min(999, Math.max(-999, (d / Math.abs(prev)) * 100)) : 0;
  const cls  = d > 0 ? 'delta-positive' : d < 0 ? 'delta-negative' : 'delta-neutral';
  return '<span class="' + cls + '">' + sign + d.toFixed(1) + (suffix||'') + ' (' + sign + pct.toFixed(1) + '%)</span>';
}

function utilColor(pct) {
  if (pct === null || isNaN(pct)) return '#f5f5f5';
  const t = Math.min(1, Math.max(0, pct / 100));
  return 'rgb(' + Math.round(240 + (30 - 240) * t) + ',' +
                  Math.round(244 + (58 - 244) * t) + ',' +
                  Math.round(255 + (138 - 255) * t) + ')';
}

function txtClr(pct) {
  if (pct === null) return '#999';
  return pct > 55 ? '#fff' : '#1a1a1a';
}

function hourLabel(h) {
  const h12 = h > 12 ? h - 12 : (h === 0 ? 12 : h);
  return h12 + (h >= 12 ? 'PM' : 'AM');
}

function staffingAction(util) {
  if (util === null) return 'none';
  if (util > ADD) return 'add';
  if (util < CUT) return 'cut';
  return 'healthy';
}

// ============================================================
// METRICS
// ============================================================
function computeMetrics(startDate, endDate) {
  const att  = DATA.ATTENDANCE.filter(r => r.date >= startDate && r.date <= endDate);
  const appt = DATA.APPOINTMENTS.filter(r => r.date >= startDate && r.date <= endDate);
  const blk  = DATA.BLOCKOUTS.filter(r =>
    r.date >= startDate && r.date <= endDate &&
    state.blockTypes.has(r.blockType)
  );

  const totalScheduledHrs = att.reduce((s, r) => s + r.scheduledHours, 0);
  const totalApptHrs      = appt.reduce((s, r) => s + r.durationMin / 60, 0);

  const grid = {};
  for (let dow = 0; dow < 7; dow++) {
    grid[dow] = {};
    for (let h = HOUR_START; h < HOUR_END; h++) {
      grid[dow][h] = { scheduled: 0, blocked: 0, appointment: 0, memberAppt: 0, therapistCount: 0 };
    }
  }

  // Track unique dates per dow for averaging therapist counts
  const dowDates = {};
  for (let dow = 0; dow < 7; dow++) dowDates[dow] = new Set();
  att.forEach(r => dowDates[r.dayOfWeek].add(r.date));

  att.forEach(r => {
    for (let h = HOUR_START; h < HOUR_END; h++) {
      const m = minsInHour(r.startMinute, r.endMinute, h * 60);
      grid[r.dayOfWeek][h].scheduled += m / 60;
      if (m > 0) grid[r.dayOfWeek][h].therapistCount += 1;
    }
  });
  blk.forEach(r => {
    for (let h = HOUR_START; h < HOUR_END; h++) {
      grid[r.dayOfWeek][h].blocked += minsInHour(r.startMinute, r.endMinute, h * 60) / 60;
    }
  });
  appt.forEach(r => {
    for (let h = HOUR_START; h < HOUR_END; h++) {
      const m = minsInHour(r.startMinute, r.endMinute, h * 60) / 60;
      grid[r.dayOfWeek][h].appointment += m;
      if (r.isMember) grid[r.dayOfWeek][h].memberAppt += m;
    }
  });

  // Derive block hours from grid (clipped to operating window) for consistency
  let totalBlockHrs = 0;
  for (let dow = 0; dow < 7; dow++)
    for (let h = HOUR_START; h < HOUR_END; h++)
      totalBlockHrs += grid[dow][h].blocked;
  const netAvailable = totalScheduledHrs - totalBlockHrs;
  const utilization  = netAvailable > 0 ? (totalApptHrs / netAvailable) * 100 : 0;

  const daily = {};
  for (let dow = 0; dow < 7; dow++) {
    let s = 0, b = 0, a = 0;
    for (let h = HOUR_START; h < HOUR_END; h++) {
      s += grid[dow][h].scheduled;
      b += grid[dow][h].blocked;
      a += grid[dow][h].appointment;
    }
    const n = s - b;
    daily[dow] = { scheduled: s, blocked: b, netAvailable: n, appointment: a,
                   utilization: n > 0 ? (a / n) * 100 : null };
  }

  const dowDateCounts = {};
  for (let dow = 0; dow < 7; dow++) dowDateCounts[dow] = dowDates[dow].size;

  return { totalScheduledHrs, totalApptHrs, totalBlockHrs, netAvailable, utilization,
           grid, daily, dowDateCounts };
}

// ============================================================
// RECALCULATE
// ============================================================
function recalculate() {
  allMetrics = PERIODS.map(p => computeMetrics(p.start, p.end));
  const n2 = PERIODS.length;
  const avg2Start = PERIODS[n2 >= 2 ? n2 - 2 : 0].start;
  avg2Metrics = computeMetrics(avg2Start, PERIODS[n2 - 1].end);
  avg2EarlierMetrics = n2 >= 4 ? computeMetrics(PERIODS[0].start, PERIODS[1].end) : null;
  avgMetrics = computeMetrics(PERIODS[0].start, PERIODS[n2 - 1].end);
  renderScorecard();
  buildHeatmapTabs();
  renderHeatmap();
  renderStaffingExtras();
  renderDailyTable();
  renderWeekendComparison();
}

// ============================================================
// FILTERS
// ============================================================
function buildFilters() {
  const bc = document.getElementById('blockTypeFilters');
  const offByDefault = ['shift adjustment', 'leaving early'];
  DATA.META.blockTypes.forEach(bt => {
    const defaultOff = offByDefault.some(s => bt.toLowerCase().includes(s));
    if (defaultOff) state.blockTypes.delete(bt);
    const lbl = document.createElement('label');
    lbl.className = 'filter-checkbox';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = !defaultOff;
    cb.addEventListener('change', () => { cb.checked ? state.blockTypes.add(bt) : state.blockTypes.delete(bt); recalculate(); });
    const sp = document.createElement('span'); sp.textContent = bt;
    lbl.append(cb, sp); bc.appendChild(lbl);
  });

  const toggle = document.getElementById('filterToggle');
  const body   = document.getElementById('filterBody');
  toggle.addEventListener('click', () => {
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? 'flex' : 'none';
    toggle.textContent  = hidden ? 'collapse' : 'expand';
  });
}

// ============================================================
// SCORECARD
// ============================================================
function renderScorecard() {
  let c, p, wLabel, compLabel;
  if (state.heatmapIdx === 'avg') {
    c = avgMetrics;
    p = avg2Metrics;
    wLabel = '4-Wk Avg';
    compLabel = 'vs 2-Wk Avg (' + PERIODS[PERIODS.length >= 2 ? PERIODS.length - 2 : 0].label + '\u2013' + PERIODS[PERIODS.length - 1].label + ')';
  } else if (state.heatmapIdx === 'avg2') {
    c = avg2Metrics;
    p = avg2EarlierMetrics;
    wLabel = '2-Wk Avg';
    compLabel = p ? 'vs earlier 2-Wk (' + PERIODS[0].label + '\u2013' + PERIODS[1].label + ')' : '';
  } else {
    c = allMetrics[state.heatmapIdx];
    p = state.heatmapIdx > 0 ? allMetrics[state.heatmapIdx - 1] : null;
    wLabel = PERIODS[state.heatmapIdx].label;
    compLabel = p ? 'vs ' + PERIODS[state.heatmapIdx - 1].label : '';
  }
  const noDelta = '<span class="delta-neutral">\u2014</span>';
  const cards = [
    { label: 'Utilization ('   + wLabel + ')', value: c.utilization.toFixed(1) + '%',             delta: p ? fmtDelta(c.utilization,       p.utilization,       '',  true)  : noDelta },
    { label: 'Scheduled Hours',                 value: c.totalScheduledHrs.toFixed(1) + 'h',       delta: p ? fmtDelta(c.totalScheduledHrs, p.totalScheduledHrs, 'h', false) : noDelta },
    { label: 'Block Time',                      value: c.totalBlockHrs.toFixed(1) + 'h',           delta: p ? fmtDelta(c.totalBlockHrs,     p.totalBlockHrs,     'h', false) : noDelta },
    { label: 'Net Available',                   value: c.netAvailable.toFixed(1) + 'h',            delta: p ? fmtDelta(c.netAvailable,      p.netAvailable,      'h', false) : noDelta },
    { label: 'Appointment Hours',               value: c.totalApptHrs.toFixed(1) + 'h',            delta: p ? fmtDelta(c.totalApptHrs,      p.totalApptHrs,      'h', false) : noDelta },
  ];
  document.getElementById('scorecardTitle').textContent = 'Overview \u2014 ' + wLabel + (compLabel ? ' (' + compLabel + ')' : '');
  document.getElementById('scorecard').innerHTML = cards.map(c =>
    '<div class="stat-card"><div class="value">' + c.value + '</div>' +
    '<div class="label">' + c.label + '</div><div class="delta">' + c.delta + '</div></div>'
  ).join('');
}

// ============================================================
// HEATMAP TABS
// ============================================================
function buildHeatmapTabs() {
  const wrap = document.getElementById('heatmapTabs');
  wrap.innerHTML = '';
  const labels = [...PERIODS.map((p, i) => ({ label: p.label, idx: i })),
                  { label: '2-Wk Avg', idx: 'avg2' },
                  { label: '4-Wk Avg', idx: 'avg' }];
  labels.forEach(({ label, idx }, btnIdx) => {
    const btn = document.createElement('button');
    btn.className = 'hm-view-btn' + (idx === state.heatmapIdx ? ' active' : '');
    btn.textContent = label;
    // Round corners only on outermost buttons
    if (btnIdx === 0) btn.style.borderRadius = '4px 0 0 4px';
    if (btnIdx === labels.length - 1) btn.style.borderRadius = '0 4px 4px 0';
    btn.addEventListener('click', () => {
      state.heatmapIdx = idx;
      wrap.querySelectorAll('.hm-view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderHeatmap();
      renderStaffingExtras();
      renderScorecard();
      renderDailyTable();
      renderWeekendComparison();
    });
    wrap.appendChild(btn);
  });
}

// ============================================================
// HEATMAP
// ============================================================
function renderHeatmap() {
  if (state.viewMode === 'staffing') { renderStaffingHeatmap(); return; }
  renderUtilizationHeatmap();
}

function renderUtilizationHeatmap() {
  const m = state.heatmapIdx === 'avg' ? avgMetrics : state.heatmapIdx === 'avg2' ? avg2Metrics : allMetrics[state.heatmapIdx];
  const dowDateCounts = m.dowDateCounts;

  let html = '<table class="hm-table"><thead><tr><th></th>';
  for (let dow = 0; dow < 7; dow++) html += '<th>' + DAY_NAMES[dow] + '</th>';
  html += '</tr></thead><tbody>';

  const colNetHrs = Array(7).fill(0);
  const colApptHrs = Array(7).fill(0);
  const colRawTher = Array(7).fill(0);
  const colAppt = Array(7).fill(0);
  const colMemberAppt = Array(7).fill(0);
  const numHours = HOUR_END - HOUR_START;

  for (let h = HOUR_START; h < HOUR_END; h++) {
    html += '<tr><td class="hour-label">' + hourLabel(h) + '</td>';

    for (let dow = 0; dow < 7; dow++) {
      const dDates = dowDateCounts[dow];
      const cc   = m.grid[dow][h];
      const net  = cc.scheduled - cc.blocked;
      const util = net > 0 ? (cc.appointment / net) * 100 : null;
      const avgT = dDates > 0 ? cc.therapistCount / dDates : null;

      const memberShare = DATA.META.hasMembership && cc.appointment > 0 ? cc.memberAppt / cc.appointment * 100 : null;
      const val = util !== null ? util.toFixed(0) + '%' : '\u2013';
      const subParts = [];
      if (util !== null && memberShare !== null) subParts.push(Math.round(memberShare) + '%M');
      if (avgT !== null) subParts.push(avgT.toFixed(1) + ' MT');
      const sub = subParts.join(' \u00b7 ');
      const bg  = utilColor(util);
      const fg  = txtClr(util);

      if (util !== null) { colNetHrs[dow] += net; colApptHrs[dow] += cc.appointment; }
      colRawTher[dow] += cc.therapistCount;
      colAppt[dow] += cc.appointment; colMemberAppt[dow] += cc.memberAppt;

      const nmShare = memberShare !== null ? 100 - memberShare : null;
      const tip = 'Sched: ' + cc.scheduled.toFixed(1) + 'h  Block: ' + cc.blocked.toFixed(1) +
        'h  Net: ' + net.toFixed(1) + 'h  Appt: ' + cc.appointment.toFixed(1) + 'h' +
        (memberShare !== null
          ? '\nMember: ' + cc.memberAppt.toFixed(1) + 'h (' + Math.round(memberShare) + '%)' +
            '  Non-member: ' + (cc.appointment - cc.memberAppt).toFixed(1) + 'h (' + Math.round(nmShare) + '%)'
          : '') +
        (avgT !== null ? '\nMTs: ' + avgT.toFixed(1) + ' on shift' : '');

      html += '<td style="background:' + bg + ';color:' + fg + '" data-tip="' + escAttr(tip) + '">' +
        '<div>' + val + '</div>' + (sub ? '<div class="hm-cell-sub">' + sub + '</div>' : '') + '</td>';
    }
    html += '</tr>';
  }

  html += '<tr class="avg-row"><td class="hour-label" style="font-weight:700">Avg</td>';
  for (let dow = 0; dow < 7; dow++) {
    const dDates = dowDateCounts[dow];
    const avgU = colNetHrs[dow] > 0 ? (colApptHrs[dow] / colNetHrs[dow] * 100).toFixed(0) + '%' : '\u2013';
    const avgT = dDates > 0 ? (colRawTher[dow] / dDates / numHours).toFixed(1) + ' MT' : '';
    const mPct = DATA.META.hasMembership && colAppt[dow] > 0 ? Math.round(colMemberAppt[dow] / colAppt[dow] * 100) : null;
    const nmPct = mPct !== null ? 100 - mPct : null;
    const split = mPct !== null
      ? '<div class="hm-cell-sub" style="font-size:10px;white-space:nowrap">' + mPct + '%M \u00b7 ' + nmPct + '%NM</div>'
      : '';
    html += '<td class="row-total">' + avgU + (avgT ? '<div class="hm-cell-sub">' + avgT + '</div>' : '') + split + '</td>';
  }
  html += '</tr>';

  html += '</tbody></table>';
  document.getElementById('heatmapContainer').innerHTML = html;
  wireTooltips();
}

function renderStaffingHeatmap() {
  const m = state.heatmapIdx === 'avg' ? avgMetrics : state.heatmapIdx === 'avg2' ? avg2Metrics : allMetrics[state.heatmapIdx];
  const dowDateCounts = m.dowDateCounts;

  let html = '<table class="hm-staffing"><thead><tr><th></th>';
  for (let dow = 0; dow < 7; dow++) html += '<th>' + DAY_NAMES[dow] + '</th>';
  html += '</tr></thead><tbody>';

  const colNetHrs = Array(7).fill(0);
  const colApptHrs = Array(7).fill(0);

  for (let h = HOUR_START; h < HOUR_END; h++) {
    html += '<tr><td class="hour-label">' + hourLabel(h) + '</td>';

    for (let dow = 0; dow < 7; dow++) {
      const dDates = dowDateCounts[dow];
      const cc   = m.grid[dow][h];
      const net  = cc.scheduled - cc.blocked;
      const util = net > 0 ? (cc.appointment / net) * 100 : null;
      const avgT = dDates > 0 ? cc.therapistCount / dDates : null;
      const action = staffingAction(util);

      const cellClass = action === 'add' ? 'cell-add' : action === 'cut' ? 'cell-cut' : action === 'healthy' ? 'cell-ok' : 'cell-empty';
      const val = util !== null ? '<span class="util-val">' + util.toFixed(0) + '%</span>' : '\u2013';
      const badge = action === 'add' ? '<div class="cell-action cell-action-add">Increase</div>' :
                    action === 'cut' ? '<div class="cell-action cell-action-cut">Decrease</div>' : '';
      const sub = avgT !== null ? '<div class="cell-sub">' + avgT.toFixed(1) + ' MTs</div>' : '';

      if (util !== null) { colNetHrs[dow] += net; colApptHrs[dow] += cc.appointment; }

      const tip = 'Util: ' + (util !== null ? util.toFixed(0) + '%' : 'N/A') + '  Action: ' + ({add:'INCREASE',cut:'DECREASE',healthy:'HEALTHY',none:'NONE'}[action]||action.toUpperCase()) +
        '\nSched: ' + cc.scheduled.toFixed(1) + 'h  Block: ' + cc.blocked.toFixed(1) +
        'h  Net: ' + net.toFixed(1) + 'h  Appt: ' + cc.appointment.toFixed(1) + 'h' +
        (avgT !== null ? '\nMTs: ' + avgT.toFixed(1) + ' on shift' : '') +
        (util !== null && action === 'add' ? '\nGap: +' + (util - ADD).toFixed(1) + ' pp above INCREASE threshold' : '') +
        (util !== null && action === 'cut' ? '\nGap: ' + (util - CUT).toFixed(1) + ' pp below DECREASE threshold' : '');

      html += '<td class="' + cellClass + '" data-tip="' + escAttr(tip) + '">' +
        val + badge + sub + '</td>';
    }
    html += '</tr>';
  }

  // Average row
  html += '<tr class="avg-row"><td class="hour-label" style="font-weight:700">Avg</td>';
  for (let dow = 0; dow < 7; dow++) {
    const avgUtil = colNetHrs[dow] > 0 ? colApptHrs[dow] / colNetHrs[dow] * 100 : null;
    const avgU = avgUtil !== null ? avgUtil.toFixed(0) + '%' : '\u2013';
    const action = avgUtil !== null ? staffingAction(avgUtil) : 'none';
    const cellClass = action === 'add' ? 'cell-add' : action === 'cut' ? 'cell-cut' : action === 'healthy' ? 'cell-ok' : 'cell-empty';
    html += '<td class="row-total ' + cellClass + '"><span class="util-val">' + avgU + '</span></td>';
  }
  html += '</tr>';

  html += '</tbody></table>';
  document.getElementById('heatmapContainer').innerHTML = html;
  wireTooltips();
}

function wireTooltips() {
  const tip = document.getElementById('tooltip');
  document.querySelectorAll('#heatmapContainer td[data-tip]').forEach(td => {
    td.addEventListener('mouseenter', e => { tip.textContent = td.dataset.tip; tip.style.display = 'block'; tip.style.left = e.pageX + 12 + 'px'; tip.style.top = e.pageY - 36 + 'px'; });
    td.addEventListener('mousemove',  e => { tip.style.left = e.pageX + 12 + 'px'; tip.style.top = e.pageY - 36 + 'px'; });
    td.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  });
}

// ============================================================
// STAFFING EXTRAS
// ============================================================
function renderStaffingExtras() {
  const legendEl = document.getElementById('staffingLegend');

  if (state.viewMode !== 'staffing') {
    legendEl.style.display = 'none';
    return;
  }

  legendEl.style.display = 'block';
  document.getElementById('lgAdd').textContent = 'Increase hours (>' + ADD + '%)';
  document.getElementById('lgOk').textContent  = 'Healthy (' + CUT + '\u2013' + ADD + '%)';
  document.getElementById('lgCut').textContent  = 'Decrease hours (<' + CUT + '%)';
}

// ============================================================
// DAILY TABLE
// ============================================================
function renderDailyTable() {
  const n = PERIODS.length;
  const selIdx = state.heatmapIdx; // number or 'avg'

  // Header
  let html = '<table class="daily-table"><thead><tr><th>Day</th>';
  PERIODS.forEach((p, i) => {
    const cls = i === selIdx ? ' class="num current-col"' : ' class="num"';
    html += '<th' + cls + '>' + p.label + '</th>';
  });
  const avg2Cls = selIdx === 'avg2' ? ' class="num current-col"' : ' class="num"';
  const avgCls = selIdx === 'avg' ? ' class="num current-col"' : ' class="num"';
  html += '<th' + avg2Cls + '>2-Wk Avg</th><th' + avgCls + '>4-Wk Avg</th><th class="num">Trend</th></tr></thead><tbody>';

  let totUtils = PERIODS.map(() => ({ a: 0, n: 0 }));
  let totAvg2 = { a: 0, n: 0 };
  let totAvg = { a: 0, n: 0 };

  for (let dow = 0; dow < 7; dow++) {
    html += '<tr><td><strong>' + DAY_NAMES[dow] + '</strong></td>';
    const weekUtils = allMetrics.map(m => m.daily[dow].utilization);

    weekUtils.forEach((u, i) => {
      const cls = i === selIdx ? ' class="num current-col"' : ' class="num"';
      html += '<td' + cls + '>' + (u !== null ? u.toFixed(1) + '%' : 'N/A') + '</td>';
      if (u !== null) {
        totUtils[i].a += allMetrics[i].daily[dow].appointment;
        totUtils[i].n += allMetrics[i].daily[dow].netAvailable;
      }
    });

    // 2-Wk avg
    const avg2U = avg2Metrics.daily[dow].utilization;
    html += '<td' + avg2Cls + '>' + (avg2U !== null ? avg2U.toFixed(1) + '%' : 'N/A') + '</td>';
    if (avg2U !== null) { totAvg2.a += avg2Metrics.daily[dow].appointment; totAvg2.n += avg2Metrics.daily[dow].netAvailable; }

    // 4-Wk avg
    const avgU = avgMetrics.daily[dow].utilization;
    html += '<td' + avgCls + '>' + (avgU !== null ? avgU.toFixed(1) + '%' : 'N/A') + '</td>';
    if (avgU !== null) { totAvg.a += avgMetrics.daily[dow].appointment; totAvg.n += avgMetrics.daily[dow].netAvailable; }

    // Trend: W4 (oldest=index 0) -> W1 (newest=last)
    const w4 = weekUtils[0], w1 = weekUtils[n - 1];
    let trend = '\u2014';
    if (w4 !== null && w1 !== null) {
      const diff = w1 - w4;
      if      (diff >  2) trend = '<span class="delta-positive">\u2191 ' + diff.toFixed(1) + '</span>';
      else if (diff < -2) trend = '<span class="delta-negative">\u2193 ' + Math.abs(diff).toFixed(1) + '</span>';
      else                trend = '<span class="delta-neutral">\u2192</span>';
    }
    html += '<td class="num">' + trend + '</td></tr>';
  }

  // Total row
  html += '<tr class="total-row"><td><strong>Total</strong></td>';
  totUtils.forEach((t, i) => {
    const u = t.n > 0 ? (t.a / t.n * 100).toFixed(1) + '%' : 'N/A';
    const cls = i === selIdx ? ' class="num current-col"' : ' class="num"';
    html += '<td' + cls + '><strong>' + u + '</strong></td>';
  });
  const avg2U = totAvg2.n > 0 ? (totAvg2.a / totAvg2.n * 100).toFixed(1) + '%' : 'N/A';
  html += '<td' + avg2Cls + '><strong>' + avg2U + '</strong></td>';
  const avgU = totAvg.n > 0 ? (totAvg.a / totAvg.n * 100).toFixed(1) + '%' : 'N/A';
  html += '<td' + avgCls + '><strong>' + avgU + '</strong></td><td></td></tr>';

  html += '</tbody></table>';
  document.getElementById('dailyTableContainer').innerHTML = html;
}

// ============================================================
// WEEKDAY vs WEEKEND
// ============================================================
function renderWeekendComparison() {
  function agg(metrics, days) {
    let s = 0, b = 0, a = 0;
    days.forEach(d => { s += metrics.daily[d].scheduled; b += metrics.daily[d].blocked; a += metrics.daily[d].appointment; });
    const n = s - b;
    return { scheduled: s, blocked: b, appointment: a, netAvailable: n, utilization: n > 0 ? (a / n) * 100 : null };
  }

  const wdDows = [0,1,2,3,4], weDows = [5,6];
  const isAvg = state.heatmapIdx === 'avg' || state.heatmapIdx === 'avg2';
  const sel = state.heatmapIdx === 'avg' ? avgMetrics : state.heatmapIdx === 'avg2' ? avg2Metrics : allMetrics[state.heatmapIdx];
  const cWd = agg(sel, wdDows), cWe = agg(sel, weDows);
  const aWd = agg(avgMetrics, wdDows), aWe = agg(avgMetrics, weDows);

  function card(title, c, ref, refLabel, showDelta) {
    const u  = c.utilization  !== null ? c.utilization.toFixed(1)  + '%' : 'N/A';
    const deltaHtml = showDelta
      ? '<div class="util-delta">vs ' + refLabel + ': ' + fmtDelta(c.utilization, ref.utilization, '', true) + '</div>'
      : '<div class="util-delta" style="color:#999">' + (state.heatmapIdx === 'avg2' ? '2-Wk' : '4-Wk') + ' Aggregate</div>';
    return '<div class="comparison-card"><h3>' + title + '</h3>' +
      '<div class="big-util">' + u + '</div>' +
      deltaHtml +
      '<div class="comparison-metric"><span class="m-label">Scheduled Hours</span><span class="m-value">' + c.scheduled.toFixed(1) + 'h</span></div>' +
      '<div class="comparison-metric"><span class="m-label">Block Hours</span><span class="m-value">' + c.blocked.toFixed(1) + 'h</span></div>' +
      '<div class="comparison-metric"><span class="m-label">Net Available</span><span class="m-value">' + c.netAvailable.toFixed(1) + 'h</span></div>' +
      '<div class="comparison-metric"><span class="m-label">Appointment Hours</span><span class="m-value">' + c.appointment.toFixed(1) + 'h</span></div>' +
      '</div>';
  }

  document.getElementById('weekendComparisonContainer').innerHTML =
    '<div class="comparison-cards">' +
    card('Weekday (Mon\u2013Fri)', cWd, aWd, '4-wk avg', !isAvg) +
    card('Weekend (Sat\u2013Sun)', cWe, aWe, '4-wk avg', !isAvg) +
    '</div>';
}

// ============================================================
// VIEW TOGGLE
// ============================================================
function buildViewToggle() {
  const wrap = document.getElementById('viewToggle');
  wrap.innerHTML = '';
  ['utilization', 'staffing'].forEach(mode => {
    const btn = document.createElement('button');
    btn.className = 'view-btn' + (state.viewMode === mode ? ' active' : '');
    btn.textContent = mode === 'utilization' ? 'Utilization' : 'Staffing Recommendations';
    btn.addEventListener('click', () => {
      state.viewMode = mode;
      wrap.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      // title stays as "Utilization Heatmap" regardless of view mode
      renderHeatmap();
      renderStaffingExtras();
    });
    wrap.appendChild(btn);
  });
}

function buildThresholdInputs() {
  const addEl = document.getElementById('addInput');
  const cutEl = document.getElementById('cutInput');
  function applyThresholds() {
    let a = parseInt(addEl.value, 10);
    let c = parseInt(cutEl.value, 10);
    if (isNaN(a) || isNaN(c)) return;
    a = Math.max(1, Math.min(100, a));
    c = Math.max(0, Math.min(99, c));
    if (a <= c) { a = c + 1; }
    addEl.value = a; cutEl.value = c;
    ADD = a; CUT = c;
    renderHeatmap();
    renderStaffingExtras();
  }
  addEl.addEventListener('change', applyThresholds);
  cutEl.addEventListener('change', applyThresholds);
  addEl.addEventListener('input', function() {
    // Remove min/max enforcement during typing so users can freely edit
    addEl.removeAttribute('min'); addEl.removeAttribute('max');
  });
  cutEl.addEventListener('input', function() {
    cutEl.removeAttribute('min'); cutEl.removeAttribute('max');
  });
  addEl.addEventListener('blur', function() {
    addEl.min = 1; addEl.max = 100; applyThresholds();
  });
  cutEl.addEventListener('blur', function() {
    cutEl.min = 0; cutEl.max = 99; applyThresholds();
  });
}

// ============================================================
// INIT
// ============================================================
const allStart = DATA.META.periods[0].start;
const allEnd   = DATA.META.periods[DATA.META.periods.length - 1].end;
document.getElementById('dateRangeText').innerHTML =
  'Current: <strong>' + DATA.META.periods[DATA.META.periods.length - 1].label + '</strong>' +
  ' &nbsp;|&nbsp; 4-week window: ' + allStart + ' \u2013 ' + allEnd;

buildFilters();
buildViewToggle();
buildThresholdInputs();
recalculate();
</script>
</body>
</html>'''


# ============================================================
# REPORT GENERATOR
# ============================================================
def generate_html_report(payload):
    """Inject payload into HTML_TEMPLATE and return the complete HTML string."""
    periods = payload['META']['periods']
    s0 = datetime.strptime(periods[0]['start'], '%Y-%m-%d')
    sN = datetime.strptime(periods[-1]['end'], '%Y-%m-%d')
    report_title = (
        f"Plantation \u2014 4-Week Utilization "
        f"({s0.strftime('%b')} {s0.day} \u2013 {sN.strftime('%b')} {sN.day})"
    )
    html = HTML_TEMPLATE.replace('__REPORT_TITLE__', html_lib.escape(report_title))
    # Escape forward slashes in closing tags to prevent </script> injection
    json_str = json.dumps(payload, default=str).replace('</', '<\\/')
    html = html.replace('__DATA_PAYLOAD__', json_str)
    return html


def compute_validation_summary(payload):
    """Return a DataFrame summarizing record counts and utilization per period."""
    rows = []
    for p in payload['META']['periods']:
        tag = ' (current)' if p.get('isCurrent') else ''
        p_att  = [r for r in payload['ATTENDANCE']   if p['start'] <= r['date'] <= p['end']]
        p_appt = [r for r in payload['APPOINTMENTS'] if p['start'] <= r['date'] <= p['end']]
        off_by_default = ['shift adjustment', 'leaving early']
        p_blk  = [r for r in payload['BLOCKOUTS']    if p['start'] <= r['date'] <= p['end']
                   and not any(s in r.get('blockType', '').lower() for s in off_by_default)]
        sched  = sum(r['scheduledHours'] for r in p_att)
        appt_h = sum(r['durationMin'] / 60 for r in p_appt)
        # Clip block hours to operating window (HOUR_START-HOUR_END) for consistency
        op_start = HOUR_START * 60
        op_end   = HOUR_END * 60
        blk_h = 0
        for r in p_blk:
            clipped = max(0, min(r['endMinute'], op_end) - max(r['startMinute'], op_start))
            blk_h += clipped / 60
        net    = sched - blk_h
        util   = appt_h / net * 100 if net > 0 else 0
        rows.append({
            'Period':           p['label'] + tag,
            'Attendance':       len(p_att),
            'Appointments':     len(p_appt),
            'Scheduled Hrs':    round(sched, 1),
            'Appt Hrs':         round(appt_h, 1),
            'Block Hrs':        round(blk_h, 1),
            'Net Available':    round(net, 1),
            'Utilization':      f"{util:.1f}%",
        })
    return pd.DataFrame(rows)


# ============================================================
# STREAMLIT UI
# ============================================================
st.title("Plantation \u2014 4-Week Utilization Report")
st.markdown("Upload Zenoti exports to generate an interactive HTML report.")

# --- Clear cached report when files change ---
def _clear_report():
    st.session_state.pop('html_report', None)
    st.session_state.pop('data_payload', None)


# --- File uploaders ---
col1, col2 = st.columns(2)
with col1:
    att_file = st.file_uploader("Attendance", type=["csv", "xls", "xlsx"], key="att", on_change=_clear_report)
    appt_file = st.file_uploader("Appointments", type=["csv", "xls", "xlsx"], key="appt", on_change=_clear_report)
with col2:
    bot_file = st.file_uploader("Block Out Time", type=["xls", "xlsx", "html", "csv"], key="bot", on_change=_clear_report)
    mem_file = st.file_uploader("Membership (optional)", type=["csv", "xls", "xlsx"], key="mem", on_change=_clear_report)


# --- Generate button ---
required_ready = att_file is not None and appt_file is not None and bot_file is not None

if st.button("Generate Report", type="primary", disabled=not required_ready):
    _clear_report()
    try:
        with st.spinner("Loading attendance..."):
            att_df = load_attendance(att_file)

        with st.spinner("Loading appointments..."):
            appt_df = load_appointments(appt_file)

        with st.spinner("Loading block out time..."):
            bot_df = load_blockouts(bot_file)

        with st.spinner("Tagging members..."):
            if mem_file is not None:
                lookup = load_membership(mem_file)
            else:
                lookup = {}
            appt_df = tag_members(appt_df, lookup)

        with st.spinner("Building payload..."):
            payload = build_data_payload(att_df, appt_df, bot_df, has_membership=(mem_file is not None))

        with st.spinner("Generating HTML report..."):
            html_report = generate_html_report(payload)

        st.session_state['html_report'] = html_report
        st.session_state['data_payload'] = payload

        n_total = len(appt_df)
        if mem_file is not None:
            n_mem = int(appt_df['isMember'].sum())
            appt_detail = f"{n_total} appointments ({n_mem} member / {n_total - n_mem} non-member)"
        else:
            appt_detail = f"{n_total} appointments"
        st.success(
            f"Report generated! "
            f"{len(att_df)} attendance records, "
            f"{appt_detail}, "
            f"{len(bot_df)} block-out entries."
        )

    except DataLoadError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"Unexpected error: {e}")
        st.code(traceback.format_exc(), language="text")

if not required_ready:
    st.caption("Upload all 3 required files (Attendance, Appointments, Block Out Time) as CSV or Excel to enable report generation.")


# --- Validation summary + download ---
if 'data_payload' in st.session_state:
    st.subheader("Validation Summary")
    summary_df = compute_validation_summary(st.session_state['data_payload'])
    st.dataframe(summary_df, use_container_width=True, hide_index=True)
    st.caption("Block hours exclude types containing \"shift adjustment\" or \"leaving early\" (matching report defaults).")

if 'html_report' in st.session_state:
    html_bytes = st.session_state['html_report'].encode('utf-8')

    # Build filename from periods
    periods = st.session_state['data_payload']['META']['periods']
    s0 = datetime.strptime(periods[0]['start'], '%Y-%m-%d')
    sN = datetime.strptime(periods[-1]['end'], '%Y-%m-%d')
    filename = f"Plantation 4-Week Utilization {s0.strftime('%b')} {s0.day} - {sN.strftime('%b')} {sN.day}.html"

    st.download_button(
        label="Download HTML Report",
        data=html_bytes,
        file_name=filename,
        mime="text/html",
    )
    st.caption(f"File size: {len(html_bytes) / 1024:.0f} KB")
