#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Slackへ週次ダイジェストを通知（堅牢・整形版）

機能:
- data/events.parquet（既定）から新着/近日締切の補助金を抽出し、Slack Webhookで通知
- 期間フィルタ: 過去N日で新着（last_seen_at or published_at） or 未来の締切が存在
- 表示: Blocksで見やすく（JST表示 / URLは<url|text>）
- エラーハンドリング: Webhook未設定、ファイル欠如、データ空でも落とさない

必要:
- pip install slack_sdk pandas pyarrow pytz (または zoneinfo が使えるPython3.9+)
- 環境変数 SLACK_WEBHOOK にIncoming Webhook URL
"""

import os
import argparse
from typing import List, Optional
from datetime import datetime, timedelta, timezone

import pandas as pd

from slack_sdk.webhook import WebhookClient

try:
    # Python 3.9+ (zoneinfo)
    from zoneinfo import ZoneInfo  # type: ignore
except Exception:  # pragma: no cover
    from pytz import timezone as ZoneInfo  # fallback


# ------------------------- helpers -------------------------

def to_dt_utc(x) -> Optional[datetime]:
    """任意の列をUTCのtz-aware datetimeへ（失敗時None）。"""
    if x is None:
        return None
    try:
        dt = pd.to_datetime(x, utc=True, errors="coerce")
        if pd.isna(dt):
            return None
        # pandas.Timestamp -> python datetime
        return dt.to_pydatetime()
    except Exception:
        return None


def jst_fmt(dt_utc: Optional[datetime], tzname: str) -> str:
    """UTC dtを指定TZで YYYY-MM-DD 表示。Noneなら '-' 。"""
    if not dt_utc:
        return "-"
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc
    return dt_utc.astimezone(tz).strftime("%Y-%m-%d")


def linkify(url: str, text: Optional[str] = None) -> str:
    """Slackの<url|text>表記に整形。urlが空なら空文字。"""
    if not url:
        return ""
    if not text:
        return f"<{url}>"
    # Slackの特殊文字を簡易エスケープ
    esc_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<{url}|{esc_text}>"


def pick_publisher(row) -> str:
    pub = (row.get("publisher") or "").strip()
    return pub or "-"


def pick_title(row) -> str:
    title = (row.get("title") or "").strip()
    return title or "(無題)"


def unique_rows(df: pd.DataFrame) -> pd.DataFrame:
    """program_id優先→なければ(title, source_url)で重複排除"""
    df = df.copy()
    if "program_id" in df.columns:
        with_pid = df["program_id"].notna()
        df_pid = df[with_pid].drop_duplicates(subset=["program_id"], keep="first")
        df_np = df[~with_pid]
        if set(["title", "source_url"]).issubset(df_np.columns):
            df_np = df_np.drop_duplicates(subset=["title", "source_url"], keep="first")
        df = pd.concat([df_pid, df_np], ignore_index=True)
    else:
        if set(["title", "source_url"]).issubset(df.columns):
            df = df.drop_duplicates(subset=["title", "source_url"], keep="first")
        else:
            df = df.drop_duplicates(keep="first")
    return df


def build_blocks(items: List[dict], tzname: str, window_days: int, total_found: int) -> List[dict]:
    """Slack Blocksを作成。itemsは辞書のリスト（title等を持つ）。"""
    if not items:
        return [
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*今週の新着/注目 補助金（過去{window_days}日）*\n該当なしでした。"}}
        ]

    header = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*今週の新着/注目 補助金（過去{window_days}日 ＋ 近日締切）*"
        }
    }
    divider = {"type": "divider"}

    body_blocks: List[dict] = []
    for r in items:
        title = pick_title(r)
        pub = pick_publisher(r)
        src = (r.get("source_url") or "").strip()
        app = (r.get("application_method") or "").strip() or "-"
        deadline = jst_fmt(r.get("_deadline_dt"), tzname)
        seen = jst_fmt(r.get("_seen_dt"), tzname)

        title_line = f"*{title}*"
        url_line = linkify(src, "公募ページ") if src else ""

        fields = [
            {"type": "mrkdwn", "text": f"*実施主体*: {pub}"},
            {"type": "mrkdwn", "text": f"*申請方法*: {app}"},
            {"type": "mrkdwn", "text": f"*締切予定*: {deadline}"},
            {"type": "mrkdwn", "text": f"*更新検知*: {seen}"},
        ]

        body_blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": title_line}},
            {"type": "section", "fields": fields},
        ]
        if url_line:
            body_blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": url_line}]})
        body_blocks.append(divider)

    footer = {
        "type": "context",
        "elements": [
            {"type": "mrkdwn",
             "text": f"表示件数: {len(items)}/{total_found} ・ タイムゾーン: {tzname} ・ 生成: {jst_fmt(datetime.now(timezone.utc), tzname)}"}
        ]
    }
    # 末尾のdividerは冗長なので削除
    if body_blocks and body_blocks[-1] == divider:
        body_blocks.pop()

    return [header, divider] + body_blocks + [footer]


def chunk_blocks(blocks: List[dict], max_blocks: int = 45) -> List[List[dict]]:
    """
    Slack Webhookはpayloadサイズ上限があるため、blocks数でざっくり分割。
    （厳密にはペイロードのバイトサイズだが、45前後なら安全域）
    """
    chunks = []
    cur = []
    for b in blocks:
        cur.append(b)
        if len(cur) >= max_blocks:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


# ------------------------- main -------------------------

def main():
    ap = argparse.ArgumentParser(description="Send weekly digest of grants to Slack via Incoming Webhook.")
    ap.add_argument("--file", default="data/events.parquet", help="入力Parquetファイル（既定: data/events.parquet）")
    ap.add_argument("--limit", type=int, default=12, help="通知最大件数（既定: 12）")
    ap.add_argument("--days", type=int, default=7, help="新着判定の過去日数（既定: 7）")
    ap.add_argument("--tz", default="Asia/Tokyo", help="表示タイムゾーン（既定: Asia/Tokyo）")
    args = ap.parse_args()

    webhook = os.environ.get("SLACK_WEBHOOK", "")
    if not webhook:
        print("SLACK_WEBHOOK is empty; skip notify.")
        return

    # 入力ファイル確認
    if not os.path.exists(args.file):
        WebhookClient(webhook).send(text="今週の新着/注目 補助金：データファイルが見つかりませんでした。")
        print(f"[WARN] file not found: {args.file}")
        return

    try:
        df = pd.read_parquet(args.file)
    except Exception as e:
        WebhookClient(webhook).send(text=f"今週の新着/注目 補助金：データ読込に失敗しました（{type(e).__name__}）。")
        print(f"[ERROR] failed to read parquet: {e}")
        return

    if df.empty:
        WebhookClient(webhook).send(text="今週の新着/注目 補助金：該当データがありませんでした。")
        print("[INFO] dataframe is empty.")
        return

    # 必要列を揃える
    for col in ["title", "publisher", "deadline_at", "application_method", "source_url", "last_seen_at", "published_at", "program_id", "status"]:
        if col not in df.columns:
            df[col] = None

    # 時刻系をUTC tz-awareに
    df["_deadline_dt"] = df["deadline_at"].apply(to_dt_utc)
    # 新着の判定: last_seen_at or published_at のうち新しい方
    seen = df["last_seen_at"].apply(to_dt_utc)
    pubd = df["published_at"].apply(to_dt_utc)
    df["_seen_dt"] = [
        a if (a and (not b or a >= b)) else b
        for a, b in zip(seen, pubd)
    ]

    # 重複除去
    df = unique_rows(df)

    # フィルタ:
    #   1) 新着: _seen_dt が過去N日以内
    #   2) 近日締切: _deadline_dt が今日(UTC)以降
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(days=args.days)
    is_new = df["_seen_dt"].apply(lambda d: (d is not None) and (d >= window_start))
    soon_due = df["_deadline_dt"].apply(lambda d: (d is not None) and (d.date() >= now_utc.date()))
    df_f = df[is_new | soon_due].copy()

    if df_f.empty:
        WebhookClient(webhook).send(text=f"今週の新着/注目 補助金（過去{args.days}日＋近日締切）：該当なしでした。")
        print("[INFO] no rows after filter.")
        return

    # 並び順: 近日締切優先（deadline昇順、Noneは後ろ）→ 新着降順
    df_f["_deadline_sort"] = df_f["_deadline_dt"].apply(lambda d: d or datetime(9999, 12, 31, tzinfo=timezone.utc))
    df_f["_seen_sort"] = df_f["_seen_dt"].apply(lambda d: d or datetime(1970, 1, 1, tzinfo=timezone.utc))
    df_f = df_f.sort_values(by=["_deadline_sort", "_seen_sort"], ascending=[True, False])

    total_found = len(df_f)
    df_f = df_f.head(args.limit)

    # Slack Blocks payloadを構築
    items = df_f.to_dict(orient="records")
    blocks = build_blocks(items, tzname=args.tz, window_days=args.days, total_found=total_found)
    chunks = chunk_blocks(blocks, max_blocks=45)

    hook = WebhookClient(webhook)
    for idx, blk in enumerate(chunks, 1):
        resp = hook.send(blocks=blk, text="今週の新着/注目 補助金ダイジェスト")
        if resp.status_code >= 400:
            print(f"[ERROR] Slack webhook failed at chunk {idx}: {resp.status_code} {resp.body}")

    print(f"[DONE] Sent {len(df_f)} rows (of {total_found}) to Slack.")


if __name__ == "__main__":
    main()
