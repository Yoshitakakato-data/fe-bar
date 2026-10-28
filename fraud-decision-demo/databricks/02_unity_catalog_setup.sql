-- ============================================================================
-- ② Unity Catalog — ガバナンス（カタログ/スキーマ/Volume/PIIマスキング/権限/リネージ）
--   すべての資産をUCで一元管理。列マスキングで顧客IDを最小権限に絞る。
--   Databricks SQL エディタ または ノートブック(%sql)で実行。
-- ============================================================================

-- 1) カタログ・スキーマ・Volume（生データ配置先）
CREATE CATALOG IF NOT EXISTS fraud_demo
  COMMENT '不正送金検知デモ（合成データ・PIIなし）';
CREATE SCHEMA IF NOT EXISTS fraud_demo.fraud
  COMMENT 'Lakeflowが生成するbronze/silver/gold と スコア済みテーブル';
CREATE VOLUME IF NOT EXISTS fraud_demo.fraud.raw
  COMMENT '生データCSVの配置先（Lakeflowの入力）';

-- 2) 業務ロール（グループは事前にアカウントで作成しておく想定）
--    fraud_analysts: レビュー担当（判断を行う） / fraud_viewers: 参照のみ
GRANT USE CATALOG ON CATALOG fraud_demo TO `fraud_analysts`;
GRANT USE SCHEMA, SELECT ON SCHEMA fraud_demo.fraud TO `fraud_analysts`;
GRANT USE CATALOG ON CATALOG fraud_demo TO `fraud_viewers`;
GRANT USE SCHEMA, SELECT ON SCHEMA fraud_demo.fraud TO `fraud_viewers`;

-- 3) PIIマスキング関数：顧客IDはアナリスト以外には下4桁のみ表示
CREATE OR REPLACE FUNCTION fraud_demo.fraud.mask_customer(id STRING)
RETURN CASE
         WHEN is_account_group_member('fraud_analysts') THEN id
         ELSE concat('C******', substr(id, -3))
       END;

-- 4) スコア済みテーブルへ列マスク適用（Lakeflow gold からモデルが書き出す想定）
--    ここではマスキングの付与方法を示す（テーブルは 03/04 で生成）。
ALTER TABLE fraud_demo.fraud.scored_transactions
  ALTER COLUMN customer_id SET MASK fraud_demo.fraud.mask_customer;

-- 5) リネージ確認（UI: Catalog Explorer > lineage、SQLでは system.access を参照）
--    Lakeflow: raw(volume) -> bronze -> silver -> gold -> scored -> Lakebase同期
SELECT * FROM system.access.table_lineage
WHERE target_table_name = 'gold_features'
ORDER BY event_time DESC LIMIT 20;
