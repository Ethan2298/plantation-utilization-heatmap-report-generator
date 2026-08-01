from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


@dataclass
class SourceLoadResult:
    df: pd.DataFrame
    input_rows: int
    drop_reasons: Dict[str, int]

    @property
    def final_rows(self) -> int:
        return len(self.df)


@dataclass
class MembershipLoadResult:
    lookup: Dict[str, List[Tuple[pd.Timestamp, pd.Timestamp]]]
    input_rows: int
    drop_reasons: Dict[str, int]


def read_tabular_path(path: str) -> pd.DataFrame:
    lower = path.lower()
    if lower.endswith(".csv"):
        return pd.read_csv(path)
    if lower.endswith((".xls", ".xlsx")):
        return pd.read_excel(path)
    raise ValueError(f"Unsupported file type: {path}")


def _require_columns(df: pd.DataFrame, required: List[str], label: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"{label} missing columns: {joined}")


# ---------------------------------------------------------------------------
# Shared normalization
# ---------------------------------------------------------------------------
def normalize_guest_code(value: Any) -> str:
    if pd.isna(value):
        return ""
    s = str(value).strip()
    if not s:
        return ""
    lowered = s.lower()
    if lowered in {"nan", "none", "nat"}:
        return ""

    # Collapse float-like numeric IDs (e.g. 123.0 -> 123)
    if re.fullmatch(r"[-+]?\d+(?:\.0+)?", s):
        try:
            return str(int(float(s)))
        except Exception:
            pass
    return s.upper()


# ---------------------------------------------------------------------------
# Mirror parsers (intentionally match app.py behavior)
# ---------------------------------------------------------------------------
def parse_schedule_to_minutes_mirror(sched_str: Any) -> Tuple[Optional[int], Optional[int]]:
    try:
        parts = str(sched_str).split(" - ")
        if len(parts) != 2:
            return None, None
        start = datetime.strptime(parts[0].strip(), "%I:%M %p")
        end = datetime.strptime(parts[1].strip(), "%I:%M %p")
        return start.hour * 60 + start.minute, end.hour * 60 + end.minute
    except Exception:
        return None, None


