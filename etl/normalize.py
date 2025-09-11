#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
取得データを共通スキーマに正規化（堅牢版）
- jGrants / 都道府県ページ / SII等リンク収集 からのJSONL/Parquet入力を統一スキーマに整形
- 日付のゆらぎ吸収（YYYY/MM/DD, YYYY年M月D日 等）
- docs_urls_json をJSON文字列に統一
- content_hash で差分/重複検知を支援
"""

import argparse
import glob
import json
import os
import re
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

SCHEMA_COLS = [
    "program_id",
    "source_url",
    "publisher",
    "title",
    "fiscal_year",
    "domain",
    "eligibility_json",
    "geography",
    "subsidy_rate",
    "subsidy_cap_jpy",
    "budget_total_jpy",
    "cost_items_allowed",
    "deadline_type",
    "deadline_at",
    "application_method",
    "requires_gbizid",
    "docs_urls_json",
    "status",
    "published_at",
    "last_seen_at",
    "content_hash",
]

# ---------- helpers ----------

SPACE_ZEN = "\u3000"
DATE_PATTERNS = [
    # 2025-09-11 / 2025/9/1 / 2025.9.1
    re.compile(r"^\s*(\d{4})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})\s*$"),
    # 2025年9月1日
    re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*$"),
]

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize_text(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s).replace(SPACE_ZEN, " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def to_str_or_none(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    return s if s else None

def parse_ymd_to_iso_utc(x: Any) -> Optional[str]:
    """
    YYYY/MM/DD, YYYY-MM-DD, YYYY年M月D日 を UTC midnight ISO8601に。
    既にISOっぽい場合はそのまま返す（tz欠落時はUTC扱いに統一）。
    """
    if x is None:
        return None
    s = normalize_text(str(x))
    if not s:
        return None

    # 既にISO8601（ざっくり）ならtz付与（なければUTC）
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat()
        except Exception:
            # フォールバックで日付部分だけ拾う
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
            if m:
                y, mo, d = map(int, m.groups())
                dt = datetime(y, mo, d, tzinfo=timezone.utc)
                return dt.isoformat()
            return None

    for pat in DATE_PATTERNS:
        m = pat.match(s)
        if m:
            y, mo, d = map(int, m.groups())
            dt = datetime(y, mo, d, tzinfo=timezone.utc)
            return dt.isoformat()

    # ここまでで解釈不能ならNone
    return None

def safe_json_dumps(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return None

def sha1_of_fields(fields: Iterable[Any]) -> str:
    raw = "|".join("" if v is None else str(v) for v in fields)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def ensure_schema(row: Dict[str, Any]) -> Dict[str, Any]:
    """不足キーをNoneで補完し、順序をSCHEMA_COLSに揃える"""
    out = {k: row.get(k) for k in SCHEMA_COLS}
    return out

# ---------- normalizers ----------

def norm_jgrants(rec: Dict[str, Any]) -> Dict[str, Any]:
    pid = rec.get("id") or rec.get("subsidyId")
    title = normalize_text(rec.get("title") or rec.get("subsidyTitle"))
    pub = normalize_text(rec.get("publisherName") or rec.get("ministryName") or "jGrants掲載")
    url = to_str_or_none(rec.get("publicUrl") or rec.get("detailUrl"))
    deadline = rec.get("applicationDeadline") or rec.get("deadline")
    status = to_str_or_none(rec.get("status") or rec.get("publicationStatus"))
    fy = to_str_or_none(rec.get("fiscalYear"))

    deadline_iso = parse_ymd_to_iso_utc(deadline)

    row = dict(
        program_id=f"jgrants:{pid}" if pid else None,
        source_url=url,
        publisher=pub or None,
        title=title or None,
        fiscal_year=fy,
        domain=None,
        eligibility_json=None,
        geography=None,
        subsidy_rate=None,
        subsidy_cap_jpy=None,
        budget_total_jpy=None,
        cost_items_allowed=None,
        deadline_type="hard" if deadline_iso else None,
        deadline_at=deadline_iso,
        application_method="jgrants",
        requires_gbizid=True,
        docs_urls_json=None,
        status=status,
        published_at=None,
        last_seen_at=now_utc_iso(),
        content_hash=None,
    )
    # content_hash: 主要差分軸から生成
    row["content_hash"] = sha1_of_fields(
        [row["program_id"], row["source_url"], row["title"], row["deadline_at"], row["publisher"]]
    )
    return ensure_schema(row)

def norm_pref_page(rec: Dict[str, Any]) -> Dict[str, Any]:
    title = normalize_text(rec.get("page_name"))
    url = to_str_or_none(rec.get("source_url"))
    pub = normalize_text(rec.get("publisher"))
    geo = to_str_or_none(rec.get("geography") or rec.get("geography_code"))
    # links構造の汎用対応（pdf_links or links）
    link_list = rec.get("pdf_links", rec.get("links", []))
    doc_urls = []
    for link in link_list or []:
        u = link.get("url") if isinstance(link, dict) else None
        if isinstance(u, str) and u.strip():
            doc_urls.append(u.strip())

    # 更新日ヒント（可能なら published_at に）
    updated_raw = rec.get("updated_at") or rec.get("updated_hint") or rec.get("updated_hint_raw")
    published_iso = parse_ymd_to_iso_utc(updated_raw)

    row = dict(
        program_id=None,
        source_url=url,
        publisher=pub or None,
        title=title or None,
        fiscal_year=None,
        domain=None,
        eligibility_json=None,
        geography=geo,
        subsidy_rate=None,
        subsidy_cap_jpy=None,
        budget_total_jpy=None,
        cost_items_allowed=None,
        deadline_type=None,
        deadline_at=None,
        application_method="direct",
        requires_gbizid=False,
        docs_urls_json=safe_json_dumps(doc_urls) if doc_urls else None,
        status="unknown",
        published_at=published_iso,
        last_seen_at=now_utc_iso(),
        content_hash=None,
    )
    row["content_hash"] = sha1_of_fields(
        [row["source_url"], row["title"], row["publisher"], row["published_at"]]
    )
    return ensure_schema(row)

def norm_sii_or_links(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    SII等のリンク収集レコードを想定。
    - source_url は 起点ページ（source_index/canonical/なければurlのドメインルート）を優先
    - docs_urls_json に実リンクを格納
    """
    # 起点URLの候補
    source_index = to_str_or_none(rec.get("source_index"))
    canonical = to_str_or_none(rec.get("canonical"))
    url = to_str_or_none(rec.get("url"))

    source_url = source_index or canonical or url
    title = normalize_text(rec.get("page_title") or rec.get("text"))
    publisher = "SII/関連"
    domain = "energy"

    # ドキュメントURL（主にPDF）
    docs = []
    if url:
        docs.append(url)

    # 更新ヒント→published_atに
    updated_hint = rec.get("updated_hint")
    published_iso = parse_ymd_to_iso_utc(updated_hint)

    row = dict(
        program_id=None,
        source_url=source_url,
        publisher=publisher,
        title=title or None,
        fiscal_year=None,
        domain=domain,
        eligibility_json=None,
        geography=None,
        subsidy_rate=None,
        subsidy_cap_jpy=None,
        budget_total_jpy=None,
        cost_items_allowed=None,
        deadline_type=None,
        deadline_at=None,
        application_method="direct",
        requires_gbizid=False,
        docs_urls_json=safe_json_dumps(docs) if docs else None,
        status="unknown",
        published_at=published_iso,
        last_seen_at=now_utc_iso(),
        content_hash=None,
    )
    row["content_hash"] = sha1_of_fields(
        [row["source_url"], row["title"], row["published_at"]]
    )
    return ensure_schema(row)

