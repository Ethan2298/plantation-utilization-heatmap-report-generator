from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from scripts.loaders import (
    compute_membership_flags,
    load_appointments_mirror,
    load_attendance_mirror,
    load_blockouts_mirror,
    load_membership,
)


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _build_periods(all_dates: list[str]) -> list[dict[str, Any]]:
    data_end = datetime.strptime(all_dates[-1], "%Y-%m-%d")
    periods = []
    p_end = data_end
    for i in range(4):
        p_start = p_end - timedelta(days=6)
        if p_start.strftime("%b") == p_end.strftime("%b"):
            label = f"{p_start.strftime('%b')} {p_start.day}\u2013{p_end.day}"
        else:
            label = f"{p_start.strftime('%b')} {p_start.day}\u2013{p_end.strftime('%b')} {p_end.day}"
        periods.insert(
            0,
            {
                "label": label,
                "start": p_start.strftime("%Y-%m-%d"),
                "end": p_end.strftime("%Y-%m-%d"),
                "isCurrent": i == 0,
            },
        )
        p_end = p_start - timedelta(days=1)
    return periods


def _build_payload(att_df: pd.DataFrame, appt_df: pd.DataFrame, blk_df: pd.DataFrame, has_membership: bool) -> Dict[str, Any]:
    attendance_records = att_df[
        ["dateStr", "dayOfWeek", "startMinute", "endMinute", "scheduledHours"]
    ].rename(columns={"dateStr": "date"}).to_dict("records")

    appt_cols = ["dateStr", "dayOfWeek", "startMinute", "endMinute", "durationMin"]
    if has_membership and "isMember" in appt_df.columns:
        appt_cols.append("isMember")
    appointment_records = appt_df[appt_cols].rename(columns={"dateStr": "date"}).to_dict("records")

    blockout_records = blk_df[
        ["dateStr", "dayOfWeek", "startMinute", "endMinute", "blockHours", "BlockOutTimeType"]
    ].rename(columns={"dateStr": "date", "BlockOutTimeType": "blockType"}).to_dict("records")

    all_dates = sorted(
        set([r["date"] for r in attendance_records] + [r["date"] for r in appointment_records] + [r["date"] for r in blockout_records])
    )
    periods = _build_periods(all_dates)

    return {
        "META": {
            "reportDate": all_dates[-1],
            "dataStartDate": all_dates[0],
            "dataEndDate": all_dates[-1],
            "blockTypes": sorted(blk_df["BlockOutTimeType"].dropna().unique().tolist()),
            "hasMembership": has_membership,
            "hourStart": 9,
            "hourEnd": 21,
            "periods": periods,
        },
        "ATTENDANCE": attendance_records,
        "APPOINTMENTS": appointment_records,
        "BLOCKOUTS": blockout_records,
    }


def _make_html(payload: Dict[str, Any]) -> str:
    json_payload = json.dumps(payload).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html><body>
