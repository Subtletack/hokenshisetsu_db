#!/usr/bin/env python3
"""Track same-facility insurance code transitions across monthly snapshots.

Features:
- Exact normalized (facility_name, address) 1-to-1 matching between disappeared
  and newly appeared medical_code_10digit records.
- Idempotent re-runs for any curr_snapshot (no duplicate history/pool rows).
- Multi-hop lineage tracking via `root_code` (e.g. A -> B -> C all share root A).
- Unmatched-removal pool (`removed_pool.csv`) to catch facilities that disappear
  for one or more snapshots and later reappear under a new code.
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
    "root_code",
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

POOL_COLUMNS = [
    "last_seen_snapshot",
    "removed_at_snapshot",
    "matched_in_snapshot",
    "medical_code_10digit",
    "pref_code",
    "pref_name",
    "facility_name",
    "address",
    "phone",
    "designated_date",
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
    proc_path = (
        repo_root
        / "subprojects"
        / "hokenshisetsu"
        / "data"
        / "processed"
        / f"hokenshisetsu_nationwide_{snapshot}.csv"
    )
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


def assign_root_codes(hist_df: pd.DataFrame) -> pd.DataFrame:
    """Compute `root_code` across multi-hop transitions (A -> B -> C => root A)."""
    if hist_df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    ordered = hist_df.sort_values(
        ["curr_snapshot", "prev_snapshot", "new_medical_code_10digit", "old_medical_code_10digit"]
    ).reset_index(drop=True)

    parent_of: dict[str, str] = {}
    for row in ordered.to_dict("records"):
        old_c = str(row["old_medical_code_10digit"])
        new_c = str(row["new_medical_code_10digit"])
        if new_c and old_c and new_c != old_c:
            parent_of[new_c] = old_c

    def resolve_root(code: str) -> str:
        cur = code
        seen = {cur}
        while cur in parent_of:
            nxt = parent_of[cur]
            if nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        return cur

    ordered["root_code"] = [resolve_root(str(c)) for c in ordered["old_medical_code_10digit"]]
    return ordered[HISTORY_COLUMNS]


def match_removed_and_added(
    removed_candidates: pd.DataFrame,
    added: pd.DataFrame,
    curr_snapshot: str,
) -> tuple[pd.DataFrame, set[str]]:
    """Match 1-to-1 unique normalized (facility_name, address) between removed_candidates and added.

    Returns (transitions_df, matched_old_codes).
    """
    if removed_candidates.empty or added.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS), set()

    rem = removed_candidates.copy()
    add = added.copy()
    rem["_key"] = [
        (normalize_match_text(n), normalize_match_text(a))
        for n, a in zip(rem["facility_name"], rem["address"])
    ]
    add["_key"] = [
        (normalize_match_text(n), normalize_match_text(a))
        for n, a in zip(add["facility_name"], add["address"])
    ]

    rem_counts = rem["_key"].value_counts()
    add_counts = add["_key"].value_counts()

    unique_rem_keys = {k for k, cnt in rem_counts.items() if cnt == 1 and k[0] and k[1]}
    unique_add_keys = {k for k, cnt in add_counts.items() if cnt == 1 and k[0] and k[1]}
    matched_keys = sorted(unique_rem_keys & unique_add_keys)

    if not matched_keys:
        return pd.DataFrame(columns=HISTORY_COLUMNS), set()

    matched_set = set(matched_keys)
    rem_map = {k: r for k, r in zip(rem["_key"], rem.to_dict("records")) if k in matched_set}
    add_map = {k: r for k, r in zip(add["_key"], add.to_dict("records")) if k in matched_set}

    rows: list[dict[str, str]] = []
    matched_old_codes: set[str] = set()
    for key in matched_keys:
        r_old = rem_map[key]
        r_new = add_map[key]
        old_code = str(r_old["medical_code_10digit"])
        matched_old_codes.add(old_code)
        rows.append(
            {
                "root_code": old_code,
                "prev_snapshot": str(r_old["last_seen_snapshot"]),
                "curr_snapshot": curr_snapshot,
                "old_medical_code_10digit": old_code,
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

    out_df = pd.DataFrame(rows, columns=HISTORY_COLUMNS).sort_values(
        ["pref_code", "new_medical_code_10digit", "old_medical_code_10digit"]
    ).reset_index(drop=True)
    return out_df, matched_old_codes


def discover_tracked_snapshots(
    trans_dir: Path,
    pool_df: pd.DataFrame,
    hist_df: pd.DataFrame,
) -> list[str]:
    """Return sorted unique YYYYMM snapshots already recorded in transitions outputs."""
    snaps: set[str] = set()
    if trans_dir.exists():
        for p in trans_dir.glob("*_code_changes.csv"):
            m = re.fullmatch(r"(\d{6})_code_changes\.csv", p.name)
            if m:
                snaps.add(m.group(1))
    if not hist_df.empty and "curr_snapshot" in hist_df.columns:
        for val in hist_df["curr_snapshot"]:
            if re.fullmatch(r"\d{6}", str(val)):
                snaps.add(str(val))
    if not pool_df.empty:
        for col in ("removed_at_snapshot", "matched_in_snapshot"):
            if col in pool_df.columns:
                for val in pool_df[col]:
                    if re.fullmatch(r"\d{6}", str(val)):
                        snaps.add(str(val))
    return sorted(snaps)


def _apply_transition_step(
    prev_snapshot: str,
    curr_snapshot: str,
    pool_df: pd.DataFrame,
    hist_df: pd.DataFrame,
    prev_df: pd.DataFrame,
    curr_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Execute one chronological transition step in memory."""
    pool_df = pool_df.copy()
    prev_codes = set(prev_df["medical_code_10digit"])
    curr_codes = set(curr_df["medical_code_10digit"])

    # If a previously pooled code reappeared unchanged in curr_df, mark it resolved
    if not pool_df.empty:
        reappeared_same = (pool_df["matched_in_snapshot"] == "") & (pool_df["medical_code_10digit"].isin(curr_codes))
        pool_df.loc[reappeared_same, "matched_in_snapshot"] = curr_snapshot

    removed_now = prev_df.loc[prev_df["medical_code_10digit"].isin(prev_codes - curr_codes)].copy()
    if not removed_now.empty:
        removed_now = removed_now[
            ["medical_code_10digit", "pref_code", "pref_name", "facility_name", "address", "phone", "designated_date"]
        ].copy()
        removed_now["last_seen_snapshot"] = prev_snapshot
        removed_now["removed_at_snapshot"] = curr_snapshot
        removed_now["matched_in_snapshot"] = ""
        removed_now = removed_now[POOL_COLUMNS]
    else:
        removed_now = pd.DataFrame(columns=POOL_COLUMNS)

    active_pool = pool_df.loc[pool_df["matched_in_snapshot"] == ""].copy()
    # Combine active historical removals + removals from this month (avoiding duplicate codes)
    if not active_pool.empty and not removed_now.empty:
        existing_codes = set(active_pool["medical_code_10digit"])
        removed_now_dedup = removed_now.loc[~removed_now["medical_code_10digit"].isin(existing_codes)]
        candidates = pd.concat([active_pool, removed_now_dedup], ignore_index=True)
    elif not active_pool.empty:
        candidates = active_pool
    else:
        candidates = removed_now

    added_now = curr_df.loc[curr_df["medical_code_10digit"].isin(curr_codes - prev_codes)].copy()
    trans_df, matched_old_codes = match_removed_and_added(candidates, added_now, curr_snapshot)

    # Update pool: mark matched old codes and append newly unmatched removals
    if not pool_df.empty and matched_old_codes:
        mask_matched = (pool_df["matched_in_snapshot"] == "") & (
            pool_df["medical_code_10digit"].isin(matched_old_codes)
        )
        pool_df.loc[mask_matched, "matched_in_snapshot"] = curr_snapshot

    if not removed_now.empty:
        removed_now.loc[
            removed_now["medical_code_10digit"].isin(matched_old_codes), "matched_in_snapshot"
        ] = curr_snapshot
        existing_pool_codes = set(pool_df.loc[pool_df["matched_in_snapshot"] == "", "medical_code_10digit"])
        to_append = removed_now.loc[~removed_now["medical_code_10digit"].isin(existing_pool_codes)]
        pool_df = pd.concat([pool_df, to_append], ignore_index=True)

    if not pool_df.empty:
        pool_df = pool_df.sort_values(
            ["removed_at_snapshot", "pref_code", "medical_code_10digit"]
        ).reset_index(drop=True)

    if not hist_df.empty:
        combined = pd.concat([hist_df, trans_df], ignore_index=True)
    else:
        combined = trans_df.copy()

    combined = assign_root_codes(combined)
    if not trans_df.empty:
        monthly_out = combined.loc[combined["curr_snapshot"] == curr_snapshot].reset_index(drop=True)
    else:
        monthly_out = pd.DataFrame(columns=HISTORY_COLUMNS)

    return pool_df, combined, monthly_out


