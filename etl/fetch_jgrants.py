#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jGrants 公開API → CSV 抽出（一覧APIのみ）
抽出カラム（順序固定）:
  補助金名, 補助金上限額, 補助率, 対象地域, 従業員数の上限, 募集期間
- config/keywords.yml の domains.* をキーワードに使用
- 新旧レスポンス形式（result / content）両対応
- 進捗ログ、重複(id)除去、指数バックオフ付き
- 詳細APIは呼ばない（軽量）
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple, Any

import requests
import yaml

API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "imm-research-fetch/1.4-listonly-csv",
    "Accept": "application/json",
})

# ====== 抽出対象の列（順序固定）======
CSV_HEADERS = ["補助金名", "補助金上限額", "補助率", "対象地域", "従業員数の上限", "募集期間"]

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
    # 戻り: (items, has_paging)
    if isinstance(data.get("result"), list):
        return data["result"], False
    if isinstance(data.get("content"), list):
        return data["content"], True
    return [], False

def fetch_list_page(keyword: str, page: Optional[int], size: Optional[int],
                    timeout: int, sleep: float, max_retry: int) -> Dict:
    params: Dict[str, Any] = {
        "keyword": keyword,
        "sort": "created_date",
        "order": "DESC",
        "acceptance": "0",
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
                           timeout: int, sleep: float, max_retry: int) -> Iterator[Dict]:
    # 0ページ目（新形式 or 先頭ページ）
    data = fetch_list_page(keyword, page=None, size=None, timeout=timeout, sleep=sleep, max_retry=max_retry)
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
        data = fetch_list_page(keyword, page=page, size=page_size, timeout=timeout, sleep=sleep, max_retry=max_retry)
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

# ========= ゆるいマッピング（一覧APIで取れる範囲）============
def first_nonempty(*vals) -> str:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            # 文字列化して結合
            s = ", ".join([str(x) for x in v if x is not None])
            if s.strip():
                return s.strip()
        if isinstance(v, dict):
            # start/end があれば結合
            s = v.get("start") or v.get("startDate") or v.get("from")
            e = v.get("end")   or v.get("endDate")   or v.get("to")
            if s or e:
                return f"{s or ''}〜{e or ''}".strip("〜")
            # dict全体
            s = json.dumps(v, ensure_ascii=False)
            if s and s != "{}":
                return s
    return ""

def fmt_period(item: Dict) -> str:
    # 募集期間に該当しそうな複数候補
    # 文字列 or {start/end} or [複数期間] に対応
    # 候補キーは適宜追加
    candidates = [
        item.get("applicationPeriod"),
        item.get("募集期間"),
        item.get("period"),
        item.get("受付期間"),
        item.get("applicationTerm"),
        item.get("applyTerm"),
        item.get("term"),
    ]
    val = first_nonempty(*candidates)
    if val:
        return val
    # 開始・終了が別キーで来る可能性
    s = first_nonempty(item.get("applicationStart"), item.get("startDate"), item.get("start"))
    e = first_nonempty(item.get("applicationEnd"),   item.get("endDate"),   item.get("end"))
    if s or e:
        return f"{s}〜{e}".strip("〜")
    return ""

def extract_row(item: Dict) -> List[str]:
    # 1) 補助金名
    name = first_nonempty(
        item.get("title"), item.get("name"), item.get("subsidyName"), item.get("事業名"), item.get("補助金名")
    )

    # 2) 補助金上限額（例：数値 or "〜円" 等）
    upper = first_nonempty(
        item.get("maxGrantAmount"), item.get("grantUpperLimit"), item.get("上限額"), item.get("補助上限額"), item.get("上限")
    )

    # 3) 補助率（例：0.5 / 50% / 1/2 など揺れ）
    rate = first_nonempty(
        item.get("subsidyRate"), item.get("grantRate"), item.get("補助率"), item.get("助成率"), item.get("rate")
    )

    # 4) 対象地域（都道府県配列/テキスト/コードなどをゆるく結合）
    area = first_nonempty(
        item.get("targetArea"), item.get("対象地域"), item.get("対象エリア"),
        item.get("prefectures"), item.get("対象都道府県"), item.get("area")
    )

    # 5) 従業員数の上限
    emp = first_nonempty(
        item.get("employeeUpperLimit"), item.get("employeeCountMax"), item.get("従業員数上限"),
        item.get("対象従業員規模"), item.get("従業員数の上限")
    )

    # 6) 募集期間（開始/終了のどれか）
    period = fmt_period(item)

    return [name, upper, rate, area, emp, period]

# ============================================================

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
    fetched_at = datetime.now(timezone.utc).isoformat()  # 使わないが残しておくと後で便利

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
                ):
                    if not isinstance(item, dict):
                        continue
                    sid = item.get("id")
                    if sid and not args.no_dedupe:
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                    row = extract_row(item)
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
