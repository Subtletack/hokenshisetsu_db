#!/usr/bin/env python3
"""Track same-facility insurance code transitions across monthly snapshots.

Matches facilities whose medical_code_10digit disappeared in `curr` against
facilities whose medical_code_10digit appeared in `curr` using exact
normalized (facility_name, address) 1-to-1 matching.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
SUB_DIR = SCRIPT_DIR.parent
REPO_ROOT = SUB_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import download_raw as dr  # noqa: E402

HISTORY_COLUMNS = [
    "prev_snapshot",
    "curr_snapshot",
    "old_medical_code_10digit",
    "new_medical_code_10digit",
    "pref_code",
    "pref_name",
    "facility_name",
    "address",
    "phone",
    "old_designated_date",
    "new_designated_date",
    "new_designation_reason",
    "match_rule",
]


def normalize_match_text(val: object) -> str:
    """Normalize NFKC, hyphens, and whitespace for exact name/address matching."""
    s = unicodedata.normalize("NFKC", "" if val is None else str(val))
    s = re.sub(r"[‐‑‒–—―ー−]", "-", s)
    s = re.sub(r"\s+", "", s)
    return s.strip()


def load_snapshot_df(snapshot: str, repo_root: Path = REPO_ROOT) -> pd.DataFrame:
    dr.validate_snapshot_str(snapshot)
    gz_path = repo_root / "release" / snapshot / f"hokenshisetsu_nationwide_{snapshot}_public.csv.gz"
    if gz_path.exists():
        return pd.read_csv(gz_path, dtype=str, keep_default_na=False, encoding="utf-8-sig", compression="gzip")
    proc_path = repo_root / "subprojects" / "hokenshisetsu" / "data" / "processed" / f"hokenshisetsu_nationwide_{snapshot}.csv"
    if proc_path.exists():
        return pd.read_csv(proc_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    raise FileNotFoundError(f"Snapshot data not found for {snapshot}: checked {gz_path} and {proc_path}")


def find_previous_snapshot(curr_snapshot: str, repo_root: Path = REPO_ROOT) -> str | None:
    release_root = repo_root / "release"
    if not release_root.exists():
        return None
    candidates: list[str] = []
    for p in release_root.iterdir():
        if p.is_dir() and re.fullmatch(r"\d{6}", p.name) and p.name < curr_snapshot:
            gz = p / f"hokenshisetsu_nationwide_{p.name}_public.csv.gz"
            if gz.exists():
                candidates.append(p.name)
    return max(candidates) if candidates else None


def detect_same_name_address_transitions(
    prev_df: pd.DataFrame,
    curr_df: pd.DataFrame,
    prev_snapshot: str,
    curr_snapshot: str,
) -> pd.DataFrame:
    prev_codes = set(prev_df["medical_code_10digit"])
    curr_codes = set(curr_df["medical_code_10digit"])

    removed = prev_df.loc[prev_df["medical_code_10digit"].isin(prev_codes - curr_codes)].copy()
    added = curr_df.loc[curr_df["medical_code_10digit"].isin(curr_codes - prev_codes)].copy()

    if removed.empty or added.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    removed["_key"] = [
        (normalize_match_text(n), normalize_match_text(a))
        for n, a in zip(removed["facility_name"], removed["address"])
    ]
    added["_key"] = [
        (normalize_match_text(n), normalize_match_text(a))
        for n, a in zip(added["facility_name"], added["address"])
    ]

    rem_counts = removed["_key"].value_counts()
    add_counts = added["_key"].value_counts()

    unique_rem_keys = {k for k, cnt in rem_counts.items() if cnt == 1 and k[0] and k[1]}
    unique_add_keys = {k for k, cnt in add_counts.items() if cnt == 1 and k[0] and k[1]}
    matched_keys = sorted(unique_rem_keys & unique_add_keys)

    if not matched_keys:
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    matched_set = set(matched_keys)
    rem_map = {
        k: r for k, r in zip(removed["_key"], removed.to_dict("records")) if k in matched_set
    }
    add_map = {
        k: r for k, r in zip(added["_key"], added.to_dict("records")) if k in matched_set
    }

    rows: list[dict[str, str]] = []
    for key in matched_keys:
        r_old = rem_map[key]
        r_new = add_map[key]
        rows.append(
            {
                "prev_snapshot": prev_snapshot,
                "curr_snapshot": curr_snapshot,
                "old_medical_code_10digit": str(r_old["medical_code_10digit"]),
                "new_medical_code_10digit": str(r_new["medical_code_10digit"]),
                "pref_code": str(r_new.get("pref_code", "")),
                "pref_name": str(r_new.get("pref_name", "")),
                "facility_name": str(r_new.get("facility_name", "")),
                "address": str(r_new.get("address", "")),
                "phone": str(r_new.get("phone", "")),
                "old_designated_date": str(r_old.get("designated_date", "")),
                "new_designated_date": str(r_new.get("designated_date", "")),
                "new_designation_reason": str(r_new.get("designation_reason", "")),
                "match_rule": "exact_norm_name_and_address",
            }
        )

    out_df = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
    return out_df.sort_values(
        ["pref_code", "new_medical_code_10digit", "old_medical_code_10digit"]
    ).reset_index(drop=True)


def run_transition_tracking(
    prev_snapshot: str,
    curr_snapshot: str,
    repo_root: Path = REPO_ROOT,
) -> dict[str, object]:
    prev_df = load_snapshot_df(prev_snapshot, repo_root=repo_root)
    curr_df = load_snapshot_df(curr_snapshot, repo_root=repo_root)

    trans_df = detect_same_name_address_transitions(prev_df, curr_df, prev_snapshot, curr_snapshot)

    trans_dir = repo_root / "data" / "public" / "transitions"
    trans_dir.mkdir(parents=True, exist_ok=True)

    monthly_csv = trans_dir / f"{curr_snapshot}_code_changes.csv"
    trans_df.to_csv(monthly_csv, index=False, encoding="utf-8", lineterminator="\n")

    history_csv = trans_dir / "facility_code_history.csv"
    if history_csv.exists():
        hist_df = pd.read_csv(history_csv, dtype=str, keep_default_na=False, encoding="utf-8")
        hist_df = hist_df.loc[
            ~((hist_df["prev_snapshot"] == prev_snapshot) & (hist_df["curr_snapshot"] == curr_snapshot))
        ]
        combined = pd.concat([hist_df, trans_df], ignore_index=True)
    else:
        combined = trans_df.copy()

    if not combined.empty:
        combined = combined.sort_values(
            ["curr_snapshot", "pref_code", "new_medical_code_10digit", "old_medical_code_10digit"]
        ).reset_index(drop=True)
    combined.to_csv(history_csv, index=False, encoding="utf-8", lineterminator="\n")

    return {
        "prev_snapshot": prev_snapshot,
        "curr_snapshot": curr_snapshot,
        "code_changes_detected": int(len(trans_df)),
        "monthly_csv": str(monthly_csv.relative_to(repo_root)),
        "history_csv": str(history_csv.relative_to(repo_root)),
        "history_total_rows": int(len(combined)),
    }


def run_self_test() -> None:
    prev = pd.DataFrame(
        [
            {
                "medical_code_10digit": "0113000010",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "テスト内科クリニック",
                "address": "東京都千代田区霞が関１−２−３",
                "phone": "03-0000-0001",
                "designated_date": "2020-04-01",
                "designation_reason": "新規",
            },
            {
                "medical_code_10digit": "0113000028",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "別名クリニック",
                "address": "東京都千代田区霞が関2-2-2",
                "phone": "03-0000-0002",
                "designated_date": "2020-04-01",
                "designation_reason": "新規",
            },
            {
                "medical_code_10digit": "0113000036",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "重複クリニック",
                "address": "東京都港区芝1-1-1",
                "phone": "03-0000-0003",
                "designated_date": "2020-04-01",
                "designation_reason": "新規",
            },
            {
                "medical_code_10digit": "0113000044",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "重複クリニック",
                "address": "東京都港区芝1-1-1",
                "phone": "03-0000-0004",
                "designated_date": "2020-04-01",
                "designation_reason": "新規",
            },
        ]
    )
    curr = pd.DataFrame(
        [
            {
                "medical_code_10digit": "0113999011",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "テスト内科クリニック",
                "address": "東京都千代田区霞が関1-2-3",
                "phone": "03-0000-0001",
                "designated_date": "2026-12-01",
                "designation_reason": "開設者変更",
            },
            {
                "medical_code_10digit": "0113999029",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "医療法人社団 別名クリニック",
                "address": "東京都千代田区霞が関2-2-2",
                "phone": "03-0000-0002",
                "designated_date": "2026-12-01",
                "designation_reason": "組織変更",
            },
            {
                "medical_code_10digit": "0113999037",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "重複クリニック",
                "address": "東京都港区芝1-1-1",
                "phone": "03-0000-0003",
                "designated_date": "2026-12-01",
                "designation_reason": "開設者変更",
            },
        ]
    )
    res = detect_same_name_address_transitions(prev, curr, "202611", "202612")
    assert len(res) == 1, f"Expected 1 exact match, got {len(res)}"
    assert res.iloc[0]["old_medical_code_10digit"] == "0113000010"
    assert res.iloc[0]["new_medical_code_10digit"] == "0113999011"

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for snap, df in (("202611", prev), ("202612", curr)):
            d = root / "release" / snap
            d.mkdir(parents=True)
            df.to_csv(d / f"hokenshisetsu_nationwide_{snap}_public.csv.gz", index=False, encoding="utf-8-sig", compression="gzip")
        summary = run_transition_tracking("202611", "202612", repo_root=root)
        assert summary["code_changes_detected"] == 1
        assert (root / "data" / "public" / "transitions" / "202612_code_changes.csv").exists()
        assert (root / "data" / "public" / "transitions" / "facility_code_history.csv").exists()


def main() -> None:
    parser = argparse.ArgumentParser(description="Track same-name/same-address code transitions across snapshots.")
    parser.add_argument("--prev-snapshot", help="Previous snapshot YYYYMM")
    parser.add_argument("--curr-snapshot", help="Current snapshot YYYYMM")
    parser.add_argument("--self-test", action="store_true", help="Run offline unit tests")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        print("TRANSITION_SELF_TEST_PASSED")
        return

    if not args.curr_snapshot:
        parser.error("--curr-snapshot is required unless --self-test is specified")
    prev = args.prev_snapshot or find_previous_snapshot(args.curr_snapshot)
    if not prev:
        print(json.dumps({"curr_snapshot": args.curr_snapshot, "skipped": "no_previous_snapshot"}, ensure_ascii=False))
        return
    summary = run_transition_tracking(prev, args.curr_snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
