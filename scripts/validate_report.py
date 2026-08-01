#!/usr/bin/env python3
"""Exhaustive adversarial validation for Streamlit-generated utilization reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation_core import render_human_report, run_validation, write_json_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exhaustive validation for Streamlit-generated utilization reports",
    )
    parser.add_argument("html_report", help="Path to generated HTML report")
    parser.add_argument("attendance", help="Path to Attendance export (.csv/.xls/.xlsx)")
    parser.add_argument("appointments", help="Path to Appointments export (.csv/.xls/.xlsx)")
    parser.add_argument("blockout", help="Path to Block Out Time export (.csv/.xls/.xlsx/.html)")
    parser.add_argument("--membership", help="Optional Membership export path", default=None)
    parser.add_argument("--json-out", help="Optional output path for machine-readable JSON report", default=None)
    parser.add_argument("--quiet", action="store_true", help="Show summary-only output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    result = run_validation(
        tool="validate_report",
        profile="full",
        html_path=args.html_report,
        attendance_path=args.attendance,
        appointments_path=args.appointments,
        blockout_path=args.blockout,
        membership_path=args.membership,
    )

    print(render_human_report(result, profile="full", quiet=args.quiet))

    if args.json_out:
        write_json_report(result, args.json_out)
        if not args.quiet:
            print(f"\nJSON report written to: {args.json_out}")

    return result.summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
