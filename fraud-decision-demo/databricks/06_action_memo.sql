-- ============================================================================
-- ⑦ GenAI 対応メモ — SHAP理由コード＋EV をアナリスト向けの短い行動メモに変換
--   判断そのもの（ブロック/承認）はクラシックML＋EV が担い、LLM は「説明文の整形」だけを担う。
--   入力は UC 管理下の scored_transactions のみ（グラウンディング）、出力も UC の Delta テーブルに記録し、
--   Lakebase に同期して App のコックピットで理由コードと並べて表示する。
--   deploy/deploy.py が `fraud_demo` と {{LLM_ENDPOINT}} を置換して実行する。
-- ============================================================================

CREATE OR REPLACE TABLE fraud_demo.fraud.action_memos
  COMMENT 'ブロック候補・レビュー上位取引の対応メモ（ai_query で生成、入力は理由コードとEVのみ）'
  TBLPROPERTIES (delta.enableChangeDataFeed = true)
AS
WITH top AS (
  SELECT txn_id, amount, payee_country, fraud_score, decision_policy, reason_codes,
         ROUND(fraud_score * amount - (1 - fraud_score) * 3200, 0) AS ev_block_jpy
  FROM fraud_demo.fraud.scored_transactions
  WHERE decision_policy IN ('BLOCK_CANDIDATE', 'REVIEW')
  ORDER BY fraud_score DESC
  LIMIT 50
)
SELECT txn_id, fraud_score, ev_block_jpy,
       ai_query(
         '{{LLM_ENDPOINT}}',
         concat(
           'あなたは銀行の不正送金調査アナリストを支援するアシスタントです。',
           '以下の取引データだけを根拠に、日本語で3文以内の対応メモを書いてください。',
           '1文目: 推奨アクション（EVが正ならブロック、負なら承認寄り）。',
           '2文目: 主な理由（理由コードの label を使い、寄与の大きい順）。',
           '3文目: 顧客への確認事項を1つ。データにない事実を推測で足さないこと。',
           '\n取引ID: ', txn_id,
           '\n送金額(円): ', CAST(ROUND(amount, 0) AS STRING),
           '\n宛先国: ', payee_country,
           '\n不正スコア: ', CAST(ROUND(fraud_score, 3) AS STRING),
           '\n判定ポリシー: ', decision_policy,
           '\nブロック期待価値EV(円): ', CAST(ev_block_jpy AS STRING),
           '\n理由コード(SHAP寄与, JSON): ', reason_codes)
       ) AS memo,
       '{{LLM_ENDPOINT}}' AS model_endpoint,
       current_timestamp() AS generated_at
FROM top;

SELECT txn_id, ROUND(fraud_score, 3) AS fraud_score, ev_block_jpy, memo
FROM fraud_demo.fraud.action_memos
ORDER BY fraud_score DESC
LIMIT 5;
