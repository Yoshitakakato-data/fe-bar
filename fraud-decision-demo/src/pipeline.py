"""Lakeflow(宣言的パイプライン)のローカル等価物：bronze -> silver -> gold。

- bronze: 生データをそのまま（型付けのみ）
- silver: クレンジング＋派生特徴量（業務的に意味のある指標に変換）
- gold  : MLと運用サービングの両方が読む「特徴量テーブル」

Databricks版(databricks/01_lakeflow_ingest.py)は同じ変換を Lakeflow Declarative
Pipeline (旧DLT) の @dlt.table として表現する。列名・ロジックは一致させている。

出力: data/gold_features.csv
"""
from __future__ import annotations
import csv
import math
import os
import numpy as np

from common import DATA_DIR, FEATURES

HIGH_RISK_COUNTRIES = {"XA", "XB", "XC"}


def _hour_risk(hour: int) -> float:
    # 深夜0-5時を高リスク(1.0)、日中を低リスク(0.1)にマップ
    night = {0: 1.0, 1: 1.0, 2: 1.0, 3: 0.9, 4: 0.8, 5: 0.6, 23: 0.7, 22: 0.5}
    return night.get(int(hour), 0.1)


def run() -> str:
    raw_path = os.path.join(DATA_DIR, "raw_transactions.csv")
    with open(raw_path) as f:
        rows = list(csv.DictReader(f))
    print(f"[pipeline] bronze rows read = {len(rows):,}")

    gold = []
    dropped = 0
    for r in rows:
        amount = float(r["amount"])
        avg = float(r["customer_avg_amount_30d"])
        if amount <= 0 or avg <= 0:      # silver: 不正な値をドロップ
            dropped += 1
            continue
        country = r["payee_country"]
        feat = {
            "txn_id": r["txn_id"],
            "customer_id": r["customer_id"],
            "event_ts": int(r["event_ts"]),
            "hour_of_day": int(r["hour_of_day"]),
            "amount": amount,
            "payee_country": country,
            "channel": r["channel"],
            # --- 派生特徴量（gold）---
            "amount_to_avg_ratio": round(amount / avg, 4),
            "amount_log": round(math.log10(amount + 1.0), 4),
            "is_new_payee": int(r["is_new_payee"]),
            "is_foreign": 0 if country == "JP" else 1,
            "is_high_risk_country": 1 if country in HIGH_RISK_COUNTRIES else 0,
            "hour_risk": _hour_risk(int(r["hour_of_day"])),
            "num_txn_24h": int(r["num_txn_24h"]),
            "device_change": int(r["device_change"]),
            "account_age_days": int(r["account_age_days"]),
            "sec_since_last_txn": round(math.log10(int(r["sec_since_last_txn"]) + 1.0), 4),
            "is_fraud": int(r["is_fraud"]),
        }
        gold.append(feat)

    out = os.path.join(DATA_DIR, "gold_features.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(gold[0].keys()))
        w.writeheader()
        w.writerows(gold)

    print(f"[pipeline] silver dropped (invalid) = {dropped}")
    print(f"[pipeline] gold rows written = {len(gold):,}  features={len(FEATURES)}  -> {out}")
    return out


def load_gold():
    """gold特徴量を numpy 行列 X, ラベル y, メタ情報 rows として返す。"""
    out = os.path.join(DATA_DIR, "gold_features.csv")
    with open(out) as f:
        rows = list(csv.DictReader(f))
    X = np.array([[float(r[k]) for k in FEATURES] for r in rows], dtype=float)
    y = np.array([int(r["is_fraud"]) for r in rows], dtype=int)
    return X, y, rows


if __name__ == "__main__":
    run()
