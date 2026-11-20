"""Generalized monthly downloader and selective ZIP extractor for Kouseikyoku raw data.

Usage:
  python3 download_raw.py --snapshot 202701 --mode all
  python3 download_raw.py --snapshot 202701 --mode discover
  python3 download_raw.py --snapshot 202701 --mode download
  python3 download_raw.py --snapshot 202701 --mode extract
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_ROOT = REPO_ROOT / "data" / "raw" / "hokenshisetsu"
DOCS_DIR = REPO_ROOT / "subprojects" / "hokenshisetsu" / "docs"

JAPANPOST_UTF_KEN_ALL_URL = "https://www.post.japanpost.jp/zipcode/dl/utf/zip/utf_ken_all.zip"

BUREAU_INDEX_PAGES: list[dict[str, str]] = [
    {
        "bureau_dir": "01_hokkaido",
        "bureau_name": "北海道厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/hokkaido/iryo_shido/hoken-kikan.html",
    },
    {
        "bureau_dir": "02_tohoku",
        "bureau_name": "東北厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/tohoku/gyomu/gyomu/hoken_kikan/itiran.html",
    },
    {
        "bureau_dir": "03_kantoshinetsu",
        "bureau_name": "関東信越厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/kantoshinetsu/chousa/shitei.html",
    },
    {
        "bureau_dir": "04_tokaihokuriku",
        "bureau_name": "東海北陸厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/tokaihokuriku/gyomu/gyomu/hoken_kikan/shitei.html",
    },
    {
        "bureau_dir": "05_kinki",
        "bureau_name": "近畿厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/kinki/tyousa/shinkishitei.html",
    },
    {
        "bureau_dir": "06_chugokushikoku",
        "bureau_name": "中国四国厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/chugokushikoku/chousaka/iryoukikanshitei.html",
    },
    {
        "bureau_dir": "07_shikoku",
        "bureau_name": "四国厚生支局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/shikoku/gyomu/gyomu/hoken_kikan/shitei/index.html",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "index_url": "https://kouseikyoku.mhlw.go.jp/kyushu/gyomu/gyomu/hoken_kikan/index_00004.html",
    },
]


class LinkParser(html.parser.HTMLParser):
    """Extract (href, anchor_text) pairs from Kouseikyoku HTML pages."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._in_a = False
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._in_a = True
            self._href = dict(attrs).get("href") or ""
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._in_a:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if self._href:
                self.links.append((self._href, text))
            self._in_a = False
            self._href = ""
            self._buf = []


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_zip_filename(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw_bytes = info.filename.encode("cp437", errors="strict")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("cp932", "utf-8"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def should_extract_member(decoded_name: str) -> tuple[bool, str]:
    normalized = unicodedata.normalize("NFKC", decoded_name.replace("\\", "/"))
    basename = normalized.rsplit("/", 1)[-1]
    if not basename or basename.startswith("._") or basename == ".DS_Store" or "__MACOSX" in normalized:
        return False, "metadata_or_dir"

    lower = basename.lower()
    if "薬局" in basename or "yakkyoku" in lower or "調剤" in basename:
        return False, "pharmacy_only"

    if "併設" in basename or "heisetsu" in lower or "heisetu" in lower:
        return True, "heisetsu"

    if "歯科" in basename and "医科" not in basename:
        return False, "dental_only"
    if re.search(r"(?:^|[^a-z])sh?ika(?:[^a-z]|$)", lower) and not re.search(
        r"(?:^|[^a-z])ika(?:[^a-z]|$)", lower
    ):
        return False, "dental_only"

    return True, "medical_or_other"


def safe_target_path(base_dir: Path, decoded_name: str) -> Path:
    parts = [p for p in decoded_name.replace("\\", "/").split("/") if p and p not in (".", "..")]
    if not parts:
        raise ValueError(f"Unsafe zip member name: {decoded_name}")
    dest = (base_dir / Path(*parts)).resolve()
    base_resolved = base_dir.resolve()
    if base_resolved not in dest.parents and dest != base_resolved:
        raise ValueError(f"Path traversal detected: {decoded_name}")
    return dest


def validate_zip_extraction_plan(zip_path: Path, extract_root: Path) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    seen_dests: dict[str, str] = {}
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            decoded = decode_zip_filename(info)
            if "?" in decoded and "?" not in info.filename:
                raise RuntimeError(
                    f"ZIP filename replacement '?' detected in {zip_path.name}: {decoded!r}"
                )
            keep, reason = should_extract_member(decoded)
            if not keep:
                plan.append(
                    {
                        "info": info,
                        "decoded": decoded,
                        "keep": False,
                        "reason": reason,
                        "dest": None,
                        "member_sha256": None,
                        "exists": False,
                        "hash_matches": None,
                    }
                )
                continue

            dest = safe_target_path(extract_root, decoded)
            collision_key = unicodedata.normalize("NFC", str(dest)).lower()
            if collision_key in seen_dests:
                raise RuntimeError(
                    f"ZIP destination path collision in {zip_path.name}: "
                    f"{decoded!r} collides with {seen_dests[collision_key]!r}"
                )
            seen_dests[collision_key] = decoded

            member_sha256 = hashlib.sha256(zf.read(info)).hexdigest()
            exists = dest.exists()
            hash_matches = (sha256_file(dest) == member_sha256) if exists else None
            plan.append(
                {
                    "info": info,
                    "decoded": decoded,
                    "keep": True,
                    "reason": reason,
                    "dest": dest,
                    "member_sha256": member_sha256,
                    "exists": exists,
                    "hash_matches": hash_matches,
                }
            )
    return plan


KYUSHU_PREFS: list[tuple[str, str]] = [
    ("40_fukuoka", "福岡県"),
    ("41_saga", "佐賀県"),
    ("42_nagasaki", "長崎県"),
    ("43_kumamoto", "熊本県"),
    ("44_oita", "大分県"),
    ("45_miyazaki", "宮崎県"),
    ("46_kagoshima", "鹿児島県"),
    ("47_okinawa", "沖縄県"),
]


def validate_snapshot_str(snapshot: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{6}", snapshot):
        raise ValueError(f"--snapshot must be YYYYMM, got {snapshot!r}")
    yyyy, mm = int(snapshot[:4]), int(snapshot[4:6])
    if not (2000 <= yyyy <= 2100 and 1 <= mm <= 12):
        raise ValueError(f"Invalid YYYYMM calendar snapshot: {snapshot!r}")
    return yyyy, mm


def extract_yms_from_text(text: str) -> set[str]:
    """Extract YYYYMM strings from Japanese era snapshot expressions in HTML text."""
    norm = unicodedata.normalize("NFKC", text)
    found: set[str] = set()
    for era, yr_tok, mo_tok in re.findall(
        r"(令和|平成)\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月(?:\s*\d{1,2}\s*日)?\s*(?:現在|時点)",
        norm,
    ):
        base = 2018 if era == "令和" else 1988
        yr = 1 if yr_tok == "元" else int(yr_tok)
        mm = int(mo_tok)
        if 1 <= mm <= 12:
            found.add(f"{base + yr:04d}{mm:02d}")
    return found


def extract_ym_from_url_or_name(url: str) -> str | None:
    name = Path(urllib.parse.urlparse(url).path).name.lower()
    m_r = re.search(r"[-_]r(\d{2})(\d{2})(?:\.|_|-|$)", name)
    if m_r:
        yy, mm = int(m_r.group(1)), int(m_r.group(2))
        if 1 <= mm <= 12:
            return f"{2018 + yy:04d}{mm:02d}"
    m_ymd = re.match(r"^s?(20\d{2})\.(\d{1,2})_", name)
    if m_ymd:
        yyyy, mm = int(m_ymd.group(1)), int(m_ymd.group(2))
        if 1 <= mm <= 12:
            return f"{yyyy:04d}{mm:02d}"
    m_tk = re.match(r"^(\d{2})(\d{2})-\d{2}-\d{2}\.zip$", name)
    if m_tk:
        yy, mm = int(m_tk.group(1)), int(m_tk.group(2))
        if 1 <= mm <= 12:
            return f"{2000 + yy:04d}{mm:02d}"
    return None


def _fetch_page(url: str) -> tuple[str, list[tuple[str, str]]]:
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw_html = resp.read().decode("utf-8", errors="replace")
    parser = LinkParser()
    parser.feed(raw_html)
    plain_text = re.sub(r"<[^>]+>", " ", raw_html)
    return plain_text, [(urllib.parse.urljoin(url, h), t) for h, t in parser.links]


def validate_discovered_targets(targets: list[dict[str, str]], snapshot: str) -> None:
    """Enforce full 8-bureau coverage and snapshot month consistency."""
    validate_snapshot_str(snapshot)
    by_bureau: dict[str, list[dict[str, str]]] = {}
    for item in targets:
        b_dir = item.get("bureau_dir", "")
        by_bureau.setdefault(b_dir, []).append(item)
        url_ym = extract_ym_from_url_or_name(item.get("url", ""))
        if url_ym is not None and url_ym != snapshot:
            raise RuntimeError(
                f"Snapshot mismatch in {b_dir}: requested {snapshot}, but URL {item.get('url')} is {url_ym}"
            )
        disc_ym = item.get("discovered_ym")
        if disc_ym and disc_ym != snapshot:
            raise RuntimeError(
                f"Snapshot mismatch in {b_dir}: requested {snapshot}, but page date is {disc_ym}"
            )
        if url_ym != snapshot and disc_ym != snapshot:
            raise RuntimeError(
                f"Missing snapshot date evidence in {b_dir} for {item.get('url')}: "
                f"expected {snapshot} from URL/filename or page date"
            )

    expected_bureaus = [b["bureau_dir"] for b in BUREAU_INDEX_PAGES]
    missing_bureaus = [b for b in expected_bureaus if not by_bureau.get(b)]
    if missing_bureaus:
        raise RuntimeError(f"Missing targets for bureaus: {missing_bureaus}")

    hk_files = [x["filename"] for x in by_bureau["01_hokkaido"]]
    if (
        not any("zentai" in f for f in hk_files)
        or not any("byoin" in f for f in hk_files)
        or not any("shinryojo" in f for f in hk_files)
    ):
        raise RuntimeError(f"01_hokkaido missing zentai/byoin/shinryojo targets: {hk_files}")

    th_files = [x["filename"] for x in by_bureau["02_tohoku"]]
    for req_tok in ("touhoku-ika-", "touhoku-ikaheisetsu-", "touhoku-shikaheisetsu-"):
        if not any(req_tok in f for f in th_files):
            raise RuntimeError(f"02_tohoku missing required target {req_tok}: {th_files}")

    kt_files = [x["filename"] for x in by_bureau["03_kantoshinetsu"]]
    for req_tok in ("shitei_ika_", "shitei_ikaheisetsu_", "shitei_shikaheisetsu_"):
        if not any(req_tok in f for f in kt_files):
            raise RuntimeError(f"03_kantoshinetsu missing required target {req_tok}: {kt_files}")

    kk_files = [x["filename"] for x in by_bureau["05_kinki"]]
    for req_tok in ("kikanzentai_ika", "kikanzentai_sika"):
        if not any(req_tok in f for f in kk_files):
            raise RuntimeError(f"05_kinki missing required target {req_tok}: {kk_files}")

    ky_files = [x["filename"] for x in by_bureau["08_kyushu"]]
    if len(ky_files) < 8:
        raise RuntimeError(f"08_kyushu expected 8 prefecture ZIPs, got {len(ky_files)}: {ky_files}")
    for pref_prefix, _ in KYUSHU_PREFS:
        if not any(f.startswith(pref_prefix) for f in ky_files):
            raise RuntimeError(f"08_kyushu missing prefecture {pref_prefix}: {ky_files}")


def discover_targets(snapshot: str) -> list[dict[str, str]]:
    """Crawl the 8 Kouseikyoku index pages (plus 1-hop subpages) for the current registry files."""
    yyyy, mm = validate_snapshot_str(snapshot)
    index_map = {b["bureau_dir"]: b for b in BUREAU_INDEX_PAGES}
    targets: list[dict[str, str]] = []
    yy_mm = f"r{yyyy - 2018:02d}{mm:02d}"

    def check_page_yms(b_dir: str, page_text: str) -> str | None:
        yms = extract_yms_from_text(page_text)
        if yms and snapshot not in yms:
            raise RuntimeError(
                f"Snapshot mismatch on {b_dir} page: requested {snapshot}, found {sorted(yms)}"
            )
        return snapshot if snapshot in yms else None

    def add(
        b_dir: str,
        category: str,
        url: str,
        filename: str | None = None,
        discovered_ym: str | None = None,
    ) -> None:
        fname = filename or Path(urllib.parse.urlparse(url).path).name
        eff_ym = extract_ym_from_url_or_name(url) or discovered_ym
        if eff_ym is not None and eff_ym != snapshot:
            raise RuntimeError(
                f"Snapshot mismatch in {b_dir}: requested {snapshot}, got {eff_ym} for {url}"
            )
        targets.append(
            {
                "bureau_dir": b_dir,
                "bureau_name": index_map[b_dir]["bureau_name"],
                "category": category,
                "url": url,
                "filename": fname,
                "discovered_ym": eff_ym or "",
            }
        )

    # 01_hokkaido: 1-hop subpages (zentai_ichiran.html + code_ichiran.html)
    _, hk_links = _fetch_page(index_map["01_hokkaido"]["index_url"])
    hk_zentai = next((u for u, t in hk_links if "zentai_ichiran" in u or "郡市区別" in t), None)
    hk_code = next((u for u, t in hk_links if "code_ichiran" in u or "コード内容別" in t), None)
    if hk_zentai:
        z_text, z_links = _fetch_page(hk_zentai)
        z_ym = check_page_yms("01_hokkaido", z_text)
        z_xlsx = [u for u, _ in z_links if u.lower().endswith(".xlsx")]
        if z_xlsx:
            add("01_hokkaido", "医科（郡市区別）", z_xlsx[0], f"hokkaido_zentai_ika_{yy_mm}.xlsx", z_ym)
    if hk_code:
        c_text, c_links = _fetch_page(hk_code)
        c_ym = check_page_yms("01_hokkaido", c_text)
        c_xlsx = [u for u, _ in c_links if u.lower().endswith(".xlsx")]
        if len(c_xlsx) >= 2:
            add("01_hokkaido", "医科（病院・コード内容別）", c_xlsx[0], f"hokkaido_code_ika_byoin_{yy_mm}.xlsx", c_ym)
            add("01_hokkaido", "医科（診療所・コード内容別）", c_xlsx[1], f"hokkaido_code_ika_shinryojo_{yy_mm}.xlsx", c_ym)

    # 02_tohoku: direct full registry files shitei-touhoku-(ika|shikaheisetsu|ikaheisetsu)-rYYMM.xlsx
    th_text, th_links = _fetch_page(index_map["02_tohoku"]["index_url"])
    th_ym = check_page_yms("02_tohoku", th_text)
    for u, t in th_links:
        fn = Path(urllib.parse.urlparse(u).path).name.lower()
        if re.fullmatch(r"shitei-touhoku-(ika|shikaheisetsu|ikaheisetsu)-r\d{4}\.xlsx", fn):
            add("02_tohoku", t or fn, u, fn, th_ym)

    # 03_kantoshinetsu: direct ZIPs shitei_(ika|shikaheisetsu|ikaheisetsu)_rYYMM.zip
    kt_text, kt_links = _fetch_page(index_map["03_kantoshinetsu"]["index_url"])
    kt_ym = check_page_yms("03_kantoshinetsu", kt_text)
    for u, t in kt_links:
        fn = Path(urllib.parse.urlparse(u).path).name.lower()
        if re.fullmatch(r"shitei_(ika|shikaheisetsu|ikaheisetsu)_r\d{4}\.zip", fn):
            add("03_kantoshinetsu", t or fn, u, fn, kt_ym)

    # 04_tokaihokuriku: 1-hop subpage (コード内容別), take 医科 ZIP
    _, tk_links = _fetch_page(index_map["04_tokaihokuriku"]["index_url"])
    tk_sub = next((u for u, t in tk_links if "コード内容別" in t and u.lower().endswith(".html")), None)
    if tk_sub:
        tk_text, tk_sub_links = _fetch_page(tk_sub)
        tk_ym = check_page_yms("04_tokaihokuriku", tk_text)
        for u, t in tk_sub_links:
            if u.lower().endswith(".zip") and "医科" in unicodedata.normalize("NFKC", t):
                add("04_tokaihokuriku", t, u, discovered_ym=tk_ym)
                break

    # 05_kinki: direct ZIPs *_kikanzentai_ika.zip and s*_kikanzentai_sika.zip (skip sinkikikan)
    kk_text, kk_links = _fetch_page(index_map["05_kinki"]["index_url"])
    kk_ym = check_page_yms("05_kinki", kk_text)
    kk_ika = next((u for u, _ in kk_links if u.lower().endswith("_kikanzentai_ika.zip")), None)
    kk_sika = next((u for u, _ in kk_links if u.lower().endswith("_kikanzentai_sika.zip")), None)
    if kk_ika:
        add("05_kinki", "医科・医科併設", kk_ika, discovered_ym=kk_ym)
    if kk_sika:
        add("05_kinki", "歯科・歯科併設（歯科併設のみ展開）", kk_sika, discovered_ym=kk_ym)

    # 06_chugokushikoku: direct latest 医科 ZIP (first link starting with 医科)
    cg_text, cg_links = _fetch_page(index_map["06_chugokushikoku"]["index_url"])
    cg_ym = check_page_yms("06_chugokushikoku", cg_text)
    for u, t in cg_links:
        if u.lower().endswith(".zip") and unicodedata.normalize("NFKC", t).startswith("医科"):
            add("06_chugokushikoku", t, u, discovered_ym=cg_ym)
            break

    # 07_shikoku: first ZIP on page is current 医科
    sk_text, sk_links = _fetch_page(index_map["07_shikoku"]["index_url"])
    sk_ym = check_page_yms("07_shikoku", sk_text)
    sk_first_zip = next((u for u, _ in sk_links if u.lower().endswith(".zip")), None)
    if sk_first_zip:
        add("07_shikoku", "医科", sk_first_zip, discovered_ym=sk_ym)

    # 08_kyushu: 1-hop subpage (コード内容別), first 8 ZIPs are the 8 prefectures of the latest month
    _, ky_links = _fetch_page(index_map["08_kyushu"]["index_url"])
    ky_sub = next((u for u, t in ky_links if "コード内容別" in t and u.lower().endswith(".html")), None)
    if ky_sub:
        ky_text, ky_sub_links = _fetch_page(ky_sub)
        ky_ym = check_page_yms("08_kyushu", ky_text)
        ky_zips = [u for u, _ in ky_sub_links if u.lower().endswith(".zip")]
        if len(ky_zips) < 8:
            raise RuntimeError(
                f"08_kyushu subpage expected >= 8 ZIP links, found {len(ky_zips)}"
            )
        for (p_prefix, p_name), u in zip(KYUSHU_PREFS, ky_zips[:8]):
            orig_name = Path(urllib.parse.urlparse(u).path).name
            add("08_kyushu", p_name, u, f"{p_prefix}_{orig_name}", ky_ym)

    validate_discovered_targets(targets, snapshot)

    out_path = DOCS_DIR / f"discovered_targets_{snapshot}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(targets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Discovered {len(targets)} targets -> {out_path.relative_to(REPO_ROOT)}")
    return targets


def load_targets(
    snapshot: str,
    targets_json: Path | None = None,
    refresh: bool = False,
) -> list[dict[str, str]]:
    if targets_json is not None:
        targets = json.loads(targets_json.read_text(encoding="utf-8"))
        validate_discovered_targets(targets, snapshot)
        return targets
    path = DOCS_DIR / f"discovered_targets_{snapshot}.json"
    if path.exists() and not refresh:
        try:
            targets = json.loads(path.read_text(encoding="utf-8"))
            validate_discovered_targets(targets, snapshot)
            return targets
        except RuntimeError:
            # Legacy cached targets file (e.g. missing discovered_ym); re-discover from index pages
            pass
    return discover_targets(snapshot)


def _history_path(snapshot: str) -> Path:
    return RAW_ROOT / "_archive" / snapshot / "download_history.json"


def _load_download_history(snapshot: str) -> dict[str, dict[str, Any]]:
    hp = _history_path(snapshot)
    if hp.exists():
        data = json.loads(hp.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _save_download_history(snapshot: str, history: dict[str, dict[str, Any]]) -> None:
    hp = _history_path(snapshot)
    hp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=hp.parent, prefix=".download_history.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(history, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp_path, hp)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _validate_downloaded_file(
    tmp_path: Path,
    expected_suffix: str,
    is_kouseikyoku_zip: bool,
) -> None:
    if not tmp_path.exists() or tmp_path.stat().st_size == 0:
        raise RuntimeError(f"Downloaded file is empty: {tmp_path.name}")
    suffix = expected_suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(tmp_path, "r") as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                raise RuntimeError(f"Corrupt ZIP member {bad_member!r} in downloaded file")
        if is_kouseikyoku_zip:
            with tempfile.TemporaryDirectory() as tmp_dir:
                validate_zip_extraction_plan(tmp_path, Path(tmp_dir))
    elif suffix in (".xlsx", ".xlsm"):
        import openpyxl

        with tmp_path.open("rb") as fh:
            wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
            try:
                if not wb.sheetnames:
                    raise RuntimeError(f"Excel workbook has no sheets: {tmp_path.name}")
                ws = wb[wb.sheetnames[0]]
                next(ws.iter_rows(max_row=1, values_only=True), None)
            finally:
                wb.close()
    elif suffix == ".xls":
        header = tmp_path.read_bytes()[:8]
        if header != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise RuntimeError(f"Invalid .xls OLE2 header in downloaded file: {tmp_path.name}")


def _archive_existing_file(
    snapshot: str,
    rel_subdir: str,
    existing_path: Path,
    old_sha256: str,
    old_bytes: int,
    old_url: str,
    old_fetched_at_utc: str,
) -> dict[str, Any]:
    ts_compact = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = RAW_ROOT / "_archive" / snapshot / rel_subdir
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_name = f"{ts_compact}_{old_sha256[:8]}_{existing_path.name}"
    archive_path = archive_dir / archive_name
    shutil.copy2(existing_path, archive_path)
    return {
        "archived_at_utc": datetime.now(timezone.utc).isoformat(),
        "archived_raw_file": str(archive_path.relative_to(REPO_ROOT)),
        "url": old_url,
        "sha256": old_sha256,
        "bytes": old_bytes,
        "fetched_at_utc": old_fetched_at_utc,
    }


def _fetch_and_sync_file(
    snapshot: str,
    url: str,
    out_path: Path,
    rel_subdir: str,
    history: dict[str, dict[str, Any]],
    headers: dict[str, str],
    refresh: bool,
    is_kouseikyoku_zip: bool,
) -> str:
    """Fetch URL to temp, validate, detect changes, archive previous version if changed, and atomically replace."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rel_key = str(out_path.relative_to(REPO_ROOT))
    prev_rec = history.get(rel_key, {})
    now_utc = datetime.now(timezone.utc).isoformat()

    req_headers = dict(headers)
    if out_path.exists() and not refresh:
        # Only use conditional HTTP headers if local file still matches recorded history sha256
        if prev_rec.get("sha256") and prev_rec.get("sha256") == sha256_file(out_path):
            if prev_rec.get("etag"):
                req_headers["If-None-Match"] = str(prev_rec["etag"])
            if prev_rec.get("last_modified"):
                req_headers["If-Modified-Since"] = str(prev_rec["last_modified"])

    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_etag = resp.headers.get("ETag")
            resp_last_mod = resp.headers.get("Last-Modified")
            fd, tmp_name = tempfile.mkstemp(
                dir=out_path.parent,
                prefix=f".{out_path.name}.",
                suffix=".tmp",
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 64)
                        if not chunk:
                            break
                        fh.write(chunk)
                _validate_downloaded_file(
                    tmp_path,
                    expected_suffix=out_path.suffix,
                    is_kouseikyoku_zip=is_kouseikyoku_zip,
                )
                new_sha256 = sha256_file(tmp_path)
                new_bytes = tmp_path.stat().st_size

                if out_path.exists():
                    old_sha256 = sha256_file(out_path)
                    old_bytes = out_path.stat().st_size
                    if new_sha256 == old_sha256:
                        tmp_path.unlink(missing_ok=True)
                        rec = dict(prev_rec)
                        rec.update(
                            {
                                "url": url,
                                "sha256": old_sha256,
                                "bytes": old_bytes,
                                "fetched_at_utc": prev_rec.get("fetched_at_utc") or now_utc,
                                "last_checked_at_utc": now_utc,
                                "etag": resp_etag or prev_rec.get("etag"),
                                "last_modified": resp_last_mod or prev_rec.get("last_modified"),
                                "revision_history": list(prev_rec.get("revision_history", [])),
                            }
                        )
                        history[rel_key] = rec
                        _save_download_history(snapshot, history)
                        print(f"UNCHANGED (sha256 match): {rel_key} ({old_bytes} bytes)")
                        return "unchanged"

                    revs = list(prev_rec.get("revision_history", []))
                    archived_file_created: Path | None = None
                    if not (revs and revs[-1].get("sha256") == old_sha256):
                        rev_entry = _archive_existing_file(
                            snapshot=snapshot,
                            rel_subdir=rel_subdir,
                            existing_path=out_path,
                            old_sha256=old_sha256,
                            old_bytes=old_bytes,
                            old_url=str(prev_rec.get("url") or url),
                            old_fetched_at_utc=str(prev_rec.get("fetched_at_utc") or ""),
                        )
                        archived_file_created = REPO_ROOT / rev_entry["archived_raw_file"]
                        revs.append(rev_entry)

                    new_rec = {
                        "url": url,
                        "sha256": new_sha256,
                        "bytes": new_bytes,
                        "fetched_at_utc": now_utc,
                        "last_checked_at_utc": now_utc,
                        "etag": resp_etag,
                        "last_modified": resp_last_mod,
                        "revision_history": revs,
                    }
                    history[rel_key] = new_rec
                    try:
                        _save_download_history(snapshot, history)
                    except Exception:
                        if prev_rec:
                            history[rel_key] = prev_rec
                        else:
                            history.pop(rel_key, None)
                        if archived_file_created is not None:
                            archived_file_created.unlink(missing_ok=True)
                        raise

                    os.replace(tmp_path, out_path)
                    print(f"UPDATED (archived previous): {rel_key} ({old_bytes} -> {new_bytes} bytes)")
                    return "updated"

                history[rel_key] = {
                    "url": url,
                    "sha256": new_sha256,
                    "bytes": new_bytes,
                    "fetched_at_utc": now_utc,
                    "last_checked_at_utc": now_utc,
                    "etag": resp_etag,
                    "last_modified": resp_last_mod,
                    "revision_history": list(prev_rec.get("revision_history", [])),
                }
                try:
                    _save_download_history(snapshot, history)
                except Exception:
                    if prev_rec:
                        history[rel_key] = prev_rec
                    else:
                        history.pop(rel_key, None)
                    raise

                os.replace(tmp_path, out_path)
                print(f"DOWNLOADED: {rel_key} ({new_bytes} bytes)")
                return "downloaded"
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and out_path.exists():
            rec = dict(prev_rec)
            rec.update(
                {
                    "url": url,
                    "sha256": sha256_file(out_path),
                    "bytes": out_path.stat().st_size,
                    "fetched_at_utc": prev_rec.get("fetched_at_utc") or now_utc,
                    "last_checked_at_utc": now_utc,
                    "revision_history": list(prev_rec.get("revision_history", [])),
                }
            )
            history[rel_key] = rec
            _save_download_history(snapshot, history)
            print(f"UNCHANGED (HTTP 304): {rel_key} ({out_path.stat().st_size} bytes)")
            return "unchanged"
        raise


def run_download(
    snapshot: str,
    targets: list[dict[str, str]],
    download_japanpost: bool,
    refresh: bool = False,
) -> dict[str, int]:
    raw_base = RAW_ROOT / snapshot
    headers = {"User-Agent": "Mozilla/5.0"}
    history = _load_download_history(snapshot)
    counts = {"downloaded": 0, "updated": 0, "unchanged": 0}

    for item in targets:
        out_path = raw_base / item["bureau_dir"] / item["filename"]
        status = _fetch_and_sync_file(
            snapshot=snapshot,
            url=item["url"],
            out_path=out_path,
            rel_subdir=item["bureau_dir"],
            history=history,
            headers=headers,
            refresh=refresh,
            is_kouseikyoku_zip=out_path.suffix.lower() == ".zip",
        )
        counts[status] += 1

    if download_japanpost:
        jp_out = raw_base / "utf_ken_all.zip"
        try:
            _fetch_and_sync_file(
                snapshot=snapshot,
                url=JAPANPOST_UTF_KEN_ALL_URL,
                out_path=jp_out,
                rel_subdir="japanpost",
                history=history,
                headers=headers,
                refresh=refresh,
                is_kouseikyoku_zip=False,
            )
        except Exception as exc:
            fallback = RAW_ROOT / "shared" / "utf_ken_all.zip"
            if jp_out.exists():
                print(
                    f"WARN: Japan Post update check failed ({exc}); "
                    f"keeping existing {jp_out.relative_to(REPO_ROOT)}"
                )
            elif fallback.exists():
                print(
                    f"WARN: Japan Post download failed ({exc}); "
                    f"using existing fallback {fallback.relative_to(REPO_ROOT)}"
                )
            else:
                raise

    _save_download_history(snapshot, history)
    return counts


def _extract_dir_matches_plan(extract_root: Path, plan: list[dict[str, Any]]) -> bool:
    if not extract_root.exists():
        return False
    expected_dests: set[Path] = set()
    for p_entry in plan:
        if not p_entry["keep"]:
            continue
        if not p_entry["exists"] or not p_entry["hash_matches"]:
            return False
        expected_dests.add(p_entry["dest"].resolve())

    for existing_file in extract_root.rglob("*"):
        if existing_file.is_file() and not existing_file.name.startswith("._"):
            if existing_file.resolve() not in expected_dests:
                return False
    return True


def _extract_zip_atomically(zip_path: Path, extract_root: Path, plan: list[dict[str, Any]]) -> None:
    extract_root.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(dir=extract_root.parent, prefix=f".{extract_root.name}.staging."))
    backup_dir: Path | None = None
    extract_root_resolved = extract_root.resolve()
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for p_entry in plan:
                if not p_entry["keep"]:
                    continue
                rel_dest = p_entry["dest"].relative_to(extract_root_resolved)
                stage_dest = staging_dir / rel_dest
                stage_dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(p_entry["info"], "r") as src:
                    stage_dest.write_bytes(src.read())
                if sha256_file(stage_dest) != p_entry["member_sha256"]:
                    raise RuntimeError(f"Staged extraction hash mismatch for {rel_dest}")

        if extract_root.exists():
            backup_dir = Path(tempfile.mkdtemp(dir=extract_root.parent, prefix=f".{extract_root.name}.backup."))
            backup_dir.rmdir()
            os.replace(extract_root, backup_dir)
        os.replace(staging_dir, extract_root)
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists() and not extract_root.exists():
            os.replace(backup_dir, extract_root)
        raise


def run_extract_and_manifest(snapshot: str, targets: list[dict[str, str]]) -> None:
    raw_base = RAW_ROOT / snapshot
    manifest_path = DOCS_DIR / f"download_manifest_{snapshot}.json"
    history = _load_download_history(snapshot)
    manifest_items: list[dict[str, Any]] = []

    for item in targets:
        file_path = raw_base / item["bureau_dir"] / item["filename"]
        if not file_path.exists():
            raise FileNotFoundError(f"Missing downloaded file: {file_path}")

        rel_key = str(file_path.relative_to(REPO_ROOT))
        hist_rec = history.get(rel_key, {})
        file_sha = sha256_file(file_path)

        entry: dict[str, Any] = {
            "bureau_dir": item["bureau_dir"],
            "bureau_name": item["bureau_name"],
            "category": item["category"],
            "url": item["url"],
            "raw_file": rel_key,
            "bytes": file_path.stat().st_size,
            "sha256": file_sha,
            "fetched_at_utc": hist_rec.get("fetched_at_utc"),
            "last_checked_at_utc": hist_rec.get("last_checked_at_utc"),
            "etag": hist_rec.get("etag"),
            "last_modified": hist_rec.get("last_modified"),
            "revision_history": list(hist_rec.get("revision_history", [])),
            "extracted_files": [],
            "skipped_zip_members": [],
        }

        if file_path.suffix.lower() == ".zip":
            extract_root = file_path.parent / "extracted" / file_path.stem
            plan = validate_zip_extraction_plan(file_path, extract_root)
            for p_entry in plan:
                if not p_entry["keep"]:
                    entry["skipped_zip_members"].append(
                        {"member": p_entry["decoded"], "reason": p_entry["reason"]}
                    )

            if not _extract_dir_matches_plan(extract_root, plan):
                _extract_zip_atomically(file_path, extract_root, plan)

            for p_entry in plan:
                if not p_entry["keep"]:
                    continue
                dest = p_entry["dest"]
                entry["extracted_files"].append(
                    {
                        "path": str(dest.relative_to(REPO_ROOT)),
                        "bytes": dest.stat().st_size,
                        "sha256": sha256_file(dest),
                        "classification": p_entry["reason"],
                    }
                )

        manifest_items.append(entry)

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot,
        "total_targets": len(manifest_items),
        "items": manifest_items,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written: {manifest_path.relative_to(REPO_ROOT)}")


def run_self_test() -> None:
    """Offline test for same-month corrections, Excel validation, partial failures, and history safety."""
    import io
    import openpyxl

    def make_zip_bytes(members: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        return buf.getvalue()

    def make_xlsx_bytes(sheet_title: str, cell_value: str) -> bytes:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_title
        ws["A1"] = cell_value
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        return buf.getvalue()

    zip_v1 = make_zip_bytes({"01_ika.xlsx": b"v1-excel-bytes", "01_yakkyoku.xlsx": b"skip-pharmacy"})
    zip_v2 = make_zip_bytes({"01_ika_revised.xlsx": b"v2-revised-excel-bytes"})
    zip2_v1 = make_zip_bytes({"02_ika.xlsx": b"v1-tohoku-bytes"})
    zip2_v2 = make_zip_bytes({"02_ika.xlsx": b"v2-tohoku-bytes"})
    zip2_v3 = make_zip_bytes({"02_ika.xlsx": b"v3-tohoku-bytes"})
    xlsx_v1 = make_xlsx_bytes("Sheet1", "v1-direct-xlsx")
    xlsx_v2 = make_xlsx_bytes("Sheet1", "v2-direct-xlsx-revised")

    routes: dict[str, dict[str, Any]] = {
        "https://example.invalid/hokkaido.zip": {"body": zip_v1, "etag": '"v1"', "status": 200},
        "https://example.invalid/tohoku.zip": {"body": zip2_v1, "etag": '"t1"', "status": 200},
        "https://example.invalid/direct.xlsx": {"body": xlsx_v1, "etag": '"x1"', "status": 200},
    }

    class FakeResponse:
        def __init__(self, body: bytes, etag: str) -> None:
            self._bio = io.BytesIO(body)
            self.headers = {"ETag": etag}

        def read(self, n: int = -1) -> bytes:
            return self._bio.read(n)

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(req: urllib.request.Request, timeout: int = 60) -> FakeResponse:
        entry = routes[req.full_url]
        if entry.get("raise_url_error"):
            raise urllib.error.URLError("simulated network failure")
        if entry["status"] != 200:
            raise urllib.error.HTTPError(req.full_url, int(entry["status"]), "err", {}, None)
        inm = req.get_header("If-none-match") or req.get_header("If-None-Match")
        if inm and inm == entry["etag"]:
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        return FakeResponse(entry["body"], entry["etag"])

    global REPO_ROOT, RAW_ROOT, DOCS_DIR, _save_download_history
    orig_roots = (REPO_ROOT, RAW_ROOT, DOCS_DIR)
    orig_urlopen = urllib.request.urlopen
    orig_save_history = _save_download_history
    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            REPO_ROOT = root
            RAW_ROOT = root / "data" / "raw" / "hokenshisetsu"
            DOCS_DIR = root / "subprojects" / "hokenshisetsu" / "docs"

            snapshot = "202612"
            targets = [
                {
                    "bureau_dir": "01_hokkaido",
                    "bureau_name": "北海道厚生局",
                    "category": "医科",
                    "url": "https://example.invalid/hokkaido.zip",
                    "filename": "hokkaido.zip",
                    "discovered_ym": snapshot,
                },
                {
                    "bureau_dir": "03_kantoshinetsu",
                    "bureau_name": "関東信越厚生局",
                    "category": "医科",
                    "url": "https://example.invalid/direct.xlsx",
                    "filename": "direct.xlsx",
                    "discovered_ym": snapshot,
                },
            ]

            # 1. Initial download + extract (ZIP + direct Excel)
            c1 = run_download(snapshot, targets, download_japanpost=False, refresh=False)
            assert c1 == {"downloaded": 2, "updated": 0, "unchanged": 0}
            run_extract_and_manifest(snapshot, targets)
            raw_zip = RAW_ROOT / snapshot / "01_hokkaido" / "hokkaido.zip"
            ext_dir = RAW_ROOT / snapshot / "01_hokkaido" / "extracted" / "hokkaido"
            raw_xlsx = RAW_ROOT / snapshot / "03_kantoshinetsu" / "direct.xlsx"
            assert (ext_dir / "01_ika.xlsx").read_bytes() == b"v1-excel-bytes"
            assert not (ext_dir / "01_yakkyoku.xlsx").exists()
            assert raw_xlsx.read_bytes() == xlsx_v1

            # 2. Unchanged re-run (HTTP 304) and forced refresh with identical content
            c2 = run_download(snapshot, targets, download_japanpost=False, refresh=False)
            assert c2 == {"downloaded": 0, "updated": 0, "unchanged": 2}
            c3 = run_download(snapshot, targets, download_japanpost=False, refresh=True)
            assert c3 == {"downloaded": 0, "updated": 0, "unchanged": 2}
            assert not (RAW_ROOT / "_archive" / snapshot / "01_hokkaido").exists()
            assert not (RAW_ROOT / "_archive" / snapshot / "03_kantoshinetsu").exists()

            # 3. Corrupt ZIP rejection preserves existing ZIP & extracted files
            routes["https://example.invalid/hokkaido.zip"] = {"body": b"not-a-zip-html", "etag": '"badz"', "status": 200}
            failed = False
            try:
                run_download(snapshot, [targets[0]], download_japanpost=False, refresh=True)
            except Exception:
                failed = True
            assert failed, "Corrupt ZIP should have raised an error"
            assert raw_zip.read_bytes() == zip_v1
            assert (ext_dir / "01_ika.xlsx").read_bytes() == b"v1-excel-bytes"
            routes["https://example.invalid/hokkaido.zip"] = {"body": zip_v1, "etag": '"v1"', "status": 200}

            # 4. 4-A: Direct Excel rejection for HTML, empty bytes, corrupt Excel, and plain ZIP
            for bad_label, bad_bytes in (
                ("html", b"<html><body>503 Service Unavailable</body></html>"),
                ("empty", b""),
                ("corrupt_xlsx", b"PK\x03\x04corrupted-not-xlsx"),
                ("plain_zip", zip_v1),
            ):
                routes["https://example.invalid/direct.xlsx"] = {
                    "body": bad_bytes,
                    "etag": f'"{bad_label}"',
                    "status": 200,
                }
                xlsx_failed = False
                try:
                    run_download(snapshot, [targets[1]], download_japanpost=False, refresh=True)
                except Exception:
                    xlsx_failed = True
                assert xlsx_failed, f"Invalid direct Excel ({bad_label}) should have failed"
                assert raw_xlsx.read_bytes() == xlsx_v1, f"Existing Excel changed on {bad_label}"

            # Valid revised direct Excel succeeds and tracks revision history
            routes["https://example.invalid/direct.xlsx"] = {"body": xlsx_v2, "etag": '"x2"', "status": 200}
            routes["https://example.invalid/hokkaido.zip"] = {"body": zip_v2, "etag": '"v2"', "status": 200}
            c4 = run_download(snapshot, targets, download_japanpost=False, refresh=False)
            assert c4 == {"downloaded": 0, "updated": 2, "unchanged": 0}
            run_extract_and_manifest(snapshot, targets)

            assert raw_zip.read_bytes() == zip_v2
            assert raw_xlsx.read_bytes() == xlsx_v2
            assert not (ext_dir / "01_ika.xlsx").exists(), "Stale extracted file must be removed"
            assert (ext_dir / "01_ika_revised.xlsx").read_bytes() == b"v2-revised-excel-bytes"

            manifest = json.loads((DOCS_DIR / f"download_manifest_{snapshot}.json").read_text(encoding="utf-8"))
            assert manifest["items"][0]["sha256"] == sha256_file(raw_zip)
            assert len(manifest["items"][0]["revision_history"]) == 1
            assert (root / manifest["items"][0]["revision_history"][0]["archived_raw_file"]).read_bytes() == zip_v1
            assert manifest["items"][1]["sha256"] == sha256_file(raw_xlsx)
            assert len(manifest["items"][1]["revision_history"]) == 1
            assert (root / manifest["items"][1]["revision_history"][0]["archived_raw_file"]).read_bytes() == xlsx_v1

            # 5. 4-B: Multi-file partial failure (item 1 updates, item 2 fails -> recover -> re-run)
            snapshot_b = "202701"
            targets_b = [
                {
                    "bureau_dir": "01_hokkaido",
                    "bureau_name": "北海道厚生局",
                    "category": "医科",
                    "url": "https://example.invalid/hokkaido.zip",
                    "filename": "hokkaido.zip",
                    "discovered_ym": snapshot_b,
                },
                {
                    "bureau_dir": "02_tohoku",
                    "bureau_name": "東北厚生局",
                    "category": "医科",
                    "url": "https://example.invalid/tohoku.zip",
                    "filename": "tohoku.zip",
                    "discovered_ym": snapshot_b,
                },
            ]
            routes["https://example.invalid/hokkaido.zip"] = {"body": zip_v1, "etag": '"v1"', "status": 200}
            routes["https://example.invalid/tohoku.zip"] = {"body": zip2_v1, "etag": '"t1"', "status": 200}
            run_download(snapshot_b, targets_b, download_japanpost=False, refresh=False)
            run_extract_and_manifest(snapshot_b, targets_b)

            # Update both to v2, but fail item 2 with network error after item 1 succeeds
            routes["https://example.invalid/hokkaido.zip"] = {"body": zip_v2, "etag": '"v2"', "status": 200}
            routes["https://example.invalid/tohoku.zip"] = {
                "body": zip2_v2,
                "etag": '"t2"',
                "status": 200,
                "raise_url_error": True,
            }
            partial_failed = False
            try:
                run_download(snapshot_b, targets_b, download_japanpost=False, refresh=False)
            except urllib.error.URLError:
                partial_failed = True
            assert partial_failed

            # Recover item 2 and re-run -> item 1's v1->v2 revision_history must be preserved without duplication
            routes["https://example.invalid/tohoku.zip"] = {"body": zip2_v2, "etag": '"t2"', "status": 200}
            c_rec = run_download(snapshot_b, targets_b, download_japanpost=False, refresh=False)
            assert c_rec == {"downloaded": 0, "updated": 1, "unchanged": 1}
            # Re-run again -> no duplicate history
            c_idem = run_download(snapshot_b, targets_b, download_japanpost=False, refresh=True)
            assert c_idem == {"downloaded": 0, "updated": 0, "unchanged": 2}
            run_extract_and_manifest(snapshot_b, targets_b)

            man_b = json.loads((DOCS_DIR / f"download_manifest_{snapshot_b}.json").read_text(encoding="utf-8"))
            assert len(man_b["items"][0]["revision_history"]) == 1
            assert (root / man_b["items"][0]["revision_history"][0]["archived_raw_file"]).read_bytes() == zip_v1
            assert len(man_b["items"][1]["revision_history"]) == 1
            assert (root / man_b["items"][1]["revision_history"][0]["archived_raw_file"]).read_bytes() == zip2_v1

            # 6. 4-B: Simulate history save failure -> raw file must not advance ahead of history; recovery succeeds
            routes["https://example.invalid/tohoku.zip"] = {"body": zip2_v3, "etag": '"t3"', "status": 200}

            def failing_save_history(snap: str, hist: dict[str, dict[str, Any]]) -> None:
                raise OSError("simulated disk full saving history")

            _save_download_history = failing_save_history  # type: ignore[assignment]
            save_failed = False
            try:
                run_download(snapshot_b, [targets_b[1]], download_japanpost=False, refresh=False)
            except OSError:
                save_failed = True
            assert save_failed
            _save_download_history = orig_save_history

            raw_tohoku = RAW_ROOT / snapshot_b / "02_tohoku" / "tohoku.zip"
            assert raw_tohoku.read_bytes() == zip2_v2, "Raw file must remain at v2 when history save fails"

            c_rec2 = run_download(snapshot_b, [targets_b[1]], download_japanpost=False, refresh=False)
            assert c_rec2 == {"downloaded": 0, "updated": 1, "unchanged": 0}
            run_extract_and_manifest(snapshot_b, targets_b)
            assert raw_tohoku.read_bytes() == zip2_v3
            man_b2 = json.loads((DOCS_DIR / f"download_manifest_{snapshot_b}.json").read_text(encoding="utf-8"))
            assert man_b2["items"][1]["sha256"] == sha256_file(raw_tohoku)
            assert len(man_b2["items"][1]["revision_history"]) == 2
            assert man_b2["items"][1]["extracted_files"][0]["sha256"] == sha256_file(
                RAW_ROOT / snapshot_b / "02_tohoku" / "extracted" / "tohoku" / "02_ika.xlsx"
            )
    finally:
        urllib.request.urlopen = orig_urlopen
        _save_download_history = orig_save_history
        REPO_ROOT, RAW_ROOT, DOCS_DIR = orig_roots


def main() -> None:
    parser = argparse.ArgumentParser(description="Generalized Kouseikyoku raw downloader.")
    parser.add_argument("--snapshot", help="Target snapshot YYYYMM (e.g. 202701)")
    parser.add_argument(
        "--mode",
        choices=["discover", "download", "extract", "all"],
        default="all",
        help="Operation mode",
    )
    parser.add_argument("--targets-json", type=Path, default=None, help="Optional explicit targets JSON")
    parser.add_argument(
        "--download-japanpost",
        action="store_true",
        help="Also download latest Japan Post utf_ken_all.zip into snapshot dir",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-discover targets (when no --targets-json) and bypass conditional HTTP cache headers",
    )
    parser.add_argument("--self-test", action="store_true", help="Run offline unit tests")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        print("DOWNLOAD_SELF_TEST_PASSED")
        return

    if not args.snapshot:
        parser.error("--snapshot is required unless --self-test is specified")

    if not re.fullmatch(r"\d{6}", args.snapshot):
        raise ValueError(f"--snapshot must be YYYYMM, got {args.snapshot!r}")

    if args.mode == "discover":
        discover_targets(args.snapshot)
        return

    targets = load_targets(args.snapshot, args.targets_json, refresh=args.refresh and args.mode == "all")
    if args.mode in ("download", "all"):
        run_download(args.snapshot, targets, args.download_japanpost, refresh=args.refresh)
    if args.mode in ("extract", "all"):
        run_extract_and_manifest(args.snapshot, targets)


if __name__ == "__main__":
    main()
