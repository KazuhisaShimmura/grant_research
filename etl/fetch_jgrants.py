#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開APIから公募情報を取得して JSONL 出力（必須パラメータ対応・堅牢版）
- 一覧APIは keyword / sort / order / acceptance が必須（未指定は 400）
- 新旧レスポンス形式（result型 / content型）の両方に対応
- content型のみ page/size でページング処理
"""
import argparse
import json
import time
from typing import Dict, List, Optional, Tuple

import requests
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "eucalia-jgrants-fetch/1.1",
    "Accept": "application/json",
})

REQ_TIMEOUT = 30
SLEEP = 0.5
MAX_RETRY = 3
PAGE_MAX_DEFAULT = 50  # content型の時だけ使う

def load_keywords() -> Tuple[List[str], int]:
    with open("config/sources_common.yml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    kws = cfg["jgrants"]["keywords"]
    size = int(cfg["jgrants"].get("page_size", PAGE_MAX_DEFAULT))
    size = max(1, min(size, PAGE_MAX_DEFAULT))
    # 正規化（空白圧縮・重複除去・2文字未満は捨てる：API仕様）
    norm, seen = [], set()
    for k in kws:
        k2 = " ".join(str(k).split()).strip()
        if len(k2) >= 2 and k2 not in seen:
            seen.add(k2)
            norm.append(k2)
    return norm, size

def get_with_retry(url: str, params: Dict) -> requests.Response:
    last = None
    for i in range(MAX_RETRY):
        r = SESSION.get(url, params=params, timeout=REQ_TIMEOUT)
        # 429/5xx はリトライ
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(SLEEP * (2 ** i))
            last = r
            continue
        return r
    return last or r

def parse_list_response(data: Dict) -> Tuple[List[Dict], bool]:
    """
    戻り値: (items, has_paging)
    - 新仕様: {"metadata":..., "result":[...]} → has_paging=False
    - 旧仕様: {"content":[...], "last": bool} → has_paging=True
    """
    if isinstance(data.get("result"), list):
        return data["result"], False
    if isinstance(data.get("content"), list):
        return data["content"], True
    # 予期しない形式
    return [], False

def fetch_list_once(keyword: str,
                    sort: str = "created_date",
                    order: str = "DESC",
                    acceptance: str = "0",
                    page: Optional[int] = None,
                    size: Optional[int] = None) -> requests.Response:
    params: Dict[str, str | int] = {
        "keyword": keyword,
        "sort": sort,
        "order": order,
        "acceptance": acceptance,  # "0": 期間内で絞らない / "1": 期間内のみ
    }
    if page is not None:
        params["page"] = page
    if size is not None:
        params["size"] = size
    return get_with_retry(API_BASE, params=params)

def fetch_list_for_keyword(keyword: str, page_size_hint: int) -> List[Dict]:
    """
    まず非ページングで試す（新仕様 result 型を想定）。
    content型だった場合のみ page/size を使ったページングに切り替える。
    """
    items: List[Dict] = []

    # 1) 非ページング（新仕様）で試行
    r = fetch_list_once(keyword)
    # 400 の典型は「必須パラメータ不足 or sizeが不正」だが、今回は必須は付けている
    # ただし運用差で 400 が返る場合は text を出して失敗させる
    if r.status_code == 400:
        raise requests.HTTPError(f"400 Bad Request: {r.text[:300]}", response=r)
    r.raise_for_status()
    data = r.json()
    chunk, has_paging = parse_list_response(data)
    items.extend(chunk)

    # 2) content型ならページングで続き取得
    if has_paging:
        page = 1  # 0ページ目は取得済み
        size = min(max(page_size_hint, 1), PAGE_MAX_DEFAULT)
        while True:
            r = fetch_list_once(keyword, page=page, size=size)
            if r.status_code == 400 and size > 20:
                # サイズが大きすぎる等の対策で縮める
                size = 20
                r = fetch_list_once(keyword, page=page, size=size)
            r.raise_for_status()
            d = r.json()
            chunk = d.get("content") or []
            items.extend(chunk)
            if d.get("last", True) or not chunk:
                break
            page += 1
            time.sleep(SLEEP)

    return items

def fetch_detail(sid: str) -> Optional[Dict]:
    # 公式の詳細APIパス（/subsidies/id/{id}）
    url = f"{API_BASE}/id/{sid}"
    r = get_with_retry(url, params={})
    if r and r.status_code == 200:
        return r.json().get("result", [None])[0] if "result" in r.json() else r.json()
    return None

def fetch_all() -> List[Dict]:
    keywords, page_size = load_keywords()
    id_seen = set()
    details: List[Dict] = []
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
            time.sleep(SLEEP)
    return details

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--acceptance", choices=["0", "1"], default="0",
                    help="募集期間内で絞込む場合は1（default: 0=絞込まない）")
    args = ap.parse_args()

    try:
        items = fetch_all()
    except requests.HTTPError as e:
        # 400 等の中身を出力してデバッグ容易に
        print(f"[ERROR] HTTPError: {e.response.status_code} {e.response.text[:300]}")
        raise

    with open(args.out, "w", encoding="utf-8") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"Wrote {len(items)} rows to {args.out}")

if __name__ == "__main__":
    main()
