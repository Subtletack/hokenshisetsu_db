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
import re
import unicodedata
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


def load_targets(snapshot: str, targets_json: Path | None = None) -> list[dict[str, str]]:
    path = targets_json or (DOCS_DIR / f"discovered_targets_{snapshot}.json")
    if path.exists():
        targets = json.loads(path.read_text(encoding="utf-8"))
        validate_discovered_targets(targets, snapshot)
        return targets
    return discover_targets(snapshot)


def run_download(snapshot: str, targets: list[dict[str, str]], download_japanpost: bool) -> None:
    raw_base = RAW_ROOT / snapshot
    headers = {"User-Agent": "Mozilla/5.0"}
    for item in targets:
        out_dir = raw_base / item["bureau_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / item["filename"]
        if out_path.exists():
            print(f"SKIP (exists): {out_path.relative_to(REPO_ROOT)} ({out_path.stat().st_size} bytes)")
            continue
        req = urllib.request.Request(item["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        out_path.write_bytes(data)
        print(f"DOWNLOADED: {out_path.relative_to(REPO_ROOT)} ({len(data)} bytes)")

    if download_japanpost:
        jp_out = raw_base / "utf_ken_all.zip"
        if not jp_out.exists():
            try:
                req = urllib.request.Request(JAPANPOST_UTF_KEN_ALL_URL, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    jp_out.write_bytes(resp.read())
                print(f"DOWNLOADED: {jp_out.relative_to(REPO_ROOT)}")
            except Exception as exc:
                fallback = RAW_ROOT / "shared" / "utf_ken_all.zip"
                if fallback.exists():
                    print(
                        f"WARN: Japan Post download failed ({exc}); "
                        f"using existing fallback {fallback.relative_to(REPO_ROOT)}"
                    )
                else:
                    raise


def run_extract_and_manifest(snapshot: str, targets: list[dict[str, str]]) -> None:
    raw_base = RAW_ROOT / snapshot
    manifest_path = DOCS_DIR / f"download_manifest_{snapshot}.json"
    manifest_items: list[dict[str, Any]] = []

    for item in targets:
        file_path = raw_base / item["bureau_dir"] / item["filename"]
        if not file_path.exists():
            raise FileNotFoundError(f"Missing downloaded file: {file_path}")

        entry: dict[str, Any] = {
            "bureau_dir": item["bureau_dir"],
            "bureau_name": item["bureau_name"],
            "category": item["category"],
            "url": item["url"],
            "raw_file": str(file_path.relative_to(REPO_ROOT)),
            "bytes": file_path.stat().st_size,
            "sha256": sha256_file(file_path),
            "extracted_files": [],
            "skipped_zip_members": [],
        }

        if file_path.suffix.lower() == ".zip":
            extract_root = file_path.parent / "extracted" / file_path.stem
            extract_root.mkdir(parents=True, exist_ok=True)
            plan = validate_zip_extraction_plan(file_path, extract_root)
            with zipfile.ZipFile(file_path, "r") as zf:
                for p_entry in plan:
                    if not p_entry["keep"]:
                        entry["skipped_zip_members"].append(
                            {"member": p_entry["decoded"], "reason": p_entry["reason"]}
                        )
                        continue
                    dest = p_entry["dest"]
                    if p_entry["exists"]:
                        if not p_entry["hash_matches"]:
                            raise RuntimeError(
                                f"Existing extracted file hash mismatch for {dest.relative_to(REPO_ROOT)}"
                            )
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(p_entry["info"], "r") as src:
                            dest.write_bytes(src.read())
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Generalized Kouseikyoku raw downloader.")
    parser.add_argument("--snapshot", required=True, help="Target snapshot YYYYMM (e.g. 202701)")
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
    args = parser.parse_args()

    if not re.fullmatch(r"\d{6}", args.snapshot):
        raise ValueError(f"--snapshot must be YYYYMM, got {args.snapshot!r}")

    if args.mode == "discover":
        discover_targets(args.snapshot)
        return

    targets = load_targets(args.snapshot, args.targets_json)
    if args.mode in ("download", "all"):
        run_download(args.snapshot, targets, args.download_japanpost)
    if args.mode in ("extract", "all"):
        run_extract_and_manifest(args.snapshot, targets)


if __name__ == "__main__":
    main()
