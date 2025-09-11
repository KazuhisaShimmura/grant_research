#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIIポータルから省エネ事業の公募メタ情報を抽出（軽量）
※本サンプルはリンク一覧の収集に留め、詳細は後段NLPで抽出します。
"""
import argparse
import json

import requests
from bs4 import BeautifulSoup

INDEX_URLS = [
    "https://sii.or.jp/"
]


def fetch_index(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    links = []
    for a in soup.select("a"):
        href = a.get("href")
        if not href:
            continue
        text = a.get_text(" ", strip=True)
        if any(k in text for k in ["補助", "省エネ", "ZEB", "交付申請", "公募", "事業"]):
            links.append(
                {"url": requests.compat.urljoin(url, href), "text": text})
    return links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    rows = []
    for u in INDEX_URLS:
        rows.extend(fetch_index(u))
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
