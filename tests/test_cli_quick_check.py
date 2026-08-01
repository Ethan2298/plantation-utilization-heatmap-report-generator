from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.fixture_factory import create_case


def test_quick_check_cli_back_compat_and_json_output(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="baseline")
    json_path = tmp_path / "quick.json"

    cmd = [
        sys.executable,
        "scripts/quick_check.py",
        case["html"],
        case["attendance"],
        case["appointments"],
        case["blockout"],
        "--json-out",
        str(json_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["tool"] == "quick_check"
    assert payload["summary"]["exit_code"] == 0


def test_quick_check_cli_quiet_mode(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="baseline")
    cmd = [
        sys.executable,
        "scripts/quick_check.py",
        case["html"],
        case["attendance"],
        case["appointments"],
        case["blockout"],
        "--quiet",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    assert "Summary:" in proc.stdout


def test_quick_check_cli_critical_fail_exit_code(tmp_path: Path) -> None:
    case = create_case(tmp_path, scenario="attendance_parse_drift")
    cmd = [
        sys.executable,
        "scripts/quick_check.py",
        case["html"],
        case["attendance"],
        case["appointments"],
        case["blockout"],
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)

    assert proc.returncode == 1
