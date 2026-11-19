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
import tempfile
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
SUB_DIR = SCRIPT_DIR.parent
REPO_ROOT = SUB_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import download_raw as dr  # noqa: E402
import geocode_nationwide as gn  # noqa: E402

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


def export_git_diff_by_pref(
    public_df: pd.DataFrame,
    snapshot: str,
    repo_root: Path = REPO_ROOT,
) -> dict[str, object]:
    """Write 47 sorted, noise-free prefecture CSVs under data/public/by_pref/."""
    by_pref_dir = repo_root / "data" / "public" / "by_pref"
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

    (repo_root / "data" / "public" / "LATEST_SNAPSHOT.txt").write_text(
        f"{snapshot}\n", encoding="utf-8"
    )
    return {
        "by_pref_dir": str(by_pref_dir.relative_to(repo_root)),
        "prefecture_files": len(pref_codes),
        "rows": total_rows,
        "columns": int(len(diff_df.columns)),
        "dropped_volatile_columns": volatile,
    }


REQUIRED_GEOCODE_META_KEYS = (
    "snapshot",
    "input_signature",
    "rules_version",
    "pua_mapping_sha256",
    "dictionary_signature",
    "jageocoder_version",
    "geocode_file",
    "geocode_sha256",
)


def _validate_existing_geocode_asset(
    snapshot: str,
    geocode_gz: Path,
    meta_path: Path,
    public_df: pd.DataFrame,
    conditions: dict[str, str] | None = None,
    db_dir: Path = gn.DEFAULT_DB_DIR,
    pua_mapping_path: Path = gn.DEFAULT_PUA_MAPPING_PATH,
) -> bool:
    """Return True only if metadata exists, is complete, and matches current inputs & generation conditions."""
    if not meta_path.exists():
        return False

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    if not isinstance(meta, dict):
        return False

    for k in REQUIRED_GEOCODE_META_KEYS:
        val = meta.get(k)
        if not isinstance(val, str) or not val:
            return False

    if meta["snapshot"] != snapshot or meta["geocode_file"] != geocode_gz.name:
        return False

    curr_conditions = conditions or gn.get_current_geocode_conditions(
        db_dir=db_dir,
        pua_mapping_path=pua_mapping_path,
    )
    for k in ("rules_version", "pua_mapping_sha256", "dictionary_signature", "jageocoder_version"):
        if not curr_conditions.get(k) or meta[k] != curr_conditions[k]:
            return False

    if meta["input_signature"] != gn.compute_geocode_input_signature(public_df):
        return False

    if meta["geocode_sha256"] != sha256_file(geocode_gz):
        return False

    try:
        geo_df = pd.read_csv(
            geocode_gz,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
            compression="gzip",
        )
    except Exception:
        return False

    if "medical_code_10digit" not in geo_df.columns:
        return False
    if len(geo_df) != len(public_df) or not geo_df["medical_code_10digit"].is_unique:
        return False
    if set(geo_df["medical_code_10digit"]) != set(public_df["medical_code_10digit"]):
        return False

    return True


