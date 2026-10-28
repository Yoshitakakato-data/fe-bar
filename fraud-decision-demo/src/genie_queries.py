"""Genie Room 相当：業務ユーザの自然言語質問 → SQL → 実データ回答。

Genie Space では業務ユーザが日本語で質問し、Genieが gold/運用テーブルに対する
SQLを生成して回答する。ここではその「代表質問→SQL→実行結果」を再現し、
実データに対する回答をテキストで outputs/genie_query_results.txt にコミットする。

Databricks版の Genie Space 設定は databricks/05_genie_space_setup.md を参照。
同じ質問文・同じテーブルで動くよう列名を一致させてある。
"""
from __future__ import annotations
import os
import sqlite3

from common import LAKEBASE_DB, OUT_DIR

# (業務ユーザの自然言語質問, 対応SQL)
QUESTIONS = [
    ("先週レビュー対象になった取引のうち、判定ポリシー別の件数と平均不正スコアは？",
     """SELECT decision_policy,
               COUNT(*)                AS 件数,
               ROUND(AVG(fraud_score),3) AS 平均スコア,
               ROUND(SUM(amount),0)      AS 合計金額
        FROM fraud_alerts
        GROUP BY decision_policy
        ORDER BY 平均スコア DESC;"""),

    ("高リスク国宛でブロック候補になっている送金トップ5を金額順で見せて",
     """SELECT txn_id, customer_id, payee_country, ROUND(amount,0) AS 金額,
               ROUND(fraud_score,3) AS スコア
        FROM fraud_alerts
        WHERE is_high_risk_country = 1 AND decision_policy = 'BLOCK_CANDIDATE'
        ORDER BY amount DESC
        LIMIT 5;"""),

    ("誤検知が多いのはどのセグメント？ 海外送金か国内かで、レビュー帯の的中率(precision)を比較して",
     """SELECT CASE WHEN is_foreign=1 THEN '海外' ELSE '国内' END AS セグメント,
               COUNT(*)                                   AS レビュー件数,
               SUM(is_fraud)                              AS 実不正,
               ROUND(1.0*SUM(is_fraud)/COUNT(*),3)        AS 的中率
        FROM fraud_alerts
        WHERE fraud_score >= 0.10
        GROUP BY セグメント
        ORDER BY 的中率 DESC;"""),

    ("時間帯別の不正発生件数は？ 深夜に偏っている？",
     """SELECT CASE WHEN hour_of_day BETWEEN 0 AND 5 THEN '深夜(0-5時)'
                    WHEN hour_of_day BETWEEN 6 AND 11 THEN '午前(6-11時)'
                    WHEN hour_of_day BETWEEN 12 AND 17 THEN '午後(12-17時)'
                    ELSE '夜(18-23時)' END              AS 時間帯,
               COUNT(*)      AS 取引件数,
               SUM(is_fraud) AS 不正件数
        FROM fraud_alerts
        GROUP BY 時間帯
        ORDER BY 不正件数 DESC;"""),

    ("端末変更ありの取引は、なしと比べて不正率が何倍高い？",
     """SELECT CASE WHEN device_change=1 THEN '端末変更あり' ELSE '端末変更なし' END AS 区分,
               COUNT(*)                             AS 件数,
               ROUND(100.0*SUM(is_fraud)/COUNT(*),2) AS 不正率pct
        FROM fraud_alerts
        GROUP BY 区分
        ORDER BY 不正率pct DESC;"""),

    ("いまPENDING(未判定)のブロック候補は何件あって、合計いくら止められる可能性がある？",
     """SELECT COUNT(*) AS 未判定ブロック候補,
               ROUND(SUM(amount),0) AS 保全対象金額
        FROM fraud_alerts
        WHERE decision_policy='BLOCK_CANDIDATE' AND review_status='PENDING';"""),
]


def _fmt_table(cols, rows):
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
              for i, c in enumerate(cols)]
    line = "  " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols))
    sep = "  " + "-+-".join("-" * widths[i] for i in range(len(cols)))
    out = [line, sep]
    for r in rows:
        out.append("  " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(len(cols))))
    return "\n".join(out)


def main():
    con = sqlite3.connect(LAKEBASE_DB)
    L = []
    L.append("Genie Room 相当 — 自然言語質問に対する実データ回答")
    L.append("=" * 72)
    for i, (q, sql) in enumerate(QUESTIONS, 1):
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        L.append("")
        L.append(f"Q{i}. {q}")
        L.append("  [生成SQL]")
        for ln in sql.strip().splitlines():
            L.append("    " + ln.strip())
        L.append("  [回答]")
        L.append(_fmt_table(cols, rows))
    con.close()
    with open(os.path.join(OUT_DIR, "genie_query_results.txt"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
