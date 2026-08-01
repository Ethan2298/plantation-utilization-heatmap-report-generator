from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from scripts.loaders import (
    SourceLoadResult,
    compute_membership_flags,
    load_appointments_mirror,
    load_appointments_reference,
    load_attendance_mirror,
    load_attendance_reference,
    load_blockouts_mirror,
    load_blockouts_reference,
    load_membership,
)


VALIDATION_VERSION = "1.0"
HOUR_START = 9
HOUR_END = 21
HOURS_TOLERANCE = 0.01
UTILIZATION_TOLERANCE_PP = 0.05


@dataclass
class CheckResult:
    id: str
    severity: str  # critical | warning | info
    status: str  # PASS | WARN | FAIL
    message: str
    details: str = ""


@dataclass
class ValidationSummary:
    pass_count: int
    warn_count: int
    fail_count: int
    critical_fail_count: int
    exit_code: int


@dataclass
class ValidationRunResult:
    tool: str
    version: str
    generated_at: str
    inputs: Dict[str, str]
    checks: List[CheckResult]
    summary: ValidationSummary
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["summary"] = asdict(self.summary)
        payload["checks"] = [asdict(c) for c in self.checks]
        return payload


class _Collector:
    def __init__(self) -> None:
        self.checks: List[CheckResult] = []

    def pass_(self, check_id: str, message: str, severity: str = "info", details: str = "") -> None:
        self.checks.append(CheckResult(check_id, severity, "PASS", message, details))

    def warn(self, check_id: str, message: str, details: str = "") -> None:
        self.checks.append(CheckResult(check_id, "warning", "WARN", message, details))

    def fail(self, check_id: str, message: str, details: str = "", severity: str = "critical") -> None:
        self.checks.append(CheckResult(check_id, severity, "FAIL", message, details))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _extract_embedded_data(html_content: str) -> Dict[str, Any]:
    match = re.search(r"const DATA\s*=\s*(\{.*?\});\s*\n", html_content, re.DOTALL)
    if not match:
        match = re.search(r"const DATA\s*=\s*(\{.*\});", html_content, re.DOTALL)
    if not match:
        raise ValueError("Could not find embedded DATA payload in HTML")
    return json.loads(match.group(1))


def _mins_in_hour(start_min: int, end_min: int, hour_start: int) -> int:
    return max(0, min(end_min, hour_start + 60) - max(start_min, hour_start))


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return round(value, 6)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def _canonical_counter(records: Sequence[Dict[str, Any]], fields: Sequence[str]) -> Counter:
    tuples = []
    for r in records:
        tuples.append(tuple(_normalize_scalar(r.get(f)) for f in fields))
    return Counter(tuples)


def _date_in_range(ds: str, start: str, end: str) -> bool:
    return start <= ds <= end


