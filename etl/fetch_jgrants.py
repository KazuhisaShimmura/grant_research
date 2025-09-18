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
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

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

def iter_ids_for_keyword(keyword: str, page_size: int, max_pages: int,
                         timeout: int, sleep: float, max_retry: int) -> Iterator[Tuple[str, Optional[Dict[str, int]]]]:
    """キーワードで一覧を取得し、IDを逐次返す。無限ループ対策あり。"""

    total_ids = 0

    # まず新形式（非ページング）を試す
    data = fetch_list_page(keyword, page=None, size=None, timeout=timeout, sleep=sleep, max_retry=max_retry)
    chunk, has_paging = parse_list_response(data)
    first_ids = [x.get("id") for x in chunk if x.get("id")]

    if first_ids:
        total_ids += len(first_ids)
        first_hint: Optional[Dict[str, int]] = {"page": 0} if has_paging else None
        for sid in first_ids:
            yield sid, first_hint

    if not has_paging:
        return

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

        total_ids += len(page_ids)
        for sid in page_ids:
            yield sid, {"page": page, "size": page_size}
        print(f"[INFO]  kw='{keyword}' page={page} got={len(page_ids)} (total_ids={total_ids})", flush=True)

        if data.get("last", True):
            break
        page += 1
        time.sleep(sleep)

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


def build_augmented_record(detail: Dict, keyword: str, fetched_at: str,
                           page_hint: Optional[Dict[str, int]]) -> Dict:
    """詳細データにメタ情報を付与する。"""

    record = dict(detail)
    record.pop("matched_keywords", None)
    record["_keyword"] = keyword
    record["_fetched_at"] = fetched_at
    if page_hint:
        record["_page_hint"] = page_hint
    else:
        record.pop("_page_hint", None)
    return record


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

    seen_ids: set[str] = set()
    written = 0
    processed = 0
    total_keywords = len(keywords)
    page_size = max(1, min(args.page_size, 50))
    max_pages = max(1, args.max_pages)

    with open(args.out, "w", encoding="utf-8") as f:
        for idx, kw in enumerate(keywords, 1):
            if written >= args.max_details:
                break

            items_got = 0
            try:
                for sid, page_hint in iter_ids_for_keyword(
                    kw,
                    page_size=page_size,
                    max_pages=max_pages,
                    timeout=args.timeout,
                    sleep=args.sleep,
                    max_retry=args.max_retry,
                ):
                    if written >= args.max_details:
                        break
                    if not sid or sid in seen_ids:
                        continue

                    seen_ids.add(sid)
                    detail = fetch_detail(sid, timeout=args.timeout, sleep=args.sleep, max_retry=args.max_retry)
                    if detail and isinstance(detail, dict):
                        fetched_at = datetime.now(timezone.utc).isoformat()
                        record = build_augmented_record(detail, kw, fetched_at, page_hint)
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                        written += 1
                        items_got += 1
                    processed += 1
                    if processed % 25 == 0:
                        print(f"[INFO] details written={written} processed={processed}", flush=True)
                    if written >= args.max_details:
                        break
                    time.sleep(args.sleep)
            except requests.HTTPError as e:
                print(
                    f"[WARN] skip keyword='{kw}' due to HTTP {e.response.status_code}: {e.response.text[:200]}",
                    flush=True,
                )
                time.sleep(args.sleep)
                continue

            print(
                f"[INFO] [{idx}/{total_keywords}] kw='{kw}' items_got={items_got} total_written={written}",
                flush=True,
            )

            if written >= args.max_details:
                print(f"[INFO] reached max_details({args.max_details}) at keyword='{kw}'", flush=True)
                break

            time.sleep(args.sleep)

    print(f"[DONE] wrote {written} rows to {args.out}", flush=True)

if __name__ == "__main__":
    main()
