"""Verify structural invariants, parser edge cases, cross-checks, and summaries for 2026-11 dataset.

Strict privacy rule: Never print or emit row-level facility names, owner/manager names,
addresses, phone numbers, or raw institution codes. Only pass/fail assertions and aggregate counts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MY_PACKAGES = REPO_ROOT / "my_packages"
if str(MY_PACKAGES) not in sys.path:
    sys.path.append(str(MY_PACKAGES))

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pandas as pd  # noqa: E402

import build_nationwide_202611 as bld  # noqa: E402
import download_raw_202611 as dl  # noqa: E402

SUBPROJECT_DIR = REPO_ROOT / "subprojects" / "hokenshisetsu"
PROCESSED_CSV = SUBPROJECT_DIR / "data" / "processed" / "hokenshisetsu_nationwide_202611.csv"
PREF_SUMMARY_CSV = SUBPROJECT_DIR / "output" / "prefecture_summary_202611.csv"
MUNI_SUMMARY_CSV = SUBPROJECT_DIR / "output" / "municipality_summary_202611.csv"
QA_REPORT_JSON = SUBPROJECT_DIR / "output" / "build_qa_report_202611.json"
UTF_KEN_ALL_ZIP = REPO_ROOT / "data" / "raw" / "hokenshisetsu" / "202611" / "utf_ken_all.zip"


def run_unit_tests() -> None:
    # 1. All 8 primary 7-digit code punctuation formats
    code_samples = [
        "01,1234,5",
        "123,456,7",
        "01-2345-6",
        "12-34567",
        "123,456.7",
        "０１・２３４５・６",
        "1234567",
        "123 456.7",
        1234567,
        54321,
    ]
    expected_codes = [
        "0112345",
        "1234567",
        "0123456",
        "1234567",
        "1234567",
        "0123456",
        "1234567",
        "1234567",
        "1234567",
        "0054321",
    ]
    for raw_v, exp in zip(code_samples, expected_codes):
        got = bld.extract_7digit_code(raw_v)
        assert got == exp, f"Code parse mismatch: {got} != {exp}"

    # 2. Japanese era date parsing
    assert bld.parse_japanese_date("昭47. 3. 1") == "1972-03-01"
    assert bld.parse_japanese_date("平元. 4. 1") == "1989-04-01"
    assert bld.parse_japanese_date("（令6.11.1）") == "2024-11-01"
    assert bld.parse_japanese_date("令元. 5. 1") == "2019-05-01"
    assert bld.parse_japanese_date("大15.12.25") == "1926-12-25"
    assert bld.parse_japanese_date("2026-11-01") == "2026-11-01"

    # 3. Block parser unit check: c1 parens vs JJ####, c3 postal, c4 ft/pt, c8 multi-bed + depts
    qa = bld.init_qa_stats()
    sample_block = [
        (1, "01,2345,6", "テスト医療機関", "〒０６０－０００１ 東京都千代田区テスト町1-1", "03-0000-0000", "開設テスト", "管理テスト", "平14. 5. 1", "一般 100", "特定機能"),
        (None, "（98,7654,3）", None, "テストビル2階", "常　勤: 10", None, None, "更新", "一般 20", "現存"),
        (None, "JJ0101", None, None, "(医 4)", None, None, "(令5. 5. 1)", "療養", "病院"),
        (None, None, None, None, "非常勤: 3", None, None, None, "精神科", None),
        (None, None, None, None, "(医 2) (歯 1)", None, None, None, "一般内科 歯科口腔外科", None),
    ]
    rec = bld.parse_block(
        block_rows=sample_block,
        seq_no=1,
        pref_code="13",
        bureau_code="03_kantoshinetsu",
        source_file_rel="synthetic_unit_test.xlsx",
        source_sheet="Sheet1",
        record_source_type="ika_main",
        qa_stats=qa,
    )
    assert rec["code_7digit"] == "0123456"
    assert rec["paired_code_7digit"] == "9876543"
    assert rec["old_gunshiku_code"] == "JJ0101"
    assert rec["postal_code_7digit"] == "0600001"
    assert rec["postal_code"] == "060-0001"
    assert rec["doctors_ft"] == 4
    assert rec["doctors_pt"] == 2
    assert rec["dentists_pt"] == 1
    assert rec["staff_ft_total"] == 10
    assert rec["staff_pt_total"] == 3
    assert rec["designated_date"] == "2002-05-01"
    assert rec["designation_reason"] == "更新"
    assert rec["valid_period_start"] == "2023-05-01"
    assert rec["beds_general"] == 120
    assert rec["beds_total"] == 120
    assert "精神科" in rec["departments"]
    assert "一般内科" in rec["departments"]
    assert rec["has_dental_dept_c8"] is True
    assert rec["facility_type"] == "病院"
    assert rec["hospital_special_type"] == "特定機能"
    assert rec["hospital_special_types"] == "特定機能"
    assert rec["is_tokutei_kinou_byoin"] is True
    assert rec["is_chiiki_iryo_shien_byoin"] is False
    assert rec["is_sougou_byoin"] is False
    assert rec["status"] == "現存"

    # 3b. Multi-category c9 regression tests (order-independent preservation)
    block_tc = [
        (2, "02,3456,7", "複数区分テストA", "〒100-0001 テスト", None, None, None, "令5. 1. 1", None, "地域支援"),
        (None, None, None, None, None, None, None, None, None, "特定機能"),
        (None, None, None, None, None, None, None, None, None, "現存"),
    ]
    rec_tc = bld.parse_block(block_tc, 2, "13", "03_kantoshinetsu", "t.xlsx", "Sheet1", "ika_main", qa)
    assert rec_tc["hospital_special_type"] == "特定機能"
    assert rec_tc["hospital_special_types"] == "特定機能|地域支援"
    assert rec_tc["is_tokutei_kinou_byoin"] is True
    assert rec_tc["is_chiiki_iryo_shien_byoin"] is True
    assert rec_tc["is_sougou_byoin"] is False

    block_ts = [
        (3, "03,4567,8", "複数区分テストB", "〒100-0001 テスト", None, None, None, "令5. 1. 1", None, "特定機能"),
        (None, None, None, None, None, None, None, None, None, "総合病院"),
        (None, None, None, None, None, None, None, None, None, "現存"),
    ]
    rec_ts = bld.parse_block(block_ts, 3, "13", "03_kantoshinetsu", "t.xlsx", "Sheet1", "ika_main", qa)
    assert rec_ts["hospital_special_type"] == "特定機能"
    assert rec_ts["hospital_special_types"] == "特定機能|総合病院"
    assert rec_ts["is_tokutei_kinou_byoin"] is True
    assert rec_ts["is_chiiki_iryo_shien_byoin"] is False
    assert rec_ts["is_sougou_byoin"] is True

    # 4. Kanji normalization helper check
    assert bld.norm_muni_kanji("檜山郡江差町") == "桧山郡江差町"
    assert bld.norm_muni_kanji("西津軽郡鰺ヶ沢町") == "西津軽郡鯵ケ沢町"
    assert bld.norm_muni_kanji("高岡郡檮原町") == "高岡郡梼原町"

    # 5. ZIP filename decoder tests (Unicode Extra Field vs legacy CP932) & live ZIP plan audit
    z_unicode = zipfile.ZipInfo(filename="2611（愛知医科）コード内容別医療機関一覧表.xlsx")
    z_unicode.flag_bits = 0
    assert dl.decode_zip_filename(z_unicode) == "2611（愛知医科）コード内容別医療機関一覧表.xlsx"

    z_cp932 = zipfile.ZipInfo(filename="医科_テスト.xlsx".encode("cp932").decode("cp437"))
    z_cp932.flag_bits = 0
    assert dl.decode_zip_filename(z_cp932) == "医科_テスト.xlsx"

    manifest_path = SUBPROJECT_DIR / "docs" / "download_manifest_202611.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        checked_zips = 0
        checked_extracted = 0
        for item in manifest["items"]:
            raw_p = REPO_ROOT / item["raw_file"]
            assert raw_p.exists() and dl.sha256_file(raw_p) == item["sha256"], f"Raw hash mismatch: {item['raw_file']}"
            if raw_p.suffix.lower() == ".zip":
                checked_zips += 1
                extract_root = raw_p.parent / "extracted" / raw_p.stem
                plan = dl.validate_zip_extraction_plan(raw_p, extract_root)
                for p_entry in plan:
                    if p_entry["keep"]:
                        assert p_entry["exists"], f"Missing extracted file for {p_entry['decoded']}"
                        assert p_entry["hash_matches"], f"Byte mismatch vs zip member for {p_entry['decoded']}"
                        checked_extracted += 1
            for f_meta in item["extracted_files"]:
                fp = REPO_ROOT / f_meta["path"]
                assert fp.exists() and dl.sha256_file(fp) == f_meta["sha256"], f"Extracted hash mismatch: {f_meta['path']}"
        assert checked_zips == 16, f"Expected 16 ZIP archives, checked {checked_zips}"
        assert checked_extracted == 65, f"Expected 65 kept extracted files, checked {checked_extracted}"


def load_canonical_muni_map(zip_path: Path) -> dict[str, str]:
    canon: dict[str, str] = {}
    with zipfile.ZipFile(zip_path, mode="r") as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        with zf.open(csv_names[0], mode="r") as raw_f:
            reader = csv.reader(io.TextIOWrapper(raw_f, encoding="utf-8"))
            for row in reader:
                if len(row) >= 8:
                    canon[row[0].strip()] = row[7].strip()
    return canon


def main() -> None:
    run_unit_tests()

    for path in (PROCESSED_CSV, PREF_SUMMARY_CSV, MUNI_SUMMARY_CSV, QA_REPORT_JSON):
        assert path.exists(), f"Missing output artifact: {path}"

    qa = json.loads(QA_REPORT_JSON.read_text(encoding="utf-8"))
    df = pd.read_csv(PROCESSED_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    pref_df = pd.read_csv(PREF_SUMMARY_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    muni_df = pd.read_csv(MUNI_SUMMARY_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    # 1. Exact record counts & breakdown
    total_rows = len(df)
    ika_main_cnt = int((df["record_source_type"] == "ika_main").sum())
    ikaheisetsu_cnt = int((df["record_source_type"] == "ikaheisetsu_from_shika").sum())
    assert total_rows == 97514, f"Expected 97514 rows, got {total_rows}"
    assert ika_main_cnt == 97436, f"Expected 97436 ika_main rows, got {ika_main_cnt}"
    assert ikaheisetsu_cnt == 78, f"Expected 78 ikaheisetsu_from_shika rows, got {ikaheisetsu_cnt}"

    # 2. All 47 prefectures & 8 bureaus covered
    assert df["pref_code"].nunique() == 47, f"Expected 47 prefectures, got {df['pref_code'].nunique()}"
    assert set(df["pref_code"].unique()) == {f"{i:02d}" for i in range(1, 48)}
    assert df["kouseikyoku_code"].nunique() == 8

    # 3. Primary keys & code formats
    assert df["medical_code_10digit"].str.fullmatch(r"\d{10}").all(), "Invalid medical_code_10digit format"
    assert df["medical_code_10digit"].is_unique, "Duplicate medical_code_10digit found!"
    assert df["primary_code_10digit"].str.fullmatch(r"\d{10}").all(), "Invalid primary_code_10digit format"
    assert df["primary_code_10digit"].is_unique, "Duplicate primary_code_10digit found!"

    expected_med_10 = df["pref_code"] + "1" + df["medical_code_7digit"]
    assert (df["medical_code_10digit"] == expected_med_10).all()

    ika_mask = df["record_source_type"] == "ika_main"
    shika_side_mask = df["record_source_type"] == "ikaheisetsu_from_shika"
    assert (df.loc[ika_mask, "tensu_table_code"] == "1").all()
    assert (df.loc[ika_mask, "primary_code_10digit"] == df.loc[ika_mask, "medical_code_10digit"]).all()
    assert (df.loc[shika_side_mask, "tensu_table_code"] == "3").all()
    assert (df.loc[shika_side_mask, "paired_code_7digit"] != "").all(), "ikaheisetsu missing paired_code"
    assert (df.loc[shika_side_mask, "dental_code_10digit"] == df.loc[shika_side_mask, "primary_code_10digit"]).all()

    # 4. Cross-checks from QA report
    assert len(qa["cross_checks"]["seq_mismatches"]) == 0, f"Seq mismatches: {qa['cross_checks']['seq_mismatches']}"
    assert qa["cross_checks"]["ika_main_intra_pref_duplicate_code7"] == 0
    assert qa["cross_checks"]["shikaheisetsu_total_keys"] == 1139
    assert qa["cross_checks"]["shikaheisetsu_not_in_ika_main"] == 0
    assert qa["cross_checks"]["ikaheisetsu_missing_paired_code"] == 0
    assert qa["cross_checks"]["hokkaido_zentai_symmetric_diff"] == 0
    assert int((df["in_shikaheisetsu_list"] == "True").sum()) == 1139

    # 5. Municipality coverage & validity against Japan Post utf_ken_all.zip
    canon_muni = load_canonical_muni_map(UTF_KEN_ALL_ZIP)
    assert df["municipality_code_5digit"].str.fullmatch(r"\d{5}").all(), "Unmatched or invalid municipality_code_5digit"
    assert (df["municipality_name"] != "").all(), "Empty municipality_name found"
    assert (df["municipality_match_method"] != "unmatched").all()
    assert (df["municipality_code_5digit"].str[:2] == df["pref_code"]).all(), "Municipality pref_code mismatch"
    assert (df["municipality_code_5digit"].map(canon_muni) == df["municipality_name"]).all(), "Municipality name mismatch vs utf_ken_all"

    # 6. Facility type, hospital classification (multi-label + flags), & status consistency
    assert len(df.columns) == 64, f"Expected 64 columns, got {len(df.columns)}"
    assert df["facility_type"].isin(["病院", "診療所"]).all(), "Unexpected facility_type"
    hosp_mask = df["facility_type"] == "病院"
    clin_mask = df["facility_type"] == "診療所"
    assert df.loc[hosp_mask, "hospital_special_type"].isin(["一般病院等", "特定機能", "地域支援", "総合病院"]).all()
    assert (df.loc[hosp_mask, "hospital_special_types"] != "").all()
    assert (df.loc[clin_mask, "hospital_special_type"] == "").all()
    assert (df.loc[clin_mask, "hospital_special_types"] == "").all()
    for bcol in ("is_tokutei_kinou_byoin", "is_chiiki_iryo_shien_byoin", "is_sougou_byoin"):
        assert (df.loc[clin_mask, bcol] == "False").all()
    assert df["status"].isin(["現存", "休止", "辞退"]).all(), "Unexpected or missing status"

    tokutei_cnt = int((df["is_tokutei_kinou_byoin"] == "True").sum())
    shien_cnt = int((df["is_chiiki_iryo_shien_byoin"] == "True").sum())
    sougou_cnt = int((df["is_sougou_byoin"] == "True").sum())
    multi_special_cnt = int(
        (
            (df["is_tokutei_kinou_byoin"] == "True").astype(int)
            + (df["is_chiiki_iryo_shien_byoin"] == "True").astype(int)
            + (df["is_sougou_byoin"] == "True").astype(int)
            >= 2
        ).sum()
    )
    assert tokutei_cnt == 88, f"Expected 88 特定機能病院, got {tokutei_cnt}"
    assert shien_cnt == 173, f"Expected 173 地域医療支援病院, got {shien_cnt}"
    assert sougou_cnt == 47, f"Expected 47 総合病院, got {sougou_cnt}"
    assert multi_special_cnt == 7, f"Expected 7 multi-category hospitals, got {multi_special_cnt}"

    audit_conflicts = REPO_ROOT / "data" / "interim" / "hokenshisetsu_audit_20261117" / "special_type_conflicts.csv"
    if audit_conflicts.exists():
        conf_df = pd.read_csv(audit_conflicts, dtype=str, keep_default_na=False)
        for _, crow in conf_df.iterrows():
            m = df[
                (df["source_file"] == crow["source_file"])
                & (df["source_sheet"] == crow["source_sheet"])
                & (df["seq_no"] == crow["seq_no"])
            ]
            assert len(m) == 1
            raw_cats = set(crow["raw_categories"].split("|"))
            kept_cats = set(m["hospital_special_types"].iloc[0].split("|"))
            assert raw_cats == kept_cats, f"Mismatch on conflict row: {raw_cats} vs {kept_cats}"

    # 7. Bed arithmetic invariants
    bed_cols = ["beds_general", "beds_ryoyo", "beds_seishin", "beds_kekkaku", "beds_kansen"]
    bed_sum_calc = sum(df[c].astype(int) for c in bed_cols)
    bed_total = df["beds_total"].astype(int)
    assert (bed_sum_calc == bed_total).all(), "beds_total arithmetic mismatch"
    for c in bed_cols + ["beds_total"]:
        assert (df[c].astype(int) >= 0).all()

    # 8. Date ISO format validation where non-empty
    for dcol in ("designated_date", "valid_period_start"):
        non_empty = df.loc[df[dcol] != "", dcol]
        assert non_empty.str.fullmatch(r"\d{4}-\d{2}-\d{2}").all(), f"Invalid ISO date in {dcol}"

    # 9. Summary table reconciliation
    assert len(pref_df) == 47
    assert pref_df["total_facilities"].astype(int).sum() == total_rows
    assert pref_df["ika_main_count"].astype(int).sum() == ika_main_cnt
    assert pref_df["ikaheisetsu_from_shika_count"].astype(int).sum() == ikaheisetsu_cnt
    assert pref_df["tokutei_kinou_byoin_count"].astype(int).sum() == 88
    assert pref_df["chiiki_iryo_shien_byoin_count"].astype(int).sum() == 173
    assert pref_df["sougou_byoin_count"].astype(int).sum() == 47
    assert pref_df["beds_total_sum"].astype(int).sum() == int(bed_total.sum())

    assert muni_df["total_facilities"].astype(int).sum() == total_rows
    assert muni_df["ika_main_count"].astype(int).sum() == ika_main_cnt
    assert muni_df["ikaheisetsu_from_shika_count"].astype(int).sum() == ikaheisetsu_cnt
    assert muni_df["tokutei_kinou_byoin_count"].astype(int).sum() == 88
    assert muni_df["chiiki_iryo_shien_byoin_count"].astype(int).sum() == 173
    assert muni_df["sougou_byoin_count"].astype(int).sum() == 47
    assert muni_df["beds_total_sum"].astype(int).sum() == int(bed_total.sum())

    print(
        json.dumps(
            {
                "verification_status": "ALL_CHECKS_PASSED",
                "unit_tests": "PASSED",
                "total_rows": total_rows,
                "total_columns": len(df.columns),
                "ika_main_rows": ika_main_cnt,
                "ikaheisetsu_from_shika_rows": ikaheisetsu_cnt,
                "prefectures_verified": int(pref_df["pref_code"].nunique()),
                "municipalities_verified": int(df["municipality_code_5digit"].nunique()),
                "hospitals": int(hosp_mask.sum()),
                "clinics": int(clin_mask.sum()),
                "tokutei_kinou_byoin": tokutei_cnt,
                "chiiki_iryo_shien_byoin": shien_cnt,
                "sougou_byoin": sougou_cnt,
                "multiple_special_category_hospitals": multi_special_cnt,
                "beds_total_sum": int(bed_total.sum()),
                "shika_heisetsu_strict": int((df["is_shika_heisetsu"] == "True").sum()),
                "shika_heisetsu_or_dental_dept": int((df["is_shika_heisetsu_or_dental_dept"] == "True").sum()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
