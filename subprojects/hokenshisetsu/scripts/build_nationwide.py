"""Generalized monthly builder for nationwide Japanese insured medical institution dataset.

Usage:
  python3 build_nationwide.py --snapshot 202611
  python3 build_nationwide.py --snapshot 202701
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MY_PACKAGES = REPO_ROOT / "my_packages"
if str(MY_PACKAGES) not in sys.path:
    sys.path.append(str(MY_PACKAGES))

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import build_nationwide_202611 as bld  # noqa: E402


def resolve_utf_ken_all(raw_root: Path, snapshot: str) -> Path:
    direct = raw_root / snapshot / "utf_ken_all.zip"
    if direct.exists():
        return direct
    shared = raw_root / "shared" / "utf_ken_all.zip"
    if shared.exists():
        return shared
    candidates = sorted(raw_root.glob("*/utf_ken_all.zip"), reverse=True)
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"Could not locate utf_ken_all.zip under {raw_root}")


def run_for_snapshot(snapshot: str) -> None:
    if not re.fullmatch(r"\d{6}", snapshot):
        raise ValueError(f"--snapshot must be YYYYMM, got {snapshot!r}")

    yyyy, mm = snapshot[:4], snapshot[4:6]
    if not (2000 <= int(yyyy) <= 2100 and 1 <= int(mm) <= 12):
        raise ValueError(f"Invalid YYYYMM calendar snapshot: {snapshot!r}")
    raw_root = REPO_ROOT / "data" / "raw" / "hokenshisetsu"
    raw_base = raw_root / snapshot
    if not raw_base.exists():
        raise FileNotFoundError(f"Missing snapshot raw directory: {raw_base}")

    sub_dir = REPO_ROOT / "subprojects" / "hokenshisetsu"
    bld.RAW_BASE = raw_base
    bld.UTF_KEN_ALL_ZIP = resolve_utf_ken_all(raw_root, snapshot)
    bld.SNAPSHOT_DATE = f"{yyyy}-{mm}-01"
    bld.PROCESSED_CSV = sub_dir / "data" / "processed" / f"hokenshisetsu_nationwide_{snapshot}.csv"
    bld.PREF_SUMMARY_CSV = sub_dir / "output" / f"prefecture_summary_{snapshot}.csv"
    bld.MUNI_SUMMARY_CSV = sub_dir / "output" / f"municipality_summary_{snapshot}.csv"
    bld.QA_REPORT_JSON = sub_dir / "output" / f"build_qa_report_{snapshot}.json"

    bld.main()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build nationwide hokenshisetsu dataset for a YYYYMM snapshot.")
    parser.add_argument("--snapshot", required=True, help="Target snapshot YYYYMM (e.g. 202611, 202701)")
    args = parser.parse_args()
    run_for_snapshot(args.snapshot)


if __name__ == "__main__":
    main()
