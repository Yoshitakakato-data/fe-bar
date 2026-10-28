# Databricks notebook source
# MAGIC %md
# MAGIC # ① Lakeflow — 生データ取り込み（宣言的パイプライン: bronze → silver → gold）
# MAGIC
# MAGIC Lakeflow Declarative Pipelines（旧 Delta Live Tables）で、合成の送金トランザクション生データを
# MAGIC 取り込み、クレンジングし、ML＋運用サービングが読む gold 特徴量テーブルまで一気通貫で構築する。
# MAGIC
# MAGIC **ローカル等価物**: `src/pipeline.py`（列名・派生ロジックを一致させてある）。
# MAGIC
# MAGIC 使い方: このノートブックを Lakeflow パイプライン(旧DLT)のソースに指定し、
# MAGIC ターゲットカタログ/スキーマを `fraud_demo.fraud` に設定して実行する。

# COMMAND ----------

import dlt
from pyspark.sql import functions as F

RAW_PATH = "/Volumes/fraud_demo/fraud/raw/raw_transactions.csv"  # Unity Catalog Volume に配置
HIGH_RISK = ["XA", "XB", "XC"]

# COMMAND ----------
# MAGIC %md ## bronze — 生データをそのまま取り込み（型付けのみ）

@dlt.table(comment="送金トランザクション生データ（合成・PIIなし）")
def bronze_transactions():
    return (
        spark.read.option("header", True).option("inferSchema", True).csv(RAW_PATH)
    )

# COMMAND ----------
# MAGIC %md ## silver — クレンジング＋期待値(データ品質)チェック

@dlt.table(comment="クレンジング済み送金取引")
@dlt.expect_or_drop("valid_amount", "amount > 0")
@dlt.expect_or_drop("valid_avg", "customer_avg_amount_30d > 0")
@dlt.expect("known_channel", "channel in ('mobile','web','branch')")
def silver_transactions():
    return dlt.read("bronze_transactions")

# COMMAND ----------
# MAGIC %md ## gold — 派生特徴量テーブル（MLと運用サービングの単一ソース）

@dlt.table(comment="不正検知の特徴量テーブル（ML学習＋Lakebase同期の元）")
def gold_features():
    df = dlt.read("silver_transactions")
    return df.select(
        "txn_id", "customer_id", "event_ts", "hour_of_day", "amount",
        "payee_country", "channel",
        F.round(F.col("amount") / F.col("customer_avg_amount_30d"), 4).alias("amount_to_avg_ratio"),
        F.round(F.log10(F.col("amount") + F.lit(1.0)), 4).alias("amount_log"),
        F.col("is_new_payee").cast("int"),
        (F.col("payee_country") != F.lit("JP")).cast("int").alias("is_foreign"),
        F.col("payee_country").isin(HIGH_RISK).cast("int").alias("is_high_risk_country"),
        # 時間帯リスク（深夜を高リスクに）
        F.when(F.col("hour_of_day").isin(0, 1, 2), 1.0)
         .when(F.col("hour_of_day") == 3, 0.9).when(F.col("hour_of_day") == 4, 0.8)
         .when(F.col("hour_of_day") == 5, 0.6).when(F.col("hour_of_day") == 23, 0.7)
         .when(F.col("hour_of_day") == 22, 0.5).otherwise(0.1).alias("hour_risk"),
        F.col("num_txn_24h").cast("int"),
        F.col("device_change").cast("int"),
        F.col("account_age_days").cast("int"),
        F.round(F.log10(F.col("sec_since_last_txn") + F.lit(1.0)), 4).alias("sec_since_last_txn"),
        F.col("is_fraud").cast("int"),
    )
