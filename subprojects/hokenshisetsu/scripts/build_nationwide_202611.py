"""Build nationwide Japanese insured medical institution dataset (2026-11 snapshot).

Inputs (read-only from parent repo data/raw/hokenshisetsu/202611/):
  - 8 regional Kouseikyoku directories (47 prefectures; 70 Layout-A files + 1 Hokkaido QA file)
  - Japan Post utf_ken_all.zip (utf_ken_all.csv) for 5-digit JIS X 0402 municipality resolution

Outputs (to subprojects/hokenshisetsu/):
  - data/processed/hokenshisetsu_nationwide_202611.csv
  - output/prefecture_summary_202611.csv
  - output/municipality_summary_202611.csv
  - output/build_qa_report_202611.json

Privacy & safety rules:
  - Never modify, move, or delete files under data/raw/.
  - Never print or log row-level facility names, owner/manager names, addresses,
    phone numbers, or raw institution codes to stdout or JSON QA reports.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
MY_PACKAGES = REPO_ROOT / "my_packages"
if str(MY_PACKAGES) not in sys.path:
    sys.path.append(str(MY_PACKAGES))

import openpyxl  # noqa: E402
import pandas as pd  # noqa: E402

RAW_BASE = REPO_ROOT / "data" / "raw" / "hokenshisetsu" / "202611"
UTF_KEN_ALL_ZIP = RAW_BASE / "utf_ken_all.zip"

SUBPROJECT_DIR = REPO_ROOT / "subprojects" / "hokenshisetsu"
PROCESSED_CSV = SUBPROJECT_DIR / "data" / "processed" / "hokenshisetsu_nationwide_202611.csv"
PREF_SUMMARY_CSV = SUBPROJECT_DIR / "output" / "prefecture_summary_202611.csv"
MUNI_SUMMARY_CSV = SUBPROJECT_DIR / "output" / "municipality_summary_202611.csv"
QA_REPORT_JSON = SUBPROJECT_DIR / "output" / "build_qa_report_202611.json"

SNAPSHOT_DATE = "2026-11-01"

BUREAU_NAMES: dict[str, str] = {
    "01_hokkaido": "北海道厚生局",
    "02_tohoku": "東北厚生局",
    "03_kantoshinetsu": "関東信越厚生局",
    "04_tokaihokuriku": "東海北陸厚生局",
    "05_kinki": "近畿厚生局",
    "06_chugokushikoku": "中国四国厚生局",
    "07_shikoku": "四国厚生支局",
    "08_kyushu": "九州厚生局",
}

PREF_CODE_TO_FULL_NAME: dict[str, str] = {
    "01": "北海道",
    "02": "青森県",
    "03": "岩手県",
    "04": "宮城県",
    "05": "秋田県",
    "06": "山形県",
    "07": "福島県",
    "08": "茨城県",
    "09": "栃木県",
    "10": "群馬県",
    "11": "埼玉県",
    "12": "千葉県",
    "13": "東京都",
    "14": "神奈川県",
    "15": "新潟県",
    "16": "富山県",
    "17": "石川県",
    "18": "福井県",
    "19": "山梨県",
    "20": "長野県",
    "21": "岐阜県",
    "22": "静岡県",
    "23": "愛知県",
    "24": "三重県",
    "25": "滋賀県",
    "26": "京都府",
    "27": "大阪府",
    "28": "兵庫県",
    "29": "奈良県",
    "30": "和歌山県",
    "31": "鳥取県",
    "32": "島根県",
    "33": "岡山県",
    "34": "広島県",
    "35": "山口県",
    "36": "徳島県",
    "37": "香川県",
    "38": "愛媛県",
    "39": "高知県",
    "40": "福岡県",
    "41": "佐賀県",
    "42": "長崎県",
    "43": "熊本県",
    "44": "大分県",
    "45": "宮崎県",
    "46": "鹿児島県",
    "47": "沖縄県",
}

PREF_SHORT_TO_CODE: dict[str, str] = {
    (name[:-1] if name != "北海道" else name): code
    for code, name in PREF_CODE_TO_FULL_NAME.items()
}

ROMAN_PREF_TO_CODE: dict[str, str] = {
    "hokkaido": "01",
    "aomori": "02",
    "iwate": "03",
    "miyagi": "04",
    "akita": "05",
    "yamagata": "06",
    "fukushima": "07",
    "ibaraki": "08",
    "tochigi": "09",
    "gunma": "10",
    "saitama": "11",
    "chiba": "12",
    "tokyo": "13",
    "kanagawa": "14",
    "niigata": "15",
    "toyama": "16",
    "ishikawa": "17",
    "fukui": "18",
    "yamanashi": "19",
    "nagano": "20",
    "gifu": "21",
    "shizuoka": "22",
    "aichi": "23",
    "mie": "24",
    "shiga": "25",
    "kyoto": "26",
    "osaka": "27",
    "hyogo": "28",
    "nara": "29",
    "wakayama": "30",
    "tottori": "31",
    "shimane": "32",
    "okayama": "33",
    "hiroshima": "34",
    "yamaguchi": "35",
    "tokushima": "36",
    "kagawa": "37",
    "ehime": "38",
    "kochi": "39",
    "fukuoka": "40",
    "saga": "41",
    "nagasaki": "42",
    "kumamoto": "43",
    "oita": "44",
    "ooita": "44",
    "miyazaki": "45",
    "kagoshima": "46",
    "okinawa": "47",
}

ERA_BASE_YEAR: dict[str, int] = {
    "明": 1867,
    "明治": 1867,
    "大": 1911,
    "大正": 1911,
    "昭": 1925,
    "昭和": 1925,
    "平": 1988,
    "平成": 1988,
    "令": 2018,
    "令和": 2018,
}

DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")
MUNI_KANJI_TRANS = str.maketrans(
    {
        "ヶ": "ケ",
        "ヵ": "カ",
        "ノ": "之",
        "檜": "桧",
        "鰺": "鯵",
        "竈": "釜",
        "嶋": "島",
        "檮": "梼",
        "惠": "恵",
        "髙": "高",
        "﨑": "崎",
        "濱": "浜",
        "櫻": "桜",
        "澤": "沢",
        "齊": "斉",
        "齋": "斎",
        "廣": "広",
        "龍": "竜",
        "壽": "寿",
        "萬": "万",
        "國": "国",
        "氣": "気",
        "眞": "真",
        "邊": "辺",
        "邉": "辺",
    }
)

BED_VALUE_RE = re.compile(
    r"^(一般|療養|精神|結核|感染症?)(?:[（(][^）)]*[）)])?\s*[:：]?\s*([\d,]+)(?:\s*床)?$"
)
BED_LABEL_ONLY_RE = re.compile(
    r"^(一般|療養|精神|結核|感染症?)(?:\s*[（(][^）)]*[）)])?$"
)
POSTAL_LINE_RE = re.compile(r"^[〒\s]*(\d{3})\s*[-−ー‐―－–—]?\s*(\d{4})\s*(.*)$")
ERA_DATE_RE = re.compile(
    r"^[（(]?\s*(令和|平成|昭和|大正|明治|[令平昭大明])\s*(元|\d{1,2})\s*[.．/年\-]\s*(\d{1,2})\s*[.．/月\-]\s*(\d{1,2})\s*日?\s*[)）]?$"
)
ISO_DATE_RE = re.compile(r"^[（(]?\s*(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})\s*[)）]?$")
STAFF_ITEM_RE = re.compile(r"[（(]\s*([医歯薬])\s*([\d,]+)\s*[)）]")

OUTPUT_COLUMNS = [
    "medical_code_10digit",
    "medical_code_7digit",
    "primary_code_10digit",
    "tensu_table_code",
    "code_7digit",
    "code_raw",
    "paired_code_7digit",
    "paired_code_10digit",
    "dental_code_7digit",
    "dental_code_10digit",
    "old_gunshiku_code",
    "record_source_type",
    "is_ikaheisetsu_from_shika",
    "kouseikyoku_code",
    "kouseikyoku_name",
    "pref_code",
    "pref_name",
    "postal_code_7digit",
    "postal_code",
    "municipality_code_5digit",
    "municipality_name",
    "municipality_match_method",
    "is_shika_heisetsu",
    "is_shika_heisetsu_or_dental_dept",
    "has_paired_code_c1",
    "in_shikaheisetsu_list",
    "has_dental_dept_c8",
    "facility_type",
    "hospital_special_type",
    "hospital_special_types",
    "is_tokutei_kinou_byoin",
    "is_chiiki_iryo_shien_byoin",
    "is_sougou_byoin",
    "status",
    "has_ryoyo_byosho_note",
    "facility_name",
    "address",
    "phone",
    "owner_name",
    "manager_name",
    "designated_date",
    "designation_reason",
    "valid_period_start",
    "beds_general",
    "beds_ryoyo",
    "beds_seishin",
    "beds_kekkaku",
    "beds_kansen",
    "beds_total",
    "has_bed_entry",
    "has_staff_info",
    "doctors_ft",
    "doctors_pt",
    "dentists_ft",
    "dentists_pt",
    "pharmacists_ft",
    "pharmacists_pt",
    "staff_ft_total",
    "staff_pt_total",
    "departments",
    "snapshot_date",
    "source_file",
    "source_sheet",
    "seq_no",
]


def norm_str(v: Any) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def normalize_digits(s: str) -> str:
    return s.translate(DIGIT_TRANS)


def norm_muni_kanji(s: str) -> str:
    s_no_ws = re.sub(r"\s+", "", s)
    return s_no_ws.translate(MUNI_KANJI_TRANS)


def join_wrapped_jp(parts: list[str]) -> str:
    """Join wrapped Excel text lines without inserting spaces inside Japanese words."""
    cleaned = [p.strip() for p in parts if p and p.strip()]
    if not cleaned:
        return ""
    out = cleaned[0]
    for nxt in cleaned[1:]:
        if out[-1].isascii() and out[-1].isalnum() and nxt[0].isascii() and nxt[0].isalnum():
            out += " " + nxt
        else:
            out += nxt
    return out


def extract_7digit_code(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, int):
        s = str(v)
    elif isinstance(v, float) and v.is_integer():
        s = str(int(v))
    else:
        s = normalize_digits(norm_str(v))
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 7:
        return digits
    if 5 <= len(digits) < 7:
        return digits.zfill(7)
    if len(digits) == 10:
        return digits[3:]
    return None


def mask_shape(v: Any) -> str:
    s = normalize_digits(norm_str(v))
    if not s:
        return "<EMPTY>"
    masked = re.sub(r"\d", "#", s)
    masked = re.sub(r"[^\x00-\x7F]", "J", masked)
    return masked[:40]


def infer_file_role(xf: Path, bureau_code: str) -> str:
    rel_low = unicodedata.normalize("NFKC", xf.relative_to(RAW_BASE).as_posix().lower())
    name_low = unicodedata.normalize("NFKC", xf.name.lower())
    # Check 歯科併設 BEFORE 医科併設 because "ikaheisetsu" is a substring of "shikaheisetsu"
    if "shikaheisetsu" in rel_low or "sikaheisetu" in rel_low or "歯科併設" in rel_low:
        return "shikaheisetsu"
    if "ikaheisetsu" in rel_low or "ikaheisetu" in rel_low or "医科併設" in rel_low:
        return "ikaheisetsu"
    if bureau_code == "01_hokkaido":
        if "zentai" in name_low:
            return "ika_zentai"
        if "byoin" in name_low:
            return "ika_byoin"
        if "shinryojo" in name_low or "shinryou" in name_low:
            return "ika_shinryojo"
    return "ika"


def infer_pref_code(xf: Path, sheet_name: str, bureau_code: str) -> Optional[str]:
    # 1. Sheet name exact/partial Japanese match (covers 02_tohoku multi-sheet workbooks)
    if sheet_name and sheet_name != "Sheet1":
        for pref_short, pcode in sorted(PREF_SHORT_TO_CODE.items(), key=lambda x: len(x[0]), reverse=True):
            if pref_short in sheet_name:
                return pcode

    # 2. Relative path excluding bureau directory
    rel_parts = xf.relative_to(RAW_BASE).parts
    search_targets = [xf.stem] + [p for p in rel_parts[1:] if p != xf.name]

    for target in search_targets:
        for pref_short, pcode in sorted(PREF_SHORT_TO_CODE.items(), key=lambda x: len(x[0]), reverse=True):
            if pref_short in target:
                return pcode

    for target in search_targets:
        t_low = target.lower()
        for roman, pcode in sorted(ROMAN_PREF_TO_CODE.items(), key=lambda x: len(x[0]), reverse=True):
            if re.search(rf"(?:^|[^a-z]){re.escape(roman)}(?:[^a-z]|$)", t_low):
                return pcode

    if bureau_code == "01_hokkaido":
        return "01"
    return None


def parse_japanese_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = normalize_digits(norm_str(v))
    if not s:
        return None

    m = ERA_DATE_RE.match(s)
    if m:
        era, yr_tok, mo_s, dy_s = m.groups()
        base = ERA_BASE_YEAR.get(era)
        if base is None:
            return None
        yr = 1 if yr_tok == "元" else int(yr_tok)
        yyyy = base + yr
        mm = int(mo_s)
        dd = int(dy_s)
        try:
            return dt.date(yyyy, mm, dd).isoformat()
        except ValueError:
            return None

    m_iso = ISO_DATE_RE.match(s)
    if m_iso:
        yyyy, mm, dd = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return dt.date(yyyy, mm, dd).isoformat()
        except ValueError:
            return None

    return None


def parse_seq_int(c0: Any) -> Optional[int]:
    if c0 is None or isinstance(c0, bool):
        return None
    if isinstance(c0, int):
        return c0 if c0 > 0 else None
    if isinstance(c0, float):
        return int(c0) if c0.is_integer() and c0 > 0 else None
    s = normalize_digits(norm_str(c0))
    if re.fullmatch(r"\d+", s):
        val = int(s)
        return val if val > 0 else None
    return None


def is_page_header_or_footer_row(row: tuple[Any, ...]) -> bool:
    c0_s = normalize_digits(norm_str(row[0] if len(row) > 0 else ""))
    if c0_s in ("計", "合計", "小計", "項番"):
        return True
    if any(kw in c0_s for kw in ("コード内容別", "抽出条件", "作成日", "現在")):
        return True
    if c0_s.startswith("令和") or c0_s.startswith("頁"):
        return True
    c1_s = norm_str(row[1] if len(row) > 1 else "")
    if c0_s in ("項番", "番号") and "医療機関" in c1_s:
        return True
    return False


class MunicipalityResolver:
    """Resolve 5-digit JIS X 0402 municipality code and name from Japan Post utf_ken_all.zip."""

    def __init__(self, zip_path: Path) -> None:
        self.zip_path = zip_path
        self.zip_to_munis: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
        self.muni_code_to_name: dict[tuple[str, str], str] = {}
        self.pref_patterns: dict[str, list[tuple[str, str, str]]] = {}
        self.raw_rows_count = 0
        self.unique_zip_count = 0
        self.unique_muni_count = 0
        self.pattern_collision_count = 0
        self._load()

    def _load(self) -> None:
        if not self.zip_path.exists():
            raise FileNotFoundError(f"Missing Japan Post postal archive: {self.zip_path}")

        full_name_to_code: dict[str, str] = {v: k for k, v in PREF_CODE_TO_FULL_NAME.items()}
        pref_munis: dict[str, dict[str, str]] = defaultdict(dict)
        seen_zips: set[str] = set()

        with zipfile.ZipFile(self.zip_path, mode="r") as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError("No CSV found inside utf_ken_all.zip")
            with zf.open(csv_names[0], mode="r") as raw_f:
                reader = csv.reader(io.TextIOWrapper(raw_f, encoding="utf-8"))
                for row in reader:
                    if len(row) < 8:
                        continue
                    self.raw_rows_count += 1
                    mcode = row[0].strip()
                    zip7 = row[2].strip()
                    pname = row[6].strip()
                    mname = row[7].strip()
                    pcode = full_name_to_code.get(pname)
                    if not pcode or len(mcode) != 5 or len(zip7) != 7:
                        continue
                    seen_zips.add(zip7)
                    self.zip_to_munis[(pcode, zip7)][mcode] = mname
                    self.muni_code_to_name[(pcode, mcode)] = mname
                    pref_munis[pcode][mcode] = mname

        self.unique_zip_count = len(seen_zips)
        self.unique_muni_count = len(self.muni_code_to_name)

        # Build per-prefecture longest-first prefix match patterns
        for pcode, munis in pref_munis.items():
            pattern_map: dict[str, list[tuple[str, str]]] = defaultdict(list)
            bare_counts: Counter[str] = Counter()

            for mcode, mname in munis.items():
                norm_full = norm_muni_kanji(mname)
                pattern_map[norm_full].append((mcode, mname))
                if "郡" in mname:
                    bare = mname.split("郡", 1)[1]
                    bare_counts[norm_muni_kanji(bare)] += 1

            # Check full-name pattern collisions within prefecture
            patterns_list: list[tuple[str, str, str]] = []
            seen_pat: set[str] = set()
            for pat, cands in pattern_map.items():
                if len(cands) > 1:
                    self.pattern_collision_count += 1
                    continue
                mcode, mname = cands[0]
                patterns_list.append((pat, mcode, mname))
                seen_pat.add(pat)

            # Register unique bare town/village names without 郡 prefix
            for mcode, mname in munis.items():
                if "郡" in mname:
                    bare_norm = norm_muni_kanji(mname.split("郡", 1)[1])
                    if bare_counts[bare_norm] == 1 and bare_norm not in seen_pat:
                        patterns_list.append((bare_norm, mcode, mname))
                        seen_pat.add(bare_norm)

            patterns_list.sort(key=lambda x: len(x[0]), reverse=True)
            self.pref_patterns[pcode] = patterns_list

    def resolve(
        self, pref_code: str, postal_code_7digit: str, address: str
    ) -> tuple[str, str, str]:
        """Return (municipality_code_5digit, municipality_name, match_method)."""
        pref_name = PREF_CODE_TO_FULL_NAME.get(pref_code, "")
        addr_clean = re.sub(r"\s+", "", address)
        if pref_name and addr_clean.startswith(pref_name):
            addr_body = addr_clean[len(pref_name):]
        else:
            addr_body = addr_clean
        addr_norm = norm_muni_kanji(addr_body)

        addr_match: Optional[tuple[str, str]] = None
        for pat, mcode, mname in self.pref_patterns.get(pref_code, []):
            if addr_norm.startswith(pat):
                addr_match = (mcode, mname)
                break

        zip_cands = (
            self.zip_to_munis.get((pref_code, postal_code_7digit), {})
            if postal_code_7digit
            else {}
        )

        if len(zip_cands) == 1 and addr_match is not None:
            z_code, z_name = next(iter(zip_cands.items()))
            if z_code == addr_match[0]:
                return z_code, z_name, "both_agree_single_zip"
            return addr_match[0], addr_match[1], "addr_prefix_overrides_zip_mismatch"

        if len(zip_cands) > 1 and addr_match is not None:
            if addr_match[0] in zip_cands:
                return addr_match[0], addr_match[1], "both_agree_multi_zip_disambiguated"
            return addr_match[0], addr_match[1], "addr_prefix_overrides_zip_mismatch"

        if addr_match is not None:
            return addr_match[0], addr_match[1], "addr_prefix_jigyosho_or_unlisted_zip"

        if len(zip_cands) == 1:
            z_code, z_name = next(iter(zip_cands.items()))
            return z_code, z_name, "zip_only_resolved"

        if len(zip_cands) > 1:
            z_code = sorted(zip_cands.keys())[0]
            return z_code, zip_cands[z_code], "zip_multi_fallback"

        return "", "", "unmatched"


def parse_block(
    block_rows: list[tuple[Any, ...]],
    seq_no: int,
    pref_code: str,
    bureau_code: str,
    source_file_rel: str,
    source_sheet: str,
    record_source_type: str,
    qa_stats: dict[str, Any],
) -> dict[str, Any]:
    row0 = block_rows[0]

    def cell(r: tuple[Any, ...], idx: int) -> Any:
        return r[idx] if idx < len(r) else None

    # c1: primary code, optional paired code in parentheses, optional old district code
    c1_raw = norm_str(cell(row0, 1))
    code_7digit = extract_7digit_code(cell(row0, 1))
    if not code_7digit:
        qa_stats["missing_primary_code_count"] += 1
        code_7digit = ""

    paired_candidates: list[str] = []
    old_gunshiku_codes: list[str] = []
    for r in block_rows[1:]:
        s = normalize_digits(norm_str(cell(r, 1)))
        if not s:
            continue
        if s.startswith(("(", "（")) and s.endswith((")", "）")):
            p7 = extract_7digit_code(s)
            if p7:
                paired_candidates.append(p7)
            else:
                qa_stats["c1_paren_unparsed_patterns"][mask_shape(s)] += 1
        else:
            if "併設" in s:
                qa_stats["c1_unparen_heisetsu_count"] += 1
            old_gunshiku_codes.append(s)

    if len(paired_candidates) > 1:
        qa_stats["multi_paired_code_blocks"] += 1
    paired_code_7digit = paired_candidates[0] if paired_candidates else ""
    old_gunshiku_code = "|".join(old_gunshiku_codes)

    # c2: facility name
    facility_name = join_wrapped_jp([norm_str(cell(r, 2)) for r in block_rows])

    # c3: postal code & address
    c3_lines = [norm_str(cell(r, 3)) for r in block_rows if norm_str(cell(r, 3))]
    postal_code_7digit = ""
    postal_code = ""
    addr_lines: list[str] = []
    if c3_lines:
        line0_digits = normalize_digits(c3_lines[0])
        m_post = POSTAL_LINE_RE.match(line0_digits)
        if m_post:
            p1, p2, rest = m_post.groups()
            postal_code_7digit = p1 + p2
            postal_code = f"{p1}-{p2}"
            if rest.strip():
                # Keep original kanji characters after postal prefix if any non-digits normalized
                # Note DIGIT_TRANS only translates ０-９ -> 0-9, preserving kanji intact.
                addr_lines.append(rest.strip())
            addr_lines.extend(c3_lines[1:])
        else:
            qa_stats["missing_postal_code_count"] += 1
            addr_lines = c3_lines
    else:
        qa_stats["missing_postal_code_count"] += 1
    address = join_wrapped_jp(addr_lines)

    # c4: phone & staffing counts
    phone = ""
    staff_mode: Optional[str] = None
    has_staff_info = False
    staff_ft_total: Optional[int] = None
    staff_pt_total: Optional[int] = None
    doctors_ft: Optional[int] = None
    doctors_pt: Optional[int] = None
    dentists_ft: Optional[int] = None
    dentists_pt: Optional[int] = None
    pharmacists_ft: Optional[int] = None
    pharmacists_pt: Optional[int] = None

    c4_items = [cell(r, 4) for r in block_rows if norm_str(cell(r, 4))]
    for idx_c4, raw_c4 in enumerate(c4_items):
        s_c4 = normalize_digits(norm_str(raw_c4))
        s_nosp = re.sub(r"\s+", "", s_c4)

        is_pt_hdr = "非常勤" in s_nosp
        is_ft_hdr = (not is_pt_hdr) and ("常勤" in s_nosp)

        if idx_c4 == 0 and not is_pt_hdr and not is_ft_hdr and not STAFF_ITEM_RE.search(s_c4):
            phone = s_c4
            continue

        if is_pt_hdr:
            has_staff_info = True
            staff_mode = "pt"
            dentists_pt = dentists_pt if dentists_pt is not None else 0
            doctors_pt = doctors_pt if doctors_pt is not None else 0
            pharmacists_pt = pharmacists_pt if pharmacists_pt is not None else 0
            m_tot = re.search(r"非常勤[:：]?([\d,]+)", s_nosp)
            if m_tot:
                staff_pt_total = (staff_pt_total or 0) + int(m_tot.group(1).replace(",", ""))
        elif is_ft_hdr:
            has_staff_info = True
            staff_mode = "ft"
            doctors_ft = doctors_ft if doctors_ft is not None else 0
            dentists_ft = dentists_ft if dentists_ft is not None else 0
            pharmacists_ft = pharmacists_ft if pharmacists_ft is not None else 0
            m_tot = re.search(r"常勤[:：]?([\d,]+)", s_nosp)
            if m_tot:
                staff_ft_total = (staff_ft_total or 0) + int(m_tot.group(1).replace(",", ""))

        matches = STAFF_ITEM_RE.findall(s_c4)
        if matches:
            has_staff_info = True
            if staff_mode is None:
                qa_stats["staff_item_without_mode_count"] += 1
            else:
                for role_ch, num_s in matches:
                    val_n = int(num_s.replace(",", ""))
                    if staff_mode == "ft":
                        if role_ch == "医":
                            doctors_ft = (doctors_ft or 0) + val_n
                        elif role_ch == "歯":
                            dentists_ft = (dentists_ft or 0) + val_n
                        elif role_ch == "薬":
                            pharmacists_ft = (pharmacists_ft or 0) + val_n
                    else:
                        if role_ch == "医":
                            doctors_pt = (doctors_pt or 0) + val_n
                        elif role_ch == "歯":
                            dentists_pt = (dentists_pt or 0) + val_n
                        elif role_ch == "薬":
                            pharmacists_pt = (pharmacists_pt or 0) + val_n
        elif not is_pt_hdr and not is_ft_hdr:
            # Phone wrapped or unknown pattern
            if not phone and re.match(r"^[\d\-()]+$", s_nosp):
                phone = s_c4
            else:
                qa_stats["c4_unknown_patterns"][mask_shape(s_c4)] += 1

    # c5: owner name, c6: manager name
    owner_name = join_wrapped_jp([norm_str(cell(r, 5)) for r in block_rows])
    manager_name = join_wrapped_jp([norm_str(cell(r, 6)) for r in block_rows])

    # c7: designated_date, designation_reason, valid_period_start
    designated_date = ""
    valid_period_start = ""
    reasons: list[str] = []
    c7_items = [cell(r, 7) for r in block_rows if norm_str(cell(r, 7))]
    for idx_c7, raw_c7 in enumerate(c7_items):
        parsed_dt = parse_japanese_date(raw_c7)
        s_c7 = normalize_digits(norm_str(raw_c7))
        if idx_c7 == 0 and parsed_dt:
            designated_date = parsed_dt
        elif parsed_dt:
            if not valid_period_start:
                valid_period_start = parsed_dt
            else:
                qa_stats["c7_extra_date_count"] += 1
        elif s_c7:
            reasons.append(s_c7)
    designation_reason = "|".join(reasons)
    if designation_reason:
        qa_stats["designation_reason_dist"][designation_reason] += 1
    if not designated_date:
        qa_stats["missing_designated_date_count"] += 1

    # c8: beds & departments
    beds_general = 0
    beds_ryoyo = 0
    beds_seishin = 0
    beds_kekkaku = 0
    beds_kansen = 0
    has_bed_entry = False
    dept_lines: list[str] = []

    for r in block_rows:
        s_c8 = normalize_digits(norm_str(cell(r, 8)))
        if not s_c8:
            continue
        m_bed = BED_VALUE_RE.match(s_c8)
        if m_bed:
            has_bed_entry = True
            b_kind, b_num_s = m_bed.groups()
            b_num = int(b_num_s.replace(",", ""))
            if b_kind == "一般":
                beds_general += b_num
            elif b_kind == "療養":
                beds_ryoyo += b_num
            elif b_kind == "精神":
                beds_seishin += b_num
            elif b_kind == "結核":
                beds_kekkaku += b_num
            elif b_kind.startswith("感染"):
                beds_kansen += b_num
            continue
        if BED_LABEL_ONLY_RE.match(s_c8):
            has_bed_entry = True
            continue
        dept_lines.append(s_c8)

    beds_total = beds_general + beds_ryoyo + beds_seishin + beds_kekkaku + beds_kansen
    departments = " ".join(dept_lines)
    has_dental_dept_c8 = ("歯" in departments) or ("口腔" in departments)

    # c9: facility_type, hospital special categories (multi-label + priority representative), status, ryoyo note
    SPECIAL_CAT_ORDER = ("特定機能", "地域支援", "総合病院")
    facility_type = ""
    special_found: set[str] = set()
    status = ""
    has_ryoyo_byosho_note = False

    c9_lines = [normalize_digits(norm_str(cell(r, 9))) for r in block_rows if norm_str(cell(r, 9))]
    for s_c9 in c9_lines:
        if "療養病床" in s_c9:
            has_ryoyo_byosho_note = True
        if s_c9 == "診療所":
            if not facility_type:
                facility_type = "診療所"
        elif s_c9 in SPECIAL_CAT_ORDER:
            facility_type = "病院"
            special_found.add(s_c9)
        elif s_c9 == "病院":
            if not facility_type:
                facility_type = "病院"
        elif s_c9 == "休止":
            status = "休止"
        elif s_c9 == "辞退":
            status = "辞退"
        elif s_c9 == "現存":
            if status not in ("休止", "辞退"):
                status = "現存"
        elif s_c9 in ("療養病床",):
            pass
        else:
            qa_stats["c9_unknown_patterns"][mask_shape(s_c9)] += 1

    is_tokutei_kinou_byoin = "特定機能" in special_found
    is_chiiki_iryo_shien_byoin = "地域支援" in special_found
    is_sougou_byoin = "総合病院" in special_found
    ordered_specials = [c for c in SPECIAL_CAT_ORDER if c in special_found]

    if facility_type == "病院":
        if ordered_specials:
            # Deterministic priority order for single representative column: 特定機能 > 地域支援 > 総合病院
            hospital_special_type = ordered_specials[0]
            hospital_special_types = "|".join(ordered_specials)
        else:
            hospital_special_type = "一般病院等"
            hospital_special_types = "一般病院等"
    else:
        hospital_special_type = ""
        hospital_special_types = ""

    if not facility_type:
        qa_stats["missing_facility_type_count"] += 1
    if not status:
        qa_stats["missing_status_count"] += 1

    # 10-digit code derivation
    if record_source_type == "ika_main":
        tensu_table_code = "1"
        medical_code_7digit = code_7digit
        dental_code_7digit = paired_code_7digit
        paired_code_10digit = (
            f"{pref_code}3{paired_code_7digit}" if paired_code_7digit else ""
        )
    else:
        tensu_table_code = "3"
        dental_code_7digit = code_7digit
        medical_code_7digit = paired_code_7digit
        paired_code_10digit = (
            f"{pref_code}1{paired_code_7digit}" if paired_code_7digit else ""
        )

    primary_code_10digit = f"{pref_code}{tensu_table_code}{code_7digit}" if code_7digit else ""
    medical_code_10digit = (
        f"{pref_code}1{medical_code_7digit}" if medical_code_7digit else ""
    )
    dental_code_10digit = (
        f"{pref_code}3{dental_code_7digit}" if dental_code_7digit else ""
    )

    return {
        "medical_code_10digit": medical_code_10digit,
        "medical_code_7digit": medical_code_7digit,
        "primary_code_10digit": primary_code_10digit,
        "tensu_table_code": tensu_table_code,
        "code_7digit": code_7digit,
        "code_raw": c1_raw,
        "paired_code_7digit": paired_code_7digit,
        "paired_code_10digit": paired_code_10digit,
        "dental_code_7digit": dental_code_7digit,
        "dental_code_10digit": dental_code_10digit,
        "old_gunshiku_code": old_gunshiku_code,
        "record_source_type": record_source_type,
        "is_ikaheisetsu_from_shika": record_source_type == "ikaheisetsu_from_shika",
        "kouseikyoku_code": bureau_code,
        "kouseikyoku_name": BUREAU_NAMES[bureau_code],
        "pref_code": pref_code,
        "pref_name": PREF_CODE_TO_FULL_NAME[pref_code],
        "postal_code_7digit": postal_code_7digit,
        "postal_code": postal_code,
        "municipality_code_5digit": "",
        "municipality_name": "",
        "municipality_match_method": "",
        "is_shika_heisetsu": False,
        "is_shika_heisetsu_or_dental_dept": False,
        "has_paired_code_c1": bool(paired_code_7digit),
        "in_shikaheisetsu_list": False,
        "has_dental_dept_c8": has_dental_dept_c8,
        "facility_type": facility_type,
        "hospital_special_type": hospital_special_type,
        "hospital_special_types": hospital_special_types,
        "is_tokutei_kinou_byoin": is_tokutei_kinou_byoin,
        "is_chiiki_iryo_shien_byoin": is_chiiki_iryo_shien_byoin,
        "is_sougou_byoin": is_sougou_byoin,
        "status": status,
        "has_ryoyo_byosho_note": has_ryoyo_byosho_note,
        "facility_name": facility_name,
        "address": address,
        "phone": phone,
        "owner_name": owner_name,
        "manager_name": manager_name,
        "designated_date": designated_date,
        "designation_reason": designation_reason,
        "valid_period_start": valid_period_start,
        "beds_general": beds_general,
        "beds_ryoyo": beds_ryoyo,
        "beds_seishin": beds_seishin,
        "beds_kekkaku": beds_kekkaku,
        "beds_kansen": beds_kansen,
        "beds_total": beds_total,
        "has_bed_entry": has_bed_entry,
        "has_staff_info": has_staff_info,
        "doctors_ft": doctors_ft,
        "doctors_pt": doctors_pt,
        "dentists_ft": dentists_ft,
        "dentists_pt": dentists_pt,
        "pharmacists_ft": pharmacists_ft,
        "pharmacists_pt": pharmacists_pt,
        "staff_ft_total": staff_ft_total,
        "staff_pt_total": staff_pt_total,
        "departments": departments,
        "snapshot_date": SNAPSHOT_DATE,
        "source_file": source_file_rel,
        "source_sheet": source_sheet,
        "seq_no": seq_no,
    }


def parse_layout_a_sheet(
    ws: Any,
    pref_code: str,
    bureau_code: str,
    source_file_rel: str,
    source_sheet: str,
    record_source_type: str,
    qa_stats: dict[str, Any],
) -> tuple[list[dict[str, Any]], int, int]:
    """Parse all facility blocks from a Layout-A worksheet. Returns (records, max_seq, parsed_blocks)."""
    if hasattr(ws, "reset_dimensions"):
        ws.reset_dimensions()
    records: list[dict[str, Any]] = []
    block_rows: list[tuple[Any, ...]] = []
    current_seq: Optional[int] = None
    in_block = False
    max_seq = 0
    extra_cols_nonempty = 0
    sheet_header_yms: set[str] = set()

    def flush() -> None:
        nonlocal block_rows, current_seq, in_block
        if in_block and block_rows and current_seq is not None:
            rec = parse_block(
                block_rows=block_rows,
                seq_no=current_seq,
                pref_code=pref_code,
                bureau_code=bureau_code,
                source_file_rel=source_file_rel,
                source_sheet=source_sheet,
                record_source_type=record_source_type,
                qa_stats=qa_stats,
            )
            records.append(rec)
        block_rows = []
        current_seq = None
        in_block = False

    for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
        if row_idx < 10:
            for cell_v in row[:10]:
                if cell_v is None:
                    continue
                s_hdr = normalize_digits(norm_str(cell_v))
                for era, yr_tok, mo_tok in re.findall(
                    r"(令和|平成)\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月(?:\s*\d{1,2}\s*日)?\s*(?:現在|時点)",
                    s_hdr,
                ):
                    base = 2018 if era == "令和" else 1988
                    yr = 1 if yr_tok == "元" else int(yr_tok)
                    mm = int(mo_tok)
                    if 1 <= mm <= 12:
                        ym_str = f"{base + yr:04d}-{mm:02d}"
                        sheet_header_yms.add(ym_str)
                        qa_stats["header_snapshot_yms"].add(ym_str)
                        if ym_str != SNAPSHOT_DATE[:7]:
                            raise ValueError(
                                f"Snapshot header date mismatch in {source_file_rel} [{source_sheet}]: "
                                f"expected {SNAPSHOT_DATE[:7]}, found {ym_str}"
                            )
        if len(row) > 10 and any(norm_str(x) for x in row[10:]):
            extra_cols_nonempty += 1

        row_10 = tuple(row[:10])
        if not any(norm_str(x) for x in row_10):
            continue

        seq = parse_seq_int(row_10[0])
        if seq is not None:
            flush()
            in_block = True
            current_seq = seq
            if seq > max_seq:
                max_seq = seq
            block_rows = [row_10]
            continue

        if is_page_header_or_footer_row(row_10):
            if in_block:
                # Page trailers like 計 or next page title close any open block cleanly
                c0_s = normalize_digits(norm_str(row_10[0]))
                if c0_s in ("計", "合計", "小計") or "コード内容別" in c0_s:
                    flush()
            continue

        if in_block:
            block_rows.append(row_10)

    flush()
    if extra_cols_nonempty > 0:
        qa_stats["extra_cols_ge10_rows"] += extra_cols_nonempty

    if records and not sheet_header_yms:
        raise ValueError(
            f"Missing snapshot header date in {source_file_rel} [{source_sheet}]: "
            f"parsed {len(records)} records without date header"
        )
    qa_stats.setdefault("sheet_snapshot_dates", []).append(
        {
            "file": source_file_rel,
            "sheet": source_sheet,
            "header_yms": sorted(sheet_header_yms),
            "records": len(records),
        }
    )

    return records, max_seq, len(records)


def read_hokkaido_zentai_codes(xf: Path) -> set[str]:
    wb = openpyxl.load_workbook(xf, read_only=True, data_only=True)
    codes: set[str] = set()
    for sname in wb.sheetnames:
        ws = wb[sname]
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        for row in ws.iter_rows(values_only=True):
            if not row:
                continue
            seq_idx: Optional[int] = None
            for idx in (2, 0, 1, 3):
                if idx < len(row) and parse_seq_int(row[idx]) is not None:
                    seq_idx = idx
                    break
            if seq_idx is None:
                continue
            for c_idx in range(seq_idx + 1, min(len(row), 10)):
                raw_val = row[c_idx]
                s_val = normalize_digits(norm_str(raw_val))
                if not s_val or s_val.startswith("〒"):
                    continue
                m_hz = re.match(r"^(\d{2},\d{4},\d|\d{7})(?:\s|$|\(|（)", s_val)
                if m_hz:
                    c7 = extract_7digit_code(m_hz.group(1))
                    if c7:
                        codes.add(c7)
                        break
    return codes


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def init_qa_stats() -> dict[str, Any]:
    return {
        "missing_primary_code_count": 0,
        "c1_unparen_heisetsu_count": 0,
        "c1_paren_unparsed_patterns": Counter(),
        "multi_paired_code_blocks": 0,
        "missing_postal_code_count": 0,
        "staff_item_without_mode_count": 0,
        "c4_unknown_patterns": Counter(),
        "c7_extra_date_count": 0,
        "missing_designated_date_count": 0,
        "designation_reason_dist": Counter(),
        "missing_facility_type_count": 0,
        "missing_status_count": 0,
        "c9_unknown_patterns": Counter(),
        "extra_cols_ge10_rows": 0,
        "header_snapshot_yms": set(),
        "sheet_snapshot_dates": [],
    }


def main() -> None:
    PROCESSED_CSV.parent.mkdir(parents=True, exist_ok=True)
    PREF_SUMMARY_CSV.parent.mkdir(parents=True, exist_ok=True)

    resolver = MunicipalityResolver(UTF_KEN_ALL_ZIP)
    qa_stats = init_qa_stats()

    ika_main_records: list[dict[str, Any]] = []
    ikaheisetsu_records: list[dict[str, Any]] = []
    shikaheisetsu_keys: set[tuple[str, str]] = set()
    shikaheisetsu_by_pref: dict[str, set[str]] = defaultdict(set)
    hokkaido_zentai_codes: set[str] = set()

    sheet_audit: list[dict[str, Any]] = []
    seq_mismatches: list[dict[str, Any]] = []

    bureaus = sorted([d for d in RAW_BASE.iterdir() if d.is_dir()])
    for b_dir in bureaus:
        b_code = b_dir.name
        if b_code not in BUREAU_NAMES:
            continue
        xlsx_files = sorted(
            [p for p in b_dir.rglob("*.xlsx") if not p.name.startswith("._")]
        )
        for xf in xlsx_files:
            role = infer_file_role(xf, b_code)
            rel_path = str(xf.relative_to(REPO_ROOT))
            if role == "ika_zentai":
                hokkaido_zentai_codes.update(read_hokkaido_zentai_codes(xf))
                sheet_audit.append(
                    {
                        "source_file": rel_path,
                        "role": role,
                        "action": "qa_crosscheck_only",
                        "unique_codes": len(hokkaido_zentai_codes),
                    }
                )
                continue

            wb = openpyxl.load_workbook(xf, read_only=True, data_only=True)
            for sname in wb.sheetnames:
                ws = wb[sname]
                pref_code = infer_pref_code(xf, sname, b_code)
                if not pref_code:
                    raise ValueError(f"Could not infer prefecture for {rel_path} [{sname}]")

                rec_type = (
                    "ikaheisetsu_from_shika"
                    if role == "ikaheisetsu"
                    else ("shikaheisetsu" if role == "shikaheisetsu" else "ika_main")
                )
                recs, max_seq, parsed_cnt = parse_layout_a_sheet(
                    ws=ws,
                    pref_code=pref_code,
                    bureau_code=b_code,
                    source_file_rel=rel_path,
                    source_sheet=sname,
                    record_source_type=rec_type,
                    qa_stats=qa_stats,
                )
                if max_seq != parsed_cnt:
                    seq_mismatches.append(
                        {
                            "source_file": rel_path,
                            "source_sheet": sname,
                            "role": role,
                            "max_seq": max_seq,
                            "parsed_blocks": parsed_cnt,
                        }
                    )

                sheet_audit.append(
                    {
                        "source_file": rel_path,
                        "source_sheet": sname,
                        "pref_code": pref_code,
                        "role": role,
                        "max_seq": max_seq,
                        "parsed_blocks": parsed_cnt,
                    }
                )

                if role == "shikaheisetsu":
                    for r in recs:
                        k = (r["pref_code"], r["code_7digit"])
                        shikaheisetsu_keys.add(k)
                        shikaheisetsu_by_pref[r["pref_code"]].add(r["code_7digit"])
                elif role == "ikaheisetsu":
                    ikaheisetsu_records.extend(recs)
                else:
                    ika_main_records.extend(recs)

    # Validate header snapshot dates across all parsed worksheets
    expected_ym = SNAPSHOT_DATE[:7]
    found_yms = sorted(qa_stats["header_snapshot_yms"])
    if not found_yms or found_yms != [expected_ym]:
        raise ValueError(
            f"Snapshot header date verification failed: expected [{expected_ym}], found {found_yms}"
        )
    missing_date_sheets = [
        s for s in qa_stats.get("sheet_snapshot_dates", [])
        if s["records"] > 0 and s["header_yms"] != [expected_ym]
    ]
    if missing_date_sheets:
        raise ValueError(
            f"Worksheet-level snapshot date verification failed: {missing_date_sheets}"
        )

    # Cross-check shikaheisetsu against ika_main
    ika_main_key_map: dict[tuple[str, str], dict[str, Any]] = {}
    ika_main_dups = 0
    for r in ika_main_records:
        k = (r["pref_code"], r["code_7digit"])
        if k in ika_main_key_map:
            ika_main_dups += 1
        ika_main_key_map[k] = r
        in_sh = k in shikaheisetsu_keys
        r["in_shikaheisetsu_list"] = in_sh
        r["is_shika_heisetsu"] = bool(r["has_paired_code_c1"] or in_sh)
        r["is_shika_heisetsu_or_dental_dept"] = bool(
            r["is_shika_heisetsu"] or r["has_dental_dept_c8"]
        )

    shikaheisetsu_not_in_ika = len(shikaheisetsu_keys - set(ika_main_key_map.keys()))

    # Validate & enrich ikaheisetsu_from_shika
    ikaheisetsu_missing_paired = 0
    for r in ikaheisetsu_records:
        if not r["paired_code_7digit"] or not r["medical_code_10digit"]:
            ikaheisetsu_missing_paired += 1
        r["in_shikaheisetsu_list"] = False
        r["is_shika_heisetsu"] = True
        r["is_shika_heisetsu_or_dental_dept"] = True

    all_records = ika_main_records + ikaheisetsu_records

    # Resolve municipalities across all records
    muni_method_counts: Counter[str] = Counter()
    muni_pref_mismatch = 0
    for r in all_records:
        mcode, mname, method = resolver.resolve(
            pref_code=r["pref_code"],
            postal_code_7digit=r["postal_code_7digit"],
            address=r["address"],
        )
        r["municipality_code_5digit"] = mcode
        r["municipality_name"] = mname
        r["municipality_match_method"] = method
        muni_method_counts[method] += 1
        if mcode and mcode[:2] != r["pref_code"]:
            muni_pref_mismatch += 1

    # Check Hokkaido zentai agreement
    hokkaido_main_codes = {
        r["code_7digit"] for r in ika_main_records if r["pref_code"] == "01"
    }
    hokkaido_sym_diff = len(hokkaido_main_codes ^ hokkaido_zentai_codes)

    # Sort deterministically: pref_code, record_source_type, seq_no (via tensu_table_code, code_7digit)
    all_records.sort(key=lambda r: (r["pref_code"], r["tensu_table_code"], r["code_7digit"]))

    df = pd.DataFrame(all_records, columns=OUTPUT_COLUMNS)
    int_zero_cols = [
        "beds_general",
        "beds_ryoyo",
        "beds_seishin",
        "beds_kekkaku",
        "beds_kansen",
        "beds_total",
        "seq_no",
    ]
    for col in int_zero_cols:
        df[col] = df[col].astype("int64")

    nullable_int_cols = [
        "doctors_ft",
        "doctors_pt",
        "dentists_ft",
        "dentists_pt",
        "pharmacists_ft",
        "pharmacists_pt",
        "staff_ft_total",
        "staff_pt_total",
    ]
    for col in nullable_int_cols:
        df[col] = df[col].astype("Int64")

    df.to_csv(PROCESSED_CSV, index=False, encoding="utf-8-sig")

    # Summary by prefecture
    pref_rows: list[dict[str, Any]] = []
    for pcode in sorted(PREF_CODE_TO_FULL_NAME.keys()):
        sub = df[df["pref_code"] == pcode]
        if sub.empty:
            continue
        ika_sub = sub[sub["record_source_type"] == "ika_main"]
        pref_rows.append(
            {
                "pref_code": pcode,
                "pref_name": PREF_CODE_TO_FULL_NAME[pcode],
                "kouseikyoku_code": sub["kouseikyoku_code"].iloc[0],
                "kouseikyoku_name": sub["kouseikyoku_name"].iloc[0],
                "total_facilities": int(len(sub)),
                "ika_main_count": int(len(ika_sub)),
                "ikaheisetsu_from_shika_count": int((sub["record_source_type"] == "ikaheisetsu_from_shika").sum()),
                "hospitals_count": int((sub["facility_type"] == "病院").sum()),
                "clinics_count": int((sub["facility_type"] == "診療所").sum()),
                "tokutei_kinou_byoin_count": int(sub["is_tokutei_kinou_byoin"].sum()),
                "chiiki_iryo_shien_byoin_count": int(sub["is_chiiki_iryo_shien_byoin"].sum()),
                "sougou_byoin_count": int(sub["is_sougou_byoin"].sum()),
                "active_genzon_count": int((sub["status"] == "現存").sum()),
                "suspended_kyushi_count": int((sub["status"] == "休止").sum()),
                "shika_heisetsu_strict_count": int(sub["is_shika_heisetsu"].sum()),
                "shika_heisetsu_or_dental_dept_count": int(sub["is_shika_heisetsu_or_dental_dept"].sum()),
                "in_shikaheisetsu_list_count": int(sub["in_shikaheisetsu_list"].sum()),
                "has_dental_dept_c8_count": int(sub["has_dental_dept_c8"].sum()),
                "beds_general_sum": int(sub["beds_general"].sum()),
                "beds_ryoyo_sum": int(sub["beds_ryoyo"].sum()),
                "beds_seishin_sum": int(sub["beds_seishin"].sum()),
                "beds_kekkaku_sum": int(sub["beds_kekkaku"].sum()),
                "beds_kansen_sum": int(sub["beds_kansen"].sum()),
                "beds_total_sum": int(sub["beds_total"].sum()),
                "unique_municipalities_count": int(sub["municipality_code_5digit"].nunique()),
                "municipality_matched_count": int((sub["municipality_code_5digit"] != "").sum()),
            }
        )
    pref_df = pd.DataFrame(pref_rows)
    pref_df.to_csv(PREF_SUMMARY_CSV, index=False, encoding="utf-8-sig")

    # Summary by municipality
    muni_grp = (
        df.groupby(["pref_code", "pref_name", "municipality_code_5digit", "municipality_name"], dropna=False)
        .agg(
            total_facilities=("medical_code_10digit", "count"),
            ika_main_count=("record_source_type", lambda s: int((s == "ika_main").sum())),
            ikaheisetsu_from_shika_count=("record_source_type", lambda s: int((s == "ikaheisetsu_from_shika").sum())),
            hospitals_count=("facility_type", lambda s: int((s == "病院").sum())),
            clinics_count=("facility_type", lambda s: int((s == "診療所").sum())),
            tokutei_kinou_byoin_count=("is_tokutei_kinou_byoin", lambda s: int(s.sum())),
            chiiki_iryo_shien_byoin_count=("is_chiiki_iryo_shien_byoin", lambda s: int(s.sum())),
            sougou_byoin_count=("is_sougou_byoin", lambda s: int(s.sum())),
            active_genzon_count=("status", lambda s: int((s == "現存").sum())),
            suspended_kyushi_count=("status", lambda s: int((s == "休止").sum())),
            shika_heisetsu_strict_count=("is_shika_heisetsu", lambda s: int(s.sum())),
            shika_heisetsu_or_dental_dept_count=("is_shika_heisetsu_or_dental_dept", lambda s: int(s.sum())),
            beds_total_sum=("beds_total", "sum"),
        )
        .reset_index()
        .sort_values(["pref_code", "municipality_code_5digit"])
    )
    muni_grp.to_csv(MUNI_SUMMARY_CSV, index=False, encoding="utf-8-sig")

    # Unique 10-digit medical codes check
    medical_10_dups = int(df["medical_code_10digit"].duplicated().sum())
    primary_10_dups = int(df["primary_code_10digit"].duplicated().sum())

    qa_report = {
        "snapshot_date": SNAPSHOT_DATE,
        "snapshot_verification": {
            "expected_year_month": expected_ym,
            "header_snapshot_year_months": found_yms,
            "sheets_verified_with_date": sum(
                1 for s in qa_stats.get("sheet_snapshot_dates", []) if s["records"] > 0
            ),
            "sheets_missing_date": len(missing_date_sheets),
        },
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "outputs": {
            "processed_csv": {
                "path": str(PROCESSED_CSV.relative_to(REPO_ROOT)),
                "rows": int(len(df)),
                "columns": list(df.columns),
                "sha256": sha256_file(PROCESSED_CSV),
            },
            "prefecture_summary_csv": {
                "path": str(PREF_SUMMARY_CSV.relative_to(REPO_ROOT)),
                "rows": int(len(pref_df)),
            },
            "municipality_summary_csv": {
                "path": str(MUNI_SUMMARY_CSV.relative_to(REPO_ROOT)),
                "rows": int(len(muni_grp)),
            },
        },
        "national_counts": {
            "total_records": int(len(df)),
            "ika_main": int((df["record_source_type"] == "ika_main").sum()),
            "ikaheisetsu_from_shika": int((df["record_source_type"] == "ikaheisetsu_from_shika").sum()),
            "prefectures_covered": int(df["pref_code"].nunique()),
            "unique_medical_code_10digit": int(df["medical_code_10digit"].nunique()),
            "duplicate_medical_code_10digit": medical_10_dups,
            "duplicate_primary_code_10digit": primary_10_dups,
            "unique_municipalities_matched": int(
                df.loc[df["municipality_code_5digit"] != "", "municipality_code_5digit"].nunique()
            ),
            "facility_type_counts": {k: int(v) for k, v in df["facility_type"].value_counts().items()},
            "hospital_special_type_counts": {
                (k if k else "<clinic>"): int(v) for k, v in df["hospital_special_type"].value_counts().items()
            },
            "hospital_special_types_multi_counts": {
                (k if k else "<clinic>"): int(v) for k, v in df["hospital_special_types"].value_counts().items()
            },
            "hospital_special_category_flag_counts": {
                "特定機能": int(df["is_tokutei_kinou_byoin"].sum()),
                "地域支援": int(df["is_chiiki_iryo_shien_byoin"].sum()),
                "総合病院": int(df["is_sougou_byoin"].sum()),
                "multiple_special_categories": int(
                    (
                        df["is_tokutei_kinou_byoin"].astype(int)
                        + df["is_chiiki_iryo_shien_byoin"].astype(int)
                        + df["is_sougou_byoin"].astype(int)
                        >= 2
                    ).sum()
                ),
            },
            "status_counts": {(k if k else "<empty>"): int(v) for k, v in df["status"].value_counts().items()},
            "ryoyo_byosho_note_count": int(df["has_ryoyo_byosho_note"].sum()),
            "shika_heisetsu_strict_count": int(df["is_shika_heisetsu"].sum()),
            "shika_heisetsu_or_dental_dept_count": int(df["is_shika_heisetsu_or_dental_dept"].sum()),
            "has_paired_code_c1_count": int(df["has_paired_code_c1"].sum()),
            "in_shikaheisetsu_list_count": int(df["in_shikaheisetsu_list"].sum()),
            "has_dental_dept_c8_count": int(df["has_dental_dept_c8"].sum()),
            "beds_sums": {
                "general": int(df["beds_general"].sum()),
                "ryoyo": int(df["beds_ryoyo"].sum()),
                "seishin": int(df["beds_seishin"].sum()),
                "kekkaku": int(df["beds_kekkaku"].sum()),
                "kansen": int(df["beds_kansen"].sum()),
                "total": int(df["beds_total"].sum()),
                "facilities_with_beds_gt0": int((df["beds_total"] > 0).sum()),
            },
        },
        "cross_checks": {
            "seq_mismatches": seq_mismatches,
            "ika_main_intra_pref_duplicate_code7": ika_main_dups,
            "shikaheisetsu_total_keys": len(shikaheisetsu_keys),
            "shikaheisetsu_not_in_ika_main": shikaheisetsu_not_in_ika,
            "ikaheisetsu_missing_paired_code": ikaheisetsu_missing_paired,
            "hokkaido_zentai_unique_codes": len(hokkaido_zentai_codes),
            "hokkaido_main_unique_codes": len(hokkaido_main_codes),
            "hokkaido_zentai_symmetric_diff": hokkaido_sym_diff,
        },
        "municipality_resolution": {
            "utf_ken_all_raw_rows": resolver.raw_rows_count,
            "utf_ken_all_unique_zips": resolver.unique_zip_count,
            "utf_ken_all_unique_municipalities": resolver.unique_muni_count,
            "pattern_collisions_within_pref": resolver.pattern_collision_count,
            "match_method_counts": dict(muni_method_counts.most_common()),
            "municipality_pref_code_mismatches": muni_pref_mismatch,
        },
        "parser_qa_counters": {
            "missing_primary_code_count": qa_stats["missing_primary_code_count"],
            "c1_unparen_heisetsu_count": qa_stats["c1_unparen_heisetsu_count"],
            "c1_paren_unparsed_patterns": dict(qa_stats["c1_paren_unparsed_patterns"].most_common(10)),
            "multi_paired_code_blocks": qa_stats["multi_paired_code_blocks"],
            "missing_postal_code_count": qa_stats["missing_postal_code_count"],
            "staff_item_without_mode_count": qa_stats["staff_item_without_mode_count"],
            "c4_unknown_patterns": dict(qa_stats["c4_unknown_patterns"].most_common(10)),
            "c7_extra_date_count": qa_stats["c7_extra_date_count"],
            "missing_designated_date_count": qa_stats["missing_designated_date_count"],
            "designation_reason_top20": dict(qa_stats["designation_reason_dist"].most_common(20)),
            "missing_facility_type_count": qa_stats["missing_facility_type_count"],
            "missing_status_count": qa_stats["missing_status_count"],
            "c9_unknown_patterns": dict(qa_stats["c9_unknown_patterns"].most_common(10)),
            "extra_cols_ge10_rows": qa_stats["extra_cols_ge10_rows"],
        },
        "sheet_audit": sheet_audit,
    }

    QA_REPORT_JSON.write_text(
        json.dumps(qa_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "total_records": qa_report["national_counts"]["total_records"],
                "ika_main": qa_report["national_counts"]["ika_main"],
                "ikaheisetsu_from_shika": qa_report["national_counts"]["ikaheisetsu_from_shika"],
                "prefectures_covered": qa_report["national_counts"]["prefectures_covered"],
                "duplicate_medical_code_10digit": medical_10_dups,
                "seq_mismatches": len(seq_mismatches),
                "shikaheisetsu_not_in_ika_main": shikaheisetsu_not_in_ika,
                "ikaheisetsu_missing_paired_code": ikaheisetsu_missing_paired,
                "hokkaido_zentai_symmetric_diff": hokkaido_sym_diff,
                "municipality_match_method_counts": dict(muni_method_counts),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
