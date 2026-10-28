# ③ Lakebase — 運用データレイヤー（低遅延Postgres同期）

Lakehouse の `scored_transactions`（Delta）を **Lakebase の synced table** として
低遅延 Postgres に同期し、Databricks App がミリ秒級で読み書きできる運用ストアにする。
これにより「夜間バッチ抽出」をやめ、アナリストがライブに判断→記録できる。

**実行済み（Lakebase Autoscaling）**: `deploy/deploy.py --steps lakebase,app` が FEVM の Lakebase Autoscaling
プロジェクト（Postgres 17）に対して以下を自動実行した。証跡は `../evidence/databricks/07_lakebase_sync.json`, `08_databricks_app.json`。

| 項目 | 実際の構成 |
|---|---|
| 同期元 | `<catalog>.fraud.scored_transactions_ops`（REVIEW / BLOCK_CANDIDATE の 613件、CDF有効） |
| 同期先 | synced table `<catalog>.fraud_ops.fraud_alerts`（Postgres 側 `fraud_ops.fraud_alerts`）、TRIGGERED |
| 対応メモ | `<catalog>.fraud.action_memos` → `fraud_ops.action_memos` |
| App 接続 | App に postgres リソース（branch/database, CAN_CONNECT_AND_CREATE）を付与、SP の OAuth 資格情報で接続 |
| 判断の書き戻し | `fraud_ops.review_decisions`（App が作成・upsert）、起動時自己チェックは `fraud_ops.app_selfcheck` |

同期元を絞った理由: アナリスト向けの `scored_transactions` には列マスク・行フィルタを掛ける一方、
運用ストアは「判断が必要な取引」だけを小さく持てばよいため。

**ローカル等価物**: `src/load_lakebase.py`（sqlite で同じ `fraud_alerts` スキーマを作成し、App が読み書き）。

以下は Lakebase Provisioned（インスタンス型）で手動構築する場合の参考手順。

---

## 手順

### 1. Lakebase インスタンス作成
Databricks UI → **Compute → Lakebase (Database Instances) → Create**
- Instance name: `fraud-ops`
- Postgres 16、最小サイズ（デモ用）

### 2. Delta → Lakebase の synced table を作成
Catalog Explorer で `fraud_demo.fraud.scored_transactions` を開き
**Create → Synced table**、またはSQLで：

```sql
-- Lakebase の synced table（Deltaの変更を継続同期）
CREATE SYNCED TABLE fraud_ops.public.fraud_alerts
FROM fraud_demo.fraud.scored_transactions
PRIMARY KEY (txn_id)
SCHEDULE CONTINUOUS;   -- または TRIGGERED
```

### 3. 運用側の可変列を追加（アナリストの判断を保持）
synced table は読み取り同期なので、判断の書き込みは別テーブル or 拡張列で持つ。
デモでは Lakebase 上に判断テーブルを作り、App が upsert する：

```sql
CREATE TABLE IF NOT EXISTS fraud_ops.public.review_decisions (
  txn_id        text PRIMARY KEY,
  review_status text NOT NULL DEFAULT 'PENDING',  -- PENDING/BLOCKED/APPROVED
  reviewed_by   text,
  review_note   text,
  decided_at    timestamptz
);
```

App は `fraud_alerts`（同期・読み取り）と `review_decisions`（運用・書き込み）を
JOIN して 1 画面に出す。ローカル版(sqlite)では単一テーブルに集約している。

### 4. 接続情報
App からは OAuth トークン + `databricks-sql-connector` / `psycopg` で接続。
接続文字列・認証は `app/app.yaml` の環境変数で注入（`app/app.py` 参照）。

---

## 証拠として見せるもの
- synced table のレコード件数が Delta と一致すること（`SELECT COUNT(*)`）。
- App からの判断書き込みが `review_decisions` に反映されること。
- 変更が数秒で同期されること（CONTINUOUS）。
