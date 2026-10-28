# Databricks 実行証跡（実ワークスペースで実行）

`databricks/deploy/deploy.py` が FEVM ワークスペース上で全工程を実行し、各 API の応答をそのまま保存したもの。
人手で編集した数値はない。時系列の全ログは [`RUN_LOG.md`](RUN_LOG.md)。

- ワークスペース: `https://fevm-serverless-stable-h87e9w.cloud.databricks.com`（FEVM, AWS us-east-1, serverless）
- 実行日: 2026-10-28 / 実行ユーザ: yoshitaka.kato@databricks.com
- カタログ: `serverless_stable_h87e9w_catalog`（FEVM は CREATE CATALOG 不可のため既定カタログ配下に `fraud` / `fraud_ops` スキーマ）

| # | コンポーネント | ファイル | 実行結果（要点） |
|---|---|---|---|
| 1 | Unity Catalog（取り込み前） | [`01_unity_catalog_part_a.json`](01_unity_catalog_part_a.json) | スキーマ・Volume・列マスク関数・行フィルタ関数を作成。グループへの GRANT は FEVM にアカウントグループがないため失敗（そのまま記録） |
| 2 | Volume | [`02_volume_upload.json`](02_volume_upload.json) | 合成生データ CSV を `/Volumes/.../fraud/raw` に配置 |
| 3 | **Lakeflow** | [`03_lakeflow_pipeline.json`](03_lakeflow_pipeline.json) | サーバレス宣言的パイプライン COMPLETED。bronze / silver / gold 各 **60,000行**、期待値チェック（valid_amount ほか）**失敗0件** |
| 4 | **クラシックML** | [`04_ml_training.json`](04_ml_training.json) | sklearn GBDT を MLflow で記録し UC に `fraud_gbdt` v1 登録。**ROC-AUC 0.9034**、SHAP 加法性の最大誤差 **1.5e-11**（base + Σshap = logit） |
| 5 | Unity Catalog（ガバナンス） | [`05_unity_catalog_part_b.json`](05_unity_catalog_part_b.json) | `scored_transactions` に列マスク＋行フィルタ。**同一ユーザ・同一SQL**で、海外チーム所属前は海外送金 **0行**、所属後 **2,034行**。顧客IDは `C******000` 形式 |
| 6 | GenAI 対応メモ | [`06_llm_action_memo.json`](06_llm_action_memo.json) | `ai_query`（`databricks-claude-sonnet-5`）で上位50件の対応メモを生成。入力は理由コードとEVのみ |
| 7 | **Lakebase** | [`07_lakebase_sync.json`](07_lakebase_sync.json) | Lakebase Autoscaling（Postgres 17）へ synced table 2本。Postgres 側 `fraud_alerts` **613行** = Delta 613行 |
| 8 | **Databricks Apps** | [`08_databricks_app.json`](08_databricks_app.json) | `yoshitaka-kato-fraud-review` デプロイ SUCCEEDED。起動時自己チェックで App の SP が Lakebase を読み（613件・メモ50件・キュー25件）、`app_selfcheck` に書き込み |
| 9 | **Genie** | [`09_genie_conversations.json`](09_genie_conversations.json) → 読みやすい版 [`../../outputs/genie_live_transcript.md`](../../outputs/genie_live_transcript.md) | Genie Space を API で作成し、6問＋フォローアップ1問を実会話。SQL は Genie が生成 |
| 10 | 最終確認 | [`10_final_verification.json`](10_final_verification.json) | synced table 2本とも `SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE`、App `RUNNING`、UC リネージに bronze→silver→gold、scored→ops→Lakebase を確認 |

## Genie の実回答（抜粋）

| 質問 | Genie の回答 |
|---|---|
| 海外送金と国内で、レビュー帯の的中率を比較して | 海外 **44.0%**（377件） vs 国内 **26.3%**（236件） |
| （フォローアップ）高リスク国宛だけに絞ると？ | **49.8%**（263件）＝ほぼ2件に1件が実不正 |
| 端末変更ありは不正率が何倍？ | **15.4倍**（17.8% vs 1.2%） |
| 深夜に偏っている？ | 0〜5時は不正の **29.5%** で「偏っているとは言えない」。最多は22時 |
| PENDING のブロック候補は何件・いくら？ | **156件・約1,910万円** |

## 再現方法

```bash
databricks auth login --host <workspace-url> -p <profile>
python3 databricks/deploy/deploy.py --profile <profile>          # 全工程（約20分）
python3 databricks/deploy/deploy.py --profile <profile> --steps genie,verify   # 一部だけ
```

## ローカル版との数値の違い

ローカル版（numpy 自作 GBDT）と Databricks 版（sklearn GBDT）は同じ特徴量・ハイパーパラメータだが実装が異なるため、
スコアは完全には一致しない（ROC-AUC 0.894 vs 0.903、レビュー帯 497件 vs 613件）。
経営向けの年換算KPI（`outputs/business_impact.txt`）はローカル版のホールドアウトで算出している。