def parse_time_to_minutes_mirror(value: Any) -> Optional[int]:
    s = str(value).strip().upper()
    for fmt in ("%I:%M%p", "%I:%M %p", "%I:%M:%S %p"):
        try:
            t = datetime.strptime(s, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Reference parsers (intentionally independent and more resilient)
# ---------------------------------------------------------------------------
_TIME_FORMATS_REFERENCE = (
    "%I:%M %p",
    "%I:%M%p",
    "%I:%M:%S %p",
    "%I:%M:%S%p",
    "%H:%M",
    "%H:%M:%S",
)


def _parse_time_reference(value: Any) -> Optional[int]:
    if isinstance(value, time):
        return value.hour * 60 + value.minute
    if isinstance(value, pd.Timestamp):
        return value.hour * 60 + value.minute
    if isinstance(value, datetime):
        return value.hour * 60 + value.minute

    s = str(value).strip()
    if not s:
        return None
    lowered = s.lower()
    if lowered in {"nan", "none", "nat"}:
        return None

    normalized = re.sub(r"\s+", " ", s).strip().upper().replace(".", "")

    # Ensure there is a space before AM/PM when missing.
    normalized = re.sub(r"(\d)(AM|PM)$", r"\1 \2", normalized)

    for fmt in _TIME_FORMATS_REFERENCE:
        try:
            t = datetime.strptime(normalized, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


def parse_schedule_to_minutes_reference(sched_str: Any) -> Tuple[Optional[int], Optional[int]]:
    text = "" if pd.isna(sched_str) else str(sched_str)
    match = re.match(r"^\s*(.+?)\s*-\s*(.+?)\s*$", text)
    if not match:
        return None, None
    start = _parse_time_reference(match.group(1))
    end = _parse_time_reference(match.group(2))
    return start, end


def parse_time_to_minutes_reference(value: Any) -> Optional[int]:
    return _parse_time_reference(value)


# ---------------------------------------------------------------------------
# Source loaders - mirror
# ---------------------------------------------------------------------------
def load_attendance_mirror(path: str) -> SourceLoadResult:
    raw = read_tabular_path(path)
    _require_columns(raw, ["Schedule Status", "Date", "Schedule"], "Attendance")

    input_rows = len(raw)
    drop_reasons = {
        "non_working": 0,
        "missing_date_or_schedule": 0,
        "schedule_parse_fail": 0,
    }

    df = raw[raw["Schedule Status"] == "Working"].copy()
    drop_reasons["non_working"] = input_rows - len(df)

    df["date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    missing_mask = df["date"].isna() | df["Schedule"].isna()
    drop_reasons["missing_date_or_schedule"] = int(missing_mask.sum())
    df = df.loc[~missing_mask].copy()

    parsed = df["Schedule"].apply(
        lambda s: pd.Series(parse_schedule_to_minutes_mirror(s), index=["startMinute", "endMinute"])
    )
    df = pd.concat([df, parsed], axis=1)
    parse_fail_mask = df["startMinute"].isna() | df["endMinute"].isna()
    drop_reasons["schedule_parse_fail"] = int(parse_fail_mask.sum())
    df = df.loc[~parse_fail_mask].copy()

    df["startMinute"] = df["startMinute"].astype(int)
    df["endMinute"] = df["endMinute"].astype(int)
    df["dayOfWeek"] = df["date"].dt.dayofweek
    df["dateStr"] = df["date"].dt.strftime("%Y-%m-%d")
    df["scheduledHours"] = (df["endMinute"] - df["startMinute"]) / 60.0
    return SourceLoadResult(df=df, input_rows=input_rows, drop_reasons=drop_reasons)


def load_appointments_mirror(path: str) -> SourceLoadResult:
    raw = read_tabular_path(path)
    _require_columns(raw, ["Start Time", "End Time"], "Appointments")

    input_rows = len(raw)
    drop_reasons = {
        "datetime_parse_fail": 0,
        "zero_duration": 0,
        "negative_duration": 0,
    }

    df = raw.copy()
    df["start_dt"] = pd.to_datetime(df["Start Time"], format="mixed", errors="coerce")
    df["end_dt"] = pd.to_datetime(df["End Time"], format="mixed", errors="coerce")

    dt_fail_mask = df["start_dt"].isna() | df["end_dt"].isna()
    drop_reasons["datetime_parse_fail"] = int(dt_fail_mask.sum())
    df = df.loc[~dt_fail_mask].copy()

    df["startMinute"] = df["start_dt"].dt.hour * 60 + df["start_dt"].dt.minute
    df["endMinute"] = df["end_dt"].dt.hour * 60 + df["end_dt"].dt.minute
    delta = df["endMinute"] - df["startMinute"]
    drop_reasons["zero_duration"] = int((delta == 0).sum())
    drop_reasons["negative_duration"] = int((delta < 0).sum())

    df = df.loc[delta > 0].copy()
    df["durationMin"] = df["endMinute"] - df["startMinute"]
    df["dayOfWeek"] = df["start_dt"].dt.dayofweek
    df["dateStr"] = df["start_dt"].dt.strftime("%Y-%m-%d")
    return SourceLoadResult(df=df, input_rows=input_rows, drop_reasons=drop_reasons)


def _load_blockout_table_mirror(path: str) -> pd.DataFrame:
    lower = path.lower()
    if lower.endswith(".csv") or lower.endswith(".xlsx"):
        return read_tabular_path(path)

    # app.py behavior: .xls and .html treated as HTML table content.
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    tables = pd.read_html(StringIO(content))
    if not tables:
        raise ValueError("No tables found in Block Out Time file")
    return tables[0].copy()


def load_blockouts_mirror(path: str) -> SourceLoadResult:
    raw = _load_blockout_table_mirror(path)
    required = ["Date", "StartTime", "EndTime", "BlockOutTimeType", "Block Out Time (in hours)"]
    _require_columns(raw, required, "Block Out Time")

    input_rows = len(raw)
    drop_reasons = {
        "missing_required_time": 0,
        "time_parse_fail": 0,
        "am_pm_wrap_applied": 0,
        "blank_or_null_block_type": 0,
    }

    df = raw.copy()
    df["date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")

    missing_mask = df["date"].isna() | df["StartTime"].isna() | df["EndTime"].isna()
    drop_reasons["missing_required_time"] = int(missing_mask.sum())
    df = df.loc[~missing_mask].copy()

    df["startMinute"] = df["StartTime"].astype(str).apply(parse_time_to_minutes_mirror)
    df["endMinute"] = df["EndTime"].astype(str).apply(parse_time_to_minutes_mirror)

    parse_fail_mask = df["startMinute"].isna() | df["endMinute"].isna()
    drop_reasons["time_parse_fail"] = int(parse_fail_mask.sum())
    df = df.loc[~parse_fail_mask].copy()

    df["startMinute"] = df["startMinute"].astype(int)
    df["endMinute"] = df["endMinute"].astype(int)

    wrap_mask = df["endMinute"] < df["startMinute"]
    drop_reasons["am_pm_wrap_applied"] = int(wrap_mask.sum())
    df.loc[wrap_mask, "endMinute"] = df.loc[wrap_mask, "endMinute"] + 720

    block_type = df["BlockOutTimeType"]
    blank_type_mask = block_type.isna() | (block_type.astype(str).str.strip() == "")
    drop_reasons["blank_or_null_block_type"] = int(blank_type_mask.sum())

    df["dayOfWeek"] = df["date"].dt.dayofweek
    df["dateStr"] = df["date"].dt.strftime("%Y-%m-%d")
    df["blockHours"] = pd.to_numeric(df["Block Out Time (in hours)"], errors="coerce").fillna(0)
    return SourceLoadResult(df=df, input_rows=input_rows, drop_reasons=drop_reasons)


# ---------------------------------------------------------------------------
# Source loaders - reference
# ---------------------------------------------------------------------------
def load_attendance_reference(path: str) -> SourceLoadResult:
    raw = read_tabular_path(path)
    _require_columns(raw, ["Schedule Status", "Date", "Schedule"], "Attendance")

    input_rows = len(raw)
    drop_reasons = {
        "non_working": 0,
        "missing_date_or_schedule": 0,
        "schedule_parse_fail": 0,
    }

    df = raw[raw["Schedule Status"].astype(str).str.strip().str.lower() == "working"].copy()
    drop_reasons["non_working"] = input_rows - len(df)

    df["date"] = pd.to_datetime(df["Date"], errors="coerce")
    missing_mask = df["date"].isna() | df["Schedule"].isna()
    drop_reasons["missing_date_or_schedule"] = int(missing_mask.sum())
    df = df.loc[~missing_mask].copy()

    parsed = df["Schedule"].apply(
        lambda s: pd.Series(parse_schedule_to_minutes_reference(s), index=["startMinute", "endMinute"])
    )
    df = pd.concat([df, parsed], axis=1)
    parse_fail_mask = df["startMinute"].isna() | df["endMinute"].isna()
    drop_reasons["schedule_parse_fail"] = int(parse_fail_mask.sum())
    df = df.loc[~parse_fail_mask].copy()

    df["startMinute"] = df["startMinute"].astype(int)
    df["endMinute"] = df["endMinute"].astype(int)
    df["dayOfWeek"] = df["date"].dt.dayofweek
    df["dateStr"] = df["date"].dt.strftime("%Y-%m-%d")
    df["scheduledHours"] = (df["endMinute"] - df["startMinute"]) / 60.0
    return SourceLoadResult(df=df, input_rows=input_rows, drop_reasons=drop_reasons)


def load_appointments_reference(path: str) -> SourceLoadResult:
    raw = read_tabular_path(path)
    _require_columns(raw, ["Start Time", "End Time"], "Appointments")

    input_rows = len(raw)
    drop_reasons = {
        "datetime_parse_fail": 0,
        "zero_duration": 0,
        "negative_duration": 0,
    }

    df = raw.copy()
    df["start_dt"] = pd.to_datetime(df["Start Time"], errors="coerce")
    df["end_dt"] = pd.to_datetime(df["End Time"], errors="coerce")

    dt_fail_mask = df["start_dt"].isna() | df["end_dt"].isna()
    drop_reasons["datetime_parse_fail"] = int(dt_fail_mask.sum())
    df = df.loc[~dt_fail_mask].copy()

    df["startMinute"] = df["start_dt"].dt.hour * 60 + df["start_dt"].dt.minute
    df["endMinute"] = df["end_dt"].dt.hour * 60 + df["end_dt"].dt.minute
    delta = df["endMinute"] - df["startMinute"]
    drop_reasons["zero_duration"] = int((delta == 0).sum())
    drop_reasons["negative_duration"] = int((delta < 0).sum())

    df = df.loc[delta > 0].copy()
    df["durationMin"] = df["endMinute"] - df["startMinute"]
    df["dayOfWeek"] = df["start_dt"].dt.dayofweek
    df["dateStr"] = df["start_dt"].dt.strftime("%Y-%m-%d")
    return SourceLoadResult(df=df, input_rows=input_rows, drop_reasons=drop_reasons)


def _load_blockout_table_reference(path: str) -> pd.DataFrame:
    lower = path.lower()

    if lower.endswith(".csv"):
        return pd.read_csv(path)

    if lower.endswith((".xls", ".xlsx")):
        # Prefer native excel parser; fallback to HTML-table parser for Zenoti HTML-backed .xls.
        try:
            return pd.read_excel(path)
        except Exception:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
            tables = pd.read_html(StringIO(content))
            if not tables:
                raise ValueError("No tables found in Block Out Time file")
            return tables[0].copy()

    if lower.endswith(".html"):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
        tables = pd.read_html(StringIO(content))
        if not tables:
            raise ValueError("No tables found in Block Out Time file")
        return tables[0].copy()

    raise ValueError(f"Unsupported blockout file type: {path}")


def load_blockouts_reference(path: str) -> SourceLoadResult:
    raw = _load_blockout_table_reference(path)
    required = ["Date", "StartTime", "EndTime", "BlockOutTimeType", "Block Out Time (in hours)"]
    _require_columns(raw, required, "Block Out Time")

    input_rows = len(raw)
    drop_reasons = {
        "missing_required_time": 0,
        "time_parse_fail": 0,
        "am_pm_wrap_applied": 0,
        "blank_or_null_block_type": 0,
    }

    df = raw.copy()
    df["date"] = pd.to_datetime(df["Date"], errors="coerce")

    missing_mask = df["date"].isna() | df["StartTime"].isna() | df["EndTime"].isna()
    drop_reasons["missing_required_time"] = int(missing_mask.sum())
    df = df.loc[~missing_mask].copy()

    df["startMinute"] = df["StartTime"].apply(parse_time_to_minutes_reference)
    df["endMinute"] = df["EndTime"].apply(parse_time_to_minutes_reference)

    parse_fail_mask = df["startMinute"].isna() | df["endMinute"].isna()
    drop_reasons["time_parse_fail"] = int(parse_fail_mask.sum())
    df = df.loc[~parse_fail_mask].copy()

    df["startMinute"] = df["startMinute"].astype(int)
    df["endMinute"] = df["endMinute"].astype(int)

    wrap_mask = df["endMinute"] < df["startMinute"]
    drop_reasons["am_pm_wrap_applied"] = int(wrap_mask.sum())
    df.loc[wrap_mask, "endMinute"] = df.loc[wrap_mask, "endMinute"] + 720

    block_type = df["BlockOutTimeType"]
    blank_type_mask = block_type.isna() | (block_type.astype(str).str.strip() == "")
    drop_reasons["blank_or_null_block_type"] = int(blank_type_mask.sum())

    df["dayOfWeek"] = df["date"].dt.dayofweek
    df["dateStr"] = df["date"].dt.strftime("%Y-%m-%d")
    df["blockHours"] = pd.to_numeric(df["Block Out Time (in hours)"], errors="coerce").fillna(0)
    return SourceLoadResult(df=df, input_rows=input_rows, drop_reasons=drop_reasons)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
def load_membership(path: str) -> MembershipLoadResult:
    raw = read_tabular_path(path)
    _require_columns(raw, ["GuestCode", "StartDate", "EndDate"], "Membership")

    input_rows = len(raw)
    drop_reasons = {
        "missing_guest_code": 0,
        "start_date_parse_fail": 0,
    }

    df = raw.copy()
    df["StartDate"] = pd.to_datetime(df["StartDate"], errors="coerce")
    df["EndDate"] = pd.to_datetime(df["EndDate"], errors="coerce")

    lookup: Dict[str, List[Tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for _, row in df.iterrows():
        code = normalize_guest_code(row["GuestCode"])
        if not code:
            drop_reasons["missing_guest_code"] += 1
            continue

        start = row["StartDate"]
        end = row["EndDate"]
        if pd.isna(start):
            drop_reasons["start_date_parse_fail"] += 1
            continue

        # Inclusive end date: valid until end-of-day.
        if pd.notna(end):
            end = pd.Timestamp(end).normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)

        lookup.setdefault(code, []).append((pd.Timestamp(start), pd.Timestamp(end) if pd.notna(end) else pd.NaT))

    return MembershipLoadResult(lookup=lookup, input_rows=input_rows, drop_reasons=drop_reasons)


def compute_membership_flags(appt_df: pd.DataFrame, membership_lookup: Dict[str, List[Tuple[pd.Timestamp, pd.Timestamp]]]) -> pd.Series:
    if not membership_lookup:
        return pd.Series([False] * len(appt_df), index=appt_df.index)

    if "Guest Code" not in appt_df.columns:
        raise ValueError("Appointments file has no 'Guest Code' column; cannot validate membership")

    def is_member(row: pd.Series) -> bool:
        code = normalize_guest_code(row.get("Guest Code"))
        if not code or code not in membership_lookup:
            return False

        appt_dt = pd.Timestamp(row["start_dt"])
        for start, end in membership_lookup[code]:
            if appt_dt >= start and (pd.isna(end) or appt_dt <= end):
                return True
        return False

    return appt_df.apply(is_member, axis=1)
