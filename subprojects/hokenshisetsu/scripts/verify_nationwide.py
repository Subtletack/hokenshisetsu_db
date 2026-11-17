"""Generalized monthly verifier for nationwide Japanese insured medical institution dataset.

Usage:
  python3 verify_nationwide.py --snapshot 202611
  python3 verify_nationwide.py --snapshot 202701
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
MY_PACKAGES = REPO_ROOT / "my_packages"
if str(MY_PACKAGES) not in sys.path:
    sys.path.append(str(MY_PACKAGES))

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import openpyxl  # noqa: E402
import pandas as pd  # noqa: E402

import build_nationwide as bn  # noqa: E402
import build_nationwide_202611 as bld  # noqa: E402
import download_raw as dr  # noqa: E402
import verify_nationwide_202611 as v26  # noqa: E402


def run_monthly_guard_tests() -> None:
    """Offline regression tests for F1 (month mismatch rejection) & F2 (missing bureau/pref rejection)."""
    # 1. F1: parse_layout_a_sheet rejects sheet header date belonging to a different month
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["コード内容別医療機関一覧表"])
    ws.append(["令和8年11月1日現在"])
    orig_snap = bld.SNAPSHOT_DATE
    try:
        bld.SNAPSHOT_DATE = "2027-01-01"
        qa = bld.init_qa_stats()
        rejected_hdr = False
        try:
            bld.parse_layout_a_sheet(ws, "01", "01_hokkaido", "t.xlsx", "Sheet1", "ika_main", qa)
        except ValueError:
            rejected_hdr = True
        assert rejected_hdr, "Expected ValueError on mismatched sheet header date"
    finally:
        bld.SNAPSHOT_DATE = orig_snap

    # 2. F1 & F2: discover_targets rejects mismatched month and missing Kyushu subpage links
    def mock_fetch_202611(url: str) -> tuple[str, list[tuple[str, str]]]:
        if "hokkaido/iryo_shido" in url:
            return "北海道", [
                ("https://kouseikyoku.mhlw.go.jp/hokkaido/gyomu/gyomu/hoken_kikan/zentai_ichiran.html", "郡市区別"),
                ("https://kouseikyoku.mhlw.go.jp/hokkaido/gyomu/gyomu/hoken_kikan/code_ichiran.html", "コード内容別"),
            ]
        if "zentai_ichiran" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/hokkaido/z.xlsx", "Excel")]
        if "code_ichiran" in url:
            return "令和8年11月1日現在", [
                ("https://kouseikyoku.mhlw.go.jp/hokkaido/b.xlsx", "Excel"),
                ("https://kouseikyoku.mhlw.go.jp/hokkaido/s.xlsx", "Excel"),
            ]
        if "tohoku" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/tohoku/shitei-touhoku-ika-r0811.xlsx", "医科")]
        if "kantoshinetsu" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/kantoshinetsu/shitei_ika_r0811.zip", "医科")]
        if "tokaihokuriku/gyomu" in url:
            return "東海", [("https://kouseikyoku.mhlw.go.jp/tokaihokuriku/sub.html", "1．コード内容別医療機関一覧表")]
        if "tokaihokuriku/sub.html" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/tokaihokuriku/2611-01-01.zip", "医科")]
        if "kinki" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/kinki/2026.11_kikanzentai_ika.zip", "医科")]
        if "chugokushikoku" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/chugokushikoku/000506607.zip", "医科")]
        if "shikoku/gyomu" in url:
            return "令和8年11月1日現在", [("https://kouseikyoku.mhlw.go.jp/shikoku/000506264.zip", "ZIP")]
        if "kyushu/gyomu/gyomu/hoken_kikan/index_00004" in url:
            return "九州", [("https://kouseikyoku.mhlw.go.jp/kyushu/sub.html", "コード内容別医療機関一覧表")]
        if "kyushu/sub.html" in url:
            return "令和8年11月1日現在", [
                (f"https://kouseikyoku.mhlw.go.jp/kyushu/{i}.zip", "ZIP") for i in range(8)
            ]
        return "", []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_docs = Path(tmpdir)
        with patch.object(dr, "DOCS_DIR", tmp_docs), patch.object(dr, "_fetch_page", side_effect=mock_fetch_202611):
            rejected_wrong_ym = False
            try:
                dr.discover_targets("202701")
            except RuntimeError:
                rejected_wrong_ym = True
            assert rejected_wrong_ym, "Expected RuntimeError when 202611 HTML is discovered for 202701"
            assert not (tmp_docs / "discovered_targets_202701.json").exists()

            # F2: empty Kyushu subpage must fail without writing discovered_targets
            def mock_fetch_missing_kyushu(url: str) -> tuple[str, list[tuple[str, str]]]:
                if "kyushu/sub.html" in url:
                    return "令和8年11月1日現在", []
                return mock_fetch_202611(url)

            with patch.object(dr, "_fetch_page", side_effect=mock_fetch_missing_kyushu):
                rejected_missing = False
                try:
                    dr.discover_targets("202611")
                except RuntimeError:
                    rejected_missing = True
                assert rejected_missing, "Expected RuntimeError when 08_kyushu subpage has 0 ZIPs"
                assert not (tmp_docs / "discovered_targets_202611.json").exists()

    # 3. F1: verify_snapshot rejects 202611 CSV/QA artifacts even if renamed to 202701
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_sub = Path(tmpdir)
        (tmp_sub / "data" / "processed").mkdir(parents=True)
        (tmp_sub / "output").mkdir(parents=True)
        (tmp_sub / "data" / "processed" / "hokenshisetsu_nationwide_202701.csv").write_text(
            "snapshot_date,facility_id\n2026-11-01,01101123456\n", encoding="utf-8-sig"
        )
        (tmp_sub / "output" / "prefecture_summary_202701.csv").write_text("pref_code\n01\n", encoding="utf-8-sig")
        (tmp_sub / "output" / "municipality_summary_202701.csv").write_text("muni_code\n01101\n", encoding="utf-8-sig")
        (tmp_sub / "output" / "build_qa_report_202701.json").write_text(
            json.dumps({
                "snapshot_date": "2026-11-01",
                "outputs": {"processed_csv": {"path": "hokenshisetsu_nationwide_202611.csv"}},
                "snapshot_verification": {"header_snapshot_year_months": ["2026-11"]},
            }),
            encoding="utf-8",
        )
        rejected_verify = False
        try:
            with patch.object(bn, "resolve_utf_ken_all", return_value=Path("/tmp/dummy.zip")):
                verify_snapshot("202701", run_guards=False, sub_dir=tmp_sub)
        except AssertionError:
            rejected_verify = True
        assert rejected_verify, "Expected AssertionError when verifying 202611 artifacts as 202701"

    # 4. F1 (P2 residual): date-less sheet with facility row must fail even if another sheet set 2026-11
    wb_nodate = openpyxl.Workbook()
    ws_nodate = wb_nodate.active
    ws_nodate.append(["コード内容別医療機関一覧表"])
    ws_nodate.append([1, "01", "テスト病院", "0600000 札幌市", "011-000-0000", "開設", "医科", "内科", "", ""])
    orig_snap = bld.SNAPSHOT_DATE
    try:
        bld.SNAPSHOT_DATE = "2026-11-01"
        qa_pre = bld.init_qa_stats()
        qa_pre["header_snapshot_yms"].add("2026-11")
        rejected_nodate = False
        try:
            bld.parse_layout_a_sheet(ws_nodate, "01", "01_hokkaido", "nodate.xlsx", "Sheet1", "ika_main", qa_pre)
        except ValueError:
            rejected_nodate = True
        assert rejected_nodate, "Expected ValueError for date-less sheet containing facility records"
    finally:
        bld.SNAPSHOT_DATE = orig_snap

    # 5. F1/F2 (P2 residuals): validate_discovered_targets / load_targets rejects missing date evidence or any of 6 supplementary targets
    full_22 = [
        {"bureau_dir": "01_hokkaido", "filename": "hokkaido_zentai_ika_r0811.xlsx", "url": "https://ex/z.xlsx", "discovered_ym": "202611"},
        {"bureau_dir": "01_hokkaido", "filename": "hokkaido_code_ika_byoin_r0811.xlsx", "url": "https://ex/b.xlsx", "discovered_ym": "202611"},
        {"bureau_dir": "01_hokkaido", "filename": "hokkaido_code_ika_shinryojo_r0811.xlsx", "url": "https://ex/s.xlsx", "discovered_ym": "202611"},
        {"bureau_dir": "02_tohoku", "filename": "shitei-touhoku-ika-r0811.xlsx", "url": "https://ex/shitei-touhoku-ika-r0811.xlsx", "discovered_ym": "202611"},
        {"bureau_dir": "02_tohoku", "filename": "shitei-touhoku-ikaheisetsu-r0811.xlsx", "url": "https://ex/shitei-touhoku-ikaheisetsu-r0811.xlsx", "discovered_ym": "202611"},
        {"bureau_dir": "02_tohoku", "filename": "shitei-touhoku-shikaheisetsu-r0811.xlsx", "url": "https://ex/shitei-touhoku-shikaheisetsu-r0811.xlsx", "discovered_ym": "202611"},
        {"bureau_dir": "03_kantoshinetsu", "filename": "shitei_ika_r0811.zip", "url": "https://ex/shitei_ika_r0811.zip", "discovered_ym": "202611"},
        {"bureau_dir": "03_kantoshinetsu", "filename": "shitei_ikaheisetsu_r0811.zip", "url": "https://ex/shitei_ikaheisetsu_r0811.zip", "discovered_ym": "202611"},
        {"bureau_dir": "03_kantoshinetsu", "filename": "shitei_shikaheisetsu_r0811.zip", "url": "https://ex/shitei_shikaheisetsu_r0811.zip", "discovered_ym": "202611"},
        {"bureau_dir": "04_tokaihokuriku", "filename": "2611-01-01.zip", "url": "https://ex/2611-01-01.zip", "discovered_ym": "202611"},
        {"bureau_dir": "05_kinki", "filename": "2026.11_kikanzentai_ika.zip", "url": "https://ex/2026.11_kikanzentai_ika.zip", "discovered_ym": "202611"},
        {"bureau_dir": "05_kinki", "filename": "s2026.11_kikanzentai_sika.zip", "url": "https://ex/s2026.11_kikanzentai_sika.zip", "discovered_ym": "202611"},
        {"bureau_dir": "06_chugokushikoku", "filename": "000506607.zip", "url": "https://ex/000506607.zip", "discovered_ym": "202611"},
        {"bureau_dir": "07_shikoku", "filename": "000506264.zip", "url": "https://ex/000506264.zip", "discovered_ym": "202611"},
    ] + [
        {"bureau_dir": "08_kyushu", "filename": f"{p}_{i}.zip", "url": f"https://ex/{i}.zip", "discovered_ym": "202611"}
        for i, (p, _) in enumerate(dr.KYUSHU_PREFS)
    ]
    dr.validate_discovered_targets(full_22, "202611")

    # Reject missing discovered_ym on numeric URL
    no_ym_targets = [dict(x) for x in full_22]
    no_ym_targets[12]["discovered_ym"] = ""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "targets_no_ym.json"
        p.write_text(json.dumps(no_ym_targets), encoding="utf-8")
        rejected_no_ym = False
        try:
            dr.load_targets("202611", targets_json=p)
        except RuntimeError:
            rejected_no_ym = True
        assert rejected_no_ym, "Expected RuntimeError when numeric URL target lacks discovered_ym"

    # Reject omitting any of the 6 supplementary/QA targets
    for omit_tok in (
        "hokkaido_zentai_ika",
        "touhoku-ikaheisetsu-",
        "touhoku-shikaheisetsu-",
        "shitei_ikaheisetsu_",
        "shitei_shikaheisetsu_",
        "kikanzentai_sika",
    ):
        partial = [x for x in full_22 if omit_tok not in x["filename"]]
        assert len(partial) == 21
        rejected_partial = False
        try:
            dr.validate_discovered_targets(partial, "202611")
        except RuntimeError:
            rejected_partial = True
        assert rejected_partial, f"Expected RuntimeError when omitting {omit_tok}"


def verify_snapshot(snapshot: str, run_guards: bool = True, sub_dir: Path | None = None) -> None:
    yyyy, mm = dr.validate_snapshot_str(snapshot)
    expected_date = f"{yyyy:04d}-{mm:02d}-01"
    expected_ym = f"{yyyy:04d}-{mm:02d}"

    if run_guards:
        v26.run_unit_tests()
        run_monthly_guard_tests()

    target_sub_dir = sub_dir or (REPO_ROOT / "subprojects" / "hokenshisetsu")
    processed_csv = target_sub_dir / "data" / "processed" / f"hokenshisetsu_nationwide_{snapshot}.csv"
    pref_summary_csv = target_sub_dir / "output" / f"prefecture_summary_{snapshot}.csv"
    muni_summary_csv = target_sub_dir / "output" / f"municipality_summary_{snapshot}.csv"
    qa_report_json = target_sub_dir / "output" / f"build_qa_report_{snapshot}.json"
    utf_ken_all_zip = bn.resolve_utf_ken_all(REPO_ROOT / "data" / "raw" / "hokenshisetsu", snapshot)

    for path in (processed_csv, pref_summary_csv, muni_summary_csv, qa_report_json):
        assert path.exists(), f"Missing output artifact: {path}"

    qa = json.loads(qa_report_json.read_text(encoding="utf-8"))
    assert qa.get("snapshot_date") == expected_date, (
        f"QA snapshot_date mismatch: {qa.get('snapshot_date')} != {expected_date}"
    )
    assert qa.get("outputs", {}).get("processed_csv", {}).get("path", "").endswith(f"_{snapshot}.csv"), (
        f"QA processed_csv path does not match snapshot {snapshot}"
    )
    assert qa.get("snapshot_verification", {}).get("header_snapshot_year_months") == [expected_ym], (
        f"QA header_snapshot_year_months mismatch: {qa.get('snapshot_verification')} != [{expected_ym}]"
    )
    assert qa.get("snapshot_verification", {}).get("sheets_missing_date") == 0, (
        f"QA reports sheets missing date: {qa.get('snapshot_verification')}"
    )
    assert qa.get("snapshot_verification", {}).get("sheets_verified_with_date", 0) > 0, (
        f"QA reports no sheets verified with date: {qa.get('snapshot_verification')}"
    )

    df = pd.read_csv(processed_csv, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    pref_df = pd.read_csv(pref_summary_csv, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    muni_df = pd.read_csv(muni_summary_csv, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    assert (df["snapshot_date"] == expected_date).all(), (
        f"CSV row snapshot_date mismatch: expected all {expected_date}"
    )

    total_rows = len(df)
    ika_main_cnt = int((df["record_source_type"] == "ika_main").sum())
    ikaheisetsu_cnt = int((df["record_source_type"] == "ikaheisetsu_from_shika").sum())
    assert total_rows > 90000, f"Unexpectedly low nationwide count: {total_rows}"
    assert ika_main_cnt + ikaheisetsu_cnt == total_rows

    assert df["pref_code"].nunique() == 47
    assert set(df["pref_code"].unique()) == {f"{i:02d}" for i in range(1, 48)}
    assert df["kouseikyoku_code"].nunique() == 8
    assert len(df.columns) == 64

    assert df["medical_code_10digit"].str.fullmatch(r"\d{10}").all()
    assert df["medical_code_10digit"].is_unique
    assert df["primary_code_10digit"].str.fullmatch(r"\d{10}").all()
    assert df["primary_code_10digit"].is_unique
    assert (df["medical_code_10digit"] == (df["pref_code"] + "1" + df["medical_code_7digit"])).all()

    ika_mask = df["record_source_type"] == "ika_main"
    shika_side_mask = df["record_source_type"] == "ikaheisetsu_from_shika"
    assert (df.loc[ika_mask, "tensu_table_code"] == "1").all()
    assert (df.loc[ika_mask, "primary_code_10digit"] == df.loc[ika_mask, "medical_code_10digit"]).all()
    assert (df.loc[shika_side_mask, "tensu_table_code"] == "3").all()
    assert (df.loc[shika_side_mask, "paired_code_7digit"] != "").all()
    assert (df.loc[shika_side_mask, "dental_code_10digit"] == df.loc[shika_side_mask, "primary_code_10digit"]).all()

    assert len(qa["cross_checks"]["seq_mismatches"]) == 0
    assert qa["cross_checks"]["ika_main_intra_pref_duplicate_code7"] == 0
    assert qa["cross_checks"]["shikaheisetsu_not_in_ika_main"] == 0
    assert qa["cross_checks"]["ikaheisetsu_missing_paired_code"] == 0
    assert qa["cross_checks"]["hokkaido_zentai_symmetric_diff"] == 0

    canon_muni = v26.load_canonical_muni_map(utf_ken_all_zip)
    assert df["municipality_code_5digit"].str.fullmatch(r"\d{5}").all()
    assert (df["municipality_name"] != "").all()
    assert (df["municipality_match_method"] != "unmatched").all()
    assert (df["municipality_code_5digit"].str[:2] == df["pref_code"]).all()
    assert (df["municipality_code_5digit"].map(canon_muni) == df["municipality_name"]).all()

    assert df["facility_type"].isin(["病院", "診療所"]).all()
    hosp_mask = df["facility_type"] == "病院"
    clin_mask = df["facility_type"] == "診療所"
    assert df.loc[hosp_mask, "hospital_special_type"].isin(["一般病院等", "特定機能", "地域支援", "総合病院"]).all()
    assert (df.loc[hosp_mask, "hospital_special_types"] != "").all()
    assert (df.loc[clin_mask, "hospital_special_type"] == "").all()
    assert (df.loc[clin_mask, "hospital_special_types"] == "").all()
    assert df["status"].isin(["現存", "休止", "辞退"]).all()

    bed_cols = ["beds_general", "beds_ryoyo", "beds_seishin", "beds_kekkaku", "beds_kansen"]
    bed_sum_calc = sum(df[c].astype(int) for c in bed_cols)
    bed_total = df["beds_total"].astype(int)
    assert (bed_sum_calc == bed_total).all()

    for dcol in ("designated_date", "valid_period_start"):
        non_empty = df.loc[df[dcol] != "", dcol]
        assert non_empty.str.fullmatch(r"\d{4}-\d{2}-\d{2}").all()

    tokutei_cnt = int((df["is_tokutei_kinou_byoin"] == "True").sum())
    shien_cnt = int((df["is_chiiki_iryo_shien_byoin"] == "True").sum())
    sougou_cnt = int((df["is_sougou_byoin"] == "True").sum())

    assert len(pref_df) == 47
    for sum_df in (pref_df, muni_df):
        assert sum_df["total_facilities"].astype(int).sum() == total_rows
        assert sum_df["ika_main_count"].astype(int).sum() == ika_main_cnt
        assert sum_df["ikaheisetsu_from_shika_count"].astype(int).sum() == ikaheisetsu_cnt
        assert sum_df["tokutei_kinou_byoin_count"].astype(int).sum() == tokutei_cnt
        assert sum_df["chiiki_iryo_shien_byoin_count"].astype(int).sum() == shien_cnt
        assert sum_df["sougou_byoin_count"].astype(int).sum() == sougou_cnt
        assert sum_df["beds_total_sum"].astype(int).sum() == int(bed_total.sum())

    print(
        json.dumps(
            {
                "snapshot": snapshot,
                "verification_status": "ALL_CHECKS_PASSED",
                "unit_tests": "PASSED",
                "total_rows": total_rows,
                "total_columns": len(df.columns),
                "prefectures_verified": int(pref_df["pref_code"].nunique()),
                "municipalities_verified": int(df["municipality_code_5digit"].nunique()),
                "tokutei_kinou_byoin": tokutei_cnt,
                "chiiki_iryo_shien_byoin": shien_cnt,
                "sougou_byoin": sougou_cnt,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify nationwide hokenshisetsu dataset for a YYYYMM snapshot.")
    parser.add_argument("--snapshot", required=True, help="Target snapshot YYYYMM (e.g. 202611, 202701)")
    args = parser.parse_args()
    verify_snapshot(args.snapshot)


if __name__ == "__main__":
    main()
