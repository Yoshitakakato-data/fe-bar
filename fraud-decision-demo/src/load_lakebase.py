"""運用ストア（Lakebase相当）へスコア済み取引を投入する。

Lakebaseは「Lakehouseから低遅延Postgresへ同期される運用DB」。ローカルでは
sqlite3で等価な運用テーブルを作る。アプリ(app/local_app.py)はこのDBを直接読み書きし、
アナリストの最終判断(review_status)を更新する＝Lakebaseの運用ユースケースを再現。

Databricks版では databricks/04_lakebase_sync.md の手順で synced table を作る。

出力: data/lakebase.sqlite （テーブル fraud_alerts）
"""
from __future__ import annotations
import csv
import os
import sqlite3

from common import DATA_DIR, LAKEBASE_DB

DDL = """
DROP TABLE IF EXISTS fraud_alerts;
CREATE TABLE fraud_alerts (
    txn_id               TEXT PRIMARY KEY,
    customer_id          TEXT,
    event_ts             INTEGER,
    hour_of_day          INTEGER,
    amount               REAL,
    payee_country        TEXT,
    channel              TEXT,
    amount_to_avg_ratio  REAL,
    is_new_payee         INTEGER,
    is_foreign           INTEGER,
    is_high_risk_country INTEGER,
    num_txn_24h          INTEGER,
    device_change        INTEGER,
    is_fraud             INTEGER,     -- 実ラベル（合成データの正解。運用では未知だが評価用に保持）
    fraud_score          REAL,
    decision_policy      TEXT,        -- AUTO_APPROVE / REVIEW / BLOCK_CANDIDATE
    reason_codes         TEXT,        -- JSON（理由コード）
    review_status        TEXT DEFAULT 'PENDING',  -- PENDING / BLOCKED / APPROVED
    reviewed_by          TEXT,
    review_note          TEXT,
    decided_at           INTEGER
);
CREATE INDEX idx_score  ON fraud_alerts(fraud_score DESC);
CREATE INDEX idx_policy ON fraud_alerts(decision_policy);
CREATE INDEX idx_status ON fraud_alerts(review_status);
"""


def main():
    con = sqlite3.connect(LAKEBASE_DB)
    con.executescript(DDL)

    with open(os.path.join(DATA_DIR, "scored_transactions.csv")) as f:
        rows = list(csv.DictReader(f))

    cols = ["txn_id", "customer_id", "event_ts", "hour_of_day", "amount", "payee_country",
            "channel", "amount_to_avg_ratio", "is_new_payee", "is_foreign",
            "is_high_risk_country", "num_txn_24h", "device_change", "is_fraud",
            "fraud_score", "decision_policy", "reason_codes"]
    con.executemany(
        f"INSERT INTO fraud_alerts ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        [[r[c] for c in cols] for r in rows],
    )
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM fraud_alerts").fetchone()[0]
    by_policy = con.execute(
        "SELECT decision_policy, COUNT(*) FROM fraud_alerts GROUP BY decision_policy ORDER BY 2 DESC"
    ).fetchall()
    con.close()
    print(f"[lakebase] loaded {n:,} rows -> {LAKEBASE_DB}")
    for p, c in by_policy:
        print(f"[lakebase]   {p:<16} {c:,}")


if __name__ == "__main__":
    main()
