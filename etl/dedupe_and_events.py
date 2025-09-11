#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import pandas as pd
import hashlib
import numpy as np

def normalize_text(s: pd.Series) -> pd.Series:
    # 欠損→空文字、前後空白削除、全角空白→半角、大小無視
    s = s.fillna("").astype(str)
    s = s.str.replace("\u3000", " ", regex=False).str.strip().str.lower()
    # 連続空白を1つに圧縮
    s = s.str.replace(r"\s+", " ", regex=True)
    return s

def sha1_hex(series: pd.Series) -> pd.Series:
    return series.map(lambda x: hashlib.sha1(x.encode("utf-8")).hexdigest())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = pd.read_parquet(args.input)

    # 必要列が無い場合に備えて作成
    for col in ["publisher", "fiscal_year", "title"]:
        if col not in df.columns:
            df[col] = ""

    pub = normalize_text(df["publisher"])
    fy  = normalize_text(df["fiscal_year"].astype(str))
    ttl = normalize_text(df["title"])

    key_source = pub + "/" + fy + "/" + ttl
    df["dedupe_key"] = sha1_hex(key_source)

    # 近傍重複の“近傍”をもう一段見るなら、タイトルを記号除去や
    # token sort（rapidfuzz）で正規化キーを足してもOK（必要時）

    df = df.drop_duplicates(subset=["dedupe_key"], keep="first").copy()

    # イベント化：締切があれば open、無ければ upcoming
    # 締切が過去なら closed、今日以降なら open 等に分岐
    now_utc = pd.Timestamp.now(tz="UTC").normalize()
    deadline_col = None
    for cand in ["deadline", "due_date", "application_deadline"]:
        if cand in df.columns:
            deadline_col = cand
            break

    if deadline_col:
        dl = pd.to_datetime(df[deadline_col], errors="coerce", utc=True)
        df["status"] = np.where(dl.isna(), "upcoming",
                         np.where(dl >= now_utc, "open", "closed"))
    else:
        # 締切列が無ければ、既存statusが欠損のみ open に
        if "status" not in df.columns:
            df["status"] = np.nan
        df["status"] = df["status"].fillna("open")

    df["last_seen_at"] = pd.Timestamp.now(tz="UTC").isoformat()  # tz-aware (…Z)

    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")

if __name__ == "__main__":
    main()
