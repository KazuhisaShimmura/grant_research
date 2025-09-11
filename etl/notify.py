#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Slackへ週次ダイジェストを通知（簡易）
"""

import os
import pandas as pd
from slack_sdk.webhook import WebhookClient


def main():
    webhook = os.environ.get("SLACK_WEBHOOK", "")
    if not webhook:
        print("SLACK_WEBHOOK is empty; skip notify.")
        return
    df = pd.read_parquet("data/events.parquet")
    top = df.head(10)[["title", "publisher", "deadline_at",
                       "application_method", "source_url"]].fillna("")
    lines = ["*今週の新着/注目 補助金（抜粋）*"]
    for _, r in top.iterrows():
        lines.append(
            f"・{
                r['title']}（{
                r['publisher']}） | 申請: {
                r['application_method']} | 締切: {
                    r['deadline_at']} | {
                        r['source_url']}")
    WebhookClient(webhook).send(text="\n".join(lines))


if __name__ == "__main__":
    main()
