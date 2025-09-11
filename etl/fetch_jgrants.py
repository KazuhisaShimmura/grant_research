#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開APIから公募情報を取得して JSONL 出力
- config/keywords.yml の domains.* をキーワードとして使用
- 新旧レスポンス形式 (result型 / content型) 両対応
- 出力先フォルダを自動生成
"""

import argparse
import requests
import time
import json
import os
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "imm-research-fetch/1.1",
    "Accept": "application/json"
})

REQ_TIMEOUT = 30
SLEEP = 0.5

# ---- キーワード読み込み ----
def load_keywords(path="config/keywords.yml"):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    kws = []
    for group, words in cfg.get("domains", {}).items():
        kws.extend(words)
    # 重複排除
    return sorted(set(kws))

# ---- API呼び出し ----
def fetch_list(keyword, page=0, size=20):
    params = {
        "keyword": keyword,
        "page": page,
        "size": size,
        "sort": "created_date",
        "order": "DESC",
        "acceptance": "0"
    }
    r = SESSION.get(API_BASE, params=params, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.json()

def fetch_detail(sid):
    url = f"{API_BASE}/id/{sid}"
    r = SESSION.get(url, timeout=REQ_TIMEOUT)
    if r.status_code == 200:
        return r.json()
    return None

# ---- 全件取得 ----
def fetch_all(keywords):
    details, seen = [], set()
    for kw in keywords:
        page = 0
        while True:
            data = fetch_list(kw, page=page, size=20)
            items = data.get("result") or data.get("content") or []
            for row in items:
                sid = row.get("id")
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                detail = fetch_detail(sid)
                if detail:
                    details.append(detail)
                time.sleep(SLEEP)
            if data.get("last", True) or not items:
                break
            page += 1
            time.sleep(SLEEP)
    return details

# ---- メイン処理 ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    keywords = load_keywords()
    print(f"[INFO] Loaded {len(keywords)} keywords from config/keywords.yml")

    try:
        items = fetch_all(keywords)
    except requests.HTTPError as e:
        print(f"[ERROR] HTTP {e.response.status_code}: {e.response.text[:200]}")
        raise

    with open(args.out, "w", encoding="utf-8") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"[INFO] Wrote {len(items)} rows to {args.out}")

if __name__ == "__main__":
    main()