def run_transition_tracking(
    prev_snapshot: str,
    curr_snapshot: str,
    repo_root: Path = REPO_ROOT,
) -> dict[str, object]:
    dr.validate_snapshot_str(prev_snapshot)
    dr.validate_snapshot_str(curr_snapshot)

    trans_dir = repo_root / "data" / "public" / "transitions"
    trans_dir.mkdir(parents=True, exist_ok=True)
    history_csv = trans_dir / "facility_code_history.csv"
    pool_csv = trans_dir / "removed_pool.csv"

    if pool_csv.exists():
        pool_df = pd.read_csv(pool_csv, dtype=str, keep_default_na=False, encoding="utf-8")
    else:
        pool_df = pd.DataFrame(columns=POOL_COLUMNS)

    if history_csv.exists():
        hist_df = pd.read_csv(history_csv, dtype=str, keep_default_na=False, encoding="utf-8")
    else:
        hist_df = pd.DataFrame(columns=HISTORY_COLUMNS)

    tracked_all = discover_tracked_snapshots(trans_dir, pool_df, hist_df)
    subsequent = [s for s in tracked_all if s > curr_snapshot]

    # Rollback state for curr_snapshot and all subsequent snapshots before rebuilding chronologically
    if not pool_df.empty:
        pool_df = pool_df.loc[pool_df["removed_at_snapshot"] < curr_snapshot].copy()
        pool_df.loc[pool_df["matched_in_snapshot"] >= curr_snapshot, "matched_in_snapshot"] = ""
    if not hist_df.empty:
        hist_df = hist_df.loc[hist_df["curr_snapshot"] < curr_snapshot].copy()

    steps: list[tuple[str, str]] = [(prev_snapshot, curr_snapshot)]
    for s in subsequent:
        prev_s = find_previous_snapshot(s, repo_root=repo_root)
        if not prev_s:
            raise FileNotFoundError(f"Previous snapshot not found for subsequent tracked snapshot {s}")
        steps.append((prev_s, s))

    # Load all required snapshot DataFrames before modifying any output files
    df_cache: dict[str, pd.DataFrame] = {}
    for p_snap, c_snap in steps:
        if p_snap not in df_cache:
            df_cache[p_snap] = load_snapshot_df(p_snap, repo_root=repo_root)
        if c_snap not in df_cache:
            df_cache[c_snap] = load_snapshot_df(c_snap, repo_root=repo_root)

    monthly_outputs: dict[str, pd.DataFrame] = {}
    for p_snap, c_snap in steps:
        pool_df, hist_df, monthly_out = _apply_transition_step(
            p_snap,
            c_snap,
            pool_df,
            hist_df,
            df_cache[p_snap],
            df_cache[c_snap],
        )
        monthly_outputs[c_snap] = monthly_out

    pool_df.to_csv(pool_csv, index=False, encoding="utf-8", lineterminator="\n")
    hist_df.to_csv(history_csv, index=False, encoding="utf-8", lineterminator="\n")
    for _, c_snap in steps:
        m_csv = trans_dir / f"{c_snap}_code_changes.csv"
        monthly_outputs[c_snap].to_csv(m_csv, index=False, encoding="utf-8", lineterminator="\n")

    monthly_csv = trans_dir / f"{curr_snapshot}_code_changes.csv"
    return {
        "prev_snapshot": prev_snapshot,
        "curr_snapshot": curr_snapshot,
        "code_changes_detected": int(len(monthly_outputs[curr_snapshot])),
        "monthly_csv": str(monthly_csv.relative_to(repo_root)),
        "history_csv": str(history_csv.relative_to(repo_root)),
        "history_total_rows": int(len(hist_df)),
        "active_removed_pool_rows": int((pool_df["matched_in_snapshot"] == "").sum()) if not pool_df.empty else 0,
        "rebuilt_subsequent_snapshots": subsequent,
    }