def _source_records_att(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return df[["dateStr", "dayOfWeek", "startMinute", "endMinute", "scheduledHours"]].rename(
        columns={"dateStr": "date"}
    ).to_dict("records")


def _source_records_appt(df: pd.DataFrame, include_membership: bool = False) -> List[Dict[str, Any]]:
    cols = ["dateStr", "dayOfWeek", "startMinute", "endMinute", "durationMin"]
    if include_membership:
        cols.append("isMember")
    return df[cols].rename(columns={"dateStr": "date"}).to_dict("records")


def _source_records_blk(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return df[["dateStr", "dayOfWeek", "startMinute", "endMinute", "blockHours", "BlockOutTimeType"]].rename(
        columns={"dateStr": "date", "BlockOutTimeType": "blockType"}
    ).to_dict("records")


def _compare_record_sets(
    source_records: List[Dict[str, Any]],
    embedded_records: List[Dict[str, Any]],
    fields: Sequence[str],
) -> Tuple[int, str]:
    src_counter = _canonical_counter(source_records, fields)
    emb_counter = _canonical_counter(embedded_records, fields)
    if src_counter == emb_counter:
        return 0, ""

    src_minus = src_counter - emb_counter
    emb_minus = emb_counter - src_counter
    mismatch_count = sum(src_minus.values()) + sum(emb_minus.values())

    lines: List[str] = []
    if src_minus:
        lines.append("Present in source only:")
        for item, count in list(src_minus.items())[:3]:
            lines.append(f"  x{count} {item}")
    if emb_minus:
        lines.append("Present in embedded only:")
        for item, count in list(emb_minus.items())[:3]:
            lines.append(f"  x{count} {item}")

    return mismatch_count, "\n".join(lines)


def _compute_period_metrics(
    attendance: List[Dict[str, Any]],
    appointments: List[Dict[str, Any]],
    blockouts: List[Dict[str, Any]],
    period_start: str,
    period_end: str,
) -> Dict[str, Any]:
    p_att = [r for r in attendance if _date_in_range(r["date"], period_start, period_end)]
    p_appt = [r for r in appointments if _date_in_range(r["date"], period_start, period_end)]
    p_blk = [r for r in blockouts if _date_in_range(r["date"], period_start, period_end)]

    raw_sched = sum(float(r["scheduledHours"]) for r in p_att)
    raw_appt = sum(float(r["durationMin"]) / 60.0 for r in p_appt)
    raw_blk = sum(float(r["blockHours"]) for r in p_blk)
    raw_net = raw_sched - raw_blk
    raw_util = (raw_appt / raw_net * 100.0) if raw_net > 0 else 0.0

    grid: Dict[int, Dict[int, Dict[str, float]]] = {}
    for dow in range(7):
        grid[dow] = {}
        for hour in range(HOUR_START, HOUR_END):
            grid[dow][hour] = {"scheduled": 0.0, "blocked": 0.0, "appointment": 0.0}

    for r in p_att:
        dow = int(r["dayOfWeek"])
        for hour in range(HOUR_START, HOUR_END):
            grid[dow][hour]["scheduled"] += _mins_in_hour(int(r["startMinute"]), int(r["endMinute"]), hour * 60) / 60.0

    for r in p_blk:
        dow = int(r["dayOfWeek"])
        for hour in range(HOUR_START, HOUR_END):
            grid[dow][hour]["blocked"] += _mins_in_hour(int(r["startMinute"]), int(r["endMinute"]), hour * 60) / 60.0

    for r in p_appt:
        dow = int(r["dayOfWeek"])
        for hour in range(HOUR_START, HOUR_END):
            grid[dow][hour]["appointment"] += _mins_in_hour(int(r["startMinute"]), int(r["endMinute"]), hour * 60) / 60.0

    grid_sched = sum(grid[d][h]["scheduled"] for d in range(7) for h in range(HOUR_START, HOUR_END))
    grid_blk = sum(grid[d][h]["blocked"] for d in range(7) for h in range(HOUR_START, HOUR_END))
    grid_appt = sum(grid[d][h]["appointment"] for d in range(7) for h in range(HOUR_START, HOUR_END))
    grid_net = grid_sched - grid_blk
    grid_util = (grid_appt / grid_net * 100.0) if grid_net > 0 else 0.0

    daily: Dict[int, Dict[str, float]] = {}
    for dow in range(7):
        sched = sum(grid[dow][h]["scheduled"] for h in range(HOUR_START, HOUR_END))
        blk = sum(grid[dow][h]["blocked"] for h in range(HOUR_START, HOUR_END))
        appt = sum(grid[dow][h]["appointment"] for h in range(HOUR_START, HOUR_END))
        net = sched - blk
        util = (appt / net * 100.0) if net > 0 else 0.0
        daily[dow] = {
            "scheduled": sched,
            "blocked": blk,
            "appointment": appt,
            "net": net,
            "utilization": util,
        }

    weekday = {k: sum(daily[d][k] for d in [0, 1, 2, 3, 4]) for k in ["scheduled", "blocked", "appointment", "net"]}
    weekend = {k: sum(daily[d][k] for d in [5, 6]) for k in ["scheduled", "blocked", "appointment", "net"]}

    return {
        "attendance_count": len(p_att),
        "appointment_count": len(p_appt),
        "blockout_count": len(p_blk),
        "raw": {
            "scheduled": raw_sched,
            "blocked": raw_blk,
            "appointment": raw_appt,
            "net": raw_net,
            "utilization": raw_util,
        },
        "grid": {
            "scheduled": grid_sched,
            "blocked": grid_blk,
            "appointment": grid_appt,
            "net": grid_net,
            "utilization": grid_util,
        },
        "daily": daily,
        "weekday": weekday,
        "weekend": weekend,
    }


def _check_period_structure(periods: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if len(periods) != 4:
        errors.append(f"Expected 4 periods, got {len(periods)}")
        return False, errors

    for idx, p in enumerate(periods):
        try:
            start = datetime.strptime(p["start"], "%Y-%m-%d")
            end = datetime.strptime(p["end"], "%Y-%m-%d")
        except Exception:
            errors.append(f"Period {idx} has invalid start/end date")
            continue

        span = (end - start).days + 1
        if span != 7:
            errors.append(f"Period {idx} spans {span} days (expected 7)")

    for i in range(len(periods) - 1):
        end_i = datetime.strptime(periods[i]["end"], "%Y-%m-%d")
        start_next = datetime.strptime(periods[i + 1]["start"], "%Y-%m-%d")
        gap = (start_next - end_i).days
        if gap != 1:
            errors.append(f"Periods {i} and {i + 1} gap={gap} days (expected 1)")

    current = [p for p in periods if p.get("isCurrent")]
    if len(current) != 1:
        errors.append(f"Expected 1 current period, found {len(current)}")
    elif periods and periods[-1] is not current[0]:
        errors.append("Current period is not the last period")

    return (len(errors) == 0), errors


def _summarize(checks: Sequence[CheckResult]) -> ValidationSummary:
    pass_count = sum(1 for c in checks if c.status == "PASS")
    warn_count = sum(1 for c in checks if c.status == "WARN")
    fail_count = sum(1 for c in checks if c.status == "FAIL")
    critical_fail_count = sum(1 for c in checks if c.status == "FAIL" and c.severity == "critical")
    exit_code = 1 if critical_fail_count > 0 else 0
    return ValidationSummary(
        pass_count=pass_count,
        warn_count=warn_count,
        fail_count=fail_count,
        critical_fail_count=critical_fail_count,
        exit_code=exit_code,
    )


def run_validation(
    *,
    tool: str,
    profile: str,
    html_path: str,
    attendance_path: str,
    appointments_path: str,
    blockout_path: str,
    membership_path: Optional[str] = None,
) -> ValidationRunResult:
    collector = _Collector()
    metrics: Dict[str, Any] = {"row_loss": {}, "record_counts": {}, "period_metrics": []}

    inputs = {
        "html": html_path,
        "attendance": attendance_path,
        "appointments": appointments_path,
        "blockout": blockout_path,
    }
    if membership_path:
        inputs["membership"] = membership_path

    # ------------------------------------------------------------------
    # File checks
    # ------------------------------------------------------------------
    file_specs = [
        ("html", html_path, None),
        ("attendance", attendance_path, {".csv", ".xls", ".xlsx"}),
        ("appointments", appointments_path, {".csv", ".xls", ".xlsx"}),
        ("blockout", blockout_path, {".csv", ".xls", ".xlsx", ".html"}),
    ]
    if membership_path:
        file_specs.append(("membership", membership_path, {".csv", ".xls", ".xlsx"}))

    hard_stop = False
    for label, path, allowed in file_specs:
        if os.path.exists(path):
            collector.pass_(f"FILE-EXISTS-{label.upper()}", f"{label} file exists", severity="info")
            if allowed is not None:
                ext = os.path.splitext(path)[1].lower()
                if ext in allowed:
                    collector.pass_(f"FILE-EXT-{label.upper()}", f"{label} extension '{ext}' is valid", severity="info")
                else:
                    collector.fail(
                        f"FILE-EXT-{label.upper()}",
                        f"{label} extension '{ext}' is invalid",
                        details=f"Allowed: {sorted(allowed)}",
                    )
        else:
            collector.fail(f"FILE-EXISTS-{label.upper()}", f"{label} file is missing: {path}")
            hard_stop = True

    if hard_stop:
        summary = _summarize(collector.checks)
        return ValidationRunResult(
            tool=tool,
            version=VALIDATION_VERSION,
            generated_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            inputs=inputs,
            checks=collector.checks,
            summary=summary,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # HTML extraction
    # ------------------------------------------------------------------
    html_content = ""
    data: Dict[str, Any] = {}
    try:
        with open(html_path, "r", encoding="utf-8") as handle:
            html_content = handle.read()
        data = _extract_embedded_data(html_content)
        collector.pass_("HTML-DATA-EXTRACT", "Extracted embedded DATA payload", severity="info")
    except Exception as exc:
        collector.fail("HTML-DATA-EXTRACT", "Failed to parse embedded DATA payload", details=str(exc))
        summary = _summarize(collector.checks)
        return ValidationRunResult(
            tool=tool,
            version=VALIDATION_VERSION,
            generated_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            inputs=inputs,
            checks=collector.checks,
            summary=summary,
            metrics=metrics,
        )

    for key in ["META", "ATTENDANCE", "APPOINTMENTS", "BLOCKOUTS"]:
        if key in data:
            collector.pass_(f"DATA-KEY-{key}", f"DATA.{key} present", severity="info")
        else:
            collector.fail(f"DATA-KEY-{key}", f"DATA.{key} missing")

    if any(c.id.startswith("DATA-KEY-") and c.status == "FAIL" for c in collector.checks):
        summary = _summarize(collector.checks)
        return ValidationRunResult(
            tool=tool,
            version=VALIDATION_VERSION,
            generated_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            inputs=inputs,
            checks=collector.checks,
            summary=summary,
            metrics=metrics,
        )

    meta = data["META"]
    emb_att = data["ATTENDANCE"]
    emb_appt = data["APPOINTMENTS"]
    emb_blk = data["BLOCKOUTS"]

    # ------------------------------------------------------------------
    # Load source: mirror + reference
    # ------------------------------------------------------------------
    try:
        att_m = load_attendance_mirror(attendance_path)
        appt_m = load_appointments_mirror(appointments_path)
        blk_m = load_blockouts_mirror(blockout_path)
        collector.pass_("LOAD-MIRROR", "Loaded source files with mirror parser", severity="info")
    except Exception as exc:
        collector.fail("LOAD-MIRROR", "Mirror loader failed", details=str(exc))
        summary = _summarize(collector.checks)
        return ValidationRunResult(
            tool=tool,
            version=VALIDATION_VERSION,
            generated_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            inputs=inputs,
            checks=collector.checks,
            summary=summary,
            metrics=metrics,
        )

    try:
        att_r = load_attendance_reference(attendance_path)
        appt_r = load_appointments_reference(appointments_path)
        blk_r = load_blockouts_reference(blockout_path)
        collector.pass_("LOAD-REFERENCE", "Loaded source files with independent reference parser", severity="info")
    except Exception as exc:
        collector.fail("LOAD-REFERENCE", "Reference loader failed", details=str(exc))
        summary = _summarize(collector.checks)
        return ValidationRunResult(
            tool=tool,
            version=VALIDATION_VERSION,
            generated_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            inputs=inputs,
            checks=collector.checks,
            summary=summary,
            metrics=metrics,
        )

    metrics["row_loss"]["attendance"] = att_m.drop_reasons
    metrics["row_loss"]["appointments"] = appt_m.drop_reasons
    metrics["row_loss"]["blockouts"] = blk_m.drop_reasons

    # ------------------------------------------------------------------
    # Mirror/reference drift checks
    # ------------------------------------------------------------------
    att_mismatch, att_details = _compare_record_sets(
        _source_records_att(att_m.df),
        _source_records_att(att_r.df),
        ["date", "dayOfWeek", "startMinute", "endMinute", "scheduledHours"],
    )
    if att_mismatch == 0:
        collector.pass_("MIRROR-REF-ATTENDANCE", "Attendance mirror/reference results match")
    else:
        collector.fail(
            "MIRROR-REF-ATTENDANCE",
            f"Attendance mirror/reference mismatch ({att_mismatch} records)",
            details=att_details,
        )

    appt_mismatch, appt_details = _compare_record_sets(
        _source_records_appt(appt_m.df, include_membership=False),
        _source_records_appt(appt_r.df, include_membership=False),
        ["date", "dayOfWeek", "startMinute", "endMinute", "durationMin"],
    )
    if appt_mismatch == 0:
        collector.pass_("MIRROR-REF-APPOINTMENTS", "Appointments mirror/reference results match")
    else:
        collector.fail(
            "MIRROR-REF-APPOINTMENTS",
            f"Appointments mirror/reference mismatch ({appt_mismatch} records)",
            details=appt_details,
        )

    blk_mismatch, blk_details = _compare_record_sets(
        _source_records_blk(blk_m.df),
        _source_records_blk(blk_r.df),
        ["date", "dayOfWeek", "startMinute", "endMinute", "blockHours", "blockType"],
    )
    if blk_mismatch == 0:
        collector.pass_("MIRROR-REF-BLOCKOUTS", "Blockouts mirror/reference results match")
    else:
        collector.fail(
            "MIRROR-REF-BLOCKOUTS",
            f"Blockouts mirror/reference mismatch ({blk_mismatch} records)",
            details=blk_details,
        )

    # ------------------------------------------------------------------
    # Source vs embedded counts + field sets
    # ------------------------------------------------------------------
    src_att = _source_records_att(att_m.df)
    src_appt = _source_records_appt(appt_m.df, include_membership=False)
    src_blk = _source_records_blk(blk_m.df)

    metrics["record_counts"] = {
        "attendance": {"source": len(src_att), "embedded": len(emb_att)},
        "appointments": {"source": len(src_appt), "embedded": len(emb_appt)},
        "blockouts": {"source": len(src_blk), "embedded": len(emb_blk)},
    }

    for label, src_count, emb_count in [
        ("ATTENDANCE", len(src_att), len(emb_att)),
        ("APPOINTMENTS", len(src_appt), len(emb_appt)),
        ("BLOCKOUTS", len(src_blk), len(emb_blk)),
    ]:
        if src_count == emb_count:
            collector.pass_(f"COUNT-{label}", f"{label} source and embedded counts match ({src_count})")
        else:
            collector.fail(
                f"COUNT-{label}",
                f"{label} count mismatch: source={src_count}, embedded={emb_count}",
            )

    comparisons = [
        (
            "FIELDS-ATTENDANCE",
            src_att,
            emb_att,
            ["date", "dayOfWeek", "startMinute", "endMinute", "scheduledHours"],
        ),
        (
            "FIELDS-APPOINTMENTS",
            src_appt,
            emb_appt,
            ["date", "dayOfWeek", "startMinute", "endMinute", "durationMin"],
        ),
        (
            "FIELDS-BLOCKOUTS",
            src_blk,
            emb_blk,
            ["date", "dayOfWeek", "startMinute", "endMinute", "blockHours", "blockType"],
        ),
    ]
    for check_id, src_records, emb_records, fields in comparisons:
        mismatch_count, details = _compare_record_sets(src_records, emb_records, fields)
        if mismatch_count == 0:
            collector.pass_(check_id, f"{check_id} match on canonical field set")
        else:
            collector.fail(check_id, f"{check_id} mismatch ({mismatch_count} records)", details=details)

    # ------------------------------------------------------------------
    # Row-loss policy checks
    # ------------------------------------------------------------------
    att_drop = att_m.drop_reasons
    appt_drop = appt_m.drop_reasons
    blk_drop = blk_m.drop_reasons

    # Critical defaults
    if att_drop.get("schedule_parse_fail", 0) == 0:
        collector.pass_("ROWLOSS-ATT-SCHEDULE-PARSE", "Attendance schedule_parse_fail == 0")
    else:
        collector.fail(
            "ROWLOSS-ATT-SCHEDULE-PARSE",
            f"Attendance schedule_parse_fail = {att_drop.get('schedule_parse_fail', 0)}",
        )

    if appt_drop.get("datetime_parse_fail", 0) == 0:
        collector.pass_("ROWLOSS-APPT-DATETIME-PARSE", "Appointments datetime_parse_fail == 0")
    else:
        collector.fail(
            "ROWLOSS-APPT-DATETIME-PARSE",
            f"Appointments datetime_parse_fail = {appt_drop.get('datetime_parse_fail', 0)}",
        )

    if blk_drop.get("time_parse_fail", 0) == 0:
        collector.pass_("ROWLOSS-BLK-TIME-PARSE", "Blockouts time_parse_fail == 0")
    else:
        collector.fail(
            "ROWLOSS-BLK-TIME-PARSE",
            f"Blockouts time_parse_fail = {blk_drop.get('time_parse_fail', 0)}",
        )

    if appt_drop.get("negative_duration", 0) == 0:
        collector.pass_("ROWLOSS-APPT-NEGATIVE", "Appointments negative_duration == 0")
    else:
        collector.fail(
            "ROWLOSS-APPT-NEGATIVE",
            f"Appointments negative_duration = {appt_drop.get('negative_duration', 0)}",
        )

    if blk_drop.get("blank_or_null_block_type", 0) == 0:
        collector.pass_("ROWLOSS-BLK-BLANK-TYPE", "Blockouts blank_or_null_block_type == 0")
    else:
        collector.fail(
            "ROWLOSS-BLK-BLANK-TYPE",
            f"Blockouts blank_or_null_block_type = {blk_drop.get('blank_or_null_block_type', 0)}",
        )

    # Informational/warning drop categories
    if appt_drop.get("zero_duration", 0) > 0:
        collector.warn(
            "ROWLOSS-APPT-ZERO-DURATION",
            f"Appointments zero_duration filtered: {appt_drop.get('zero_duration', 0)}",
        )
    else:
        collector.pass_("ROWLOSS-APPT-ZERO-DURATION", "Appointments zero_duration == 0", severity="info")

    if att_drop.get("missing_date_or_schedule", 0) > 0:
        collector.warn(
            "ROWLOSS-ATT-MISSING-DATE-SCHEDULE",
            f"Attendance missing_date_or_schedule = {att_drop.get('missing_date_or_schedule', 0)}",
        )
    else:
        collector.pass_("ROWLOSS-ATT-MISSING-DATE-SCHEDULE", "Attendance missing_date_or_schedule == 0", severity="info")

    if blk_drop.get("missing_required_time", 0) > 0:
        collector.warn(
            "ROWLOSS-BLK-MISSING-TIME",
            f"Blockouts missing_required_time = {blk_drop.get('missing_required_time', 0)}",
        )
    else:
        collector.pass_("ROWLOSS-BLK-MISSING-TIME", "Blockouts missing_required_time == 0", severity="info")

    collector.pass_(
        "ROWLOSS-BLK-WRAP-APPLIED",
        f"Blockouts am_pm_wrap_applied = {blk_drop.get('am_pm_wrap_applied', 0)}",
        severity="info",
    )

    # ------------------------------------------------------------------
    # META + block type integrity
    # ------------------------------------------------------------------
    meta_required = ["reportDate", "dataStartDate", "dataEndDate", "blockTypes", "hourStart", "hourEnd", "periods"]
    for key in meta_required:
        if key in meta:
            collector.pass_(f"META-{key.upper()}", f"META.{key} present", severity="info")
        else:
            collector.fail(f"META-{key.upper()}", f"META.{key} missing")

    if meta.get("hourStart") == HOUR_START:
        collector.pass_("META-HOUR-START", f"META.hourStart == {HOUR_START}")
    else:
        collector.fail("META-HOUR-START", f"META.hourStart is {meta.get('hourStart')} (expected {HOUR_START})")

    if meta.get("hourEnd") == HOUR_END:
        collector.pass_("META-HOUR-END", f"META.hourEnd == {HOUR_END}")
    else:
        collector.fail("META-HOUR-END", f"META.hourEnd is {meta.get('hourEnd')} (expected {HOUR_END})")

    actual_block_types = sorted(
        {
            str(r.get("blockType")).strip()
            for r in emb_blk
            if r.get("blockType") is not None and str(r.get("blockType")).strip() != ""
        }
    )
    meta_block_types = sorted(str(v).strip() for v in meta.get("blockTypes", []))

    if actual_block_types == meta_block_types:
        collector.pass_("META-BLOCK-TYPES", "META.blockTypes matches blockout data")
    else:
        data_only = sorted(set(actual_block_types) - set(meta_block_types))
        meta_only = sorted(set(meta_block_types) - set(actual_block_types))
        if data_only:
            collector.fail(
                "META-BLOCK-TYPES",
                "Blockout data contains types missing from META.blockTypes",
                details=f"data_only={data_only}",
            )
        elif meta_only:
            collector.warn(
                "META-BLOCK-TYPES",
                "META.blockTypes contains values absent from current data",
                details=f"meta_only={meta_only}",
            )

    embedded_blank_block = sum(
        1
        for r in emb_blk
        if r.get("blockType") is None or str(r.get("blockType")).strip() == ""
    )
    if embedded_blank_block == 0:
        collector.pass_("BLOCKOUT-BLANK-TYPE-EMBEDDED", "No embedded blockouts with blank/null blockType")
    else:
        collector.fail(
            "BLOCKOUT-BLANK-TYPE-EMBEDDED",
            f"Embedded blockouts with blank/null blockType: {embedded_blank_block}",
        )

    # ------------------------------------------------------------------
    # dayOfWeek + numeric quality
    # ------------------------------------------------------------------
    dow_errors = 0
    for records in [emb_att, emb_appt, emb_blk]:
        for r in records:
            actual = datetime.strptime(r["date"], "%Y-%m-%d").weekday()
            if int(r["dayOfWeek"]) != actual:
                dow_errors += 1
    if dow_errors == 0:
        collector.pass_("CALENDAR-DAYOFWEEK", "All records have correct dayOfWeek")
    else:
        collector.fail("CALENDAR-DAYOFWEEK", f"dayOfWeek mismatches found: {dow_errors}")

    def _check_nan(records: List[Dict[str, Any]], fields: Sequence[str], check_id: str) -> None:
        for rec in records:
            for field in fields:
                value = rec.get(field)
                if value is None:
                    collector.fail(check_id, f"Null value detected in {field}")
                    return
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    collector.fail(check_id, f"NaN/Inf detected in {field}")
                    return
        collector.pass_(check_id, "Numeric fields are finite and non-null")

    _check_nan(emb_att, ["startMinute", "endMinute", "dayOfWeek", "scheduledHours"], "NUMERIC-ATTENDANCE")
    _check_nan(emb_appt, ["startMinute", "endMinute", "dayOfWeek", "durationMin"], "NUMERIC-APPOINTMENTS")
    _check_nan(emb_blk, ["startMinute", "endMinute", "dayOfWeek", "blockHours"], "NUMERIC-BLOCKOUTS")

    # ------------------------------------------------------------------
    # Period boundaries + cross-section invariants
    # ------------------------------------------------------------------
    periods = meta.get("periods", [])
    period_ok, period_errors = _check_period_structure(periods)
    if period_ok:
        collector.pass_("PERIOD-STRUCTURE", "Periods are 4x7-day contiguous with one current period")
    else:
        collector.fail("PERIOD-STRUCTURE", "Period structure invalid", details="\n".join(period_errors))

    for period in periods:
        label = period["label"]
        p_metrics = _compute_period_metrics(emb_att, emb_appt, emb_blk, period["start"], period["end"])

        raw = p_metrics["raw"]
        grid = p_metrics["grid"]

        metric_row = {
            "label": label,
            "start": period["start"],
            "end": period["end"],
            "attendance": p_metrics["attendance_count"],
            "appointments": p_metrics["appointment_count"],
            "scheduled_hours": round(raw["scheduled"], 3),
            "appointment_hours": round(raw["appointment"], 3),
            "block_hours": round(raw["blocked"], 3),
            "net_available_hours": round(raw["net"], 3),
            "scorecard_util": round(raw["utilization"], 3),
            "grid_util": round(grid["utilization"], 3),
            "delta_pp": round(raw["utilization"] - grid["utilization"], 3),
        }
        metrics["period_metrics"].append(metric_row)

        for metric_name in ["scheduled", "blocked", "appointment", "net"]:
            diff = abs(raw[metric_name] - grid[metric_name])
            cid = f"INVARIANT-{label}-RAW-GRID-{metric_name.upper()}"
            if diff <= HOURS_TOLERANCE:
                collector.pass_(cid, f"{label}: raw vs grid {metric_name} match (diff={diff:.3f}h)")
            else:
                collector.fail(
                    cid,
                    f"{label}: raw vs grid {metric_name} mismatch (diff={diff:.3f}h)",
                    details=f"raw={raw[metric_name]:.3f}, grid={grid[metric_name]:.3f}",
                )

        util_diff = abs(raw["utilization"] - grid["utilization"])
        util_cid = f"INVARIANT-{label}-UTILIZATION"
        if util_diff <= UTILIZATION_TOLERANCE_PP:
            collector.pass_(util_cid, f"{label}: utilization diff={util_diff:.3f}pp")
        else:
            collector.fail(
                util_cid,
                f"{label}: utilization mismatch diff={util_diff:.3f}pp",
                details=f"raw={raw['utilization']:.3f}, grid={grid['utilization']:.3f}",
            )

        weekday = p_metrics["weekday"]
        weekend = p_metrics["weekend"]
        total_sched = weekday["scheduled"] + weekend["scheduled"]
        total_appt = weekday["appointment"] + weekend["appointment"]
        if abs(total_sched - raw["scheduled"]) <= HOURS_TOLERANCE and abs(total_appt - raw["appointment"]) <= HOURS_TOLERANCE:
            collector.pass_(f"INVARIANT-{label}-WEEKPART", f"{label}: weekday+weekend totals match period totals")
        else:
            collector.fail(
                f"INVARIANT-{label}-WEEKPART",
                f"{label}: weekday/weekend aggregate mismatch",
                details=(
                    f"weekday+weekend scheduled={total_sched:.3f}, raw={raw['scheduled']:.3f}; "
                    f"weekday+weekend appt={total_appt:.3f}, raw={raw['appointment']:.3f}"
                ),
            )

    # ------------------------------------------------------------------
    # Membership validation
    # ------------------------------------------------------------------
    report_has_is_member = any("isMember" in r for r in emb_appt)

    if membership_path:
        try:
            membership = load_membership(membership_path)
            metrics["row_loss"]["membership"] = membership.drop_reasons
            collector.pass_("MEMBERSHIP-LOAD", "Membership file loaded", severity="info")
        except Exception as exc:
            collector.fail("MEMBERSHIP-LOAD", "Failed to load membership file", details=str(exc))
            membership = None

        if membership is not None:
            if not report_has_is_member:
                collector.fail(
                    "MEMBERSHIP-EMBEDDED-FIELD",
                    "Membership file provided but embedded appointments have no isMember field",
                )
            else:
                try:
                    appt_with_membership = appt_m.df.copy()
                    appt_with_membership["isMember"] = compute_membership_flags(appt_with_membership, membership.lookup)
                    src_mem = _source_records_appt(appt_with_membership, include_membership=True)
                    emb_mem = []
                    for r in emb_appt:
                        rec = dict(r)
                        rec["isMember"] = bool(rec.get("isMember", False))
                        emb_mem.append(rec)

                    mismatch_count, details = _compare_record_sets(
                        src_mem,
                        emb_mem,
                        ["date", "dayOfWeek", "startMinute", "endMinute", "durationMin", "isMember"],
                    )
                    if mismatch_count == 0:
                        collector.pass_("MEMBERSHIP-MATCH", "Embedded membership tagging matches independent recomputation")
                    else:
                        collector.fail(
                            "MEMBERSHIP-MATCH",
                            f"Membership mismatch count: {mismatch_count}",
                            details=details,
                        )
                except Exception as exc:
                    collector.fail("MEMBERSHIP-MATCH", "Membership validation failed", details=str(exc))

            # Membership ledger quality
            if membership.drop_reasons.get("start_date_parse_fail", 0) > 0:
                collector.warn(
                    "MEMBERSHIP-STARTDATE-PARSE",
                    f"Membership start_date_parse_fail = {membership.drop_reasons.get('start_date_parse_fail', 0)}",
                )
            else:
                collector.pass_("MEMBERSHIP-STARTDATE-PARSE", "Membership start_date_parse_fail == 0", severity="info")

    else:
        if report_has_is_member:
            collector.warn(
                "MEMBERSHIP-UNVERIFIED",
                "Report includes isMember fields but no membership file was provided for validation",
            )

    # ------------------------------------------------------------------
    # Additional full-profile checks
    # ------------------------------------------------------------------
    if profile == "full":
        # Coverage gaps
        if periods:
            window_start = datetime.strptime(periods[0]["start"], "%Y-%m-%d")
            window_end = datetime.strptime(periods[-1]["end"], "%Y-%m-%d")
            present_dates = set(r["date"] for r in emb_att) | set(r["date"] for r in emb_appt)

            gaps = []
            d = window_start
            while d <= window_end:
                ds = d.strftime("%Y-%m-%d")
                if ds not in present_dates:
                    gaps.append(ds)
                d += timedelta(days=1)

            if not gaps:
                collector.pass_("COVERAGE-GAPS", "No missing dates in period window")
            else:
                collector.warn("COVERAGE-GAPS", f"Missing dates in period window: {len(gaps)}", details=str(gaps[:20]))

        # Duplicate patterns (warning only)
        def _dupe_count(records: List[Dict[str, Any]], fields: Sequence[str]) -> int:
            tuples = [tuple(_normalize_scalar(r.get(f)) for f in fields) for r in records]
            return len(tuples) - len(set(tuples))

        att_dupes = _dupe_count(emb_att, ["date", "startMinute", "endMinute", "scheduledHours"])
        appt_dupes = _dupe_count(emb_appt, ["date", "startMinute", "endMinute", "durationMin"])
        blk_dupes = _dupe_count(emb_blk, ["date", "startMinute", "endMinute", "blockType"])

        if att_dupes:
            collector.warn("DUPES-ATTENDANCE", f"Duplicate attendance pattern count: {att_dupes}")
        else:
            collector.pass_("DUPES-ATTENDANCE", "No duplicate attendance patterns", severity="info")

        if appt_dupes:
            collector.warn("DUPES-APPOINTMENTS", f"Duplicate appointment pattern count: {appt_dupes}")
        else:
            collector.pass_("DUPES-APPOINTMENTS", "No duplicate appointment patterns", severity="info")

        if blk_dupes:
            collector.warn("DUPES-BLOCKOUTS", f"Duplicate blockout pattern count: {blk_dupes}")
        else:
            collector.pass_("DUPES-BLOCKOUTS", "No duplicate blockout patterns", severity="info")

        # HTML structure checks
        required_elements = [
            'id="scorecard"',
            'id="heatmapContainer"',
            'id="dailyTableContainer"',
            'id="weekendComparisonContainer"',
            'id="tooltip"',
            'id="filterBody"',
            'id="dateRangeText"',
        ]
        required_js = ["function computeMetrics", "function renderHeatmap", "function minsInHour"]

        missing = [pat for pat in required_elements if pat not in html_content]
        missing += [pat for pat in required_js if pat not in html_content]
        if not missing:
            collector.pass_("HTML-STRUCTURE", "Required HTML/JS structure markers are present")
        else:
            collector.warn("HTML-STRUCTURE", "Missing expected HTML/JS structure markers", details=str(missing))

    summary = _summarize(collector.checks)
    return ValidationRunResult(
        tool=tool,
        version=VALIDATION_VERSION,
        generated_at=datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        inputs=inputs,
        checks=collector.checks,
        summary=summary,
        metrics=metrics,
    )


def write_json_report(result: ValidationRunResult, path: str) -> None:
    payload = result.to_dict()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def render_human_report(result: ValidationRunResult, *, profile: str, quiet: bool = False) -> str:
    lines: List[str] = []

    header = " QUICK CHECK — Streamlit Report Generator " if result.tool == "quick_check" else " ADVERSARIAL VALIDATION — Streamlit Report Generator "
    lines.append("=" * 70)
    lines.append(header)
    lines.append("=" * 70)
    lines.append("")

    if not quiet:
        lines.append("Inputs:")
        for key, value in result.inputs.items():
            lines.append(f"  {key:12s} {value}")
        lines.append("")

        counts = result.metrics.get("record_counts", {})
        if counts:
            lines.append("Record Counts (source vs embedded):")
            for key in ["attendance", "appointments", "blockouts"]:
                if key in counts:
                    src = counts[key]["source"]
                    emb = counts[key]["embedded"]
                    marker = "OK" if src == emb else "MISMATCH"
                    lines.append(f"  {key:12s} source={src:5d} embedded={emb:5d}  {marker}")
            lines.append("")

        row_loss = result.metrics.get("row_loss", {})
        if row_loss:
            lines.append("Row-Loss Ledger:")
            for source, reasons in row_loss.items():
                pairs = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
                lines.append(f"  {source:12s} {pairs}")
            lines.append("")

        period_metrics = result.metrics.get("period_metrics", [])
        if period_metrics:
            lines.append("Period Utilization (scorecard vs grid):")
            for row in period_metrics:
                lines.append(
                    f"  {row['label']:16s} util={row['scorecard_util']:.1f}% grid={row['grid_util']:.1f}% "
                    f"delta={row['delta_pp']:+.1f}pp"
                )
            lines.append("")

        lines.append("Checks:")
        for check in result.checks:
            lines.append(f"  [{check.status}] {check.id}: {check.message}")
            if check.details:
                for detail_line in check.details.split("\n")[:6]:
                    lines.append(f"      {detail_line}")
        lines.append("")

    lines.append("Summary:")
    lines.append(
        f"  {result.summary.pass_count} passed, {result.summary.fail_count} failed, "
        f"{result.summary.warn_count} warnings"
    )
    lines.append(f"  critical_failures={result.summary.critical_fail_count}")
    lines.append("  exit_policy: fail only on critical failures")

    if result.summary.exit_code == 0:
        lines.append("  RESULT: PASS (no critical failures)")
    else:
        lines.append("  RESULT: FAIL (critical failures present)")

    return "\n".join(lines)
