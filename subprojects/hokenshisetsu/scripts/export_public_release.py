#!/usr/bin/env python3
"""Export a sanitized public release bundle for a verified snapshot.

Drops personal-name columns (owner_name, manager_name) from the nationwide CSV,
compresses it to .csv.gz, copies summary CSVs and QA JSON, and writes SHA256SUMS.txt
under release/<snapshot>/.
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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

    return {
        "snapshot": snapshot,
        "release_dir": str(release_dir.relative_to(REPO_ROOT)),
        "rows": int(len(public_df)),
        "columns": int(len(public_df.columns)),
        "dropped_columns": drop_cols,
        "assets": {p.name: p.stat().st_size for p in copied_files + [checksum_path]},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized public release assets.")
    parser.add_argument("--snapshot", required=True, help="Snapshot YYYYMM (e.g., 202611)")
    args = parser.parse_args()
    summary = export_public_release(args.snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
