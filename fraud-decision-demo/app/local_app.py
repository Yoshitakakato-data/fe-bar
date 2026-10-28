"""不正送金レビュー支援アプリ（Databricks Apps 相当のローカル版, stdlib http.server）。

業務ユーザ(不正調査アナリスト)の1画面:
  - レビュー待ちキュー（スコア降順）
  - 各取引の「なぜ止めるべきか」= 理由コード（SHAP相当の加法的寄与）
  - 期待値ベースの意思決定支援（EV = score*金額 - (1-score)*誤検知コスト）
  - 「ブロック / 承認」判断 -> 運用ストア(Lakebase相当sqlite)を更新
  - 上部にビジネスKPIサマリ

起動:  python3 app/local_app.py            -> http://127.0.0.1:8000
証跡:  python3 app/local_app.py --demo     -> outputs/app_demo_output.txt を生成

Databricks版は app/app.py（Flask + databricks-sql-connector, Lakebaseに接続）。
UI構造・意思決定ロジック・EV計算は同一。
"""
from __future__ import annotations
import html
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
from common import LAKEBASE_DB, OUT_DIR, FALSE_POSITIVE_COST_JPY, BLOCK_THRESHOLD  # noqa: E402


def _con():
    con = sqlite3.connect(LAKEBASE_DB)
    con.row_factory = sqlite3.Row
    return con


# ---------------- データ/ロジック層（http・demoで共有） ----------------
def get_queue(limit=25):
    con = _con()
    rows = con.execute(
        """SELECT * FROM fraud_alerts
           WHERE decision_policy IN ('BLOCK_CANDIDATE','REVIEW') AND review_status='PENDING'
           ORDER BY fraud_score DESC LIMIT ?""", (limit,)).fetchall()
    con.close()
    return rows


def get_detail(txn_id):
    con = _con()
    r = con.execute("SELECT * FROM fraud_alerts WHERE txn_id=?", (txn_id,)).fetchone()
    con.close()
    return r


def expected_value_of_block(score, amount):
    """ブロックの期待金額価値。正なら「止める方が得」を示す意思決定支援指標。"""
    return score * amount - (1 - score) * FALSE_POSITIVE_COST_JPY


def decide(txn_id, status, who="analyst"):
    con = _con()
    con.execute(
        "UPDATE fraud_alerts SET review_status=?, reviewed_by=?, decided_at=? WHERE txn_id=?",
        (status, who, int(time.time()), txn_id))
    con.commit()
    con.close()


