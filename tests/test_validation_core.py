from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from hypothesis import given, strategies as st

from scripts.loaders import load_blockouts_mirror
from scripts.validation_core import (
    _check_period_structure,
    _mins_in_hour,
    run_validation,
    write_json_report,
)
from tests.fixture_factory import create_case


def _check_status(result, check_id: str) -> str:
    for check in result.checks:
        if check.id == check_id:
            return check.status
    raise AssertionError(f"missing check id: {check_id}")


def _run_case(case: dict[str, str], *, tool: str = "validate_report", profile: str = "full", membership: str | None = None):
    return run_validation(
        tool=tool,
        profile=profile,
        html_path=case["html"],
        attendance_path=case["attendance"],
        appointments_path=case["appointments"],
        blockout_path=case["blockout"],
        membership_path=membership,
    )


def test_baseline_valid_data_passes(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="baseline")
    result = _run_case(case)
    assert result.summary.exit_code == 0
    assert result.summary.critical_fail_count == 0


def test_attendance_parse_drift_is_critical_failure(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="attendance_parse_drift")
    result = _run_case(case)
    assert result.summary.exit_code == 1
    assert _check_status(result, "ROWLOSS-ATT-SCHEDULE-PARSE") == "FAIL"


def test_blockout_parse_drift_is_critical_failure(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="blockout_parse_drift")
    result = _run_case(case)
    assert result.summary.exit_code == 1
    assert _check_status(result, "ROWLOSS-BLK-TIME-PARSE") == "FAIL"


def test_negative_duration_is_critical_failure(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="negative_duration")
    result = _run_case(case)
    assert result.summary.exit_code == 1
    assert _check_status(result, "ROWLOSS-APPT-NEGATIVE") == "FAIL"


def test_zero_duration_is_warning_not_failure(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="zero_duration")
    result = _run_case(case)
    assert result.summary.exit_code == 0
    assert _check_status(result, "ROWLOSS-APPT-ZERO-DURATION") == "WARN"


def test_blank_block_type_is_critical_failure(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="blank_block_type")
    result = _run_case(case)
    assert result.summary.exit_code == 1
    assert _check_status(result, "ROWLOSS-BLK-BLANK-TYPE") == "FAIL"


def test_membership_end_date_boundary_mismatch_detected(tmp_path: Path) -> None:
    case = create_case(
        tmp_path,
        scenario="membership_enddate_mismatch",
        with_membership_file=True,
        embed_membership_mode="buggy",
    )
    result = _run_case(case, membership=case["membership"])
    assert result.summary.exit_code == 1
    assert _check_status(result, "MEMBERSHIP-MATCH") == "FAIL"


def test_guest_code_normalization_mismatch_detected(tmp_path: Path) -> None:
    case = create_case(
        tmp_path,
        scenario="guest_code_normalization_mismatch",
        with_membership_file=True,
        embed_membership_mode="buggy",
    )
    result = _run_case(case, membership=case["membership"])
    assert result.summary.exit_code == 1
    assert _check_status(result, "MEMBERSHIP-MATCH") == "FAIL"


def test_scorecard_grid_invariant_break_is_detected(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="scorecard_invariant_break")
    result = _run_case(case)
    assert result.summary.exit_code == 1
    failed_ids = {c.id for c in result.checks if c.status == "FAIL"}
    assert any("INVARIANT-" in cid and "UTILIZATION" in cid for cid in failed_ids)


def test_json_report_schema_and_stable_check_ids(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="baseline")
    result = _run_case(case, tool="quick_check", profile="quick")
    out_path = tmp_path / "report.json"
    write_json_report(result, str(out_path))

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {"checks", "generated_at", "inputs", "metrics", "summary", "tool", "version"}
    assert set(payload["summary"].keys()) == {
        "critical_fail_count",
        "exit_code",
        "fail_count",
        "pass_count",
        "warn_count",
    }
    assert all(set(c.keys()) == {"details", "id", "message", "severity", "status"} for c in payload["checks"])

    ids = [c["id"] for c in payload["checks"]]
    assert ids == [c.id for c in result.checks]


@given(
    start=st.integers(min_value=-120, max_value=1800),
    end=st.integers(min_value=-120, max_value=1800),
    hour=st.integers(min_value=0, max_value=23),
)
def test_mins_in_hour_non_negative_and_bounded(start: int, end: int, hour: int) -> None:
    lo, hi = (start, end) if start <= end else (end, start)
    mins = _mins_in_hour(lo, hi, hour * 60)
    assert mins >= 0
    assert mins <= 60
    assert mins <= (hi - lo)


@given(
    bad_times=st.lists(
        st.text(alphabet=st.characters(whitelist_categories=["Lu", "Ll"]), min_size=1, max_size=8),
        min_size=1,
        max_size=15,
    )
)
def test_blockout_row_loss_reasons_are_accounted_for(bad_times: list[str]) -> None:
    rows = []
    for idx, token in enumerate(bad_times):
        rows.append(
            {
                "Date": f"2026-02-{(idx % 27) + 1:02d}",
                "StartTime": token,
                "EndTime": token,
                "BlockOutTimeType": "Lunch",
                "Block Out Time (in hours)": 1.0,
            }
        )

    with tempfile.TemporaryDirectory() as td:
        csv_path = Path(td) / "bad_blockouts.csv"
        pd.DataFrame(rows).to_csv(csv_path, index=False)

        result = load_blockouts_mirror(str(csv_path))
        drops = result.drop_reasons

        assert result.input_rows == result.final_rows + drops["missing_required_time"] + drops["time_parse_fail"]


def _valid_periods(start_date: datetime) -> list[dict[str, str]]:
    periods = []
    p_start = start_date
    for i in range(4):
        p_end = p_start + timedelta(days=6)
        periods.append(
            {
                "label": f"P{i+1}",
                "start": p_start.strftime("%Y-%m-%d"),
                "end": p_end.strftime("%Y-%m-%d"),
                "isCurrent": i == 3,
            }
        )
        p_start = p_end + timedelta(days=1)
    return periods


@given(day_offset=st.integers(min_value=0, max_value=2000))
def test_period_structure_detection(day_offset: int) -> None:
    base = datetime(2020, 1, 1) + timedelta(days=day_offset)
    periods = _valid_periods(base)
    ok, errors = _check_period_structure(periods)
    assert ok
    assert not errors

    bad = [dict(p) for p in periods]
    bad[2]["start"] = (datetime.strptime(bad[1]["end"], "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    ok_bad, errors_bad = _check_period_structure(bad)
    assert not ok_bad
    assert errors_bad
