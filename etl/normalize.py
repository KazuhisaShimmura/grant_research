#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
取得データを共通スキーマに正規化
- jGrants / 都道府県ページ / SII等リンク収集 からのJSONL/Parquet入力を統一スキーマに整形
- 日付のゆらぎ吸収（YYYY/MM/DD, YYYY年M月D日 等）
"""

import argparse
import glob
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

SCHEMA_COLS = [
    "title",
    "subsidy_cap_jpy",
    "subsidy_rate",
    "geography",
    "employee_limit",
    "application_period",
    "source_url",
]

SPACE_ZEN = "\u3000"
DATE_PATTERNS = [
    re.compile(r"^\s*(\d{4})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})\s*$"),
    re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*$"),
]
DATE_IN_TEXT_PATTERNS = [
    re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})"),
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"),
]


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
    if x is None:
        return None
    s = normalize_text(str(x))
    if not s:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat()
        except Exception:  # noqa: BLE001
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

    return None


def ensure_schema(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row.get(k) for k in SCHEMA_COLS}


def get_nested_value(data: Dict[str, Any], key: str) -> Any:
    parts = key.split(".")
    cur: Any = data
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def first_non_empty(data: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = get_nested_value(data, key)
        if isinstance(value, str):
            value = value.strip()
        if isinstance(value, (list, tuple)):
            for item in value:
                if item not in (None, ""):
                    return item
            continue
        if value not in (None, ""):
            return value
    return None


def parse_amount_jpy(value: Any) -> Optional[int]:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value:  # NaN
            return None
        return int(value)

    if isinstance(value, (list, tuple)):
        parsed = [parse_amount_jpy(v) for v in value]
        parsed = [v for v in parsed if v is not None]
        if parsed:
            return max(parsed)
        return None

    if not isinstance(value, str):
        return None

    s = normalize_text(value)
    if not s:
        return None

    if any(term in s for term in ["上限なし", "上限無し", "制限なし", "制限無し", "なし", "無し"]):
        return None

    s = s.replace(",", "")
    total = 0
    matched = False

    def _parse_fragment(fragment: str, multiplier: int) -> int:
        frag = fragment.strip()
        if not frag:
            return 0
        inner = parse_amount_jpy(frag)
        return 0 if inner is None else inner * multiplier

    for unit, mult in (("億", 100_000_000), ("万", 10_000)):
        if unit in s:
            left, right = s.split(unit, 1)
            total += _parse_fragment(left, mult)
            s = right
            matched = True

    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    if m:
        num = float(m.group(1))
        tail = s[m.end():]
        multiplier = 1
        if tail.startswith("千"):
            multiplier = 1_000
        total += int(num * multiplier)
        matched = True

    return total if matched and total > 0 else None


def parse_employee_limit(value: Any) -> Optional[int]:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value:
            return None
        return int(value)

    if isinstance(value, (list, tuple)):
        parsed = [parse_employee_limit(v) for v in value]
        parsed = [v for v in parsed if v is not None]
        if parsed:
            return max(parsed)
        return None

    if not isinstance(value, str):
        return None

    s = normalize_text(value)
    if not s:
        return None
    if any(term in s for term in ["制限なし", "制限無し", "上限なし", "上限無し", "なし", "無し"]):
        return None

    s = s.replace(",", "")
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    return int(m.group(1))


def extract_subsidy_rate(rec: Dict[str, Any]) -> Optional[str]:
    raw = first_non_empty(
        rec,
        [
            "subsidyRate",
            "subsidy_rate",
            "grantRate",
            "grant_rate",
            "supportRate",
            "rate",
            "benefitRate",
            "補助率",
            "助成率",
        ],
    )
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        parts = [normalize_text(str(x)) for x in raw if normalize_text(str(x))]
        return ", ".join(parts) if parts else None
    if isinstance(raw, dict):
        parts = []
        for key in ["min", "max", "rate", "value"]:
            val = raw.get(key)
            if val not in (None, ""):
                parts.append(normalize_text(str(val)))
        if parts:
            return " - ".join(parts)
        try:
            return json.dumps(raw, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return None
    s = normalize_text(str(raw))
    return s or None


def extract_geography(rec: Dict[str, Any]) -> Optional[str]:
    raw = first_non_empty(
        rec,
        [
            "geography",
            "geography_code",
            "prefecture",
            "prefectures",
            "region",
            "regions",
            "targetRegion",
            "targetPrefecture",
            "applicantRegion",
        ],
    )
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        parts = [normalize_text(str(x)) for x in raw if normalize_text(str(x))]
        return ", ".join(parts) if parts else None
    return normalize_text(str(raw)) or None


def dates_from_text(text: str) -> List[str]:
    out: List[str] = []
    for pat in DATE_IN_TEXT_PATTERNS:
        for m in pat.finditer(text):
            y, mo, d = m.groups()
            try:
                dt = datetime(int(y), int(mo), int(d), tzinfo=timezone.utc)
            except ValueError:
                continue
            out.append(dt.date().isoformat())
    return out


def normalize_date_string(value: Any) -> Optional[str]:
    iso = parse_ymd_to_iso_utc(value)
    if iso:
        return iso[:10]
    if isinstance(value, str):
        dates = dates_from_text(value)
        if dates:
            return dates[0]
    return None


def extract_application_period(rec: Dict[str, Any]) -> Optional[str]:
    start_raw = first_non_empty(
        rec,
        [
            "applicationStartDate",
            "applicationStart",
            "application_start",
            "applicationStartAt",
            "acceptanceStartDate",
            "receptionStartDate",
            "recruitmentStartDate",
            "applicationPeriod.start",
            "applicationPeriod.from",
            "application_period.start",
            "application_period.from",
            "period.start",
        ],
    )
    end_raw = first_non_empty(
        rec,
        [
            "applicationDeadline",
            "applicationEndDate",
            "deadline",
            "receptionEndDate",
            "recruitmentEndDate",
            "acceptanceEndDate",
            "applicationPeriod.end",
            "applicationPeriod.to",
            "application_period.end",
            "application_period.to",
            "period.end",
        ],
    )

    period_raw = first_non_empty(
        rec,
        [
            "applicationPeriod",
            "application_period",
            "receptionPeriod",
            "acceptancePeriod",
            "募集期間",
        ],
    )

    start = normalize_date_string(start_raw)
    end = normalize_date_string(end_raw)

    if isinstance(period_raw, dict):
        start = start or normalize_date_string(period_raw.get("start") or period_raw.get("from"))
        end = end or normalize_date_string(period_raw.get("end") or period_raw.get("to"))
    elif isinstance(period_raw, (list, tuple)) and period_raw:
        if len(period_raw) >= 2:
            start = start or normalize_date_string(period_raw[0])
            end = end or normalize_date_string(period_raw[1])
    elif isinstance(period_raw, str):
        dates = dates_from_text(period_raw)
        if dates:
            start = start or dates[0]
            if len(dates) > 1:
                end = end or dates[-1]

    if start and end:
        if start == end:
            return start
        return f"{start} - {end}"
    if end:
        return end
    if start:
        return start
    if isinstance(period_raw, str):
        clean = normalize_text(period_raw)
        return clean or None
    return None


def norm_jgrants(rec: Dict[str, Any]) -> Dict[str, Any]:
    title = normalize_text(rec.get("title") or rec.get("subsidyTitle")) or None
    source_url = to_str_or_none(rec.get("publicUrl") or rec.get("detailUrl"))

    amount_candidates = [
        "subsidyCap",
        "subsidy_cap",
        "grantUpperLimit",
        "grantUpper",
        "limitAmount",
        "upperLimit",
        "subsidyLimit",
        "grantLimit",
        "supportUpperLimit",
        "benefitUpperLimit",
    ]
    subsidy_cap = parse_amount_jpy(first_non_empty(rec, amount_candidates))

    employee_candidates = [
        "employeeLimit",
        "employee_limit",
        "employeesUpperLimit",
        "employeeUpperLimit",
        "employeeNumberUpperLimit",
        "maxEmployee",
        "targetEmployeeUpper",
        "eligibleEmployeeUpper",
    ]
    employee_limit = parse_employee_limit(first_non_empty(rec, employee_candidates))

    row = dict(
        title=title,
        subsidy_cap_jpy=subsidy_cap,
        subsidy_rate=extract_subsidy_rate(rec),
        geography=extract_geography(rec),
        employee_limit=employee_limit,
        application_period=extract_application_period(rec),
        source_url=source_url,
    )
    return ensure_schema(row)


def norm_pref_page(rec: Dict[str, Any]) -> Dict[str, Any]:
    title = normalize_text(rec.get("page_name"))
    url = to_str_or_none(rec.get("source_url"))
    geo = to_str_or_none(rec.get("geography") or rec.get("geography_code"))
    pub = normalize_text(rec.get("publisher"))

    row = dict(
        title=title or pub or None,
        subsidy_cap_jpy=None,
        subsidy_rate=None,
        geography=geo or pub or None,
        employee_limit=None,
        application_period=None,
        source_url=url,
    )
    return ensure_schema(row)


def norm_sii_or_links(rec: Dict[str, Any]) -> Dict[str, Any]:
    source_index = to_str_or_none(rec.get("source_index"))
    canonical = to_str_or_none(rec.get("canonical"))
    url = to_str_or_none(rec.get("url"))

    source_url = source_index or canonical or url
    title = normalize_text(rec.get("page_title") or rec.get("text"))

    row = dict(
        title=title or None,
        subsidy_cap_jpy=None,
        subsidy_rate=None,
        geography=None,
        employee_limit=None,
        application_period=extract_application_period(rec),
        source_url=source_url,
    )
    return ensure_schema(row)


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
        return


def classify_and_normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    if ("links" in rec or "pdf_links" in rec) and ("page_name" in rec or "publisher" in rec):
        return norm_pref_page(rec)
    if ("id" in rec or "subsidyId" in rec) or ("publicUrl" in rec or "detailUrl" in rec):
        return norm_jgrants(rec)
    if "url" in rec and (("source_index" in rec) or ("text" in rec)):
        return norm_sii_or_links(rec)
    return norm_sii_or_links(rec)


def main() -> None:
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
        df_empty = pd.DataFrame([], columns=SCHEMA_COLS)
        df_empty.to_parquet(args.out, index=False)
        print(f"Wrote 0 rows to {args.out}")
        return

    df = pd.DataFrame(rows, columns=SCHEMA_COLS)

    for col in ["subsidy_cap_jpy", "employee_limit"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    df = df.drop_duplicates(subset=["source_url", "title", "application_period"], keep="first")

    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()
