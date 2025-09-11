#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開APIから公募情報を取得して JSONL 出力（堅牢版）
"""
import argparse
import requests
import time
import json
import math
from urllib.parse import quote
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "eucalia-jgrants-fetch/1.0",
    "Accept": "application/json"
})

API_PAGE_MAX = 50          # 仮上限。400が出る場合はさらに下げる（20など）
REQ_TIMEOUT = 30
SLEEP_BETWEEN_REQ = 0.5    # 連続アクセスの穏当な間隔
MAX_RETRY = 3              # 429/5xx向け

def load_keywords():
    with open("config/sources_common.yml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    kws = cfg["jgrants"]["keywords"]
    size = int(cfg["jgrants"].get("page_size", API_PAGE_MAX))
    size = max(1, min(size, API_PAGE_MAX))  # クランプ
    # キーワード正規化：空白トリム・重複削除
    norm = []
    seen = set()
    for k in kws:
        k2 = " ".join(str(k).split()).strip()
        if k2 and k2 not in seen:
            seen.add(k2)
            norm.append(k2)
    return norm, size

def get_with_retry(url, params=None):
    for i in range(MAX_RETRY):
        r = SESSION.get(url, params=params, timeout=REQ_TIMEOUT)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(SLEEP_BETWEEN_REQ * (2 ** i))
            continue
        return r
    return r  # 最後の応答を返す

def fetch_list_for_keyword(keyword, page_size):
    """
    1キーワードずつ検索し、全ページを取得
    """
    items = []
    page = 0  # 0-based想定
    while True:
        params = {
            "keyword": keyword,  # 単一キーワードのみ
            "page": page,
            "size": page_size,
        }
        r = get_with_retry(API_BASE, params=params)
        if r.status_code == 400 and page_size > 20:
            # サイズが大きすぎる場合に縮めて再試行
            page_size = 20
            continue
        r.raise_for_status()
        data = r.json()
        # contentが無ければ終了
        content = data.get("content") or []
        items.extend(content)
        # ページング制御
        if data.get("last", True) or not content:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_REQ)
    return items

def fetch_detail(sid):
    url = f"{API_BASE}/id/{sid}"   # 仕様によっては f"{API_BASE}/{sid}" の可能性あり
    r = get_with_retry(url)
    if r.status_code == 200:
        return r.json()
    return None

def fetch_all():
    keywords, page_size = load_keywords()
    # 1キーワードずつ取って、IDで重複排除
    id_seen = set()
    details = []
    for kw in keywords:
        lst = fetch_list_for_keyword(kw, page_size)
        for row in lst:
            sid = row.get("id")
            if not sid or sid in id_seen:
                continue
            id_seen.add(sid)
            detail = fetch_detail(sid)
            if detail:
                details.append(detail)
            time.sleep(SLEEP_BETWEEN_REQ)
    return details

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        items = fetch_all()
    except requests.HTTPError as e:
        print(f"[ERROR] HTTPError: {e.response.status_code} {e.response.text[:300]}")
        raise
    with open(args.out, "w", encoding="utf-8") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"Wrote {len(items)} rows to {args.out}")

if __name__ == "__main__":
    main()
