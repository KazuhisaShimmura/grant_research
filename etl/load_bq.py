#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BigQuery にロード
"""
import argparse
import pandas as pd
import os
from google.cloud import bigquery


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    args = ap.parse_args()

    project = os.environ.get("GCP_PROJECT", "")
    dataset = os.environ.get("BQ_DATASET", "grants_hc")
    table = os.environ.get("BQ_TABLE", "programs")
    if not project:
        print("GCP_PROJECT is empty; skip load.")
        return

    client = bigquery.Client(project=project)
    table_id = f"{project}.{dataset}.{table}"
    df = pd.read_parquet(args.input)
    job = client.load_table_from_dataframe(df, table_id)
    job.result()
    print(f"Loaded {len(df)} rows to {table_id}")


if __name__ == "__main__":
    main()
