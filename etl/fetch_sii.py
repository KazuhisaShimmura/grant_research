#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SIIポータルから省エネ事業の公募系リンクのメタ情報を抽出（堅牢・軽量版）
- インデックスページ群を走査し、関係しそうなアンカーを抽出してJSONL出力
- 後段のNLPで詳細抽出を行う前提の「リンク一覧」用途
- 取得は軽量ながらも、安定性・精度・再現性を高める実装

改良点:
- requests.Session + リトライ / User-Agent / 文字コード自動判定
- lxml が無い環境でも html.parser にフォールバック
- 正規表現ベースの日本語キーワード判定（全角/半角・大文字小文字に強い）
- 同一ドメイン限定、mailto/javascript除外
- .pdf 検出 + オプションで HEAD 検証 (--verify-pdf)
- ページから title / canonical / 更新日らしき文字列の簡易抽出
- JSONL出力に content_type / status_code / fetched_at(UTC, tz-aware) 等を付与
"""
import argparse
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry

DEFAULT_INDEX_URLS = [
    "https://sii.or.jp/",
]

# 日本語の補助金系語を広く拾うためのパターン（全角/半角/大小/カナ差をある程度許容）
KEYWORD_PATTERN = re.compile(
    r"(補助|省\s*エネ|省ｴﾈ|ＺＥＢ|ZEB|交付\s*申請|公募|事業)",
    re.IGNORECASE
)

# 日付っぽいものの簡易抽出（和暦は後段で個別処理想定）
DATE_CANDIDATE = re.compile(
    r"(?:(\d{4})[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})日?)"
)

def build_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "eucalia-sii-scraper/1.0 (+research-use)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess

def same_domain(url: str, base: str) -> bool:
    pu = urlparse(url)
    pb = urlparse(base)
    return pu.netloc == "" or pu.netloc == pb.netloc  # 相対 or 同一ホスト

def looks_pdf(href: str) -> bool:
    return href.lower().endswith(".pdf")

def verify_pdf_head(sess: requests.Session, url: str, timeout: int = 15) -> bool:
    try:
        h = sess.head(url, allow_redirects=True, timeout=timeout)
        ctype = (h.headers.get("Content-Type") or "").lower()
        return ctype.startswith("application/pdf")
    except Exception:
        return False

def detect_encoding_and_soup(text: str):
    # lxml優先、失敗時はhtml.parser
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:
        return BeautifulSoup(text, "html.parser")

def extract_updated_hint(soup: BeautifulSoup) -> str | None:
    # 1) よくある更新日ラベルの周辺テキストから
    labels = ["更新日", "最終更新", "掲載日", "公開日", "お知らせ"]
    body_text = soup.get_text(" ", strip=True)
    for m in DATE_CANDIDATE.finditer(body_text):
        y, mo, d = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d))
            # 日付文字列をそのまま返すと後段でパースしやすい
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            continue
    # メタタグからの取得（更新日時）
    for name in ("last-modified", "lastmodified", "modified", "og:updated_time"):
        tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None

def extract_title_and_canonical(soup: BeautifulSoup, base_url: str) -> tuple[str | None, str | None]:
    title = (soup.title.string.strip() if soup.title and soup.title.string else None)
    link = soup.find("link", rel=lambda x: x and "canonical" in x)
    canonical = urljoin(base_url, link.get("href")) if link and link.get("href") else None
    return title, canonical

def fetch_index(sess: requests.Session, url: str, verify_pdf: bool, sleep_sec: float):
    r = sess.get(url, timeout=30)
    # 文字コード判定（日本語サイト向け）
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "us-ascii"):
        r.encoding = r.apparent_encoding or r.encoding
    r.raise_for_status()

    soup = detect_encoding_and_soup(r.text)
    page_title, canonical = extract_title_and_canonical(soup, url)
    updated_hint = extract_updated_hint(soup)

    results = []
    seen = set()

    for a in soup.select("a"):
        href = a.get("href")
        if not href:
            continue
        if href.startswith(("mailto:", "javascript:", "#")):
            continue

        abs_url = urljoin(url, href)
        if not same_domain(abs_url, url):
            # 同一ドメイン内に限定
            continue

        text = a.get_text(" ", strip=True)
        if not text:
            continue

        # キーワードに合致するリンクのみ採用
        if not KEYWORD_PATTERN.search(text):
            continue

        is_pdf = looks_pdf(abs_url)
        content_type = None
        status_code = None

        # オプション：HEADでPDFを確認（コスト増なので必要時のみ）
        if verify_pdf and is_pdf:
            is_pdf = verify_pdf_head(sess, abs_url)

        key = (abs_url, text, is_pdf)
        if key in seen:
            continue
        seen.add(key)

        # 軽量方針なのでHEADはデフォルト行わない。他用途向けにコメント残し
        # try:
        #     h = sess.head(abs_url, allow_redirects=True, timeout=15)
        #     status_code = h.status_code
        #     content_type = h.headers.get("Content-Type")
        # except Exception:
        #     pass

        results.append({
            "source_index": url,
            "page_title": page_title,
            "canonical": canonical,
            "updated_hint": updated_hint,           # 後段で正規化する前提の素朴なヒント
            "url": abs_url,
            "text": text,
            "is_pdf": bool(is_pdf),
            "status_code": status_code,             # 取得していない場合は None
            "content_type": content_type,           # 同上
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    time.sleep(max(sleep_sec, 0.0))  # サイトに優しく
    return results

def parse_args():
    ap = argparse.ArgumentParser(description="SIIポータルの公募系リンク抽出（軽量）")
    ap.add_argument("--out", required=True, help="出力JSONLファイルパス")
    ap.add_argument("--urls", nargs="*", default=None,
                    help="走査するインデックスURL（未指定時は既定のSIIトップ）")
    ap.add_argument("--verify-pdf", action="store_true",
                    help="PDFリンクをHEADで検証（遅くなる）")
    ap.add_argument("--sleep", type=float, default=0.3,
                    help="ページ間のスリープ秒（デフォルト0.3s）")
    ap.add_argument("--max", type=int, default=None,
                    help="上限件数（テスト用）。超過したら打ち切り")
    return ap.parse_args()

def main():
    args = parse_args()
    index_urls = args.urls or DEFAULT_INDEX_URLS

    sess = build_session()
    rows = []
    for u in index_urls:
        try:
            links = fetch_index(sess, u, verify_pdf=args.verify_pdf, sleep_sec=args.sleep)
            rows.extend(links)
            if args.max and len(rows) >= args.max:
                rows = rows[:args.max]
                break
        except requests.HTTPError as e:
            print(f"[WARN] HTTPError {e.response.status_code} at {u}")
        except Exception as e:
            print(f"[WARN] Failed at {u}: {e}")

    # 重複URLの最終フィルタ（テキストは異なる場合もあるがURL優先でユニーク化したいケース向け）
    dedup = {}
    for r in rows:
        dedup.setdefault(r["url"], r)  # 先勝ち
    rows = list(dedup.values())

    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"Wrote {len(rows)} rows to {args.out}")

if __name__ == "__main__":
    main()
