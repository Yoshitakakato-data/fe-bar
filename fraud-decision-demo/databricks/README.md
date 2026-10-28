# Databricks 移植版 — 配備ガイド

ローカル版（`src/`, `app/local_app.py`）と **1:1 対応**する Databricks ネイティブ実装。
列名・しきい値・コスト前提・理由コードの意味をローカルと一致させてあるため、
ローカルで得た数値がそのまま Databricks 上で再現できる。

## 実行順序

| # | ファイル | コンポーネント | 内容 |
|---|---|---|---|
| 0 | `../data/raw_transactions.csv` を Volume `fraud_demo.fraud.raw` にアップロード | — | 合成生データ |
| 1 | `01_lakeflow_ingest.py` | **Lakeflow** | 宣言的パイプラインで bronze→silver→gold |
| 2 | `02_unity_catalog_setup.sql` | **Unity Catalog** | カタログ/スキーマ/Volume/PIIマスク/権限/リネージ |
| 3 | `03_train_model.py` | **クラシックML** | GBDT学習＋SHAP理由コード→MLflow(UC)登録→`scored_transactions` |
| 4 | `04_lakebase_sync.md` | **Lakebase** | Delta→Postgres synced table＋判断テーブル |
| 5 | `05_genie_space_setup.md` | **Genie Room** | Genie Space 設定＋サンプル質問6問 |
| 6 | `../app/app.py` + `app.yaml` | **Databricks Apps** | Flask レビュー支援アプリを Lakebase 接続でデプロイ |

## ローカル ↔ Databricks 対応表

| ローカル | Databricks | 一致させている点 |
|---|---|---|
| `src/pipeline.py` | `01_lakeflow_ingest.py` | gold特徴量の派生ロジック・列名 |
| `src/gbdt.py`（numpy GBDT＋木経路寄与） | `03_train_model.py`（sklearn GBDT＋`shap.TreeExplainer`） | logit空間の加法的寄与＝理由コード |
| `src/train_model.py` | `03_train_model.py` | しきい値ポリシー・reason_codes JSON |
| `src/load_lakebase.py`（sqlite） | `04_lakebase_sync.md`（Postgres synced table） | `fraud_alerts` スキーマ |
| `src/genie_queries.py` | `05_genie_space_setup.md` | 6つの質問文と対応SQL |
| `app/local_app.py`（http.server） | `app/app.py`（Flask＋psycopg） | UI・EV計算・判断書き込み |

## デプロイ（Databricks CLI 例）
```bash
# Lakeflowパイプライン作成（UI推奨）→ 01 をソース指定
# UC設定
databricks sql -f 02_unity_catalog_setup.sql
# ML学習ノートブック実行（Job or ノートブックUI）
# Lakebase synced table 作成（04の手順）
# App デプロイ
cd ../app && databricks apps deploy fraud-review --source-code-path .
```

## 前提
- Unity Catalog 有効、Serverless SQL Warehouse、Model Serving/MLflow、Lakebase 利用可能。
- 合成データのみ。実顧客PIIは使用しない（本デモの主眼が説明性とガバナンス）。
