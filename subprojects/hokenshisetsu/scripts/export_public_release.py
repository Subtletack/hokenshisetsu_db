#!/usr/bin/env python3
"""Export sanitized public release assets and Git-diffable prefecture CSVs.

1. release/<snapshot>/ :
   - hokenshisetsu_nationwide_<snapshot>_public.csv.gz (drops owner_name, manager_name)
   - prefecture_summary_<snapshot>.csv
   - municipality_summary_<snapshot>.csv
   - build_qa_report_<snapshot>.json
   - SHA256SUMS.txt

2. data/public/by_pref/<pref_code>_<pref_slug>.csv :
   - 47 prefecture CSVs sorted by medical_code_10digit for clean monthly Git diffs.
   - Drops owner_name, manager_name and volatile metadata columns
     (snapshot_date, source_file, source_sheet, seq_no).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
SUB_DIR = SCRIPT_DIR.parent
REPO_ROOT = SUB_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import download_raw as dr  # noqa: E402

DROPPED_PUBLIC_COLUMNS = ("owner_name", "manager_name")
GIT_DIFF_VOLATILE_COLUMNS = ("snapshot_date", "source_file", "source_sheet", "seq_no")

PREF_SLUGS: dict[str, str] = {
    "01": "hokkaido",
    "02": "aomori",
    "03": "iwate",
    "04": "miyagi",
    "05": "akita",
    "06": "yamagata",
    "07": "fukushima",
    "08": "ibaraki",
    "09": "tochigi",
    "10": "gunma",
    "11": "saitama",
    "12": "chiba",
    "13": "tokyo",
    "14": "kanagawa",
    "15": "niigata",
    "16": "toyama",
    "17": "ishikawa",
    "18": "fukui",
    "19": "yamanashi",
    "20": "nagano",
    "21": "gifu",
    "22": "shizuoka",
    "23": "aichi",
    "24": "mie",
    "25": "shiga",
    "26": "kyoto",
    "27": "osaka",
    "28": "hyogo",
    "29": "nara",
    "30": "wakayama",
    "31": "tottori",
    "32": "shimane",
    "33": "okayama",
    "34": "hiroshima",
    "35": "yamaguchi",
    "36": "tokushima",
    "37": "kagawa",
    "38": "ehime",
    "39": "kochi",
    "40": "fukuoka",
    "41": "saga",
    "42": "nagasaki",
    "43": "kumamoto",
    "44": "oita",
    "45": "miyazaki",
    "46": "kagoshima",
    "47": "okinawa",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def export_git_diff_by_pref(public_df: pd.DataFrame, snapshot: str) -> dict[str, object]:
    """Write 47 sorted, noise-free prefecture CSVs under data/public/by_pref/."""
    by_pref_dir = REPO_ROOT / "data" / "public" / "by_pref"
    by_pref_dir.mkdir(parents=True, exist_ok=True)

    volatile = [c for c in GIT_DIFF_VOLATILE_COLUMNS if c in public_df.columns]
    diff_df = public_df.drop(columns=volatile)
    for forbidden in DROPPED_PUBLIC_COLUMNS + GIT_DIFF_VOLATILE_COLUMNS:
        assert forbidden not in diff_df.columns, f"Forbidden diff column remained: {forbidden}"

    sort_cols = [c for c in ("medical_code_10digit", "record_source_type", "facility_name") if c in diff_df.columns]
    if sort_cols:
        diff_df = diff_df.sort_values(sort_cols, kind="mergesort").reset_index(drop=True)

    pref_codes = sorted(diff_df["pref_code"].unique())
    assert len(pref_codes) == 47, f"Expected 47 prefectures, got {len(pref_codes)}"

    total_rows = 0
    for pref_code in pref_codes:
        slug = PREF_SLUGS[pref_code]
        sub = diff_df.loc[diff_df["pref_code"] == pref_code]
        out_csv = by_pref_dir / f"{pref_code}_{slug}.csv"
        sub.to_csv(out_csv, index=False, encoding="utf-8", lineterminator="\n")
        total_rows += int(len(sub))

    (REPO_ROOT / "data" / "public" / "LATEST_SNAPSHOT.txt").write_text(
        f"{snapshot}\n", encoding="utf-8"
    )
    return {
        "by_pref_dir": str(by_pref_dir.relative_to(REPO_ROOT)),
        "prefecture_files": len(pref_codes),
        "rows": total_rows,
        "columns": int(len(diff_df.columns)),
        "dropped_volatile_columns": volatile,
    }


def export_public_release(snapshot: str) -> dict[str, object]:
    yyyy, mm = dr.validate_snapshot_str(snapshot)
    expected_date = f"{yyyy:04d}-{mm:02d}-01"

    processed_csv = SUB_DIR / "data" / "processed" / f"hokenshisetsu_nationwide_{snapshot}.csv"
    pref_csv = SUB_DIR / "output" / f"prefecture_summary_{snapshot}.csv"
    muni_csv = SUB_DIR / "output" / f"municipality_summary_{snapshot}.csv"
    qa_json = SUB_DIR / "output" / f"build_qa_report_{snapshot}.json"

    for p in (processed_csv, pref_csv, muni_csv, qa_json):
        if not p.exists():
            raise FileNotFoundError(f"Required verified artifact not found: {p}")

    qa = json.loads(qa_json.read_text(encoding="utf-8"))
    if qa.get("snapshot_date") != expected_date:
        raise ValueError(f"QA snapshot_date mismatch: {qa.get('snapshot_date')} != {expected_date}")

    df = pd.read_csv(processed_csv, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if not (df["snapshot_date"] == expected_date).all():
        raise ValueError(f"CSV snapshot_date mismatch: expected all {expected_date}")

    drop_cols = [c for c in DROPPED_PUBLIC_COLUMNS if c in df.columns]
    public_df = df.drop(columns=drop_cols)
    for forbidden in DROPPED_PUBLIC_COLUMNS:
        assert forbidden not in public_df.columns, f"Forbidden column remained: {forbidden}"

    release_dir = REPO_ROOT / "release" / snapshot
    release_dir.mkdir(parents=True, exist_ok=True)

    public_gz = release_dir / f"hokenshisetsu_nationwide_{snapshot}_public.csv.gz"
    public_df.to_csv(public_gz, index=False, encoding="utf-8-sig", compression="gzip")

    copied_files = [public_gz]
    for src in (pref_csv, muni_csv, qa_json):
        dst = release_dir / src.name
        shutil.copy2(src, dst)
        copied_files.append(dst)

    checksum_lines = [f"{sha256_file(p)}  {p.name}" for p in copied_files]
    checksum_path = release_dir / "SHA256SUMS.txt"
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    git_diff_summary = export_git_diff_by_pref(public_df, snapshot)

    return {
        "snapshot": snapshot,
        "release_dir": str(release_dir.relative_to(REPO_ROOT)),
        "rows": int(len(public_df)),
        "columns": int(len(public_df.columns)),
        "dropped_columns": drop_cols,
        "assets": {p.name: p.stat().st_size for p in copied_files + [checksum_path]},
        "git_diff_export": git_diff_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized public release assets and Git-diffable prefecture CSVs.")
    parser.add_argument("--snapshot", required=True, help="Snapshot YYYYMM (e.g., 202611)")
    args = parser.parse_args()
    summary = export_public_release(args.snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