def run_self_test() -> None:
    snap_1 = pd.DataFrame(
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
                "facility_name": "一時消滅クリニック",
                "address": "東京都新宿区西新宿1-1-1",
                "phone": "03-0000-0002",
                "designated_date": "2020-04-01",
                "designation_reason": "新規",
            },
            {
                "medical_code_10digit": "0113000036",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "名称変更クリニック",
                "address": "東京都渋谷区1-1-1",
                "phone": "03-0000-0003",
                "designated_date": "2020-04-01",
                "designation_reason": "新規",
            },
        ]
    )
    snap_2 = pd.DataFrame(
        [
            # A -> B in 202612
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
            # 0113000028 disappears in 202612 (goes to removed_pool)
            # Name change is intentionally not matched
            {
                "medical_code_10digit": "0113999037",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "医療法人社団 名称変更クリニック",
                "address": "東京都渋谷区1-1-1",
                "phone": "03-0000-0003",
                "designated_date": "2026-12-01",
                "designation_reason": "組織変更",
            },
        ]
    )
    snap_3 = pd.DataFrame(
        [
            # B -> C in 202701 (should share root_code 0113000010)
            {
                "medical_code_10digit": "0113999099",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "テスト内科クリニック",
                "address": "東京都千代田区霞が関1-2-3",
                "phone": "03-0000-0001",
                "designated_date": "2027-01-01",
                "designation_reason": "移転等",
            },
            # Delayed reappearance of 0113000028 from 202611 -> 0113999029 in 202701
            {
                "medical_code_10digit": "0113999029",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "一時消滅クリニック",
                "address": "東京都新宿区西新宿１−１−１",
                "phone": "03-0000-0002",
                "designated_date": "2027-01-01",
                "designation_reason": "再指定",
            },
            {
                "medical_code_10digit": "0113999037",
                "pref_code": "13",
                "pref_name": "東京都",
                "facility_name": "医療法人社団 名称変更クリニック",
                "address": "東京都渋谷区1-1-1",
                "phone": "03-0000-0003",
                "designated_date": "2026-12-01",
                "designation_reason": "組織変更",
            },
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for snap, df in (("202611", snap_1), ("202612", snap_2), ("202701", snap_3)):
            d = root / "release" / snap
            d.mkdir(parents=True)
            df.to_csv(
                d / f"hokenshisetsu_nationwide_{snap}_public.csv.gz",
                index=False,
                encoding="utf-8-sig",
                compression="gzip",
            )

        # 1. Run 202612 twice to verify strict idempotency
        s2_a = run_transition_tracking("202611", "202612", repo_root=root)
        hist_after_1 = (root / "data" / "public" / "transitions" / "facility_code_history.csv").read_text(encoding="utf-8")
        pool_after_1 = (root / "data" / "public" / "transitions" / "removed_pool.csv").read_text(encoding="utf-8")
        s2_b = run_transition_tracking("202611", "202612", repo_root=root)
        hist_after_2 = (root / "data" / "public" / "transitions" / "facility_code_history.csv").read_text(encoding="utf-8")
        pool_after_2 = (root / "data" / "public" / "transitions" / "removed_pool.csv").read_text(encoding="utf-8")
        assert s2_a == s2_b
        assert hist_after_1 == hist_after_2, "Idempotency failed on facility_code_history.csv"
        assert pool_after_1 == pool_after_2, "Idempotency failed on removed_pool.csv"
        assert s2_a["code_changes_detected"] == 1

        # 2. Run 202701 (and re-run) -> verify A->B->C root_code & delayed reappearance from pool
        s3_a = run_transition_tracking("202612", "202701", repo_root=root)
        s3_b = run_transition_tracking("202612", "202701", repo_root=root)
        assert s3_a == s3_b
        assert s3_a["code_changes_detected"] == 2  # B->C and 0113000028->0113999029

        hist = pd.read_csv(
            root / "data" / "public" / "transitions" / "facility_code_history.csv",
            dtype=str,
            keep_default_na=False,
        )
        assert len(hist) == 3
        chain = hist.loc[hist["root_code"] == "0113000010"]
        assert len(chain) == 2
        assert set(chain["new_medical_code_10digit"]) == {"0113999011", "0113999099"}
        delayed = hist.loc[hist["old_medical_code_10digit"] == "0113000028"].iloc[0]
        assert delayed["prev_snapshot"] == "202611"
        assert delayed["curr_snapshot"] == "202701"
        assert delayed["new_medical_code_10digit"] == "0113999029"

        # 3. Re-run 202612 after 202701 is already tracked -> subsequent state must be preserved/rebuilt identically
        hist_before = (root / "data" / "public" / "transitions" / "facility_code_history.csv").read_text(encoding="utf-8")
        pool_before = (root / "data" / "public" / "transitions" / "removed_pool.csv").read_text(encoding="utf-8")
        m12_before = (root / "data" / "public" / "transitions" / "202612_code_changes.csv").read_text(encoding="utf-8")
        m01_before = (root / "data" / "public" / "transitions" / "202701_code_changes.csv").read_text(encoding="utf-8")

        s2_rerun = run_transition_tracking("202611", "202612", repo_root=root)
        assert s2_rerun["rebuilt_subsequent_snapshots"] == ["202701"]

        pool_df_after = pd.read_csv(
            root / "data" / "public" / "transitions" / "removed_pool.csv",
            dtype=str,
            keep_default_na=False,
        )
        row_028 = pool_df_after.loc[pool_df_after["medical_code_10digit"] == "0113000028"].iloc[0]
        assert row_028["matched_in_snapshot"] == "202701", f"Expected matched_in_snapshot=202701, got {row_028['matched_in_snapshot']!r}"

        assert (root / "data" / "public" / "transitions" / "facility_code_history.csv").read_text(encoding="utf-8") == hist_before
        assert (root / "data" / "public" / "transitions" / "removed_pool.csv").read_text(encoding="utf-8") == pool_before
        assert (root / "data" / "public" / "transitions" / "202612_code_changes.csv").read_text(encoding="utf-8") == m12_before
        assert (root / "data" / "public" / "transitions" / "202701_code_changes.csv").read_text(encoding="utf-8") == m01_before

        # 4. Past-month correction in 202612 -> rebuilding from 202612 matches clean chronological run
        snap_2_corr = snap_2.loc[snap_2["medical_code_10digit"] != "0113999037"].copy()
        snap_2_corr.to_csv(
            root / "release" / "202612" / "hokenshisetsu_nationwide_202612_public.csv.gz",
            index=False,
            encoding="utf-8-sig",
            compression="gzip",
        )
        run_transition_tracking("202611", "202612", repo_root=root)
        hist_rebuilt = (root / "data" / "public" / "transitions" / "facility_code_history.csv").read_text(encoding="utf-8")
        pool_rebuilt = (root / "data" / "public" / "transitions" / "removed_pool.csv").read_text(encoding="utf-8")
        m12_rebuilt = (root / "data" / "public" / "transitions" / "202612_code_changes.csv").read_text(encoding="utf-8")
        m01_rebuilt = (root / "data" / "public" / "transitions" / "202701_code_changes.csv").read_text(encoding="utf-8")

        # Compare with clean from-scratch chronological run
        for p in (root / "data" / "public" / "transitions").glob("*"):
            p.unlink()
        run_transition_tracking("202611", "202612", repo_root=root)
        run_transition_tracking("202612", "202701", repo_root=root)
        assert (root / "data" / "public" / "transitions" / "facility_code_history.csv").read_text(encoding="utf-8") == hist_rebuilt
        assert (root / "data" / "public" / "transitions" / "removed_pool.csv").read_text(encoding="utf-8") == pool_rebuilt
        assert (root / "data" / "public" / "transitions" / "202612_code_changes.csv").read_text(encoding="utf-8") == m12_rebuilt
        assert (root / "data" / "public" / "transitions" / "202701_code_changes.csv").read_text(encoding="utf-8") == m01_rebuilt


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
        trans_dir = REPO_ROOT / "data" / "public" / "transitions"
        pool_csv = trans_dir / "removed_pool.csv"
        hist_csv = trans_dir / "facility_code_history.csv"
        pool_df = pd.read_csv(pool_csv, dtype=str, keep_default_na=False, encoding="utf-8") if pool_csv.exists() else pd.DataFrame(columns=POOL_COLUMNS)
        hist_df = pd.read_csv(hist_csv, dtype=str, keep_default_na=False, encoding="utf-8") if hist_csv.exists() else pd.DataFrame(columns=HISTORY_COLUMNS)
        subsequent = [s for s in discover_tracked_snapshots(trans_dir, pool_df, hist_df) if s > args.curr_snapshot]
        if subsequent:
            first_sub = subsequent[0]
            prev_sub = find_previous_snapshot(first_sub) or args.curr_snapshot
            summary = run_transition_tracking(prev_sub, first_sub)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        print(json.dumps({"curr_snapshot": args.curr_snapshot, "skipped": "no_previous_snapshot"}, ensure_ascii=False))
        return
    summary = run_transition_tracking(prev, args.curr_snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
