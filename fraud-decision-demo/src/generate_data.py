"""合成の送金トランザクション生データを生成する（公開データではなく完全な合成データ）。

実在の顧客・PIIは一切含まない。銀行の海外/国内送金を模し、
不正取引は「平常と異なるシグナル」を確率的に強めることで生成する。
モデルが自明に解けないよう、正常取引にも高リスク挙動を混ぜ、誤検知が
発生する現実的な難易度にしてある（＝解釈性とビジネス判断の価値が出る）。

出力: data/raw_transactions.csv （Lakeflowが取り込む「生データ」相当）
"""
from __future__ import annotations
import csv
import math
import os
import numpy as np

from common import DATA_DIR, SEED

N_CUSTOMERS = 4000
N_TXN = 60000
DAYS = 90
HIGH_RISK_COUNTRIES = {"XA", "XB", "XC"}  # 合成の高リスク国コード
FOREIGN_COUNTRIES = ["US", "GB", "SG", "HK", "AU", "XA", "XB", "XC"]


def main() -> None:
    rng = np.random.default_rng(SEED)

    # 顧客ごとの平常プロファイル
    cust_avg_amount = rng.lognormal(mean=10.2, sigma=0.6, size=N_CUSTOMERS)  # 平均送金額(円)
    cust_account_age = rng.integers(30, 3650, size=N_CUSTOMERS)
    cust_home_device = rng.integers(0, 1_000_000, size=N_CUSTOMERS)

    rows = []
    base_ts = 1_700_000_000  # 固定の基準エポック秒（再現性）
    last_txn_ts = {}

    for i in range(N_TXN):
        c = int(rng.integers(0, N_CUSTOMERS))
        # 取引の発生時刻（90日に分散）
        ts = base_ts + int(rng.uniform(0, DAYS * 86400))
        hour = (ts // 3600) % 24

        # このイベントが不正かどうか（ベースレート ~2.2%）
        is_fraud = 1 if rng.random() < 0.022 else 0

        avg = cust_avg_amount[c]
        if is_fraud:
            # 不正の3割は「ステルス型」= 正常に紛れる（自明に検知させない -> recallに難易度）
            stealth = rng.random() < 0.30
            if stealth:
                ratio = rng.lognormal(mean=0.3, sigma=0.6)
                is_new_payee = 1 if rng.random() < 0.3 else 0
                country = (rng.choice(FOREIGN_COUNTRIES) if rng.random() < 0.3 else "JP")
                hour = int(rng.choice(range(24), p=_day_biased()))
                device_change = 1 if rng.random() < 0.2 else 0
                num_txn_24h = int(rng.integers(1, 5))
            else:
                # 典型的な不正：高額化・海外/高リスク・新規送金先・深夜・端末変更・高頻度
                ratio = rng.lognormal(mean=1.2, sigma=0.7)
                is_new_payee = 1 if rng.random() < 0.7 else 0
                country = (rng.choice(FOREIGN_COUNTRIES, p=[0.05, 0.05, 0.05, 0.05, 0.05, 0.25, 0.25, 0.25])
                           if rng.random() < 0.6 else "JP")
                hour = int(rng.choice(range(24), p=_night_biased()))
                device_change = 1 if rng.random() < 0.6 else 0
                num_txn_24h = int(rng.integers(2, 12))
            amount = max(1000.0, avg * ratio)
        else:
            # 正常：1割強は「疑わしいが正当」= 誤検知の源泉（高額/海外/新規/高頻度）
            ratio = rng.lognormal(mean=0.0, sigma=0.5)
            suspicious = rng.random() < 0.12
            if suspicious:
                ratio *= rng.uniform(3, 9)     # 旅行・大型購入・不動産頭金など
                is_new_payee = 1 if rng.random() < 0.5 else 0
                country = (rng.choice(FOREIGN_COUNTRIES) if rng.random() < 0.4 else "JP")
                num_txn_24h = int(rng.integers(3, 10))
                device_change = 1 if rng.random() < 0.15 else 0
                hour = int(rng.choice(range(24), p=_night_biased() if rng.random() < 0.3 else _day_biased()))
            else:
                is_new_payee = 1 if rng.random() < 0.15 else 0
                country = (rng.choice(FOREIGN_COUNTRIES) if rng.random() < 0.06 else "JP")
                num_txn_24h = int(rng.integers(1, 6))
                device_change = 1 if rng.random() < 0.04 else 0
                hour = int(rng.choice(range(24), p=_day_biased()))
            amount = max(500.0, avg * ratio)

        is_foreign = 0 if country == "JP" else 1
        is_high_risk = 1 if country in HIGH_RISK_COUNTRIES else 0

        prev = last_txn_ts.get(c)
        sec_since_last = (ts - prev) if prev is not None else int(rng.integers(3600, 5 * 86400))
        sec_since_last = max(1, sec_since_last)
        last_txn_ts[c] = ts

        channel = rng.choice(["mobile", "web", "branch"], p=[0.6, 0.35, 0.05])

        rows.append({
            "txn_id": f"T{i:07d}",
            "customer_id": f"C{c:06d}",
            "event_ts": ts,
            "hour_of_day": hour,
            "amount": round(float(amount), 2),
            "customer_avg_amount_30d": round(float(avg), 2),
            "is_new_payee": int(is_new_payee),
            "payee_country": str(country),
            "channel": str(channel),
            "device_change": int(device_change),
            "num_txn_24h": int(num_txn_24h),
            "account_age_days": int(cust_account_age[c]),
            "sec_since_last_txn": int(sec_since_last),
            "is_fraud": int(is_fraud),  # ラベル（教師データ）
        })

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "raw_transactions.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_fraud = sum(r["is_fraud"] for r in rows)
    print(f"[generate_data] rows={len(rows):,}  customers={N_CUSTOMERS:,}  days={DAYS}")
    print(f"[generate_data] fraud={n_fraud:,} ({100*n_fraud/len(rows):.2f}%)  -> {path}")


def _night_biased():
    # 深夜(0-5時)に偏る分布
    w = np.array([3, 3, 3, 3, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 3, 3], dtype=float)
    return w / w.sum()


def _day_biased():
    # 日中(9-21時)に偏る分布
    w = np.array([1, 1, 1, 1, 1, 1, 2, 3, 4, 5, 5, 5, 5, 5, 5, 5, 5, 5, 4, 4, 3, 2, 1, 1], dtype=float)
    return w / w.sum()


if __name__ == "__main__":
    main()
