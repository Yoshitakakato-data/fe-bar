# Databricks notebook source
# MAGIC %md
# MAGIC # ④ クラシックML — 勾配ブースティング＋SHAPによる理由コード
# MAGIC
# MAGIC gold特徴量テーブルで **GradientBoostingClassifier** を学習し、**SHAP(TreeExplainer)** で
# MAGIC 1件ごとの理由コード（なぜ止めるべきか）を生成。MLflow経由で Unity Catalog モデルレジストリに登録し、
# MAGIC スコア＋理由コードを Delta テーブル `scored_transactions` に書き出す（Lakebase同期の元）。
# MAGIC
# MAGIC **ローカル等価物**: `src/gbdt.py`（numpy版GBDT＋木経路寄与）＋ `src/train_model.py`。
# MAGIC アルゴリズム・特徴量・理由コードの意味は同一。ローカルはSHAP相当の加法的寄与を自作している。

# COMMAND ----------
# MAGIC %pip install shap scikit-learn mlflow
# MAGIC %restart_python

# COMMAND ----------
import mlflow, shap, json
import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, precision_recall_fscore_support

FEATURES = ["amount_to_avg_ratio", "amount_log", "is_new_payee", "is_foreign",
            "is_high_risk_country", "hour_risk", "num_txn_24h", "device_change",
            "account_age_days", "sec_since_last_txn"]
LABELS_JP = {
    "amount_to_avg_ratio": "送金額が平常時の平均を大きく上回る", "amount_log": "送金額が高額",
    "is_new_payee": "初めての送金先", "is_foreign": "海外への送金",
    "is_high_risk_country": "高リスク国宛の送金", "hour_risk": "普段利用しない時間帯（深夜など）",
    "num_txn_24h": "短時間に取引が集中（高頻度）", "device_change": "普段と異なる端末からの操作",
    "account_age_days": "口座開設からの経過期間が短い", "sec_since_last_txn": "直前取引からの間隔が異常"}
REVIEW_TH, BLOCK_TH = 0.10, 0.60

dbutils.widgets.text("catalog", "fraud_demo")
CATALOG = dbutils.widgets.get("catalog")
SCHEMA = f"{CATALOG}.fraud"

# COMMAND ----------
# MAGIC %md ## 学習データ読み込み（Lakeflow gold）

# COMMAND ----------

pdf = spark.table(f"{SCHEMA}.gold_features").toPandas()
X, y = pdf[FEATURES].values, pdf["is_fraud"].values
Xtr, Xte, ytr, yte, idx_tr, idx_te = train_test_split(
    X, y, np.arange(len(y)), test_size=0.3, random_state=42, stratify=y)

# COMMAND ----------
# MAGIC %md ## 学習＋評価（MLflowで記録）

# COMMAND ----------

mlflow.set_registry_uri("databricks-uc")
with mlflow.start_run(run_name="gbdt_fraud") as run:
    clf = GradientBoostingClassifier(n_estimators=150, learning_rate=0.15,
                                     max_depth=3, random_state=42)
    clf.fit(Xtr, ytr)
    proba = clf.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, proba)
    pred = (proba >= BLOCK_TH).astype(int)
    p, r, f1, _ = precision_recall_fscore_support(yte, pred, average="binary", zero_division=0)
    mlflow.log_params({"n_estimators": 150, "max_depth": 3, "learning_rate": 0.15})
    mlflow.log_metrics({"roc_auc": auc, "precision@0.6": p, "recall@0.6": r, "f1@0.6": f1})
    mi = mlflow.sklearn.log_model(clf, "model", input_example=Xtr[:5],
                                  registered_model_name=f"{SCHEMA}.fraud_gbdt")
    print(f"ROC-AUC={auc:.4f}  precision@0.6={p:.3f}  recall@0.6={r:.3f}")

# COMMAND ----------
# MAGIC %md ## SHAPで理由コード生成（加法的: base_value + Σshap = logit）

# COMMAND ----------

explainer = shap.TreeExplainer(clf)
sv = explainer.shap_values(Xte)          # (n, n_features)  logit空間の寄与

# 加法性の検算: base_value + Σshap が モデルの logit(decision_function) と一致すること
logit = clf.decision_function(Xte)
additivity_err = float(np.max(np.abs(explainer.expected_value + sv.sum(axis=1) - logit)))
print(f"SHAP additivity max|base+Σshap-logit| = {additivity_err:.2e}")

