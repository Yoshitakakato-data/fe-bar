# Databricks notebook source
# MAGIC %md
# MAGIC # ③' Lakebase — Postgres 側の SQL 実行（同期確認・App サービスプリンシパルへの GRANT）
# MAGIC
# MAGIC `deploy/deploy.py` から Job として実行される。実行ユーザ自身の OAuth 資格情報で Lakebase に接続し、
# MAGIC ウィジェット `statements`（JSON配列）の SQL を順に実行して結果を Job 出力として返す。
# MAGIC 資格情報はこのサーバレス Job 内でだけ発行・使用し、ローカルや Job 出力には出さない。
# MAGIC Lakebase Autoscaling（`endpoint` = projects/.../branches/.../endpoints/...）と
# MAGIC Provisioned（`instance`）の両方に対応。

# COMMAND ----------
# MAGIC %pip install "psycopg[binary]" "databricks-sdk>=0.50"
# MAGIC %restart_python

# COMMAND ----------
import json, uuid
import psycopg
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("endpoint", "")
dbutils.widgets.text("instance", "")
dbutils.widgets.text("statements", "[]")
ENDPOINT = dbutils.widgets.get("endpoint")
INSTANCE = dbutils.widgets.get("instance")
STATEMENTS = json.loads(dbutils.widgets.get("statements"))

w = WorkspaceClient()
user = w.current_user.me().user_name
if ENDPOINT:
    host = w.postgres.get_endpoint(name=ENDPOINT).status.hosts.host
    token = w.postgres.generate_database_credential(endpoint=ENDPOINT).token
else:
    host = w.database.get_database_instance(name=INSTANCE).read_write_dns
    token = w.database.generate_database_credential(request_id=str(uuid.uuid4()),
                                                    instance_names=[INSTANCE]).token

out = []
with psycopg.connect(host=host, dbname="databricks_postgres", user=user,
                     password=token, sslmode="require", autocommit=True) as con:
    for s in STATEMENTS:
        try:
            cur = con.execute(s)
            rows = cur.fetchall() if cur.description else []
            out.append({"sql": s, "ok": True, "rows": [[str(v) for v in r] for r in rows[:20]]})
        except Exception as e:
            out.append({"sql": s, "ok": False, "error": str(e)[:500]})
        print(out[-1])

dbutils.notebook.exit(json.dumps(out, ensure_ascii=False))
