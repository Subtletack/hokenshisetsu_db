"""Download and selectively extract November 2026 Kouseikyoku designated medical institution files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_BASE_DIR = REPO_ROOT / "data" / "raw" / "hokenshisetsu" / "202611"
MANIFEST_PATH = REPO_ROOT / "subprojects" / "hokenshisetsu" / "docs" / "download_manifest_202611.json"

TARGETS: list[dict[str, str]] = [
    # 01 Hokkaido
    {
        "bureau_dir": "01_hokkaido",
        "bureau_name": "北海道厚生局",
        "category": "医科（郡市区別）",
        "url": "https://kouseikyoku.mhlw.go.jp/hokkaido/000506053.xlsx",
        "filename": "hokkaido_zentai_ika_r0811.xlsx",
    },
    {
        "bureau_dir": "01_hokkaido",
        "bureau_name": "北海道厚生局",
        "category": "医科（病院・コード内容別）",
        "url": "https://kouseikyoku.mhlw.go.jp/hokkaido/000506059.xlsx",
        "filename": "hokkaido_code_ika_byoin_r0811.xlsx",
    },
    {
        "bureau_dir": "01_hokkaido",
        "bureau_name": "北海道厚生局",
        "category": "医科（診療所・コード内容別）",
        "url": "https://kouseikyoku.mhlw.go.jp/hokkaido/000506061.xlsx",
        "filename": "hokkaido_code_ika_shinryojo_r0811.xlsx",
    },
    # 02 Tohoku
    {
        "bureau_dir": "02_tohoku",
        "bureau_name": "東北厚生局",
        "category": "医科",
        "url": "https://kouseikyoku.mhlw.go.jp/tohoku/shitei-touhoku-ika-r0811.xlsx",
        "filename": "shitei-touhoku-ika-r0811.xlsx",
    },
    {
        "bureau_dir": "02_tohoku",
        "bureau_name": "東北厚生局",
        "category": "歯科併設",
        "url": "https://kouseikyoku.mhlw.go.jp/tohoku/shitei-touhoku-shikaheisetsu-r0811.xlsx",
        "filename": "shitei-touhoku-shikaheisetsu-r0811.xlsx",
    },
    {
        "bureau_dir": "02_tohoku",
        "bureau_name": "東北厚生局",
        "category": "医科併設（確認用）",
        "url": "https://kouseikyoku.mhlw.go.jp/tohoku/shitei-touhoku-ikaheisetsu-r0811.xlsx",
        "filename": "shitei-touhoku-ikaheisetsu-r0811.xlsx",
    },
    # 03 Kantoshinetsu
    {
        "bureau_dir": "03_kantoshinetsu",
        "bureau_name": "関東信越厚生局",
        "category": "医科",
        "url": "https://kouseikyoku.mhlw.go.jp/kantoshinetsu/shitei_ika_r0811.zip",
        "filename": "shitei_ika_r0811.zip",
    },
    {
        "bureau_dir": "03_kantoshinetsu",
        "bureau_name": "関東信越厚生局",
        "category": "医科（歯科併設）",
        "url": "https://kouseikyoku.mhlw.go.jp/kantoshinetsu/shitei_shikaheisetsu_r0811.zip",
        "filename": "shitei_shikaheisetsu_r0811.zip",
    },
    {
        "bureau_dir": "03_kantoshinetsu",
        "bureau_name": "関東信越厚生局",
        "category": "歯科（医科併設・確認用）",
        "url": "https://kouseikyoku.mhlw.go.jp/kantoshinetsu/shitei_ikaheisetsu_r0811.zip",
        "filename": "shitei_ikaheisetsu_r0811.zip",
    },
    # 04 Tokaihokuriku
    {
        "bureau_dir": "04_tokaihokuriku",
        "bureau_name": "東海北陸厚生局",
        "category": "医科",
        "url": "https://kouseikyoku.mhlw.go.jp/tokaihokuriku/2611-01-01.zip",
        "filename": "2611-01-01.zip",
    },
    # 05 Kinki
    {
        "bureau_dir": "05_kinki",
        "bureau_name": "近畿厚生局",
        "category": "医科・医科併設",
        "url": "https://kouseikyoku.mhlw.go.jp/kinki/2026.11_kikanzentai_ika.zip",
        "filename": "2026.11_kikanzentai_ika.zip",
    },
    {
        "bureau_dir": "05_kinki",
        "bureau_name": "近畿厚生局",
        "category": "歯科・歯科併設（歯科併設のみ展開）",
        "url": "https://kouseikyoku.mhlw.go.jp/kinki/s2026.11_kikanzentai_sika.zip",
        "filename": "s2026.11_kikanzentai_sika.zip",
    },
    # 06 Chugokushikoku
    {
        "bureau_dir": "06_chugokushikoku",
        "bureau_name": "中国四国厚生局",
        "category": "医科",
        "url": "https://kouseikyoku.mhlw.go.jp/chugokushikoku/000506607.zip",
        "filename": "000506607.zip",
    },
    # 07 Shikoku
    {
        "bureau_dir": "07_shikoku",
        "bureau_name": "四国厚生支局",
        "category": "医科",
        "url": "https://kouseikyoku.mhlw.go.jp/shikoku/000506264.zip",
        "filename": "000506264.zip",
    },
    # 08 Kyushu
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "福岡県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507012.zip",
        "filename": "40_fukuoka_000507012.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "佐賀県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507013.zip",
        "filename": "41_saga_000507013.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "長崎県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507014.zip",
        "filename": "42_nagasaki_000507014.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "熊本県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507015.zip",
        "filename": "43_kumamoto_000507015.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "大分県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507016.zip",
        "filename": "44_oita_000507016.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "宮崎県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507017.zip",
        "filename": "45_miyazaki_000507017.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "鹿児島県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507018.zip",
        "filename": "46_kagoshima_000507018.zip",
    },
    {
        "bureau_dir": "08_kyushu",
        "bureau_name": "九州厚生局",
        "category": "沖縄県",
        "url": "https://kouseikyoku.mhlw.go.jp/kyushu/000507347.zip",
        "filename": "47_okinawa_000507347.zip",
    },
]


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
        # Python 3.11+ decodes Info-ZIP Unicode Path Extra Field (0x7075) automatically.
        return info.filename
    for enc in ("cp932", "utf-8"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def validate_zip_extraction_plan(zip_path: Path, extract_root: Path) -> list[dict[str, Any]]:
    """Validate member filename decoding, path collisions, and existing file hashes without modifying disk."""
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

            member_bytes = zf.read(info)
            member_sha256 = hashlib.sha256(member_bytes).hexdigest()
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


def should_extract_member(decoded_name: str) -> tuple[bool, str]:
    normalized = decoded_name.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    if not basename or basename.startswith("._") or basename == ".DS_Store" or "__MACOSX" in normalized:
        return False, "metadata_or_dir"

    lower = basename.lower()
    if "薬局" in basename or "yakkyoku" in lower or "調剤" in basename:
        return False, "pharmacy_only"

    if "併設" in basename or "heisetsu" in lower or "heisetu" in lower:
        return True, "heisetsu"

    # Skip dental-only files while keeping medical files.
    if "歯科" in basename and "医科" not in basename:
        return False, "dental_only"
    if re.search(r"(?:^|[^a-z])sh?ika(?:[^a-z]|$)", lower) and not re.search(r"(?:^|[^a-z])ika(?:[^a-z]|$)", lower):
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


def run_download() -> None:
    headers = {"User-Agent": "Mozilla/5.0"}
    for item in TARGETS:
        out_dir = RAW_BASE_DIR / item["bureau_dir"]
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


def run_inspect_zips() -> None:
    for item in TARGETS:
        zip_path = RAW_BASE_DIR / item["bureau_dir"] / item["filename"]
        if zip_path.suffix.lower() != ".zip" or not zip_path.exists():
            continue
        print(f"=== {zip_path.relative_to(REPO_ROOT)} ===")
        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = decode_zip_filename(info)
                keep, reason = should_extract_member(name)
                status = "KEEP" if keep else "SKIP"
                print(f"  [{status}:{reason}] {name} ({info.file_size} bytes)")


def run_extract_and_manifest() -> None:
    manifest_items: list[dict[str, Any]] = []
    for item in TARGETS:
        file_path = RAW_BASE_DIR / item["bureau_dir"] / item["filename"]
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
        "snapshot_label": "令和8年11月1日現在（2026年11月時点）",
        "total_targets": len(manifest_items),
        "items": manifest_items,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest written: {MANIFEST_PATH.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and extract Kouseikyoku raw data.")
    parser.add_argument(
        "--mode",
        choices=["download", "inspect-zips", "extract"],
        required=True,
        help="Operation mode",
    )
    args = parser.parse_args()
    if args.mode == "download":
        run_download()
    elif args.mode == "inspect-zips":
        run_inspect_zips()
    elif args.mode == "extract":
        run_extract_and_manifest()


if __name__ == "__main__":
    main()
