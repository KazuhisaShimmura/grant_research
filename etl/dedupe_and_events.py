#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
近傍重複の解消とイベント化（簡易）
"""
import argparse
import pandas as pd
import hashlib


def keyish(row):
    base = f"{row.get('publisher',
                      '')}/{row.get('fiscal_year',
                                    '')}/{row.get('title',
                                                  '')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    df = pd.read_parquet(args.input)
    df["dedupe_key"] = df.apply(keyish, axis=1)
    df = df.drop_duplicates("dedupe_key")
    # 簡易イベント: 締切日があるものは "open"、無ければ "upcoming" 扱い
    df["status"] = df["status"].fillna("open")
    df["last_seen_at"] = pd.Timestamp.utcnow().isoformat()
    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()