def export_public_release(
    snapshot: str,
    repo_root: Path = REPO_ROOT,
    sub_dir: Path | None = None,
    db_dir: Path = gn.DEFAULT_DB_DIR,
    pua_mapping_path: Path = gn.DEFAULT_PUA_MAPPING_PATH,
    conditions: dict[str, str] | None = None,
) -> dict[str, object]:
    yyyy, mm = dr.validate_snapshot_str(snapshot)
    expected_date = f"{yyyy:04d}-{mm:02d}-01"

    target_sub_dir = sub_dir or (repo_root / "subprojects" / "hokenshisetsu")
    processed_csv = target_sub_dir / "data" / "processed" / f"hokenshisetsu_nationwide_{snapshot}.csv"
    pref_csv = target_sub_dir / "output" / f"prefecture_summary_{snapshot}.csv"
    muni_csv = target_sub_dir / "output" / f"municipality_summary_{snapshot}.csv"
    qa_json = target_sub_dir / "output" / f"build_qa_report_{snapshot}.json"

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

    release_dir = repo_root / "release" / snapshot
    release_dir.mkdir(parents=True, exist_ok=True)

    public_gz = release_dir / f"hokenshisetsu_nationwide_{snapshot}_public.csv.gz"
    public_df.to_csv(public_gz, index=False, encoding="utf-8-sig", compression="gzip")

    copied_files = [public_gz]
    for src in (pref_csv, muni_csv, qa_json):
        dst = release_dir / src.name
        shutil.copy2(src, dst)
        copied_files.append(dst)

    geocode_gz = release_dir / f"hokenshisetsu_geocode_{snapshot}.csv.gz"
    geocode_meta = target_sub_dir / "output" / f"geocode_meta_{snapshot}.json"
    geocode_status = "absent"
    if geocode_gz.exists():
        if _validate_existing_geocode_asset(
            snapshot,
            geocode_gz,
            geocode_meta,
            public_df,
            conditions=conditions,
            db_dir=db_dir,
            pua_mapping_path=pua_mapping_path,
        ):
            copied_files.append(geocode_gz)
            geocode_status = "preserved_valid"
        else:
            geocode_gz.unlink(missing_ok=True)
            geocode_meta.unlink(missing_ok=True)
            geocode_status = "removed_stale"

    checksum_lines = [f"{sha256_file(p)}  {p.name}" for p in copied_files]
    checksum_path = release_dir / "SHA256SUMS.txt"
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    git_diff_summary = export_git_diff_by_pref(public_df, snapshot, repo_root=repo_root)

    return {
        "snapshot": snapshot,
        "release_dir": str(release_dir.relative_to(repo_root)),
        "rows": int(len(public_df)),
        "columns": int(len(public_df.columns)),
        "dropped_columns": drop_cols,
        "geocode_asset_status": geocode_status,
        "assets": {p.name: p.stat().st_size for p in copied_files + [checksum_path]},
        "git_diff_export": git_diff_summary,
    }


