-- ============================================================================
-- ② Unity Catalog — ガバナンス（カタログ/スキーマ/Volume/PIIマスキング/行フィルタ/権限/リネージ）
--   すべての資産をUCで一元管理。列マスクで顧客IDを、行フィルタで閲覧セグメントを最小権限に絞る。
--   Databricks SQL エディタ または ノートブック(%sql)で実行。
--   deploy/deploy.py は `fraud_demo` を実カタログ名に置換し、PART A → (Lakeflow/ML) → PART B の順に実行する。
-- ============================================================================

-- ============================ PART A: 取り込み前 ============================

-- 1) カタログ・スキーマ・Volume（生データ配置先）
CREATE CATALOG IF NOT EXISTS fraud_demo
  COMMENT '不正送金検知デモ（合成データ・PIIなし）';
CREATE SCHEMA IF NOT EXISTS fraud_demo.fraud
  COMMENT 'Lakeflowが生成するbronze/silver/gold と スコア済みテーブル';
CREATE VOLUME IF NOT EXISTS fraud_demo.fraud.raw
  COMMENT '生データCSVの配置先（Lakeflowの入力）';

-- 2) 業務ロール（本番はアカウントグループ。FEVM のようにアカウント管理者権限がない環境では
--    ワークスペースグループで代用し、関数は is_member() でも判定する。UC の GRANT は
--    アカウントグループにしか付与できないため、ワークスペースグループの環境では下の GRANT は失敗する）
--    fraud_analysts        : 不正調査部門のレビュー担当（顧客IDを平文で閲覧・判断を行う）
--    fraud_analysts_global : 海外送金チーム（海外送金セグメントも閲覧可）
--    fraud_viewers         : リスク管理部門の参照のみ（顧客IDはマスク）
GRANT USE CATALOG ON CATALOG fraud_demo TO `fraud_analysts`;
GRANT USE SCHEMA, SELECT ON SCHEMA fraud_demo.fraud TO `fraud_analysts`;
GRANT USE CATALOG ON CATALOG fraud_demo TO `fraud_viewers`;
GRANT USE SCHEMA, SELECT ON SCHEMA fraud_demo.fraud TO `fraud_viewers`;

-- 3) PIIマスキング関数：顧客IDはアナリスト以外には下3桁のみ表示
CREATE OR REPLACE FUNCTION fraud_demo.fraud.mask_customer(id STRING)
RETURN CASE
         WHEN is_account_group_member('fraud_analysts') OR is_member('fraud_analysts') THEN id
         ELSE concat('C******', substr(id, -3))
       END;

-- 4) 行フィルタ関数：海外送金の行は海外送金チームだけが見られる
--    国内担当のアナリストには国内送金だけが見える＝セグメント単位の最小権限
CREATE OR REPLACE FUNCTION fraud_demo.fraud.segment_filter(is_foreign INT)
RETURN is_foreign = 0
    OR is_account_group_member('fraud_analysts_global')
    OR is_member('fraud_analysts_global');

-- ============================ PART B: スコア書き出し後 ============================

-- 5) スコア済みテーブル（03 が書き出す）へ列マスク・行フィルタを適用
ALTER TABLE fraud_demo.fraud.scored_transactions
  ALTER COLUMN customer_id SET MASK fraud_demo.fraud.mask_customer;
ALTER TABLE fraud_demo.fraud.scored_transactions
  SET ROW FILTER fraud_demo.fraud.segment_filter ON (is_foreign);

-- 6) リネージ確認（UI: Catalog Explorer > lineage、SQLでは system.access を参照）
--    Lakeflow: raw(volume) -> bronze -> silver -> gold -> scored -> Lakebase同期
SELECT source_table_full_name, target_table_full_name, entity_type, event_time
FROM system.access.table_lineage
WHERE target_table_catalog = 'fraud_demo'
ORDER BY event_time DESC LIMIT 20;
