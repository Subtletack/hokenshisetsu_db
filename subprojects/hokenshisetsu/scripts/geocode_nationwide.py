#!/usr/bin/env python3
"""Geocode nationwide medical institutions using jageocoder (local government open-data dictionary).

Outputs a separate release asset (Option B) so Git-diffable prefecture CSVs remain clean:
  release/<snapshot>/hokenshisetsu_geocode_<snapshot>.csv.gz
and updates release/<snapshot>/SHA256SUMS.txt idempotently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
SUB_DIR = SCRIPT_DIR.parent
REPO_ROOT = SUB_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import download_raw as dr  # noqa: E402

DEFAULT_DB_DIR = REPO_ROOT / "data" / "raw" / "jageocoder_db"
DEFAULT_SOURCE_LABEL = "jageocoder_jukyo_all_v22"
DEFAULT_PUA_MAPPING_PATH = SCRIPT_DIR / "pua_mapping.json"
RULES_VERSION = "2026-11-18.g3"

GEOCODE_COLUMNS = [
    "medical_code_10digit",
    "latitude",
    "longitude",
    "geocode_level",
    "geocode_level_code",
    "geocode_source",
    "geocode_matched",
]

LEVEL_NAME_MAP: dict[int, str] = {
    8: "house_number",
    7: "gaiku",
    6: "aza",
    5: "oaza_cho",
    4: "municipality",
    3: "municipality",
    2: "prefecture",
    1: "prefecture",
}

MUNICIPALITY_VARIANT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("塩釜市", "塩竈市"),
    ("龍ヶ崎市", "龍ケ崎市"),
    ("鎌ヶ谷市", "鎌ケ谷市"),
    ("袖ヶ浦市", "袖ケ浦市"),
    ("桧原村", "檜原村"),
    ("梼原町", "檮原町"),
    ("須惠町", "須恵町"),
    ("三宅島三宅村", "三宅村"),
    ("八丈島八丈町", "八丈町"),
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_geocode_input_signature(df: pd.DataFrame) -> str:
    """Compute a deterministic SHA-256 over (medical_code_10digit, pref_name, municipality_name, address)."""
    h = hashlib.sha256()
    has_muni = "municipality_name" in df.columns
    has_bureau = "kouseikyoku_code" in df.columns
    cols = ["medical_code_10digit", "pref_name"]
    if has_muni:
        cols.append("municipality_name")
    if has_bureau:
        cols.append("kouseikyoku_code")
    cols.append("address")
    sub = df[cols].sort_values("medical_code_10digit", kind="mergesort")
    for row in sub.itertuples(index=False, name=None):
        h.update(("\t".join(str(x) for x in row) + "\n").encode("utf-8"))
    return h.hexdigest()


def compute_pua_mapping_sha256(path: Path = DEFAULT_PUA_MAPPING_PATH) -> str:
    if path.exists():
        return sha256_file(path)
    return hashlib.sha256(b"[]").hexdigest()


def compute_dictionary_signature(db_dir: Path) -> str:
    """Compute a content-based SHA-256 signature over all files in the dictionary directory."""
    if not db_dir.exists() or not db_dir.is_dir():
        return ""
    h = hashlib.sha256()
    files = sorted(
        (p for p in db_dir.rglob("*") if p.is_file() and not p.name.startswith("._")),
        key=lambda p: p.relative_to(db_dir).as_posix(),
    )
    if not files:
        return ""
    for p in files:
        rel = p.relative_to(db_dir).as_posix()
        file_hash = sha256_file(p)
        h.update(f"FILE:{rel}:{p.stat().st_size}:{file_hash}\n".encode("utf-8"))
    return h.hexdigest()


def get_jageocoder_version() -> str:
    import jageocoder

    return str(getattr(jageocoder, "__version__", "unknown"))


def get_current_geocode_conditions(
    db_dir: Path = DEFAULT_DB_DIR,
    pua_mapping_path: Path = DEFAULT_PUA_MAPPING_PATH,
    rules_version: str = RULES_VERSION,
    dictionary_signature: str | None = None,
    jageocoder_version: str | None = None,
) -> dict[str, str]:
    """Return current rule, PUA mapping, dictionary, and jageocoder identifiers."""
    dict_sig = dictionary_signature if dictionary_signature is not None else compute_dictionary_signature(db_dir)
    jg_ver = jageocoder_version if jageocoder_version is not None else get_jageocoder_version()
    return {
        "rules_version": rules_version,
        "pua_mapping_sha256": compute_pua_mapping_sha256(pua_mapping_path),
        "dictionary_signature": dict_sig,
        "jageocoder_version": jg_ver,
    }


def load_verified_pua_map(path: Path = DEFAULT_PUA_MAPPING_PATH) -> dict[tuple[str, str], str]:
    """Load verified, source_scope-bound PUA -> Unicode replacements."""
    if not path.exists():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8"))
    mapping: dict[tuple[str, str], str] = {}
    for item in entries:
        if item.get("review_status") != "verified":
            continue
        scope = str(item.get("source_scope", "")).strip()
        cp = str(item.get("original_codepoint", "")).strip().upper()
        rep = str(item.get("unicode_replacement", ""))
        if not scope or not cp.startswith("U+") or not rep:
            continue
        ch = chr(int(cp[2:], 16))
        mapping[(scope, ch)] = rep
    return mapping


def apply_scoped_pua(
    text: str,
    source_scope: str,
    pua_map: dict[tuple[str, str], str],
) -> str:
    """Replace only PUA characters verified for the exact source_scope."""
    if not source_scope or not pua_map:
        return text
    out_chars: list[str] = []
    changed = False
    for ch in text:
        if unicodedata.category(ch) == "Co":
            rep = pua_map.get((source_scope, ch))
            if rep is not None:
                out_chars.append(rep)
                changed = True
                continue
        out_chars.append(ch)
    return "".join(out_chars) if changed else text


def extract_kyoto_formal_candidate(
    pref_name: str,
    municipality_name: str,
    addr_no_ws: str,
) -> str | None:
    """For Kyoto ward street-name addresses (上る/下る/東入/西入), extract trailing town+banchi."""
    if pref_name != "京都府" or not municipality_name.startswith("京都市"):
        return None
    matches = list(re.finditer(r"(?:上る|下る|上ル|下ル|東入る|西入る|東入|西入)", addr_no_ws))
    if not matches:
        return None
    tail = addr_no_ws[matches[-1].end() :]
    if not re.match(r"^[^\d]{1,20}\d", tail):
        return None
    return f"京都府{municipality_name}{tail}"


def _normalize_municipality_token(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "")
    s = re.sub(r"\s+", "", s)
    for src, dst in MUNICIPALITY_VARIANT_REPLACEMENTS:
        s = s.replace(src, dst)
    s = s.replace("ヶ", "ケ").replace("惠", "恵")
    return s


def build_query_address(pref_name: str, address: str) -> str:
    """Normalize address and prepend pref_name when omitted."""
    pref = unicodedata.normalize("NFKC", pref_name or "").strip()
    addr = unicodedata.normalize("NFKC", address or "").strip()
    if not addr:
        return pref
    if pref and not addr.startswith(pref):
        return f"{pref}{addr}"
    return addr


def build_query_candidates(
    pref_name: str,
    municipality_name: str,
    address: str,
    source_scope: str = "",
    pua_map: dict[tuple[str, str], str] | None = None,
) -> list[str]:
    """Generate ordered query candidates: baseline first, then whitespace/variant/Kyoto/scoped-PUA normalizations."""
    pref = unicodedata.normalize("NFKC", pref_name or "").strip()
    muni = unicodedata.normalize("NFKC", municipality_name or "").strip()
    base = build_query_address(pref, address)
    if not base:
        return []

    candidates: list[str] = [base]

    def add_cand(s: str) -> None:
        if s and s not in candidates:
            candidates.append(s)

    no_ws = re.sub(r"\s+", "", base)
    add_cand(no_ws)

    # Apply municipality Kanji variant normalizations
    variant = no_ws
    for src, dst in MUNICIPALITY_VARIANT_REPLACEMENTS:
        variant = variant.replace(src, dst)
    add_cand(variant)

    # Scoped PUA replacement (only for verified entries matching source_scope)
    if pua_map and source_scope:
        pua_fixed = apply_scoped_pua(variant, source_scope, pua_map)
        if pua_fixed != variant:
            add_cand(pua_fixed)
            add_cand(re.sub(r"[‐‑‒–—―ー−－]", "-", pua_fixed))

    # Normalize prolonged sound marks / dash variants to ASCII hyphen
    dash_norm = re.sub(r"[‐‑‒–—―ー−－]", "-", variant)
    add_cand(dash_norm)

    # Kyoto street-name (通り名) -> formal town + banchi extraction
    kyoto_cand = extract_kyoto_formal_candidate(pref, muni, no_ws)
    if kyoto_cand:
        add_cand(kyoto_cand)
        add_cand(re.sub(r"[‐‑‒–—―ー−－]", "-", kyoto_cand))

    # If a leading building name precedes municipality_name, slice from municipality_name onwards
    if muni:
        muni_no_ws = re.sub(r"\s+", "", muni)
        idx = no_ws.find(muni_no_ws)
        if idx > len(pref):
            sliced = f"{pref}{no_ws[idx:]}"
            add_cand(sliced)
            add_cand(re.sub(r"[‐‑‒–—―ー−－]", "-", sliced))

    return candidates


def _get_node_fullname(node: Any) -> list[str] | None:
    fn = getattr(node, "get_fullname", None)
    if callable(fn):
        res = fn()
        if isinstance(res, (list, tuple)):
            return [str(x) for x in res]
    raw_fn = getattr(node, "fullname", None)
    if isinstance(raw_fn, (list, tuple)):
        return [str(x) for x in raw_fn]
    return None


def is_consistent_match(node: Any, pref_name: str, municipality_name: str) -> bool:
    """Verify matched node agrees with expected prefecture and municipality when available."""
    fullname = _get_node_fullname(node)
    if not fullname:
        return True
    pref = _normalize_municipality_token(pref_name)
    if pref and fullname[0]:
        if _normalize_municipality_token(fullname[0]) != pref:
            return False

    muni = _normalize_municipality_token(municipality_name)
    lvl = int(getattr(node, "level", -1))
    if muni and lvl >= 3 and len(fullname) >= 2:
        joined = _normalize_municipality_token("".join(fullname[1:4]))
        if muni not in joined:
            return False
    return True


def resolve_node_with_coords(node: Any) -> Any | None:
    """Walk up parent chain if node has sentinel coordinates (e.g., 999.9)."""
    cur = node
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        x = getattr(cur, "x", None)
        y = getattr(cur, "y", None)
        if x is not None and y is not None and abs(float(x)) <= 180.0 and abs(float(y)) <= 90.0:
            return cur
        cur = getattr(cur, "parent", None)
    return None


def map_geocode_level(level_code: int | None) -> str:
    if level_code is None:
        return "unmatched"
    return LEVEL_NAME_MAP.get(int(level_code), "unmatched")


def geocode_dataframe(
    df: pd.DataFrame,
    source_label: str = DEFAULT_SOURCE_LABEL,
    search_fn: Callable[[str], list[Any]] | None = None,
    pua_map: dict[tuple[str, str], str] | None = None,
) -> pd.DataFrame:
    if search_fn is None:
        import jageocoder

        search_fn = jageocoder.searchNode
    if pua_map is None:
        pua_map = load_verified_pua_map()

    has_muni = "municipality_name" in df.columns
    has_bureau = "kouseikyoku_code" in df.columns
    cols = (
        ["medical_code_10digit", "pref_name", "address"]
        + (["municipality_name"] if has_muni else [])
        + (["kouseikyoku_code"] if has_bureau else [])
    )

    rows: list[dict[str, str]] = []
    for rec in df[cols].to_dict("records"):
        code = str(rec["medical_code_10digit"])
        pref_name = str(rec.get("pref_name", ""))
        muni_name = str(rec.get("municipality_name", "")) if has_muni else ""
        source_scope = str(rec.get("kouseikyoku_code", "")) if has_bureau else ""
        address = str(rec.get("address", ""))

        candidates = build_query_candidates(
            pref_name,
            muni_name,
            address,
            source_scope=source_scope,
            pua_map=pua_map,
        )

        best_resolved: Any | None = None
        best_matched = ""
        best_level = -1

        for idx, query in enumerate(candidates):
            results = search_fn(query)
            if results:
                for cand_res in results:
                    resolved = resolve_node_with_coords(getattr(cand_res, "node", None))
                    if resolved is None:
                        continue
                    if not is_consistent_match(resolved, pref_name, muni_name):
                        continue
                    lvl = int(getattr(resolved, "level", -1))
                    if lvl > best_level:
                        best_level = lvl
                        best_resolved = resolved
                        best_matched = str(getattr(cand_res, "matched", "") or "")
                    break
            # Fast path: if baseline already resolved to house_number (8), skip extra candidates
            if idx == 0 and best_level >= 8:
                break

        if best_resolved is not None:
            lat_str = f"{float(best_resolved.y):.6f}"
            lon_str = f"{float(best_resolved.x):.6f}"
            level_label = map_geocode_level(best_level)
            level_code_str = str(best_level) if best_level >= 0 else ""
            source_str = source_label
            matched_str = best_matched
        else:
            lat_str = ""
            lon_str = ""
            level_label = "unmatched"
            level_code_str = ""
            source_str = ""
            matched_str = ""

        rows.append(
            {
                "medical_code_10digit": code,
                "latitude": lat_str,
                "longitude": lon_str,
                "geocode_level": level_label,
                "geocode_level_code": level_code_str,
                "geocode_source": source_str,
                "geocode_matched": matched_str,
            }
        )

    out_df = pd.DataFrame(rows, columns=GEOCODE_COLUMNS)
    out_df = out_df.sort_values("medical_code_10digit", kind="mergesort").reset_index(drop=True)
    return out_df


def update_sha256sums(release_dir: Path, target_filename: str) -> None:
    target_path = release_dir / target_filename
    if not target_path.exists():
        raise FileNotFoundError(f"Cannot checksum missing asset: {target_path}")
    new_digest = sha256_file(target_path)
    new_line = f"{new_digest}  {target_filename}"

    sums_path = release_dir / "SHA256SUMS.txt"
    lines: list[str] = []
    replaced = False
    if sums_path.exists():
        for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            parts = stripped.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == target_filename:
                lines.append(new_line)
                replaced = True
            else:
                lines.append(stripped)
    if not replaced:
        lines.append(new_line)

    fd, tmp_name = tempfile.mkstemp(dir=release_dir, prefix=".SHA256SUMS.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp_path, sums_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def load_input_snapshot(snapshot: str, repo_root: Path = REPO_ROOT) -> pd.DataFrame:
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
    raise FileNotFoundError(f"Snapshot CSV not found for {snapshot}: checked {gz_path} and {proc_path}")


def run_geocode(
    snapshot: str,
    db_dir: Path = DEFAULT_DB_DIR,
    repo_root: Path = REPO_ROOT,
    source_label: str = DEFAULT_SOURCE_LABEL,
    search_fn: Callable[[str], list[Any]] | None = None,
    pua_mapping_path: Path = DEFAULT_PUA_MAPPING_PATH,
) -> dict[str, Any]:
    dr.validate_snapshot_str(snapshot)
    if search_fn is None:
        if not db_dir.exists():
            raise FileNotFoundError(
                f"jageocoder dictionary directory not found: {db_dir}. "
                "Install dictionary with `jageocoder install-dictionary -y --db-dir=data/raw/jageocoder_db <zip>`."
            )
        import jageocoder

        jageocoder.init(db_dir=str(db_dir))
        conditions = get_current_geocode_conditions(
            db_dir=db_dir,
            pua_mapping_path=pua_mapping_path,
        )
    else:
        conditions = get_current_geocode_conditions(
            db_dir=db_dir,
            pua_mapping_path=pua_mapping_path,
            dictionary_signature=compute_dictionary_signature(db_dir) or "mock_dict",
            jageocoder_version="mock",
        )

    pua_map = load_verified_pua_map(pua_mapping_path)
    in_df = load_input_snapshot(snapshot, repo_root=repo_root)
    geo_df = geocode_dataframe(in_df, source_label=source_label, search_fn=search_fn, pua_map=pua_map)

    release_dir = repo_root / "release" / snapshot
    release_dir.mkdir(parents=True, exist_ok=True)
    out_filename = f"hokenshisetsu_geocode_{snapshot}.csv.gz"
    out_path = release_dir / out_filename

    fd, tmp_name = tempfile.mkstemp(dir=release_dir, prefix=f".{out_filename}.", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        geo_df.to_csv(tmp_path, index=False, encoding="utf-8-sig", compression="gzip")
        os.replace(tmp_path, out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    update_sha256sums(release_dir, out_filename)

    meta_dir = repo_root / "subprojects" / "hokenshisetsu" / "output"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / f"geocode_meta_{snapshot}.json"
    meta_payload = {
        "snapshot": snapshot,
        "input_signature": compute_geocode_input_signature(in_df),
        "rules_version": conditions["rules_version"],
        "pua_mapping_sha256": conditions["pua_mapping_sha256"],
        "dictionary_signature": conditions["dictionary_signature"],
        "jageocoder_version": conditions["jageocoder_version"],
        "geocode_file": out_filename,
        "geocode_sha256": sha256_file(out_path),
        "total_rows": int(len(geo_df)),
        "geocode_source": source_label,
    }
    meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    level_counts = {str(k): int(v) for k, v in geo_df["geocode_level"].value_counts().items()}
    matched_count = int((geo_df["geocode_level"] != "unmatched").sum())
    return {
        "snapshot": snapshot,
        "output_path": str(out_path.relative_to(repo_root)),
        "bytes": int(out_path.stat().st_size),
        "total_rows": int(len(geo_df)),
        "matched_rows": matched_count,
        "match_rate_pct": round(100.0 * matched_count / len(geo_df), 2) if len(geo_df) else 0.0,
        "geocode_level_counts": level_counts,
        "geocode_source": source_label,
    }


def run_self_test() -> None:
    from types import SimpleNamespace

    parent_oaza = SimpleNamespace(x=139.750000, y=35.670000, level=5, parent=None, fullname=["東京都", "千代田区", "霞が関"])
    shinjuku_oaza = SimpleNamespace(x=139.691700, y=35.689500, level=5, parent=None, fullname=["東京都", "新宿区", "西新宿"])
    node_bld = SimpleNamespace(x=139.752212, y=35.673378, level=8, parent=parent_oaza, fullname=["東京都", "千代田区", "霞が関", "一丁目", "2番", "2号"])
    node_missing_coord = SimpleNamespace(x=999.9, y=999.9, level=8, parent=shinjuku_oaza, fullname=["東京都", "新宿区", "西新宿"])

    kumamoto_city = SimpleNamespace(x=130.707891, y=32.803100, level=3, parent=None, fullname=["熊本県", "熊本市"])
    kumamoto_bld = SimpleNamespace(x=130.751234, y=32.812345, level=8, parent=kumamoto_city, fullname=["熊本県", "熊本市", "東区", "保田窪", "五丁目", "10番", "23号"])

    miyagi_pref = SimpleNamespace(x=140.871944, y=38.268889, level=1, parent=None, fullname=["宮城県"])
    shiogama_bld = SimpleNamespace(x=141.012345, y=38.312345, level=8, parent=miyagi_pref, fullname=["宮城県", "塩竈市", "藤倉", "三丁目", "17番", "5号"])

    nagano_pref = SimpleNamespace(x=138.181111, y=36.651289, level=1, parent=None, fullname=["長野県"])
    nagano_oaza = SimpleNamespace(x=138.234567, y=36.687654, level=5, parent=nagano_pref, fullname=["長野県", "長野市", "大字吉"])

    wrong_pref_bld = SimpleNamespace(x=135.0, y=35.0, level=8, parent=None, fullname=["大阪府", "大阪市"])
    kyoto_city = SimpleNamespace(x=135.756, y=35.029, level=4, parent=None, fullname=["京都府", "京都市", "上京区"])
    kyoto_gaiku = SimpleNamespace(x=135.759, y=35.030, level=7, parent=kyoto_city, fullname=["京都府", "京都市", "上京区", "御所八幡町", "110番"])
    shizuoka_city = SimpleNamespace(x=138.489, y=35.021, level=4, parent=None, fullname=["静岡県", "静岡市", "清水区"])
    shizuoka_house = SimpleNamespace(x=138.489, y=35.021, level=8, parent=shizuoka_city, fullname=["静岡県", "静岡市", "清水区", "辻", "一丁目"])

    def fake_search(query: str) -> list[Any]:
        if "霞が関" in query:
            return [SimpleNamespace(node=node_bld, matched="東京都千代田区霞が関1-2-")]
        if "西新宿" in query:
            return [SimpleNamespace(node=node_missing_coord, matched="東京都新宿区西新宿1-1-1")]
        if query == "熊本県熊本市 東区保田窪5-10-23":
            return [SimpleNamespace(node=kumamoto_city, matched="熊本県熊本市")]
        if query == "熊本県熊本市東区保田窪5-10-23":
            return [SimpleNamespace(node=kumamoto_bld, matched="熊本県熊本市東区保田窪5-10-23")]
        if query == "宮城県塩釜市藤倉 3ー17ー5":
            return [SimpleNamespace(node=miyagi_pref, matched="宮城県")]
        if query in ("宮城県塩竈市藤倉3ー17ー5", "宮城県塩竈市藤倉3-17-5"):
            return [SimpleNamespace(node=shiogama_bld, matched="宮城県塩竈市藤倉3-17-5")]
        if query == "長野県レインボーテラス内 長野市大字吉1827-1":
            return [SimpleNamespace(node=nagano_pref, matched="長野県")]
        if query == "長野県長野市大字吉1827-1":
            return [SimpleNamespace(node=nagano_oaza, matched="長野県長野市大字吉")]
        if "矛盾住所" in query:
            return [SimpleNamespace(node=wrong_pref_bld, matched="大阪府大阪市")]
        if query == "京都府京都市上京区烏丸通上立売下る御所八幡町110番地14 烏丸今出川ビル5F":
            return [SimpleNamespace(node=kyoto_city, matched="京都府京都市上京区")]
        if query.startswith("京都府京都市上京区御所八幡町110番地14"):
            return [SimpleNamespace(node=kyoto_gaiku, matched="京都府京都市上京区御所八幡町110番地")]
        if query == "京都府京都市上京区丸太町通大宮東入":
            return [SimpleNamespace(node=kyoto_city, matched="京都府京都市上京区")]
        if query.startswith("静岡県静岡市清水区辻1丁目15番3号"):
            return [SimpleNamespace(node=shizuoka_house, matched="静岡県静岡市清水区辻1丁目15番3号")]
        if query.startswith("静岡県静岡市清水区\ue192") or query.startswith("静岡県静岡市清水区\ue999"):
            return [SimpleNamespace(node=shizuoka_city, matched="静岡県静岡市清水区")]
        return []

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir).resolve()
        rel_dir = root / "release" / "202701"
        rel_dir.mkdir(parents=True)

        sample = pd.DataFrame(
            [
                {
                    "medical_code_10digit": "0113000028",
                    "kouseikyoku_code": "03_kantoshinetsu",
                    "pref_name": "東京都",
                    "municipality_name": "新宿区",
                    "address": "新宿区西新宿1-1-1",
                },
                {
                    "medical_code_10digit": "0113000010",
                    "kouseikyoku_code": "03_kantoshinetsu",
                    "pref_name": "東京都",
                    "municipality_name": "千代田区",
                    "address": "東京都千代田区霞が関1-2-2 霞が関ビル\ue18d3F",
                },
                {
                    "medical_code_10digit": "0113000036",
                    "kouseikyoku_code": "03_kantoshinetsu",
                    "pref_name": "東京都",
                    "municipality_name": "千代田区",
                    "address": "存在しない住所999",
                },
                {
                    "medical_code_10digit": "4317210195",
                    "kouseikyoku_code": "08_kyushu",
                    "pref_name": "熊本県",
                    "municipality_name": "熊本市東区",
                    "address": "熊本市 東区保田窪5－10－23",
                },
                {
                    "medical_code_10digit": "0410310874",
                    "kouseikyoku_code": "02_tohoku",
                    "pref_name": "宮城県",
                    "municipality_name": "塩竈市",
                    "address": "塩釜市藤倉 3ー17ー5",
                },
                {
                    "medical_code_10digit": "2010119564",
                    "kouseikyoku_code": "03_kantoshinetsu",
                    "pref_name": "長野県",
                    "municipality_name": "長野市",
                    "address": "レインボーテラス内 長野市大字吉1827－1",
                },
                {
                    "medical_code_10digit": "0113000099",
                    "kouseikyoku_code": "03_kantoshinetsu",
                    "pref_name": "東京都",
                    "municipality_name": "千代田区",
                    "address": "矛盾住所1-1",
                },
                {
                    "medical_code_10digit": "2610205078",
                    "kouseikyoku_code": "05_kinki",
                    "pref_name": "京都府",
                    "municipality_name": "京都市上京区",
                    "address": "京都市上京区烏丸通上立売下る御所八幡町110番地14 烏丸今出川ビル5Ｆ",
                },
                {
                    "medical_code_10digit": "2610203552",
                    "kouseikyoku_code": "05_kinki",
                    "pref_name": "京都府",
                    "municipality_name": "京都市上京区",
                    "address": "京都市上京区丸太町通大宮東入",
                },
                {
                    "medical_code_10digit": "2213210384",
                    "kouseikyoku_code": "04_tokaihokuriku",
                    "pref_name": "静岡県",
                    "municipality_name": "静岡市清水区",
                    "address": "静岡市清水区\ue1921丁目15番3号",
                },
                {
                    "medical_code_10digit": "2213210385",
                    "kouseikyoku_code": "02_tohoku",
                    "pref_name": "静岡県",
                    "municipality_name": "静岡市清水区",
                    "address": "静岡市清水区\ue1921丁目15番3号",
                },
                {
                    "medical_code_10digit": "2213210386",
                    "kouseikyoku_code": "04_tokaihokuriku",
                    "pref_name": "静岡県",
                    "municipality_name": "静岡市清水区",
                    "address": "静岡市清水区\ue9991丁目15番3号",
                },
            ]
        )
        pub_gz = rel_dir / "hokenshisetsu_nationwide_202701_public.csv.gz"
        sample.to_csv(pub_gz, index=False, encoding="utf-8-sig", compression="gzip")
        (rel_dir / "SHA256SUMS.txt").write_text(
            f"{sha256_file(pub_gz)}  hokenshisetsu_nationwide_202701_public.csv.gz\n",
            encoding="utf-8",
        )

        summary = run_geocode("202701", repo_root=root, search_fn=fake_search)
        assert summary["total_rows"] == 12
        assert summary["matched_rows"] == 10

        out_df = pd.read_csv(
            rel_dir / "hokenshisetsu_geocode_202701.csv.gz",
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
            compression="gzip",
        ).set_index("medical_code_10digit")
        assert list(out_df.reset_index().columns) == GEOCODE_COLUMNS
        assert out_df.loc["0113000010", "geocode_level"] == "house_number"
        assert out_df.loc["0113000028", "geocode_level"] == "oaza_cho"
        assert out_df.loc["0113000036", "geocode_level"] == "unmatched"
        assert out_df.loc["0113000099", "geocode_level"] == "unmatched", "Inconsistent prefecture must be rejected"
        assert out_df.loc["4317210195", "geocode_level"] == "house_number"
        assert out_df.loc["0410310874", "geocode_level"] == "house_number"
        assert out_df.loc["2010119564", "geocode_level"] == "oaza_cho"
        assert out_df.loc["2610205078", "geocode_level"] == "gaiku"
        assert out_df.loc["2610203552", "geocode_level"] == "municipality", "Ambiguous Kyoto street-only must stay municipality"
        assert out_df.loc["2213210384", "geocode_level"] == "house_number", "Verified scoped PUA should resolve"
        assert out_df.loc["2213210385", "geocode_level"] == "municipality", "Different source_scope must NOT apply PUA"
        assert out_df.loc["2213210386", "geocode_level"] == "municipality", "Unknown PUA must NOT be guessed"

        # Re-running updates SHA256SUMS.txt idempotently without duplicate lines
        run_geocode("202701", repo_root=root, search_fn=fake_search)
        sums_lines = [l for l in (rel_dir / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(sums_lines) == 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Geocode snapshot institutions using jageocoder (Option B).")
    parser.add_argument("--snapshot", help="Target snapshot YYYYMM (e.g. 202701)")
    parser.add_argument("--db-dir", type=Path, default=DEFAULT_DB_DIR, help="Path to jageocoder dictionary directory")
    parser.add_argument("--only-january", action="store_true", help="Skip execution unless snapshot month is 01")
    parser.add_argument("--self-test", action="store_true", help="Run offline unit tests")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        print("GEOCODE_SELF_TEST_PASSED")
        return

    if not args.snapshot:
        parser.error("--snapshot is required unless --self-test is specified")
    _, mm = dr.validate_snapshot_str(args.snapshot)
    if args.only_january and mm != 1:
        print(json.dumps({"snapshot": args.snapshot, "skipped": "not_january"}, ensure_ascii=False))
        return

    summary = run_geocode(args.snapshot, db_dir=args.db_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
