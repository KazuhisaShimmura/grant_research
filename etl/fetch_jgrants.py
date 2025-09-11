#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開API → JSONL 取得（安定・進捗ログ版）
- config/keywords.yml の domains.* をキーワードに使用
- 新旧レスポンス形式（result / content）両対応
- 進捗ログを標準出力に表示
- 無限ループ対策: ページ数上限（--max-pages）/ 同一ページ検出
- スロットリング対策: スリープ間隔（--sleep）/ ページサイズ（--page-size）
- フェイルセーフ: 詳細取得の総件数上限（--max-details）
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "imm-research-fetch/1.2",
    "Accept": "application/json",
})

def load_keywords(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    kws: List[str] = []
    for _, words in (cfg.get("domains") or {}).items():
        kws.extend(words or [])
    # 正規化&重複除去
    norm = []
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
    return last or r  # 最後の応答を返す

def parse_list_response(data: Dict) -> Tuple[List[Dict], bool]:
    """戻り: (items, has_paging)"""
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
        params["size"] = size
    r = get_with_retry(API_BASE, params=params, timeout=timeout, sleep=sleep, max_retry=max_retry)
    # 400系はそのまま停止して内容を表示
    if r.status_code == 400:
        raise requests.HTTPError(f"400 Bad Request: {r.text[:300]}", response=r)
    r.raise_for_status()
    return r.json()

def fetch_ids_for_keyword(keyword: str, page_size: int, max_pages: int,
                          timeout: int, sleep: float, max_retry: int) -> List[str]:
    """キーワードで一覧を取得し、IDのリストを返す。無限ループ対策あり。"""
    ids: List[str] = []

    # まず新形式（非ページング）を試す
    data = fetch_list_page(keyword, page=None, size=None, timeout=timeout, sleep=sleep, max_retry=max_retry)
    chunk, has_paging = parse_list_response(data)
    ids.extend([x.get("id") for x in chunk if x.get("id")])

    if not has_paging:
        return ids

    # 旧形式（ページング）
    page = 1  # 0ページ目は取得済み
    seen_page_fingerprint: Optional[str] = None
    while page <= max_pages:
        data = fetch_list_page(keyword, page=page, size=page_size, timeout=timeout, sleep=sleep, max_retry=max_retry)
        content = data.get("content") or []
        page_ids = [x.get("id") for x in content if x.get("id")]
        # ページ内容が変わらない（もしくは空）場合は打ち切り
        fingerprint = ",".join(page_ids[:10])  # 先頭10件で簡易判定
        if not page_ids or fingerprint == seen_page_fingerprint:
            break
        seen_page_fingerprint = fingerprint

        ids.extend(page_ids)
        print(f"[INFO]  kw='{keyword}' page={page} got={len(page_ids)} (total_ids={len(ids)})", flush=True)

        if data.get("last", True):
            break
        page += 1
        time.sleep(sleep)

    return ids

def fetch_detail(sid: str, timeout: int, sleep: float, max_retry: int) -> Optional[Dict]:
    url = f"{API_BASE}/id/{sid}"
    r = get_with_retry(url, params={}, timeout=timeout, sleep=sleep, max_retry=max_retry)
    if r.status_code == 200:
        try:
            j = r.json()
        except Exception:
            return None
        return j.get("result", [None])[0] if isinstance(j.get("result"), list) else j
    if r.status_code in (429, 500, 502, 503, 504):
        # ここまで来たらリトライ済みなので諦める
        return None
    r.raise_for_status()
    return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, help="出力先（例：data/jgrants.jsonl）")
    p.add_argument("--keywords-file", default="config/keywords.yml", help="キーワード定義YAMLのパス")
    p.add_argument("--page-size", type=int, default=20, help="一覧取得のページサイズ（旧形式のみ）")
    p.add_argument("--max-pages", type=int, default=50, help="1キーワードあたりの最大ページ数（無限ループ対策）")
    p.add_argument("--max-details", type=int, default=500, help="詳細取得の総件数上限（フェイルセーフ）")
    p.add_argument("--max-keywords", type=int, default=30, help="実行時に使う最大キーワード数（大杉対策）")
    p.add_argument("--sleep", type=float, default=0.5, help="API呼び出しの間隔(秒)")
    p.add_argument("--timeout", type=int, default=30, help="HTTPタイムアウト(秒)")
    p.add_argument("--max-retry", type=int, default=3, help="リトライ回数(429/5xx)")
    args = p.parse_args()

    # 出力ディレクトリ作成
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

    # 一覧→ID収集
    all_ids: List[str] = []
    for i, kw in enumerate(keywords, 1):
        try:
            ids = fetch_ids_for_keyword(
                kw, page_size=max(1, min(args.page_size, 50)),
                max_pages=max(1, args.max_pages),
                timeout=args.timeout, sleep=args.sleep, max_retry=args.max_retry
            )
        except requests.HTTPError as e:
            print(f"[WARN] skip keyword='{kw}' due to HTTP {e.response.status_code}: {e.response.text[:200]}", flush=True)
            continue

        before = len(all_ids)
        # 重複排除
        all_ids = list(dict.fromkeys(all_ids + ids))
        print(f"[INFO] [{i}/{len(keywords)}] kw='{kw}' ids_got={len(ids)} ids_total={len(all_ids)}", flush=True)

        # 詳細取得上限に向けた早期終了（IDが多すぎる場合）
        if len(all_ids) >= args.max_details:
            print(f"[INFO] reached max_details({args.max_details}) at keyword='{kw}'", flush=True)
            break

        time.sleep(args.sleep)

    # 詳細取得
    written = 0
    with open(args.out, "w", encoding="utf-8") as f:
        for j, sid in enumerate(all_ids, 1):
            if written >= args.max_details:
                break
            detail = fetch_detail(sid, timeout=args.timeout, sleep=args.sleep, max_retry=args.max_retry)
            if detail:
                f.write(json.dumps(detail, ensure_ascii=False) + "\n")
                written += 1
            if j % 25 == 0 or j == len(all_ids):
                print(f"[INFO] details {written}/{min(len(all_ids), args.max_details)} (processed {j})", flush=True)
            time.sleep(args.sleep)

    print(f"[DONE] wrote {written} rows to {args.out}", flush=True)

if __name__ == "__main__":
    main()
