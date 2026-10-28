"""⑥ Databricks Apps — 不正送金レビュー支援アプリ（Flask + Lakebase/Postgres）。

Databricks Apps 上で動く本番相当のアプリ。Lakebase(Postgres)に接続し、
レビューキュー・理由コード・EV意思決定支援・判断の記録を1画面で提供する。

UI/意思決定ロジック/EV計算は app/local_app.py（stdlib版）と同一。
ローカルは sqlite、本番は Lakebase(Postgres) という接続先だけが違う。

デプロイ:
  databricks apps deploy fraud-review --source-code-path .
環境変数(app.yaml): PGHOST/PGDATABASE/PGUSER/PGPASSWORD（Lakebase接続情報）
"""
from __future__ import annotations
import json
import os
from flask import Flask, request, redirect

app = Flask(__name__)

FALSE_POSITIVE_COST_JPY = 3200
BLOCK_THRESHOLD = 0.60


def get_conn():
    import psycopg
    return psycopg.connect(
        host=os.environ["PGHOST"], dbname=os.environ.get("PGDATABASE", "databricks_postgres"),
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], sslmode="require")


def query(sql, args=(), fetch=True):
    with get_conn() as con, con.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, args)
        return cur.fetchall() if fetch else None


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


def expected_value_of_block(score, amount):
    return score * amount - (1 - score) * FALSE_POSITIVE_COST_JPY


# fraud_alerts(同期・読取) と review_decisions(運用・書込) を JOIN
QUEUE_SQL = """
SELECT a.*, COALESCE(d.review_status,'PENDING') AS review_status
FROM fraud_alerts a
LEFT JOIN review_decisions d USING (txn_id)
WHERE a.decision_policy IN ('BLOCK_CANDIDATE','REVIEW')
  AND COALESCE(d.review_status,'PENDING')='PENDING'
ORDER BY a.fraud_score DESC LIMIT 25;
"""


@app.route("/")
def home():
    rows = query(QUEUE_SQL)
    trs = ""
    for r in rows:
        ev = expected_value_of_block(r["fraud_score"], r["amount"])
        codes = json.loads(r["reason_codes"] or "[]")
        top = codes[0]["label"] if codes else "-"
        trs += (f"<tr><td><a href='/txn?id={r['txn_id']}'>{r['txn_id']}</a></td>"
                f"<td>{r['fraud_score']:.3f}</td><td>{r['amount']:,.0f}円</td>"
                f"<td>{top}</td><td>{ev:,.0f}円</td></tr>")
    return (f"<h1>🛡️ 不正送金レビュー支援</h1><table border=1 cellpadding=6>"
            f"<tr><th>取引</th><th>スコア</th><th>金額</th><th>主な理由</th><th>EV</th></tr>"
            f"{trs}</table>")


@app.route("/txn")
def txn():
    tid = request.args.get("id", "")
    r = query("SELECT * FROM fraud_alerts WHERE txn_id=%s", (tid,))[0]
    codes = json.loads(r["reason_codes"] or "[]")
    ev = expected_value_of_block(r["fraud_score"], r["amount"])
    reasons = "".join(f"<li>+{c['contribution']:.3f} {c['label']} ({c['feature']}={c['value']})</li>"
                      for c in codes)
    suggest = "🔴 ブロック推奨" if ev > 0 else "🟢 承認寄り"
    return (f"<a href='/'>← 戻る</a><h2>{tid}</h2>"
            f"<p>スコア {r['fraud_score']:.3f}／金額 {r['amount']:,.0f}円／宛先 {r['payee_country']}</p>"
            f"<p>ブロック期待価値 EV = <b>{ev:,.0f}円</b>（{suggest}）</p>"
            f"<h3>なぜ止めるべきか（理由コード）</h3><ul>{reasons}</ul>"
            f"<a href='/decide?id={tid}&s=BLOCKED'>🔴 ブロック</a> &nbsp; "
            f"<a href='/decide?id={tid}&s=APPROVED'>🟢 承認</a>")


@app.route("/decide")
def decide():
    tid = request.args.get("id", "")
    status = request.args.get("s", "PENDING")
    query("""INSERT INTO review_decisions(txn_id, review_status, reviewed_by, decided_at)
             VALUES (%s,%s,%s,now())
             ON CONFLICT (txn_id) DO UPDATE
             SET review_status=EXCLUDED.review_status, reviewed_by=EXCLUDED.reviewed_by,
                 decided_at=now()""",
          (tid, status, os.environ.get("DATABRICKS_USER", "analyst")), fetch=False)
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
