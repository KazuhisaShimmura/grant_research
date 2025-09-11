#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開APIから公募情報を取得して JSONL 出力
"""
import argparse
import requests
import time
import json

import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"


def load_keywords():
    cfg = yaml.safe_load(
        open(
            "config/sources_common.yml",
            "r",
            encoding="utf-8"))
    return cfg["jgrants"]["keywords"], cfg["jgrants"].get("page_size", 100)


def fetch_all():
    keywords, page_size = load_keywords()
    items = []
    page = 1
    # キーワードをカンマ連結で投げる（API仕様に合わせて調整）
    params = {"keyword": ",".join(keywords), "page": page, "size": page_size}
    while True:
        r = requests.get(API_BASE, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for row in data.get("content", []):
            sid = row.get("id")
            if not sid:
                continue
            detail = requests.get(f"{API_BASE}/id/{sid}", timeout=30)
            if detail.status_code != 200:
                continue
            items.append(detail.json())
        if data.get("last", True):
            break
        page += 1
        params["page"] = page
        time.sleep(0.3)
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    items = fetch_all()
    with open(args.out, "w", encoding="utf-8") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"Wrote {len(items)} rows to {args.out}")


if __name__ == "__main__":
    main()
