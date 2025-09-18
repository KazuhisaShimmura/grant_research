#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開API → CSV 抽出（一覧＋必ず詳細補完）
列（順序固定）:
  補助金名, 補助金上限額, 補助率, 対象地域, 従業員数の上限, 募集期間, 詳細URL

- 一覧APIで基本項目を取得し、必ず詳細APIを1回呼んで「補助率(subsidy_rate)」「詳細URL(front_subsidy_detail_page_url)」を補完
- 募集中のみ（acceptance=1）をデフォルト。--include-closed で終了案件も含められる。
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple, Any

import requests
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "imm-research-fetch/1.7-list+detail-url",
    "Accept": "application/json",
})

CSV_HEADERS = ["補助金名", "補助金上限額", "補助率", "対象地域", "従業員数の上限", "募集期間", "詳細URL"]

# -------------------- 共通ユーティリティ --------------------

def load_keywords(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    kws: List[str] = []
    for _, words in (cfg.get("domains") or {}).items():
        kws.extend(words or [])
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
    # 戻り: (items, has_paging)
    if isinstance(data.get("result"), list):
        return data["result"], False
    if isinstance(data.get("content"), list):
        return data["content"], True
    return [], False

def fetch_list_page(keyword: str, page: Optional[int], size: Optional[int],
                    timeout: int, sleep: float, max_retry: int, acceptance: str) -> Dict:
    params: Dict[str, Any] = {
        "keyword": keyword,
        "sort": "created_date",
        "order": "DESC",
        "acceptance": acceptance,  # 0=すべて / 1=募集中のみ（既定）
    }
    if page is not None:
        params["page"] = page
    if size is not None:
        params["size"] = max(1, min(int(size), 50))
    r = get_with_retry(API_BASE, params=params, timeout=timeout, sleep=sleep, max_retry=max_retry)
    if r.status_code == 400:
        raise requests.HTTPError(f"400 Bad Request: {r.text[:300]}", response=r)
    r.raise_for_status()
    return r.json()

def iter_items_for_keyword(keyword: str, page_size: int, max_pages: int,
                           timeout: int, sleep: float, max_retry: int, acceptance: str) -> Iterator[Dict]:
    # 0ページ目（新形式 or 先頭ページ）
    data = fetch_list_page(keyword, page=None, size=None, timeout=timeout, sleep=sleep, max_retry=max_retry, acceptance=acceptance)
    chunk, has_paging = parse_list_response(data)
    if chunk:
        for it in chunk:
            if isinstance(it, dict):
                yield it

    if not has_paging:
        return

    # 旧形式ページング
    page = 1
    seen_page_fingerprint: Optional[str] = None
    while page <= max_pages:
        data = fetch_list_page(keyword, page=page, size=page_size, timeout=timeout, sleep=sleep, max_retry=max_retry, acceptance=acceptance)
        content = data.get("content") or []
        page_ids = [x.get("id") for x in content if isinstance(x, dict) and x.get("id")]
        fingerprint = ",".join([str(i) for i in page_ids[:10]])
        if not content or fingerprint == seen_page_fingerprint:
            break
        seen_page_fingerprint = fingerprint

        for it in content:
            if isinstance(it, dict):
                yield it

        print(f"[INFO]  kw='{keyword}' page={page} got={len(content)}", flush=True)
        if data.get("last", True):
            break
        page += 1
        time.sleep(sleep)

# -------------------- 抽出/補完 --------------------

def iso_to_date(s: Optional[str]) -> str:
    if not s or not isinstance(s, str):
        return ""
    try:
        # "2020-02-28T16:41:41.090Z" → "2020-02-28"
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.date().isoformat()
    except Exception:
        return s  # そのまま返す

def build_period(item: Dict) -> str:
    s = iso_to_date(item.get("acceptance_start_datetime"))
    e = iso_to_date(item.get("acceptance_end_datetime"))
    if s or e:
        return f"{s}〜{e}".strip("〜")
    return ""

# 詳細のキャッシュ（rate/URLの二重取得を避ける）
_DETAIL_CACHE: Dict[str, Dict[str, str]] = {}

def get_detail_fields(sid: str, timeout: int, sleep: float, max_retry: int) -> Tuple[str, str]:
    """
    必ず詳細APIを叩いて (subsidy_rate, front_subsidy_detail_page_url) を返す。
    """
    if not sid:
        return "", ""

    if sid in _DETAIL_CACHE:
        d = _DETAIL_CACHE[sid]
        return d.get("rate", ""), d.get("url", "")

    url = f"{API_BASE}/id/{sid}"
    r = get_with_retry(url, params={}, timeout=timeout, sleep=sleep, max_retry=max_retry)
    if r.status_code != 200:
        return "", ""
    try:
        j = r.json()
    except Exception:
        return "", ""

    detail = j.get("result", [None])[0] if isinstance(j.get("result"), list) else j
    rate = (detail or {}).get("subsidy_rate")
    front_url = (detail or {}).get("front_subsidy_detail_page_url")
    rate_s = str(rate) if rate is not None else ""
    url_s = str(front_url) if front_url is not None else ""

    _DETAIL_CACHE[sid] = {"rate": rate_s, "url": url_s}
    return rate_s, url_s

def extract_row(item: Dict, timeout: int, sleep: float, max_retry: int) -> List[str]:
    sid = item.get("id") or ""

    # 1) 補助金名（title）
    name = (item.get("title") or "").strip()

    # 2) 補助金上限額（subsidy_max_limit）
    upper = item.get("subsidy_max_limit")
    upper_str = str(upper) if upper is not None else ""

    # 3) 補助率 ＆ 7) 詳細URL（詳細APIから必ず取得）
    rate_str, front_url = get_detail_fields(sid, timeout, sleep, max_retry)

    # 4) 対象地域（target_area_search）
    area = (item.get("target_area_search") or "").strip()

    # 5) 従業員数の上限（target_number_of_employees）
    emp = (item.get("target_number_of_employees") or "").strip()

    # 6) 募集期間（acceptance_start_datetime / acceptance_end_datetime）
    period = build_period(item)

    # 7) 詳細URL（front_subsidy_detail_page_url）
    url_out = front_url

    return [name, upper_str, rate_str, area, emp, period, url_out]

# -------------------- main --------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, help="出力先（CSV例：data/jgrants_list.csv）")
    p.add_argument("--keywords-file", default="config/keywords.yml", help="キーワード定義YAMLのパス")
    p.add_argument("--page-size", type=int, default=20, help="一覧取得のページサイズ（旧形式のみ、最大50）")
    p.add_argument("--max-pages", type=int, default=50, help="1キーワードあたりの最大ページ数（無限ループ対策）")
    p.add_argument("--max-keywords", type=int, default=30, help="実行時に使う最大キーワード数（大杉対策）")
    p.add_argument("--sleep", type=float, default=0.5, help="API呼び出しの間隔(秒)")
    p.add_argument("--timeout", type=int, default=30, help="HTTPタイムアウト(秒)")
    p.add_argument("--max-retry", type=int, default=3, help="429/5xx時のリトライ回数")
    p.add_argument("--no-dedupe", action="store_true", help="idによる重複除去を無効化（既定は除去）")
    p.add_argument("--include-closed", action="store_true", help="募集終了も含める（既定は募集中のみ）")
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
    page_size = max(1, min(args.page_size, 50))
    max_pages = max(1, args.max_pages)
    acceptance = "0" if args.include_closed else "1"

    written = 0
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)

        for i, kw in enumerate(keywords, 1):
            got = 0
            try:
                for item in iter_items_for_keyword(
                    kw,
                    page_size=page_size,
                    max_pages=max_pages,
                    timeout=args.timeout,
                    sleep=args.sleep,
                    max_retry=args.max_retry,
                    acceptance=acceptance,
                ):
                    if not isinstance(item, dict):
                        continue
                    sid = item.get("id")
                    if sid and not args.no_dedupe:
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                    row = extract_row(item, timeout=args.timeout, sleep=args.sleep, max_retry=args.max_retry)
                    writer.writerow(row)
                    written += 1
                    got += 1
                    time.sleep(args.sleep)

            except requests.HTTPError as e:
                print(f"[WARN] skip keyword='{kw}' due to HTTP {getattr(e.response, 'status_code', '??')}: {str(e)[:200]}", flush=True)
                time.sleep(args.sleep)
                continue

            print(f"[INFO] [{i}/{len(keywords)}] kw='{kw}' rows_written={got} total={written}", flush=True)
            time.sleep(args.sleep)

    print(f"[DONE] wrote {written} rows to {args.out}", flush=True)

if __name__ == "__main__":
    main()
