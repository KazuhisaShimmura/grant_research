#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import re
import sys
from typing import Any

import numpy as np
import pandas as pd

SPACE_ZEN = "\u3000"
DATE_TOKEN_PATTERNS = [
    re.compile(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})"),
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"),
]


def normalize_text(series: pd.Series) -> pd.Series:
    series = series.fillna("").astype(str)
    series = series.str.replace(SPACE_ZEN, " ", regex=False).str.strip().str.lower()
    series = series.str.replace(r"\s+", " ", regex=True)
    return series


def sha1_hex(series: pd.Series) -> pd.Series:
    return series.map(lambda x: hashlib.sha1(x.encode("utf-8")).hexdigest())


def extract_dates(text: str) -> list[pd.Timestamp]:
    tokens: list[pd.Timestamp] = []
    for pat in DATE_TOKEN_PATTERNS:
        for match in pat.finditer(text):
            y, mo, d = match.groups()
            try:
                dt = pd.Timestamp(year=int(y), month=int(mo), day=int(d), tz="UTC")
            except ValueError:
                continue
            tokens.append(dt)
    return tokens


def parse_period_end(value: Any) -> pd.Timestamp:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return pd.NaT

    if isinstance(value, pd.Timestamp):
        return value.tz_convert("UTC") if value.tzinfo else value.tz_localize("UTC")

    if isinstance(value, (list, tuple)):
        if not value:
            return pd.NaT
        return parse_period_end(value[-1])

    if isinstance(value, dict):
        for key in ("end", "to", "deadline", "finish", "until"):
            if key in value:
                result = parse_period_end(value[key])
                if not pd.isna(result):
                    return result
        for key in ("start", "from"):
            if key in value:
                result = parse_period_end(value[key])
                if not pd.isna(result):
                    return result
        return pd.NaT

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return pd.NaT
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None and not isinstance(parsed, str):
            return parse_period_end(parsed)
        tokens = extract_dates(text)
        if tokens:
            return tokens[-1]
        dt = pd.to_datetime(text, errors="coerce", utc=True)
        return dt

    return pd.to_datetime(value, errors="coerce", utc=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = pd.read_parquet(args.input)

    required = [
        "title",
        "geography",
        "subsidy_rate",
        "application_period",
        "source_url",
        "subsidy_cap_jpy",
        "employee_limit",
    ]
    for col in required:
        if col not in df.columns:
            df[col] = pd.NA

    title = normalize_text(df["title"])
    geography = normalize_text(df["geography"])
    rate = normalize_text(df["subsidy_rate"])
    period = normalize_text(df["application_period"])
    source = normalize_text(df["source_url"])

    cap_numeric = pd.to_numeric(df["subsidy_cap_jpy"], errors="coerce")
    cap_text = cap_numeric.map(lambda x: "" if pd.isna(x) else str(int(x)))

    employee_numeric = pd.to_numeric(df["employee_limit"], errors="coerce")
    employee_text = employee_numeric.map(lambda x: "" if pd.isna(x) else str(int(x)))

    key_source = (
        title
        + "|"
        + geography
        + "|"
        + rate
        + "|"
        + cap_text
        + "|"
        + employee_text
        + "|"
        + period
        + "|"
        + source
    )
    df["dedupe_key"] = sha1_hex(key_source)

    df = df.drop_duplicates(subset=["dedupe_key"], keep="first").copy()

    now_utc = pd.Timestamp.now(tz="UTC").normalize()
    deadlines = df["application_period"].apply(parse_period_end)
    deadlines = pd.to_datetime(deadlines, errors="coerce", utc=True)
    df["status"] = np.where(
        deadlines.isna(),
        "upcoming",
        np.where(deadlines >= now_utc, "open", "closed"),
    )

    df["last_seen_at"] = pd.Timestamp.now(tz="UTC").isoformat()

    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()