def kpi_summary():
    path = os.path.join(OUT_DIR, "business_impact.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def queue_stats():
    con = _con()
    s = con.execute(
        """SELECT
             SUM(CASE WHEN review_status='PENDING' AND decision_policy IN ('BLOCK_CANDIDATE','REVIEW') THEN 1 ELSE 0 END) pending,
             SUM(CASE WHEN review_status='BLOCKED' THEN 1 ELSE 0 END) blocked,
             SUM(CASE WHEN review_status='APPROVED' THEN 1 ELSE 0 END) approved
           FROM fraud_alerts""").fetchone()
    con.close()
    return s


# ---------------- HTML描画 ----------------
CSS = """
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1116;color:#e6e8ee}
 header{background:#161a22;padding:16px 24px;border-bottom:1px solid #262c38}
 h1{font-size:18px;margin:0}
 .wrap{padding:20px 24px;max-width:1100px;margin:0 auto}
 .kpi{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0 24px}
 .card{background:#161a22;border:1px solid #262c38;border-radius:10px;padding:14px 16px;min-width:150px}
 .card .v{font-size:22px;font-weight:700;color:#5eead4}
 .card .l{font-size:12px;color:#93a0b5;margin-top:4px}
 table{border-collapse:collapse;width:100%;background:#12151c;border-radius:8px;overflow:hidden}
 th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #222834;font-size:13px}
 th{background:#1b2029;color:#93a0b5}
 .score{font-weight:700}
 .hi{color:#f87171}.mid{color:#fbbf24}
 a{color:#7dd3fc;text-decoration:none}
 .reason{background:#12151c;border:1px solid #262c38;border-radius:8px;padding:12px 16px;margin:8px 0}
 .bar{height:10px;background:#f87171;border-radius:3px;display:inline-block;vertical-align:middle}
 .btn{display:inline-block;padding:8px 18px;border-radius:8px;font-weight:600;margin-right:10px}
 .block{background:#7f1d1d;color:#fecaca}.approve{background:#14532d;color:#bbf7d0}
 .ev-pos{color:#f87171;font-weight:700}.ev-neg{color:#4ade80;font-weight:700}
 .pill{padding:2px 8px;border-radius:20px;font-size:11px;background:#1b2029;color:#93a0b5}
</style>"""


def _score_cls(s):
    return "hi" if s >= BLOCK_THRESHOLD else "mid"


def render_home():
    k = kpi_summary()
    st = queue_stats()
    kpis = ""
    if k:
        d = k["delta_annualized"]
        p = k["proposed"]
        kpis = f"""
        <div class="kpi">
          <div class="card"><div class="v">{d['net_impact_gain_jpy']/1e8:.2f}億円</div><div class="l">ネット改善額 / 年</div></div>
          <div class="card"><div class="v">{d['review_hours_saved']:,.0f}h</div><div class="l">調査時間削減 / 年</div></div>
          <div class="card"><div class="v">{d['friction_count_reduced']:,.0f}件</div><div class="l">誤検知(顧客摩擦)削減 / 年</div></div>
          <div class="card"><div class="v">{p['standardization_rate']*100:.1f}%</div><div class="l">判断標準化率</div></div>
        </div>"""
    rows = get_queue()
    trs = []
    for r in rows:
        s = r["fraud_score"]
        codes = json.loads(r["reason_codes"] or "[]")
        top = codes[0]["label"] if codes else "-"
        ev = expected_value_of_block(s, r["amount"])
        trs.append(
            f"<tr><td><a href='/txn?id={r['txn_id']}'>{r['txn_id']}</a></td>"
            f"<td>{r['customer_id']}</td>"
            f"<td class='score {_score_cls(s)}'>{s:.3f}</td>"
            f"<td>{r['amount']:,.0f}円</td>"
            f"<td>{html.escape(r['payee_country'])}</td>"
            f"<td>{html.escape(top)}</td>"
            f"<td class='{'ev-pos' if ev>0 else 'ev-neg'}'>{ev:,.0f}円</td>"
            f"<td><span class='pill'>{r['decision_policy']}</span></td></tr>")
    return f"""<!doctype html><html><head><meta charset=utf-8><title>不正送金レビュー</title>{CSS}</head>
    <body><header><h1>🛡️ 不正送金レビュー支援 — アナリスト・コックピット</h1></header>
    <div class="wrap">{kpis}
      <p>レビュー待ち <b>{st['pending'] or 0}</b> 件 ／ 本日ブロック {st['blocked'] or 0} ・承認 {st['approved'] or 0}
      　<span class="pill">スコア≥{BLOCK_THRESHOLD} は要ブロック検討</span></p>
      <table><tr><th>取引ID</th><th>顧客</th><th>スコア</th><th>金額</th><th>宛先</th>
      <th>主な理由</th><th>ブロック期待価値(EV)</th><th>ポリシー</th></tr>
      {''.join(trs)}</table>
    </div></body></html>"""


def render_txn(txn_id):
    r = get_detail(txn_id)
    if not r:
        return "<h1>not found</h1>"
    s = r["fraud_score"]
    codes = json.loads(r["reason_codes"] or "[]")
    ev = expected_value_of_block(s, r["amount"])
    max_c = max([c["contribution"] for c in codes], default=1) or 1
    reason_html = ""
    for c in codes:
        w = int(220 * c["contribution"] / max_c)
        reason_html += (f"<div class='reason'>{html.escape(c['label'])}"
                        f"　<span class='bar' style='width:{w}px'></span>"
                        f"　<b>+{c['contribution']:.3f}</b>"
                        f"<span class='pill' style='margin-left:8px'>{c['feature']}={c['value']}</span></div>")
    suggest = "🔴 ブロック推奨" if ev > 0 else "🟢 承認寄り（誤検知コストが上回る）"
    label = "不正" if int(r["is_fraud"]) else "正常"
    return f"""<!doctype html><html><head><meta charset=utf-8><title>{txn_id}</title>{CSS}</head>
    <body><header><h1>取引レビュー {txn_id}</h1></header>
    <div class="wrap">
      <p><a href="/">← キューに戻る</a></p>
      <div class="kpi">
        <div class="card"><div class="v {_score_cls(s)}">{s:.3f}</div><div class="l">不正スコア</div></div>
        <div class="card"><div class="v">{r['amount']:,.0f}円</div><div class="l">送金額 (平常比 {r['amount_to_avg_ratio']}x)</div></div>
        <div class="card"><div class="v {'ev-pos' if ev>0 else 'ev-neg'}">{ev:,.0f}円</div><div class="l">ブロック期待価値(EV)</div></div>
        <div class="card"><div class="v">{suggest}</div><div class="l">意思決定支援</div></div>
      </div>
      <p>顧客 <b>{r['customer_id']}</b>／宛先 <b>{html.escape(r['payee_country'])}</b>／チャネル {r['channel']}
         ／{r['hour_of_day']}時／直近24h取引 {r['num_txn_24h']}件
         ／端末変更 {'あり' if r['device_change'] else 'なし'}
         　<span class="pill">合成データ実ラベル: {label}</span></p>
      <h3>なぜ止めるべきか（理由コード / 加法的寄与）</h3>
      {reason_html or '<p>理由コードなし</p>'}
      <h3>判断</h3>
      <a class="btn block" href="/decide?id={txn_id}&s=BLOCKED">🔴 送金をブロック</a>
      <a class="btn approve" href="/decide?id={txn_id}&s=APPROVED">🟢 送金を承認</a>
      <p style="color:#93a0b5;font-size:12px;margin-top:14px">判断は運用ストア(Lakebase相当)に記録され、標準化された理由とともに監査可能になります。</p>
    </div></body></html>"""


# ---------------- HTTPサーバ ----------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(render_home())
        elif u.path == "/txn":
            self._send(render_txn(q.get("id", [""])[0]))
        elif u.path == "/decide":
            decide(q.get("id", [""])[0], q.get("s", ["PENDING"])[0])
            self.send_response(302); self.send_header("Location", "/"); self.end_headers()
        else:
            self._send("<h1>404</h1>", 404)

    def log_message(self, *a):
        pass


def run_server(port=8000):
    print(f"[app] http://127.0.0.1:{port}  (Ctrl+C to stop)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ---------------- ヘッドレスdemo（テキスト証跡） ----------------
def demo():
    L = []
    L.append("不正送金レビュー支援アプリ — ヘッドレス実行ログ（App動作の証跡）")
    L.append("=" * 72)
    st = queue_stats()
    L.append(f"レビュー待ちキュー: PENDING={st['pending']}  BLOCKED={st['blocked']}  APPROVED={st['approved']}")
    L.append("")
    L.append("[1] キュー上位10件（スコア降順）＋ ブロック期待価値EV")
    L.append(f"    {'txn_id':<10}{'score':>7}{'amount':>12}{'EV(円)':>14}  主な理由")
    for r in get_queue(10):
        s = r["fraud_score"]; ev = expected_value_of_block(s, r["amount"])
        codes = json.loads(r["reason_codes"] or "[]")
        top = codes[0]["label"] if codes else "-"
        L.append(f"    {r['txn_id']:<10}{s:>7.3f}{r['amount']:>12,.0f}{ev:>14,.0f}  {top}")

    # 詳細＋判断のデモ（先頭の取引をブロック）
    top_row = get_queue(1)[0]
    tid = top_row["txn_id"]
    L.append("")
    L.append(f"[2] 取引詳細レビュー: {tid}")
    codes = json.loads(top_row["reason_codes"] or "[]")
    s = top_row["fraud_score"]; ev = expected_value_of_block(s, top_row["amount"])
    L.append(f"    スコア={s:.3f}  金額={top_row['amount']:,.0f}円  宛先={top_row['payee_country']}  "
             f"EV={ev:,.0f}円  ({'ブロック推奨' if ev>0 else '承認寄り'})")
    L.append(f"    理由コード:")
    for c in codes:
        L.append(f"      +{c['contribution']:.3f}  {c['label']}  ({c['feature']}={c['value']})")
    L.append("")
    L.append(f"[3] アナリスト判断: {tid} をブロック -> 運用ストア更新")
    decide(tid, "BLOCKED", "demo_analyst")
    r2 = get_detail(tid)
    L.append(f"    確認: review_status={r2['review_status']} reviewed_by={r2['reviewed_by']} decided_at={r2['decided_at']}")
    st2 = queue_stats()
    L.append(f"    キュー更新後: PENDING={st2['pending']}  BLOCKED={st2['blocked']}")
    # デモ後に元に戻す（再実行の冪等性のため）
    decide(tid, "PENDING", None)
    con = _con(); con.execute("UPDATE fraud_alerts SET reviewed_by=NULL, decided_at=NULL WHERE txn_id=?", (tid,)); con.commit(); con.close()

    out = os.path.join(OUT_DIR, "app_demo_output.txt")
    with open(out, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\n[app] demo output -> {out}")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        run_server()
