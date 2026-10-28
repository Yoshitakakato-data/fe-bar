# ③ Lakebase — 運用データレイヤー（低遅延Postgres同期）

Lakehouse の `scored_transactions`（Delta）を **Lakebase の synced table** として
低遅延 Postgres に同期し、Databricks App がミリ秒級で読み書きできる運用ストアにする。
これにより「夜間バッチ抽出」をやめ、アナリストがライブに判断→記録できる。

**ローカル等価物**: `src/load_lakebase.py`（sqlite で同じ `fraud_alerts` スキーマを作成し、App が読み書き）。

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
