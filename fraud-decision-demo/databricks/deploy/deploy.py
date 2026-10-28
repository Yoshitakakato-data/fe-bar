"""Databricks ネイティブ版を実ワークスペースにデプロイ・実行し、実行証跡を evidence/databricks/ に保存する。

依存: Databricks CLI（認証済みプロファイル）＋ Python 標準ライブラリのみ。
Lakebase(Postgres) 側の SQL は 07_lakebase_sql.py をサーバレス Job として実行する。

使い方:
  python3 databricks/deploy/deploy.py --profile <profile> [--catalog <catalog>] [--steps all]
  --catalog 省略時: `fraud_demo` を作成し、権限がなければ（FEVM 等）ワークスペースの既定カタログを使う。
  Lakebase インスタンス・App・DBカタログ名は共有メタストアでの衝突を避けるためユーザ名を接頭辞にする。
  steps: uc_a,upload,pipeline,train,uc_b,memo,lakebase,app,genie （カンマ区切り。all で全部）

各ステップの API 応答を evidence/databricks/NN_<step>.json に、要約を evidence/databricks/RUN_LOG.md に追記する。
トークン・パスワードは保存しない。
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVID = os.path.join(ROOT, "evidence", "databricks")
PG_SCHEMA = "fraud_ops"   # Lakebase(Postgres) 側のスキーマ＝UC 側の同期テーブルのスキーマ
STEPS = ["uc_a", "upload", "pipeline", "train", "uc_b", "memo", "lakebase", "app", "genie", "verify"]

GENIE_QUESTIONS = [
    "判定ポリシー別の件数と平均不正スコア、合計金額は？",
    "高リスク国宛でブロック候補になっている送金トップ5を金額順で見せて",
    "海外送金と国内で、レビュー帯（スコア0.10以上）の的中率を比較して",
    "時間帯別の不正発生件数は？深夜に偏っている？",
    "端末変更ありの取引は、なしと比べて不正率が何倍高い？",
    "いまPENDINGのブロック候補は何件あって、合計いくら止められる可能性がある？",
]
# 同一会話内のフォローアップ（会話として文脈を引き継げることの証跡）
GENIE_FOLLOWUPS = {2: "その海外送金のうち、高リスク国宛だけに絞ると的中率はどうなる？"}

GENIE_INSTRUCTIONS = """このスペースは銀行の不正送金レビューを支援する。
- fraud_score は不正確率(0-1)。decision_policy は AUTO_APPROVE / REVIEW / BLOCK_CANDIDATE。
- レビュー帯 = fraud_score >= 0.10。ブロック候補 = fraud_score >= 0.60。
- reason_codes は各取引がなぜ高リスクかを説明するJSON（SHAP寄与、logit空間）。
- is_foreign=1 は海外送金、is_high_risk_country=1 は高リスク国宛（XA/XB/XC）。
- 「的中率(precision)」= 対象のうち実際に不正だった割合 = AVG(is_fraud)。
- 金額は円。hour_of_day は0-23時。深夜は0-5時。
- ユーザは不正対策部長（Head of Fraud Operations）配下の調査アナリストと、CRO配下のリスク管理者。"""


class Ctx:
    def __init__(self, a):
        self.profile = a.profile
        self.args = a
        self.state_path = os.path.join(EVID, "state.json")
        self.state = json.load(open(self.state_path)) if os.path.exists(self.state_path) else {}
        self.catalog = a.catalog or self.state.get("catalog")
        self.schema = f"{self.catalog}.fraud" if self.catalog else None
        self.me = None
        self.ws_dir = None

    def save_state(self):
        json.dump(self.state, open(self.state_path, "w"), indent=2, ensure_ascii=False)


# ------------------------------------------------------------------ helpers
def cli(ctx, *args, input_json=None, check=True):
    cmd = ["databricks", *args, "-p", ctx.profile, "-o", "json"]
    if input_json is not None:
        cmd += ["--json", json.dumps(input_json, ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        if check:
            raise RuntimeError(f"{' '.join(args[:3])} failed: {r.stderr.strip()[:2000]}")
        return {"_error": r.stderr.strip()[:2000]}
    out = r.stdout.strip()
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {"_raw": out}


def api(ctx, method, path, body=None, check=True):
    args = ["api", method, path]
    return cli(ctx, *args, input_json=body, check=check)


def record(name, obj):
    os.makedirs(EVID, exist_ok=True)
    with open(os.path.join(EVID, f"{name}.json"), "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, default=str)


def log(line):
    os.makedirs(EVID, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(line)
    with open(os.path.join(EVID, "RUN_LOG.md"), "a") as f:
        f.write(f"- `{stamp}` {line}\n")


def sql(ctx, statement, wait=True):
    body = {"warehouse_id": ctx.state["warehouse_id"], "statement": statement,
            "wait_timeout": "50s", "on_wait_timeout": "CONTINUE", "disposition": "INLINE",
            "format": "JSON_ARRAY"}
    r = api(ctx, "post", "/api/2.0/sql/statements", body)
    while wait and r.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        time.sleep(3)
        r = api(ctx, "get", f"/api/2.0/sql/statements/{r['statement_id']}")
    return r


def sql_rows(r):
    cols = [c["name"] for c in r.get("manifest", {}).get("schema", {}).get("columns", [])]
    return cols, r.get("result", {}).get("data_array", []) or []


def run_sql_script(ctx, text):
    """; 区切りの SQL を1文ずつ実行し、結果（成否・行）を返す"""
    out = []
    for stmt in [s.strip() for s in strip_comments(text).split(";") if s.strip()]:
        r = sql(ctx, stmt)
        st = r.get("status", {})
        cols, rows = sql_rows(r)
        out.append({"statement": stmt, "state": st.get("state"),
                    "error": st.get("error", {}).get("message"), "columns": cols, "rows": rows[:20]})
        log(f"SQL `{stmt.splitlines()[0][:80]}` → {st.get('state')}"
            + (f" ({st.get('error', {}).get('message', '')[:160]})" if st.get("state") != "SUCCEEDED" else ""))
    return out


def strip_comments(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))


def uc_sql(ctx, part):
    text = open(os.path.join(ROOT, "databricks", "02_unity_catalog_setup.sql")).read()
    a, b = text.split("-- ============================ PART B")
    chosen = a if part == "A" else "--" + b
    return chosen.replace("fraud_demo", ctx.catalog)


def wait(fn, done, fail=(), every=10, timeout=3600, label=""):
    t0 = time.time()
    while True:
        r = fn()
        s = done(r)
        if s is True:
            return r
        if s in fail:
            raise RuntimeError(f"{label} failed: {json.dumps(r, ensure_ascii=False)[:1500]}")
        if time.time() - t0 > timeout:
            raise TimeoutError(label)
        time.sleep(every)


def import_notebook(ctx, local, remote):
    lang = "SQL" if local.endswith(".sql") else "PYTHON"
    subprocess.run(["databricks", "workspace", "import", remote, "--file", local, "--format", "SOURCE",
                    "--language", lang, "--overwrite", "-p", ctx.profile], check=True,
                   capture_output=True)


# ------------------------------------------------------------------ steps
def prepare(ctx):
    ctx.me = cli(ctx, "current-user", "me")["userName"]
    ctx.ws_dir = f"/Workspace/Users/{ctx.me}/fraud-decision-demo"
    # ~/.databrickscfg からホスト名だけを読む（資格情報は読まない）
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(os.path.expanduser("~/.databrickscfg"))
    if cfg.has_option(ctx.profile, "host"):
        ctx.state["host"] = cfg.get(ctx.profile, "host")
    if "warehouse_id" not in ctx.state:
        whs = cli(ctx, "warehouses", "list")
        whs = sorted(whs, key=lambda w: (not w.get("enable_serverless_compute"), w["state"] != "RUNNING"))
        ctx.state["warehouse_id"] = whs[0]["id"]
        ctx.state["warehouse_name"] = whs[0]["name"]
    if not ctx.catalog:
        r = sql(ctx, "CREATE CATALOG IF NOT EXISTS fraud_demo COMMENT '不正送金検知デモ（合成データ・PIIなし）'")
        if r.get("status", {}).get("state") == "SUCCEEDED":
            ctx.catalog = "fraud_demo"
        else:
            _, rows = sql_rows(sql(ctx, "SELECT current_catalog()"))
            ctx.catalog = rows[0][0]
            log(f"CREATE CATALOG 権限なし → 既定カタログ `{ctx.catalog}` を使用")
        ctx.schema = f"{ctx.catalog}.fraud"
    prefix = ctx.me.split("@")[0].replace(".", "-")
    a = ctx.args
    ctx.instance = a.lakebase_instance or f"{prefix}-fraud-ops"
    ctx.app_name = a.app_name or f"{prefix}-fraud-review"[:30]
    ctx.db_catalog = a.db_catalog or f"{prefix.replace('-', '_')}_fraud_ops"
    ctx.state.update({"catalog": ctx.catalog, "user": ctx.me, "lakebase_instance": ctx.instance,
                      "app_name": ctx.app_name, "db_catalog": ctx.db_catalog})
    ctx.save_state()
    subprocess.run(["databricks", "workspace", "mkdirs", ctx.ws_dir, "-p", ctx.profile], check=True)


def step_uc_a(ctx):
    res = run_sql_script(ctx, uc_sql(ctx, "A"))
    record("01_unity_catalog_part_a", res)


def step_upload(ctx):
    dst = f"dbfs:/Volumes/{ctx.catalog}/fraud/raw/raw_transactions.csv"
    subprocess.run(["databricks", "fs", "cp", os.path.join(ROOT, "data", "raw_transactions.csv"), dst,
                    "--overwrite", "-p", ctx.profile], check=True)
    ls = cli(ctx, "fs", "ls", f"dbfs:/Volumes/{ctx.catalog}/fraud/raw")
    record("02_volume_upload", ls)
    log(f"Volume へ生データをアップロード: {dst}")


def step_pipeline(ctx):
    nb = f"{ctx.ws_dir}/01_lakeflow_ingest"
    import_notebook(ctx, os.path.join(ROOT, "databricks", "01_lakeflow_ingest.py"), nb)
    spec = {"name": "fraud-demo-lakeflow-ingest", "catalog": ctx.catalog, "schema": "fraud",
            "serverless": True, "development": False, "photon": True, "channel": "CURRENT",
            "libraries": [{"notebook": {"path": nb}}],
            "configuration": {"fraud.raw_path": f"/Volumes/{ctx.catalog}/fraud/raw/raw_transactions.csv"}}
    pid = ctx.state.get("pipeline_id")
    if pid:
        api(ctx, "put", f"/api/2.0/pipelines/{pid}", {**spec, "id": pid})
    else:
        pid = api(ctx, "post", "/api/2.0/pipelines", spec)["pipeline_id"]
        ctx.state["pipeline_id"] = pid
        ctx.save_state()
    # 設定更新や失敗時の自動リトライで走っている更新があれば止めてから、フルリフレッシュで開始する
    api(ctx, "post", f"/api/2.0/pipelines/{pid}/stop", {}, check=False)
    wait(lambda: api(ctx, "get", f"/api/2.0/pipelines/{pid}"),
         lambda r: True if r.get("state") in ("IDLE", "FAILED") else r.get("state"), every=10,
         timeout=600, label="pipeline stop")
    uid = api(ctx, "post", f"/api/2.0/pipelines/{pid}/updates", {"full_refresh": True})["update_id"]
    log(f"Lakeflow パイプライン {pid} 更新開始 update={uid}")
    upd = wait(lambda: api(ctx, "get", f"/api/2.0/pipelines/{pid}/updates/{uid}"),
               lambda r: True if r["update"]["state"] == "COMPLETED" else r["update"]["state"],
               fail=("FAILED", "CANCELED"), every=15, label="pipeline")
    events = api(ctx, "get", f"/api/2.0/pipelines/{pid}/events?max_results=250")
    dq = []
    for e in events.get("events", []):
        det = e.get("details", {}).get("flow_progress", {})
        if det.get("data_quality") or det.get("metrics"):
            dq.append({"flow": e.get("origin", {}).get("flow_name"), "status": det.get("status"),
                       "metrics": det.get("metrics"), "data_quality": det.get("data_quality")})
    counts = run_sql_script(ctx, ";".join(
        f"SELECT '{t}' AS tbl, COUNT(*) AS n FROM {ctx.schema}.{t}"
        for t in ("bronze_transactions", "silver_transactions", "gold_features")))
    record("03_lakeflow_pipeline", {"pipeline_id": pid, "update": upd, "flow_progress": dq,
                                    "row_counts": counts})
    log(f"Lakeflow 完了: " + ", ".join(f"{c['rows'][0][0]}={c['rows'][0][1]}" for c in counts if c["rows"]))


def run_notebook_job(ctx, local, name, params):
    nb = f"{ctx.ws_dir}/{name}"
    import_notebook(ctx, local, nb)
    body = {"run_name": f"fraud-demo-{name}",
            "tasks": [{"task_key": name, "notebook_task": {"notebook_path": nb, "base_parameters": params}}]}
    run_id = api(ctx, "post", "/api/2.1/jobs/runs/submit", body)["run_id"]
    log(f"Job 実行開始 {name} run_id={run_id}")
    run = wait(lambda: api(ctx, "get", f"/api/2.1/jobs/runs/get?run_id={run_id}"),
               lambda r: True if r["state"].get("life_cycle_state") == "TERMINATED"
               and r["state"].get("result_state") == "SUCCESS"
               else r["state"].get("result_state") or r["state"].get("life_cycle_state"),
               fail=("FAILED", "CANCELED", "TIMEDOUT", "INTERNAL_ERROR", "SKIPPED"),
               every=20, label=name)
    task_run = run["tasks"][0]["run_id"]
    out = api(ctx, "get", f"/api/2.1/jobs/runs/get-output?run_id={task_run}")
    return run, out


def step_train(ctx):
    run, out = run_notebook_job(ctx, os.path.join(ROOT, "databricks", "03_train_model.py"),
                                "03_train_model", {"catalog": ctx.catalog})
    result = json.loads(out.get("notebook_output", {}).get("result", "{}"))
    record("04_ml_training", {"run_page_url": run.get("run_page_url"), "notebook_result": result})
    log(f"ML 学習完了: ROC-AUC={result.get('roc_auc')} SHAP加法性誤差={result.get('shap_additivity_max_abs_err')}"
        f" モデル={result.get('registered_model')} v{result.get('model_version')}")


def group_member(ctx, group, add):
    gid = cli(ctx, "groups", "list", "--filter", f'displayName eq "{group}"')[0]["id"]
    uid = cli(ctx, "current-user", "me")["id"]
    op = {"op": "add", "path": "members", "value": [{"value": uid}]} if add else \
        {"op": "remove", "path": f'members[value eq "{uid}"]'}
    cli(ctx, "groups", "patch", gid, input_json={
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": [op]}, check=False)


def step_uc_b(ctx):
    res = run_sql_script(ctx, uc_sql(ctx, "B"))
    # 行フィルタ・列マスクの効き目を「同じユーザ・同じSQL」でグループ所属の前後比較として記録する
    probe = (f"SELECT is_member('fraud_analysts_global') AS in_global, is_member('fraud_analysts') AS in_analysts, "
             f"SUM(CASE WHEN is_foreign=1 THEN 1 ELSE 0 END) AS foreign_rows_visible, "
             f"SUM(CASE WHEN is_foreign=0 THEN 1 ELSE 0 END) AS domestic_rows_visible, "
             f"MIN(customer_id) AS sample_customer_id FROM {ctx.schema}.scored_transactions")
    group_member(ctx, "fraud_analysts_global", add=False)
    time.sleep(5)
    before = run_sql_script(ctx, probe)
    group_member(ctx, "fraud_analysts_global", add=True)   # 以降の Genie は海外チーム権限で全セグメントを見る
    time.sleep(30)
    after = run_sql_script(ctx, probe)
    res.append({"governance_probe": {"before_join_fraud_analysts_global": before,
                                     "after_join_fraud_analysts_global": after}})
    # ガバナンスの効き目: 管理者（自分）視点と、関数定義・付与状況を記録
    res += run_sql_script(ctx, f"""
        DESCRIBE TABLE EXTENDED {ctx.schema}.scored_transactions;
        SHOW GRANTS ON SCHEMA {ctx.schema};
        SELECT customer_id, is_foreign, fraud_score FROM {ctx.schema}.scored_transactions
          ORDER BY fraud_score DESC LIMIT 5""")
    record("05_unity_catalog_part_b", res)


def step_memo(ctx):
    eps = cli(ctx, "serving-endpoints", "list")
    names = [e["name"] for e in eps]
    pref = [n for n in names if "claude" in n and "sonnet" in n] or [n for n in names if "claude" in n] \
        or [n for n in names if "llama" in n]
    ep = pref[0]
    text = open(os.path.join(ROOT, "databricks", "06_action_memo.sql")).read()
    text = text.replace("fraud_demo", ctx.catalog).replace("{{LLM_ENDPOINT}}", ep)
    res = run_sql_script(ctx, text)
    record("06_llm_action_memo", {"endpoint": ep, "results": res})
    log(f"LLM 対応メモ生成（{ep}）")


def pg_exec(ctx, statements):
    """Lakebase(Postgres) で SQL を実行。資格情報をローカルに出さないよう、
    07_lakebase_sql.py をサーバレス Job として実行し、その出力（結果行のみ）を受け取る"""
    _, out = run_notebook_job(ctx, os.path.join(ROOT, "databricks", "07_lakebase_sql.py"), "07_lakebase_sql",
                              {"endpoint": ctx.endpoint, "statements": json.dumps(statements)})
    return json.loads(out.get("notebook_output", {}).get("result", "[]"))


def lakebase_names(ctx):
    proj = ctx.args.lakebase_project or ctx.state.get("lakebase_project")
    if not proj:
        proj = cli(ctx, "postgres", "list-projects")[0]["project_id"]
    ctx.state["lakebase_project"] = proj
    ctx.branch = f"projects/{proj}/branches/production"
    ctx.endpoint = f"{ctx.branch}/endpoints/primary"
    ctx.database = f"{ctx.branch}/databases/databricks-postgres"


def step_lakebase(ctx):
    """Lakebase Autoscaling（FEVM が払い出したプロジェクト）へ Delta を同期する"""
    lakebase_names(ctx)
    proj = cli(ctx, "postgres", "get-project", f"projects/{ctx.state['lakebase_project']}")
    ep = cli(ctx, "postgres", "get-endpoint", ctx.endpoint)
    # 自分の Postgres ロール（UC 同期テーブル作成者として必要）
    roles = cli(ctx, "postgres", "list-roles", ctx.branch)
    if not any(r.get("status", {}).get("postgres_role") == ctx.me for r in roles):
        cli(ctx, "postgres", "create-role", ctx.branch, "--role-id", "user-" + ctx.me.split("@")[0].replace(".", "-"),
            input_json={"spec": {"identity_type": "USER", "postgres_role": ctx.me,
                                 "auth_method": "LAKEBASE_OAUTH_V1",
                                 "membership_roles": ["DATABRICKS_SUPERUSER"]}})
        log(f"Lakebase ロール作成: {ctx.me}")
    # Lakebase の DB を専用 UC カタログとして登録するには CREATE CATALOG が要る（FEVM では不可）。
    # 同期テーブルは既存カタログ内のスキーマ fraud_ops に作り、Postgres 側も同名スキーマに作られる。
    cat = run_sql_script(ctx, f"CREATE SCHEMA IF NOT EXISTS {ctx.catalog}.{PG_SCHEMA} "
                              "COMMENT 'Lakebase 同期テーブル（Postgres 側も同名スキーマ）'")
    synced = {}
    for src, dst in (("scored_transactions_ops", "fraud_alerts"), ("action_memos", "action_memos")):
        full = f"{ctx.catalog}.{PG_SCHEMA}.{dst}"
        st = cli(ctx, "postgres", "get-synced-table", f"synced_tables/{full}", check=False)
        if "_error" in st:
            st = cli(ctx, "postgres", "create-synced-table", full, "--timeout", "40m", input_json={
                "spec": {"source_table_full_name": f"{ctx.schema}.{src}", "primary_key_columns": ["txn_id"],
                         "scheduling_policy": "TRIGGERED", "branch": ctx.branch,
                         "postgres_database": "databricks_postgres",
                         "new_pipeline_spec": {"storage_catalog": ctx.catalog, "storage_schema": "fraud"}}})
            log(f"synced table 作成 {full} ← {ctx.schema}.{src}")
        synced[dst] = cli(ctx, "postgres", "get-synced-table", f"synced_tables/{full}")
    pg = pg_exec(ctx, [
        f"SELECT COUNT(*) FROM {PG_SCHEMA}.fraud_alerts",
        f"SELECT decision_policy, COUNT(*) FROM {PG_SCHEMA}.fraud_alerts GROUP BY 1 ORDER BY 1",
        f"SELECT COUNT(*) FROM {PG_SCHEMA}.action_memos",
    ])
    delta = run_sql_script(ctx, f"SELECT COUNT(*) FROM {ctx.schema}.scored_transactions_ops")
    record("07_lakebase_sync", {"project": proj, "endpoint": {k: ep.get(k) for k in ("name", "status")},
                                "database_catalog": cat, "synced_tables": synced,
                                "postgres_checks": pg, "delta_count": delta})
    log(f"Lakebase 同期確認: Postgres fraud_alerts={pg[0].get('rows')} / Delta={delta[0]['rows']}")


def step_app(ctx):
    lakebase_names(ctx)
    app = api(ctx, "get", f"/api/2.0/apps/{ctx.app_name}", check=False)
    if "_error" in app:
        api(ctx, "post", "/api/2.0/apps",
            {"name": ctx.app_name, "description": "不正送金レビュー支援コックピット（Lakebase）",
             "resources": [{"name": "postgres", "postgres": {
                 "branch": ctx.branch, "database": ctx.database, "permission": "CAN_CONNECT_AND_CREATE"}}]})
        log(f"App {ctx.app_name} 作成")
    app = wait(lambda: api(ctx, "get", f"/api/2.0/apps/{ctx.app_name}"),
               lambda r: True if r.get("compute_status", {}).get("state") == "ACTIVE"
               else r.get("compute_status", {}).get("state"), fail=("ERROR",), every=15, label="app compute")
    sp = app["service_principal_client_id"]
    # App の SP に同期テーブルの参照と、判断テーブル作成の権限を付与（Lakebase 側の GRANT）
    grants = pg_exec(ctx, [
        f'GRANT USAGE, CREATE ON SCHEMA {PG_SCHEMA} TO "{sp}"',
        f'GRANT SELECT ON {PG_SCHEMA}.fraud_alerts TO "{sp}"',
        f'GRANT SELECT ON {PG_SCHEMA}.action_memos TO "{sp}"',
    ])
    src = f"{ctx.ws_dir}/app"
    # app.yaml の Lakebase エンドポイント名を実名に差し替えたステージングコピーをアップロード
    import shutil, tempfile
    stage = os.path.join(tempfile.mkdtemp(), "app")
    shutil.copytree(os.path.join(ROOT, "app"), stage,
                    ignore=shutil.ignore_patterns("local_app.py", "__pycache__"))
    y = os.path.join(stage, "app.yaml")
    open(y, "w").write(open(y).read().replace("__LAKEBASE_ENDPOINT__", ctx.endpoint))
    subprocess.run(["databricks", "workspace", "import-dir", stage, src, "--overwrite",
                    "-p", ctx.profile], check=True, capture_output=True)
    dep = api(ctx, "post", f"/api/2.0/apps/{ctx.app_name}/deployments", {"source_code_path": src})
    dep = wait(lambda: api(ctx, "get", f"/api/2.0/apps/{ctx.app_name}/deployments/{dep['deployment_id']}"),
               lambda r: True if r.get("status", {}).get("state") == "SUCCEEDED" else r.get("status", {}).get("state"),
               fail=("FAILED", "CANCELLED"), every=15, label="app deploy")
    time.sleep(20)
    # App 起動時の自己チェック（Lakebase 読取＋書込）の記録を Postgres 側から確認する
    check = pg_exec(ctx, [
        "SELECT checked_at, app_user, fraud_alerts_n, action_memos_n, pending_queue_n "
        f"FROM {PG_SCHEMA}.app_selfcheck ORDER BY checked_at DESC LIMIT 3",
        f"SELECT COUNT(*) FROM {PG_SCHEMA}.review_decisions",
    ])
    record("08_databricks_app", {"app": {k: app.get(k) for k in ("name", "url", "app_status", "compute_status",
                                                                   "service_principal_client_id", "resources")},
                                 "deployment": dep, "lakebase_grants": grants, "selfcheck": check})
    log(f"App デプロイ完了 {app['url']} / 自己チェック: {check[0].get('rows')}")


def genie_wait(ctx, sid, cid, mid):
    return wait(lambda: api(ctx, "get", f"/api/2.0/genie/spaces/{sid}/conversations/{cid}/messages/{mid}"),
                lambda r: True if r.get("status") == "COMPLETED" else r.get("status"),
                fail=("FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"), every=4, timeout=600, label="genie")


def genie_turn(ctx, sid, msg, cid):
    msg = genie_wait(ctx, sid, cid, msg["message_id"] if "message_id" in msg else msg["id"])
    turn = {"question": msg.get("content"), "message_id": msg.get("message_id") or msg.get("id"),
            "status": msg.get("status"), "answers": []}
    for att in msg.get("attachments", []) or []:
        if att.get("text"):
            turn["answers"].append({"type": "text", "content": att["text"].get("content")})
        if att.get("query"):
            q = att["query"]
            res = api(ctx, "get", f"/api/2.0/genie/spaces/{sid}/conversations/{cid}/messages/"
                                  f"{turn['message_id']}/attachments/{att['attachment_id']}/query-result",
                      check=False)
            sr = res.get("statement_response", {})
            cols, rows = sql_rows(sr)
            turn["answers"].append({"type": "query", "description": q.get("description"),
                                    "sql": q.get("query"), "columns": cols, "rows": rows[:20],
                                    "row_count": sr.get("manifest", {}).get("total_row_count")})
    return turn


def step_genie(ctx):
    table = f"{ctx.schema}.scored_transactions"
    space = {"version": 1,
             "config": {"sample_questions": [{"id": uuid.uuid4().hex, "question": [q]}
                                             for q in GENIE_QUESTIONS]},
             "data_sources": {"tables": [{"identifier": table}]},
             "instructions": {"text_instructions": [{"id": uuid.uuid4().hex,
                                                     "content": [GENIE_INSTRUCTIONS]}]}}
    sid = ctx.state.get("genie_space_id")
    if not sid:
        r = api(ctx, "post", "/api/2.0/genie/spaces",
                {"warehouse_id": ctx.state["warehouse_id"], "title": "不正送金レビュー Genie",
                 "description": "スコア済み送金取引（UCで列マスク・行フィルタ適用済み）を日本語で探索する",
                 "serialized_space": json.dumps(space, ensure_ascii=False)})
        sid = r.get("space_id") or r.get("id")
        ctx.state["genie_space_id"] = sid
        ctx.save_state()
        log(f"Genie Space 作成 space_id={sid}")
    space_info = api(ctx, "get", f"/api/2.0/genie/spaces/{sid}")
    transcript = []
    for i, q in enumerate(GENIE_QUESTIONS):
        start = api(ctx, "post", f"/api/2.0/genie/spaces/{sid}/start-conversation", {"content": q})
        cid = start["conversation_id"]
        turn = genie_turn(ctx, sid, start.get("message", start), cid)
        turn["conversation_id"] = cid
        conv = [turn]
        if i in GENIE_FOLLOWUPS:
            fu = api(ctx, "post", f"/api/2.0/genie/spaces/{sid}/conversations/{cid}/messages",
                     {"content": GENIE_FOLLOWUPS[i]})
            conv.append(genie_turn(ctx, sid, fu, cid))
        transcript.append(conv)
        log(f"Genie Q{i + 1} 回答取得（会話 {cid}、{len(conv)} ターン）")
    record("09_genie_conversations", {"space_id": sid, "space": space_info, "transcript": transcript})
    write_genie_markdown(ctx, sid, transcript)


def write_genie_markdown(ctx, sid, transcript):
    L = ["# Genie Space 実会話ログ（Databricks 上で実行）", "",
         f"- ワークスペース: `{ctx.state.get('host', '')}`",
         f"- Genie Space ID: `{sid}` / SQL Warehouse: `{ctx.state.get('warehouse_name')}`",
         f"- 対象テーブル: `{ctx.schema}.scored_transactions`（Unity Catalog 列マスク・行フィルタ適用済み）",
         f"- 取得日時: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}（API: start-conversation / messages / query-result）",
         "- SQL は Genie が自然言語から生成したもの（人手で書いた SQL ではない）。生データは "
         "`evidence/databricks/09_genie_conversations.json`。", ""]
    for i, conv in enumerate(transcript, 1):
        for j, t in enumerate(conv):
            L.append(f"## Q{i}{'' if j == 0 else ' — フォローアップ'}: {t['question']}")
            L.append("")
            for a in t["answers"]:
                if a["type"] == "text":
                    L += [f"**Genie:** {a['content']}", ""]
                else:
                    if a.get("description"):
                        L += [f"**Genie（解釈）:** {a['description']}", ""]
                    L += ["```sql", (a.get("sql") or "").strip(), "```", ""]
                    if a["columns"]:
                        L.append("| " + " | ".join(a["columns"]) + " |")
                        L.append("|" + "---|" * len(a["columns"]))
                        for row in a["rows"][:10]:
                            L.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
                        L.append("")
    with open(os.path.join(ROOT, "outputs", "genie_live_transcript.md"), "w") as f:
        f.write("\n".join(L) + "\n")


def step_verify(ctx):
    """全工程の最終状態をまとめて取り直す（作成直後ではなく、落ち着いた後の状態を証跡にする）"""
    lakebase_names(ctx)
    synced = {}
    for dst in ("fraud_alerts", "action_memos"):
        st = cli(ctx, "postgres", "get-synced-table", f"synced_tables/{ctx.catalog}.{PG_SCHEMA}.{dst}")
        synced[dst] = st.get("status", st)
    app = api(ctx, "get", f"/api/2.0/apps/{ctx.app_name}")
    pg = pg_exec(ctx, [
        f"SELECT COUNT(*) FROM {PG_SCHEMA}.fraud_alerts",
        f"SELECT decision_policy, COUNT(*) FROM {PG_SCHEMA}.fraud_alerts GROUP BY 1 ORDER BY 1",
        f"SELECT COUNT(*) FROM {PG_SCHEMA}.action_memos",
        f"SELECT checked_at, app_user, fraud_alerts_n, action_memos_n, pending_queue_n "
        f"FROM {PG_SCHEMA}.app_selfcheck ORDER BY checked_at DESC LIMIT 3",
        f"SELECT review_status, COUNT(*) FROM {PG_SCHEMA}.review_decisions GROUP BY 1",
    ])
    uc = run_sql_script(ctx, f"""
        SELECT 'scored_transactions' t, COUNT(*) n FROM {ctx.schema}.scored_transactions;
        SELECT 'scored_transactions_ops' t, COUNT(*) n FROM {ctx.schema}.scored_transactions_ops;
        SELECT 'action_memos' t, COUNT(*) n FROM {ctx.schema}.action_memos;
        SELECT source_table_full_name, target_table_full_name FROM system.access.table_lineage
          WHERE target_table_catalog = '{ctx.catalog}' AND target_table_schema IN ('fraud', '{PG_SCHEMA}')
          AND source_table_full_name IS NOT NULL
          GROUP BY 1, 2 ORDER BY 2, 1""")
    record("10_final_verification", {
        "synced_tables": synced,
        "app": {k: app.get(k) for k in ("name", "url", "app_status", "compute_status", "active_deployment")},
        "lakebase_postgres": pg, "unity_catalog": uc})
    log("最終確認: synced=" + ", ".join(f"{k}:{v.get('detailed_state')}" for k, v in synced.items())
        + f" / app={app.get('app_status', {}).get('state')} / Postgres={[x.get('rows') for x in pg[:3]]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--catalog", default=None)
    ap.add_argument("--steps", default="all")
    ap.add_argument("--lakebase-instance", default=None)
    ap.add_argument("--lakebase-project", default=None)
    ap.add_argument("--db-catalog", default=None)
    ap.add_argument("--app-name", default=None)
    a = ap.parse_args()
    ctx = Ctx(a)
    prepare(ctx)
    steps = STEPS if a.steps == "all" else a.steps.split(",")
    for s in steps:
        log(f"**step {s}** 開始")
        globals()[f"step_{s}"](ctx)
        ctx.save_state()


if __name__ == "__main__":
    main()
