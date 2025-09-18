#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開API → JSONL 取得（一覧APIのみ・軽量版）
- config/keywords.yml の domains.* をキーワードに使用
- 新旧レスポンス形式（result / content）両対応
- 進捗ログを標準出力に表示
- 無限ループ対策: ページ数上限（--max-pages）/ 同一ページ検出
- スロットリング対策: スリープ間隔（--sleep）/ ページサイズ（--page-size）
- 出力は一覧APIの各アイテムをそのままJSONL化＋keyword/fetched_at/page_hintを付与
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

import requests
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "imm-research-fetch/1.3-listonly",
    "Accept": "application/json",
})

def load_keywords(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    kws: List[str] = []
    for _, words in (cfg.get("domains") or {}).items():
        kws.extend(words or [])
    # 正規化&重複除去
    norm: List[str] = []
    seen = set()
    for k in kws:
        k2 = " ".join(str(k).split()).strip()
        if k2 and k2 not in seen:
            seen.add(k2)
            norm.append(k2)
    return norm

def get_with_retry(url: str, params: Dict, timeout: int, sleep: float, max_retry: int) -> requests.Response:
    last = None
    for i in range(max_retry):
        r = SESSION.get(url, params=params, timeout=timeout)
        if r.status_code in (429, 500, 502, 503, 504):
            wait = sleep * (2 ** i)
            print(f"[WARN] {r.status_code} on {url} params={params} -> retry in {wait:.1f}s", flush=True)
            time.sleep(wait)
            last = r
            continue
        return r
    return last or r

def parse_list_response(data: Dict) -> Tuple[List[Dict], bool]:
    """
    戻り: (items, has_paging)
    - 新形式: {"result": [ ... ]}（ページング無し）
    - 旧形式: {"content": [ ... ], "last": bool, ...}（ページングあり）
    """
    if isinstance(data.get("result"), list):
        return data["result"], False
    if isinstance(data.get("content"), list):
        return data["content"], True
    return [], False

def fetch_list_page(keyword: str, page: Optional[int], size: Optional[int],
                    timeout: int, sleep: float, max_retry: int) -> Dict:
    params: Dict[str, str | int] = {
        "keyword": keyword,
        "sort": "created_date",
        "order": "DESC",
        "acceptance": "0",
    }
    if page is not None:
        params["page"] = page
    if size is not None:
        params["size"] = max(1, min(int(size), 50))  # 念のため50上限
    r = get_with_retry(API_BASE, params=params, timeout=timeout, sleep=sleep, max_retry=max_retry)
    if r.status_code == 400:
        raise requests.HTTPError(f"400 Bad Request: {r.text[:300]}", response=r)
    r.raise_for_status()
    return r.json()

def iter_items_for_keyword(keyword: str, page_size: int, max_pages: int,
                           timeout: int, sleep: float, max_retry: int) -> Iterator[Tuple[Dict, Optional[Dict[str, int]]]]:
    """キーワードで一覧を取得し、各アイテム(dict)とpageヒントを逐次返す。"""
    # まず新形式（非ページング） or 0ページ目相当を取得
    data = fetch_list_page(keyword, page=None, size=None, timeout=timeout, sleep=sleep, max_retry=max_retry)
    chunk, has_paging = parse_list_response(data)
    if chunk:
        first_hint: Optional[Dict[str, int]] = {"page": 0} if has_paging else None
        for it in chunk:
            if isinstance(it, dict):
                yield it, first_hint

    if not has_paging:
        return

    # 旧形式（ページング）
    page = 1
    seen_page_fingerprint: Optional[str] = None
    while page <= max_pages:
        data = fetch_list_page(keyword, page=page, size=page_size, timeout=timeout, sleep=sleep, max_retry=max_retry)
        content = data.get("content") or []
        page_ids = [x.get("id") for x in content if isinstance(x, dict) and x.get("id")]
        fingerprint = ",".join([str(i) for i in page_ids[:10]])
        if not content or fingerprint == seen_page_fingerprint:
            break
        seen_page_fingerprint = fingerprint

        for it in content:
            if isinstance(it, dict):
                yield it, {"page": page, "size": page_size}

        print(f"[INFO]  kw='{keyword}' page={page} got={len(content)}", flush=True)
        if data.get("last", True):
            break
        page += 1
        time.sleep(sleep)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, help="出力先（例：data/jgrants_list.jsonl）")
    p.add_argument("--keywords-file", default="config/keywords.yml", help="キーワード定義YAMLのパス")
    p.add_argument("--page-size", type=int, default=20, help="一覧取得のページサイズ（旧形式のみ、最大50）")
    p.add_argument("--max-pages", type=int, default=50, help="1キーワードあたりの最大ページ数（無限ループ対策）")
    p.add_argument("--max-keywords", type=int, default=30, help="実行時に使う最大キーワード数（大杉対策）")
    p.add_argument("--sleep", type=float, default=0.5, help="API呼び出しの間隔(秒)")
    p.add_argument("--timeout", type=int, default=30, help="HTTPタイムアウト(秒)")
    p.add_argument("--max-retry", type=int, default=3, help="429/5xx時のリトライ回数")
    p.add_argument("--no-dedupe", action="store_true", help="idによる重複除去を無効化（既定は除去）")
    args = p.parse_args()

    # 出力ディレクトリ
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    # キーワード読込
    try:
        all_keywords = load_keywords(args.keywords_file)
    except Exception as e:
        print(f"[ERROR] failed to load keywords: {e}", file=sys.stderr)
        sys.exit(1)
    if not all_keywords:
        print("[ERROR] no keywords loaded", file=sys.stderr)
        sys.exit(1)
    keywords = all_keywords[: args.max_keywords]
    print(f"[INFO] keywords loaded={len(all_keywords)} (using first {len(keywords)})", flush=True)

    seen_ids: set[str] = set()
    written = 0
    page_size = max(1, min(args.page_size, 50))
    max_pages = max(1, args.max_pages)
    fetched_at = datetime.now(timezone.utc).isoformat()

    with open(args.out, "w", encoding="utf-8") as f:
        for i, kw in enumerate(keywords, 1):
            items_got = 0
            try:
                for item, page_hint in iter_items_for_keyword(
                    kw,
                    page_size=page_size,
                    max_pages=max_pages,
                    timeout=args.timeout,
                    sleep=args.sleep,
                    max_retry=args.max_retry,
                ):
                    if not isinstance(item, dict):
                        continue
                    sid = item.get("id")
                    if sid and not args.no_dedupe:
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                    out_obj = dict(item)
                    out_obj["_keyword"] = kw
                    out_obj["_fetched_at"] = fetched_at
                    if page_hint:
                        out_obj["_page_hint"] = page_hint
                    # 不要ならここでフィールドを間引くことも可
                    f.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
                    written += 1
                    items_got += 1
                    time.sleep(args.sleep)
            except requests.HTTPError as e:
                print(f"[WARN] skip keyword='{kw}' due to HTTP {getattr(e.response, 'status_code', '??')}: {str(e)[:200]}", flush=True)
                time.sleep(args.sleep)
                continue

            print(f"[INFO] [{i}/{len(keywords)}] kw='{kw}' items_got={items_got} written_total={written}", flush=True)
            time.sleep(args.sleep)

    print(f"[DONE] wrote {written} rows to {args.out}", flush=True)

if __name__ == "__main__":
    main()
