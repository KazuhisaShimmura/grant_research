# grants_hc — ヘルスケア補助金 収集・正規化・通知テンプレ

このテンプレは、**東京都＋神奈川県**を初期実装し、他都道府県に横展開できる構成です。  
ランタイムは GitHub Actions / Python 3.11 / BigQuery（任意）を想定。

## 収集ポリシー（要旨）
1. **一次情報優先**（jGrants API → 公式ページ/要綱PDF → 軽量スクレイピング）。
2. **共通スキーマに正規化**（補助率/上限/対象/締切/地域/所管/申請方法 等）。
3. **変更検知**（ETag/Last-Modified, PDFのSHA256）。
4. **イベント化/通知**（募集開始/要件改定/締切延長/締切接近/終了）。
5. **人手レビュー前提の確信度フラグ**（抽出精度0.8未満は要レビュー）。

## すぐ動かす手順（最小構成）
1. GitHubにリポジトリ作成 → この一式をアップロード
2. リポジトリの **Settings → Secrets and variables → Actions** で以下を登録
   - `GCP_PROJECT` : BigQueryのプロジェクトID（使わない場合は空でOK）
   - `GCP_SA_JSON` : サービスアカウントJSON（使わない場合は空でOK）
   - `SLACK_WEBHOOK` : 通知用Webhook（使わない場合は空でOK）
3. `.github/workflows/daily.yml` のcronは JST 07:30 に設定済（UTC換算で 22:30）
4. `requirements.txt` で依存をインストール
5. `.github/workflows/daily.yml` から **Run workflow** を手動実行して初回データを流し込み

## BigQueryを使わない場合
- `etl/load_bq.py` はスキップ可能。`.github/workflows/daily.yml` の該当ステップをコメントアウトしてください。
- データは `data/normalized.parquet` および `data/events.parquet` に出力されます。

## 他都道府県の追加
- `config/sources_<pref>.yml` を追加し、公式ページのURLを登録してください（医療局/福祉部局/財団/エネルギー執行団体）。
- セレクタの例・想定は `config/sources_tokyo.yml` / `config/sources_kanagawa.yml` を参照。

最終更新: 2025-09-10T09:43:43.449068
