"""⑥ Databricks Apps — 不正送金レビュー支援アプリ（Flask + Lakebase/Postgres）。

Databricks Apps 上で動く本番相当のアプリ。Lakebase(Postgres)に接続し、
レビューキュー・理由コード・EV意思決定支援・判断の記録を1画面で提供する。

UI/意思決定ロジック/EV計算は app/local_app.py（stdlib版）と同一。
ローカルは sqlite、本番は Lakebase(Postgres) という接続先だけが違う。

デプロイ:
  databricks apps deploy fraud-review --source-code-path <workspace上のapp/>
接続: App に Lakebase の postgres リソースを付与すると PGHOST/PGDATABASE/PGUSER/PGPORT が
注入される。パスワードは App のサービスプリンシパルの OAuth 資格情報（databricks-sdk で都度発行）。
起動時に自己チェック（同期件数の読取と app_selfcheck への書込）を行い、Lakebase 上に記録する。
"""
from __future__ import annotations
import json
import os
import uuid
from flask import Flask, request, redirect, jsonify

app = Flask(__name__)

FALSE_POSITIVE_COST_JPY = 3200
BLOCK_THRESHOLD = 0.60


LAKEBASE_ENDPOINT = os.environ.get("LAKEBASE_ENDPOINT", "")   # Lakebase Autoscaling
LAKEBASE_INSTANCE = os.environ.get("LAKEBASE_INSTANCE", "")   # Lakebase Provisioned


def _password():
    # ローカル検証用に PGPASSWORD があればそれを使い、App 上では SP の OAuth 資格情報を発行する
    if os.environ.get("PGPASSWORD"):
        return os.environ["PGPASSWORD"]
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    if LAKEBASE_ENDPOINT:
        return w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT).token
    if LAKEBASE_INSTANCE:
        return w.database.generate_database_credential(
            request_id=str(uuid.uuid4()), instance_names=[LAKEBASE_INSTANCE]).token
    return w.config.oauth_token().access_token


def get_conn():
    import psycopg
    return psycopg.connect(
        host=os.environ["PGHOST"], dbname=os.environ.get("PGDATABASE", "databricks_postgres"),
        user=os.environ["PGUSER"], password=_password(),
        port=int(os.environ.get("PGPORT", 5432)), sslmode="require",
        options=f"-c search_path={os.environ.get('PGSCHEMA', 'fraud_ops')},public")


def query(sql, args=(), fetch=True):
    with get_conn() as con, con.cursor(row_factory=_dict_row()) as cur:
        cur.execute(sql, args)
        return cur.fetchall() if fetch else None


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


def expected_value_of_block(score, amount):
    return score * amount - (1 - score) * FALSE_POSITIVE_COST_JPY


# 判断テーブル（運用・書込）。synced table は読み取り専用なので別テーブルで持つ
DDL = """
CREATE TABLE IF NOT EXISTS review_decisions (
  txn_id        text PRIMARY KEY,
  review_status text NOT NULL DEFAULT 'PENDING',
  reviewed_by   text,
  review_note   text,
  decided_at    timestamptz
);
"""


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
    try:
        m = query("SELECT memo, model_endpoint FROM action_memos WHERE txn_id=%s", (tid,))
    except Exception:
        m = []
    memo = (f"<h3>対応メモ（GenAI・理由コードとEVのみを根拠に生成）</h3>"
            f"<blockquote>{m[0]['memo']}</blockquote><small>{m[0]['model_endpoint']}</small>") if m else ""
    return (f"<a href='/'>← 戻る</a><h2>{tid}</h2>"
            f"<p>スコア {r['fraud_score']:.3f}／金額 {r['amount']:,.0f}円／宛先 {r['payee_country']}</p>"
            f"<p>ブロック期待価値 EV = <b>{ev:,.0f}円</b>（{suggest}）</p>"
            f"<h3>なぜ止めるべきか（理由コード）</h3><ul>{reasons}</ul>{memo}"
            f"<a href='/decide?id={tid}&s=BLOCKED'>🔴 ブロック</a> &nbsp; "
            f"<a href='/decide?id={tid}&s=APPROVED'>🟢 承認</a>")


@app.route("/health")
def health():
    """デプロイ検証用: Lakebase 上の同期件数・判断件数を返す（evidence/ に記録）"""
    c = query("SELECT COUNT(*) AS n FROM fraud_alerts")[0]["n"]
    by = query("SELECT decision_policy, COUNT(*) AS n FROM fraud_alerts GROUP BY 1 ORDER BY 1")
    d = query("SELECT review_status, COUNT(*) AS n FROM review_decisions GROUP BY 1 ORDER BY 1")
    return jsonify({"lakebase_fraud_alerts_rows": c, "by_policy": by, "review_decisions": d})


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


SELFCHECK_DDL = """
CREATE TABLE IF NOT EXISTS app_selfcheck (
  checked_at       timestamptz PRIMARY KEY DEFAULT now(),
  app_user         text,
  fraud_alerts_n   bigint,
  action_memos_n   bigint,
  pending_queue_n  bigint
);
"""


def selfcheck():
    """起動時の疎通確認: 同期テーブルを読み、結果を Lakebase に書く（読み・書きの両経路を検証）"""
    query(DDL, fetch=False)
    query(SELFCHECK_DDL, fetch=False)
    n = query("SELECT COUNT(*) AS n FROM fraud_alerts")[0]["n"]
    try:
        m = query("SELECT COUNT(*) AS n FROM action_memos")[0]["n"]
    except Exception:
        m = None
    q = len(query(QUEUE_SQL))
    query("INSERT INTO app_selfcheck(app_user, fraud_alerts_n, action_memos_n, pending_queue_n) "
          "VALUES (current_user, %s, %s, %s)", (n, m, q), fetch=False)
    print(f"selfcheck ok: fraud_alerts={n} action_memos={m} queue={q}")


try:
    selfcheck()
except Exception as e:  # 起動時に Lakebase 未準備でも App 自体は立ち上げる
    print(f"selfcheck failed: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