<div id=\"scorecard\"></div>
<div id=\"heatmapContainer\"></div>
<div id=\"dailyTableContainer\"></div>
<div id=\"weekendComparisonContainer\"></div>
<div id=\"tooltip\"></div>
<div id=\"filterBody\"></div>
<div id=\"dateRangeText\"></div>
<script>
function computeMetrics(){{}}
function renderHeatmap(){{}}
function minsInHour(){{}}
const DATA = {json_payload};
</script>
</body></html>
"""


def _buggy_membership_flags(appt_df: pd.DataFrame, mem_df: pd.DataFrame) -> pd.Series:
    lookup: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    local_mem = mem_df.copy()
    local_mem["StartDate"] = pd.to_datetime(local_mem["StartDate"], errors="coerce")
    local_mem["EndDate"] = pd.to_datetime(local_mem["EndDate"], errors="coerce")

    for _, row in local_mem.iterrows():
        code = str(row["GuestCode"]).strip()
        if code and code != "nan" and pd.notna(row["StartDate"]):
            lookup.setdefault(code, []).append((row["StartDate"], row["EndDate"]))

    def is_member(row: pd.Series) -> bool:
        code = str(row["Guest Code"]).strip() if pd.notna(row["Guest Code"]) else ""
        if not code or code not in lookup:
            return False
        for start, end in lookup[code]:
            if row["start_dt"] >= start and (pd.isna(end) or row["start_dt"] <= end):
                return True
        return False

    return appt_df.apply(is_member, axis=1)


def create_case(
    tmp_path: Path,
    *,
    scenario: str = "baseline",
    with_membership_file: bool = False,
    embed_membership_mode: Optional[str] = None,
) -> Dict[str, str]:
    start = datetime(2026, 1, 30)
    days = [start + timedelta(days=i) for i in range(28)]

    attendance = pd.DataFrame(
        {
            "Schedule Status": ["Working"] * 28,
            "Date": [_fmt_date(d) for d in days],
            "Schedule": ["09:00 AM - 05:00 PM"] * 28,
        }
    )

    appointments = pd.DataFrame(
        {
            "Start Time": [f"{_fmt_date(d)} 10:00 AM" for d in days],
            "End Time": [f"{_fmt_date(d)} 11:00 AM" for d in days],
            "Guest Code": ["ABC123"] * 28,
        }
    )

    blockouts = pd.DataFrame(
        {
            "Date": [_fmt_date(d) for d in days],
            "StartTime": ["12:00PM"] * 28,
            "EndTime": ["01:00PM"] * 28,
            "BlockOutTimeType": ["Lunch"] * 28,
            "Block Out Time (in hours)": [1.0] * 28,
        }
    )

    membership = pd.DataFrame(
        {
            "GuestCode": ["ABC123"],
            "StartDate": ["2026-01-01"],
            "EndDate": ["2026-12-31"],
        }
    )

    if scenario == "attendance_parse_drift":
        attendance.loc[0, "Schedule"] = "09:00AM-05:00PM"

    if scenario == "blockout_parse_drift":
        blockouts.loc[0, "StartTime"] = "13:00"
        blockouts.loc[0, "EndTime"] = "14:00"

    if scenario == "negative_duration":
        appointments.loc[0, "Start Time"] = f"{_fmt_date(days[0])} 11:00 AM"
        appointments.loc[0, "End Time"] = f"{_fmt_date(days[0])} 10:00 AM"

    if scenario == "zero_duration":
        extra = pd.DataFrame(
            {
                "Start Time": [f"{_fmt_date(days[0])} 10:00 AM"],
                "End Time": [f"{_fmt_date(days[0])} 10:00 AM"],
                "Guest Code": ["ABC123"],
            }
        )
        appointments = pd.concat([appointments, extra], ignore_index=True)

    if scenario == "blank_block_type":
        blockouts.loc[0, "BlockOutTimeType"] = ""

    if scenario == "membership_enddate_mismatch":
        target_date = datetime(2026, 2, 15)
        idx = days.index(target_date)
        appointments.loc[:, "Guest Code"] = "EDGE001"
        membership = pd.DataFrame(
            {
                "GuestCode": ["EDGE001"],
                "StartDate": ["2026-01-01"],
                "EndDate": ["2026-02-15"],
            }
        )
        appointments.loc[idx, "Start Time"] = f"{_fmt_date(target_date)} 08:00 PM"
        appointments.loc[idx, "End Time"] = f"{_fmt_date(target_date)} 09:00 PM"

    if scenario == "guest_code_normalization_mismatch":
        appointments.loc[:, "Guest Code"] = "123.0"
        membership = pd.DataFrame(
            {
                "GuestCode": [123],
                "StartDate": ["2026-01-01"],
                "EndDate": ["2026-12-31"],
            }
        )

    att_path = tmp_path / "Attendance.csv"
    appt_path = tmp_path / "Appointments.csv"
    blk_path = tmp_path / "Blockouts.csv"
    mem_path = tmp_path / "Membership.csv"

    attendance.to_csv(att_path, index=False)
    appointments.to_csv(appt_path, index=False)
    blockouts.to_csv(blk_path, index=False)

    if with_membership_file or embed_membership_mode:
        membership.to_csv(mem_path, index=False)

    att_df = load_attendance_mirror(str(att_path)).df
    appt_df = load_appointments_mirror(str(appt_path)).df
    blk_df = load_blockouts_mirror(str(blk_path)).df

    has_membership = False
    if embed_membership_mode:
        has_membership = True
        if embed_membership_mode == "correct":
            membership_lookup = load_membership(str(mem_path)).lookup
            appt_df["isMember"] = compute_membership_flags(appt_df, membership_lookup)
        elif embed_membership_mode == "buggy":
            mem_df = pd.read_csv(mem_path)
            appt_df["isMember"] = _buggy_membership_flags(appt_df, mem_df)
        else:
            raise ValueError(f"Unsupported embed_membership_mode: {embed_membership_mode}")

    payload = _build_payload(att_df, appt_df, blk_df, has_membership=has_membership)

    if scenario == "scorecard_invariant_break":
        payload["APPOINTMENTS"][0]["durationMin"] = payload["APPOINTMENTS"][0]["durationMin"] + 30

    if scenario == "period_overlap_break":
        payload["META"]["periods"][1]["start"] = payload["META"]["periods"][0]["end"]

    html_path = tmp_path / "report.html"
    html_path.write_text(_make_html(payload), encoding="utf-8")

    result = {
        "html": str(html_path),
        "attendance": str(att_path),
        "appointments": str(appt_path),
        "blockout": str(blk_path),
    }

    if with_membership_file or embed_membership_mode:
        result["membership"] = str(mem_path)

    return result