def run_self_test() -> None:
    import os
    import sqlite3
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        sub = root / "subprojects" / "hokenshisetsu"
        proc_dir = sub / "data" / "processed"
        out_dir = sub / "output"
        proc_dir.mkdir(parents=True)
        out_dir.mkdir(parents=True)

        snap = "202611"
        rows = []
        for idx, pref_code in enumerate(sorted(PREF_SLUGS.keys()), start=1):
            rows.append(
                {
                    "medical_code_10digit": f"{pref_code}10000{idx:02d}",
                    "pref_code": pref_code,
                    "pref_name": f"県{pref_code}",
                    "address": f"テスト市1-{idx}",
                    "owner_name": "開設者",
                    "manager_name": "管理者",
                    "snapshot_date": "2026-11-01",
                    "source_file": "f.xlsx",
                    "source_sheet": "s1",
                    "seq_no": str(idx),
                }
            )
        proc_csv = proc_dir / f"hokenshisetsu_nationwide_{snap}.csv"
        pd.DataFrame(rows).to_csv(proc_csv, index=False, encoding="utf-8-sig")
        (out_dir / f"prefecture_summary_{snap}.csv").write_text("pref_code\n01\n", encoding="utf-8")
        (out_dir / f"municipality_summary_{snap}.csv").write_text("municipality_code_5digit\n01101\n", encoding="utf-8")
        (out_dir / f"build_qa_report_{snap}.json").write_text(
            json.dumps({"snapshot_date": "2026-11-01"}), encoding="utf-8"
        )

        # Setup temporary dictionary dir and PUA mapping for G4/G5/G5-R tests
        tmp_db = root / "jageocoder_db"
        tmp_db.mkdir(parents=True)
        (tmp_db / "README.md").write_text("2026年4月17日\n", encoding="utf-8")
        (tmp_db / "dataset.sqlite").write_bytes(b"sqlite-v1")
        (tmp_db / "address.trie").write_bytes(b"trie-content-v1")
        addr_sqlite = tmp_db / "address_node.sqlite"
        with sqlite3.connect(addr_sqlite) as conn:
            conn.execute("CREATE TABLE nodes (latitude REAL)")
            conn.execute("INSERT INTO nodes (latitude) VALUES (35.0)")
            conn.commit()
        tmp_pua = root / "pua_mapping.json"
        tmp_pua.write_text("[]\n", encoding="utf-8")

        def make_geocode() -> None:
            fake_node = SimpleNamespace(x=139.75, y=35.67, level=8, parent=None, fullname=None)
            gn.run_geocode(
                snap,
                db_dir=tmp_db,
                repo_root=root,
                search_fn=lambda q: [SimpleNamespace(node=fake_node, matched=q)],
                pua_mapping_path=tmp_pua,
            )

        # 1. Initial export (no geocode)
        res1 = export_public_release(snap, repo_root=root, sub_dir=sub, db_dir=tmp_db, pua_mapping_path=tmp_pua)
        assert res1["geocode_asset_status"] == "absent"
        rel_dir = root / "release" / snap
        # Add an unrelated temp file in release_dir -> must never appear in SHA256SUMS.txt
        (rel_dir / "temp_ignore.tmp").write_text("tmp", encoding="utf-8")

        # 2. Manual geocode run -> re-export preserves valid geocode checksum from real file
        make_geocode()
        mock_cond = gn.get_current_geocode_conditions(
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            jageocoder_version="mock",
        )
        res2 = export_public_release(
            snap,
            repo_root=root,
            sub_dir=sub,
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            conditions=mock_cond,
        )
        assert res2["geocode_asset_status"] == "preserved_valid"
        sums_text = (rel_dir / "SHA256SUMS.txt").read_text(encoding="utf-8")
        lines = [l.strip() for l in sums_text.splitlines() if l.strip()]
        assert len(lines) == 5
        names = [l.split(None, 1)[1] for l in lines]
        assert f"hokenshisetsu_geocode_{snap}.csv.gz" in names
        assert "temp_ignore.tmp" not in names
        geo_line = [l for l in lines if l.endswith(f"hokenshisetsu_geocode_{snap}.csv.gz")][0]
        assert geo_line.split()[0] == sha256_file(rel_dir / f"hokenshisetsu_geocode_{snap}.csv.gz")

        # 3. G4: Missing metadata must NOT be treated as valid (even if codes match or address changed)
        meta_file = out_dir / f"geocode_meta_{snap}.json"
        meta_file.unlink()
        res_missing_meta = export_public_release(
            snap, repo_root=root, sub_dir=sub, db_dir=tmp_db, pua_mapping_path=tmp_pua, conditions=mock_cond
        )
        assert res_missing_meta["geocode_asset_status"] == "removed_stale"
        assert not (rel_dir / f"hokenshisetsu_geocode_{snap}.csv.gz").exists()

        # 4. G4: Corrupt JSON metadata or missing required key must be rejected
        make_geocode()
        meta_file.write_text("{corrupt-json", encoding="utf-8")
        res_corrupt = export_public_release(
            snap, repo_root=root, sub_dir=sub, db_dir=tmp_db, pua_mapping_path=tmp_pua, conditions=mock_cond
        )
        assert res_corrupt["geocode_asset_status"] == "removed_stale"

        make_geocode()
        meta_obj = json.loads(meta_file.read_text(encoding="utf-8"))
        del meta_obj["rules_version"]
        meta_file.write_text(json.dumps(meta_obj), encoding="utf-8")
        res_incomplete = export_public_release(
            snap, repo_root=root, sub_dir=sub, db_dir=tmp_db, pua_mapping_path=tmp_pua, conditions=mock_cond
        )
        assert res_incomplete["geocode_asset_status"] == "removed_stale"

        # 5. G5: Changing rules_version, PUA mapping, or dictionary (with unchanged input data) rejects reuse
        make_geocode()
        cond_rule_changed = dict(mock_cond, rules_version="9999-01-01.changed")
        res_rule = export_public_release(
            snap, repo_root=root, sub_dir=sub, db_dir=tmp_db, pua_mapping_path=tmp_pua, conditions=cond_rule_changed
        )
        assert res_rule["geocode_asset_status"] == "removed_stale"

        make_geocode()
        tmp_pua.write_text('[{"changed": true}]\n', encoding="utf-8")
        res_pua = export_public_release(
            snap,
            repo_root=root,
            sub_dir=sub,
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            conditions=gn.get_current_geocode_conditions(db_dir=tmp_db, pua_mapping_path=tmp_pua, jageocoder_version="mock"),
        )
        assert res_pua["geocode_asset_status"] == "removed_stale"

        make_geocode()
        (tmp_db / "README.md").write_text("2027年1月1日\n", encoding="utf-8")
        res_dict = export_public_release(
            snap,
            repo_root=root,
            sub_dir=sub,
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            conditions=gn.get_current_geocode_conditions(db_dir=tmp_db, pua_mapping_path=tmp_pua, jageocoder_version="mock"),
        )
        assert res_dict["geocode_asset_status"] == "removed_stale"

        # 5b. G5-R: Same-size SQLite body update, same-size trie update, and mtime-only preservation
        make_geocode()
        sig_before = gn.compute_dictionary_signature(tmp_db)
        size_before_sqlite = addr_sqlite.stat().st_size
        with sqlite3.connect(addr_sqlite) as conn:
            conn.execute("UPDATE nodes SET latitude = 36.0")
            conn.commit()
        assert addr_sqlite.stat().st_size == size_before_sqlite, "SQLite size must remain identical for G5-R test"
        sig_after_sqlite = gn.compute_dictionary_signature(tmp_db)
        assert sig_after_sqlite != sig_before, "Same-size SQLite update must change dictionary_signature"
        res_sqlite_same_size = export_public_release(
            snap,
            repo_root=root,
            sub_dir=sub,
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            conditions=gn.get_current_geocode_conditions(db_dir=tmp_db, pua_mapping_path=tmp_pua, jageocoder_version="mock"),
        )
        assert res_sqlite_same_size["geocode_asset_status"] == "removed_stale"

        make_geocode()
        sig_before_trie = gn.compute_dictionary_signature(tmp_db)
        trie_path = tmp_db / "address.trie"
        assert len(b"trie-content-v2") == len(trie_path.read_bytes())
        trie_path.write_bytes(b"trie-content-v2")
        sig_after_trie = gn.compute_dictionary_signature(tmp_db)
        assert sig_after_trie != sig_before_trie, "Same-size trie update must change dictionary_signature"
        res_trie_same_size = export_public_release(
            snap,
            repo_root=root,
            sub_dir=sub,
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            conditions=gn.get_current_geocode_conditions(db_dir=tmp_db, pua_mapping_path=tmp_pua, jageocoder_version="mock"),
        )
        assert res_trie_same_size["geocode_asset_status"] == "removed_stale"

        make_geocode()
        sig_before_mtime = gn.compute_dictionary_signature(tmp_db)
        st = addr_sqlite.stat()
        os.utime(addr_sqlite, (st.st_atime + 100, st.st_mtime + 100))
        sig_after_mtime = gn.compute_dictionary_signature(tmp_db)
        assert sig_after_mtime == sig_before_mtime, "mtime-only change must NOT change dictionary_signature"
        res_mtime_only = export_public_release(
            snap,
            repo_root=root,
            sub_dir=sub,
            db_dir=tmp_db,
            pua_mapping_path=tmp_pua,
            conditions=gn.get_current_geocode_conditions(db_dir=tmp_db, pua_mapping_path=tmp_pua, jageocoder_version="mock"),
        )
        assert res_mtime_only["geocode_asset_status"] == "preserved_valid"

        # 6. Modify an address in processed_csv -> re-export invalidates & removes stale geocode asset
        make_geocode()
        curr_cond = gn.get_current_geocode_conditions(db_dir=tmp_db, pua_mapping_path=tmp_pua, jageocoder_version="mock")
        rows[0]["address"] = "テスト市99-99"
        pd.DataFrame(rows).to_csv(proc_csv, index=False, encoding="utf-8-sig")
        res3 = export_public_release(
            snap, repo_root=root, sub_dir=sub, db_dir=tmp_db, pua_mapping_path=tmp_pua, conditions=curr_cond
        )
        assert res3["geocode_asset_status"] == "removed_stale"
        assert not (rel_dir / f"hokenshisetsu_geocode_{snap}.csv.gz").exists()
        sums_lines_3 = [l.strip() for l in (rel_dir / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(sums_lines_3) == 4


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized public release assets and Git-diffable prefecture CSVs.")
    parser.add_argument("--snapshot", help="Snapshot YYYYMM (e.g., 202611)")
    parser.add_argument("--self-test", action="store_true", help="Run offline unit tests")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
        print("EXPORT_SELF_TEST_PASSED")
        return
    if not args.snapshot:
        parser.error("--snapshot is required unless --self-test is specified")
    summary = export_public_release(args.snapshot)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