# ---------- input readers ----------

def iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

def iter_parquet(path: str) -> Iterable[Dict[str, Any]]:
    df = pd.read_parquet(path)
    for rec in df.to_dict(orient="records"):
        yield rec

def iter_input(path: str) -> Iterable[Dict[str, Any]]:
    low = path.lower()
    if low.endswith(".jsonl") or low.endswith(".ndjson"):
        yield from iter_jsonl(path)
    elif low.endswith(".parquet"):
        yield from iter_parquet(path)
    else:
        # 他形式は無視（将来拡張余地）
        return

# ---------- detect & normalize ----------

def classify_and_normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    # 都道府県ページ
    if ("links" in rec or "pdf_links" in rec) and ("page_name" in rec or "publisher" in rec):
        return norm_pref_page(rec)
    # jGrants
    if ("id" in rec or "subsidyId" in rec) or ("publicUrl" in rec or "detailUrl" in rec):
        return norm_jgrants(rec)
    # SIIやその他リンク収集
    if "url" in rec and (("source_index" in rec) or ("text" in rec)):
        return norm_sii_or_links(rec)
    # フォールバック（最小限）
    return norm_sii_or_links(rec)

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="Normalize harvested records to a common schema.")
    ap.add_argument("inputs", nargs="+", help="Input file globs (.jsonl/.ndjson/.parquet)")
    ap.add_argument("--out", required=True, help="Output Parquet file")
    args = ap.parse_args()

    files: List[str] = []
    for pattern in args.inputs:
        files.extend(glob.glob(pattern))
    files = sorted(set(files))

    rows: List[Dict[str, Any]] = []
    for path in files:
        for rec in iter_input(path):
            row = classify_and_normalize(rec)
            rows.append(row)

    if not rows:
        # 空でもスキーマ列で空DFを書き出しておくと下流が楽
        df_empty = pd.DataFrame([], columns=SCHEMA_COLS)
        df_empty.to_parquet(args.out, index=False)
        print(f"Wrote 0 rows to {args.out}")
        return

    df = pd.DataFrame(rows, columns=SCHEMA_COLS)

    # 軽い重複排除：
    # 1) program_id があるものは program_id でユニーク化
    # 2) program_id 無しは (source_url, title, deadline_at) でユニーク化
    has_pid = df["program_id"].notna()
    df_pid = df[has_pid].drop_duplicates(subset=["program_id"], keep="first")
    df_nopid = df[~has_pid].drop_duplicates(
        subset=["source_url", "title", "deadline_at"], keep="first"
    )
    df = pd.concat([df_pid, df_nopid], ignore_index=True)

    # 出力
    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")

if __name__ == "__main__":
    main()
