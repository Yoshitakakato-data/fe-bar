# Databricks 移植版 — 配備ガイド

ローカル版（`src/`, `app/local_app.py`）と **1:1 対応**する Databricks ネイティブ実装。
列名・しきい値・コスト前提・理由コードの意味をローカルと一致させてあるため、
ローカルで得た数値がそのまま Databricks 上で再現できる。

> **実ワークスペースで実行済み**。`deploy/deploy.py` が下表の順に全工程を実行し、
> API 応答を `../evidence/databricks/` に保存する（2026-10-28, FEVM で実行）。
>
> ```bash
> python3 databricks/deploy/deploy.py --profile <profile>
> ```

## 実行順序

| # | ファイル | コンポーネント | 内容 |
|---|---|---|---|
| 0 | `../data/raw_transactions.csv` を Volume `fraud_demo.fraud.raw` にアップロード | — | 合成生データ |
| 1 | `01_lakeflow_ingest.py` | **Lakeflow** | 宣言的パイプラインで bronze→silver→gold |
| 2 | `02_unity_catalog_setup.sql` | **Unity Catalog** | カタログ/スキーマ/Volume/PIIマスク/権限/リネージ |
| 3 | `03_train_model.py` | **クラシックML** | GBDT学習＋SHAP理由コード→MLflow(UC)登録→`scored_transactions` |
| 4 | `04_lakebase_sync.md` | **Lakebase** | Delta→Postgres synced table＋判断テーブル |
| 5 | `05_genie_space_setup.md` | **Genie** | Genie Space を API で作成し、6問＋フォローアップを実会話 |
| 6 | `../app/app.py` + `app.yaml` | **Databricks Apps** | Flask レビュー支援アプリを Lakebase 接続でデプロイ |
| 7 | `06_action_memo.sql` | **GenAI** | `ai_query` で理由コード＋EV から対応メモ（判断はしない） |
| 8 | `07_lakebase_sql.py` | Lakebase 補助 | Postgres 側の GRANT・件数確認をサーバレス Job 内で実行（資格情報をローカルに出さない） |

## ローカル ↔ Databricks 対応表

| ローカル | Databricks | 一致させている点 |
|---|---|---|
| `src/pipeline.py` | `01_lakeflow_ingest.py` | gold特徴量の派生ロジック・列名 |
| `src/gbdt.py`（numpy GBDT＋木経路寄与） | `03_train_model.py`（sklearn GBDT＋`shap.TreeExplainer`） | logit空間の加法的寄与＝理由コード |
| `src/train_model.py` | `03_train_model.py` | しきい値ポリシー・reason_codes JSON |
| `src/load_lakebase.py`（sqlite） | `04_lakebase_sync.md`（Postgres synced table） | `fraud_alerts` スキーマ |
| `src/genie_queries.py`（人手のSQL） | Genie Space（SQL は Genie が生成） | 同じ6つの質問文。実回答は `../outputs/genie_live_transcript.md` |
| `app/local_app.py`（http.server） | `app/app.py`（Flask＋psycopg） | UI・EV計算・判断書き込み |

## デプロイ
`deploy/deploy.py` が CLI 経由で以下を行う（各ステップ単体でも `--steps` で実行可）。

| step | 内容 |
|---|---|
| `uc_a` | 02 の PART A（スキーマ・Volume・マスク/行フィルタ関数） |
| `upload` | 生データ CSV を Volume へ |
| `pipeline` | 01 をサーバレス Lakeflow パイプラインとして作成・フルリフレッシュ |
| `train` | 03 をサーバレス Job で実行、結果 JSON を回収 |
| `uc_b` | 02 の PART B（列マスク・行フィルタ適用）＋グループ所属前後の可視性比較 |
| `memo` | 06（`ai_query`） |
| `lakebase` | Lakebase Autoscaling へ synced table 作成、Postgres 側件数確認 |
| `app` | App 作成（postgres リソース付与）→ SP へ GRANT → デプロイ → 起動時自己チェック確認 |
| `genie` | Genie Space 作成 → 6問＋フォローアップの実会話 → 会話ログ出力 |
| `verify` | 最終状態（synced table / App / Postgres / リネージ）をまとめて記録 |

## 前提
- Unity Catalog 有効、Serverless SQL Warehouse、Model Serving/MLflow、Lakebase 利用可能。
- FEVM のように CREATE CATALOG / アカウントグループがない環境では、既定カタログ配下にスキーマを作り、
  ワークスペースグループ＋`is_member()` でマスク・行フィルタを判定する（GRANT は失敗として記録）。
- 合成データのみ。実顧客PIIは使用しない（本デモの主眼が説明性とガバナンス）。
