from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.fixture_factory import create_case


def test_validate_report_cli_back_compat_and_json_output(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="baseline")
    json_path = tmp_path / "validate.json"

    cmd = [
        sys.executable,
        "scripts/validate_report.py",
        case["html"],
        case["attendance"],
        case["appointments"],
        case["blockout"],
        "--json-out",
        str(json_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["tool"] == "validate_report"
    assert payload["summary"]["exit_code"] == 0


def test_validate_report_membership_mismatch_fails(tmp_path: Path) -> None:
    case = create_case(
        tmp_path,
        scenario="membership_enddate_mismatch",
        with_membership_file=True,
        embed_membership_mode="buggy",
    )

    cmd = [
        sys.executable,
        "scripts/validate_report.py",
        case["html"],
        case["attendance"],
        case["appointments"],
        case["blockout"],
        "--membership",
        case["membership"],
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert proc.returncode == 1


def test_validate_report_membership_correct_passes(tmp_path: Path) -> None:
    case = create_case(
        tmp_path,
        scenario="baseline",
        with_membership_file=True,
        embed_membership_mode="correct",
    )

    cmd = [
        sys.executable,
        "scripts/validate_report.py",
        case["html"],
        case["attendance"],
        case["appointments"],
        case["blockout"],
        "--membership",
        case["membership"],
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert proc.returncode == 0