def reason_codes(row_shap, row_x, top_k=3):
    order = np.argsort(-row_shap)
    out = []
    for fi in order[:top_k]:
        if row_shap[fi] <= 0:
            break
        f = FEATURES[fi]
        out.append({"feature": f, "label": LABELS_JP[f],
                    "contribution": round(float(row_shap[fi]), 4),
                    "value": round(float(row_x[fi]), 4)})
    return out

# COMMAND ----------
# MAGIC %md ## スコア＋理由コード＋判定ポリシーを Delta へ書き出し

# COMMAND ----------

import pandas as pd
scored = pdf.iloc[idx_te].copy()
scored["fraud_score"] = proba
scored["decision_policy"] = np.where(proba >= BLOCK_TH, "BLOCK_CANDIDATE",
                             np.where(proba >= REVIEW_TH, "REVIEW", "AUTO_APPROVE"))
scored["reason_codes"] = [json.dumps(reason_codes(sv[i], Xte[i]), ensure_ascii=False)
                          for i in range(len(Xte))]
scored["review_status"] = "PENDING"
# 再実行時: 02 PART B で付けた列マスク・行フィルタを外してから上書きする（上書き後に deploy が付け直す）
if spark.catalog.tableExists(f"{SCHEMA}.scored_transactions"):
    for ddl in (f"ALTER TABLE {SCHEMA}.scored_transactions DROP ROW FILTER",
                f"ALTER TABLE {SCHEMA}.scored_transactions ALTER COLUMN customer_id DROP MASK"):
        try:
            spark.sql(ddl)
        except Exception as e:
            print(f"skip: {ddl}: {e}")
(spark.createDataFrame(scored)
      .write.mode("overwrite").option("overwriteSchema", "true")
      .saveAsTable(f"{SCHEMA}.scored_transactions"))
# 運用ストア(Lakebase)へ同期する元テーブル: 判断が必要な取引（レビュー帯＋ブロック候補）だけ。
# アナリスト向けの scored_transactions には列マスク・行フィルタを掛け、App は Lakebase 経由で読む。
spark.sql(f"""CREATE OR REPLACE TABLE {SCHEMA}.scored_transactions_ops
              TBLPROPERTIES (delta.enableChangeDataFeed = true)
              COMMENT 'Lakebase 同期元: REVIEW/BLOCK_CANDIDATE の取引（App のレビューキュー）'
              AS SELECT * FROM {SCHEMA}.scored_transactions
              WHERE decision_policy IN ('REVIEW', 'BLOCK_CANDIDATE')""")
# Lakebase synced table(TRIGGERED/CONTINUOUS) の前提となる Change Data Feed を有効化
spark.sql(f"ALTER TABLE {SCHEMA}.scored_transactions "
          "SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
print(f"scored rows written: {len(scored):,}")
policy = spark.sql(f"""SELECT decision_policy, COUNT(*) n, ROUND(AVG(fraud_score),3) avg_score
                      FROM {SCHEMA}.scored_transactions GROUP BY 1 ORDER BY 3 DESC""")
display(policy)

# COMMAND ----------
# MAGIC %md ## 実行証跡（Job の出力として返し、リポジトリの evidence/ にコミット）

# COMMAND ----------

example = scored.sort_values("fraud_score", ascending=False).iloc[0]
dbutils.notebook.exit(json.dumps({
    "mlflow_run_id": run.info.run_id,
    "registered_model": f"{SCHEMA}.fraud_gbdt",
    "model_version": getattr(mi, "registered_model_version", None),
    "train_rows": int(len(ytr)), "test_rows": int(len(yte)),
    "test_fraud_rate": round(float(yte.mean()), 4),
    "roc_auc": round(float(auc), 4),
    "precision@0.6": round(float(p), 4), "recall@0.6": round(float(r), 4),
    "shap_additivity_max_abs_err": additivity_err,
    "policy_counts": {row["decision_policy"]: int(row["n"]) for row in policy.collect()},
    "top_example": {"txn_id": example["txn_id"], "fraud_score": round(float(example["fraud_score"]), 4),
                    "reason_codes": json.loads(example["reason_codes"])},
}, ensure_ascii=False))
