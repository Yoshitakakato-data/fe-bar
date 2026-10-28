# Genie Space 実会話ログ（Databricks 上で実行）

- ワークスペース: `https://fevm-serverless-stable-h87e9w.cloud.databricks.com`（FEVM, AWS us-east-1）
- Genie Space ID: `01f1d2afeea5141fbac4c8debfbe8cd7` / SQL Warehouse: `Serverless Starter Warehouse`
- 対象テーブル: `serverless_stable_h87e9w_catalog.fraud.scored_transactions`（Unity Catalog 列マスク・行フィルタ適用済み）
- 取得日時: 2026-10-28 18:16（API: start-conversation / messages / query-result）
- SQL は Genie が自然言語から生成したもの（人手で書いた SQL ではない）。生データは `evidence/databricks/09_genie_conversations.json`。

## Q1: 判定ポリシー別の件数と平均不正スコア、合計金額は？

**Genie（解釈）:** 判定ポリシーごとに、取引の件数、平均不正スコア、合計金額を知りたいということですね。

```sql
SELECT `decision_policy`, COUNT(*) AS `txn_count`, AVG(`fraud_score`) AS `avg_fraud_score`, SUM(`amount`) AS `total_amount` FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions` WHERE `decision_policy` IS NOT NULL AND `fraud_score` IS NOT NULL AND `amount` IS NOT NULL GROUP BY `decision_policy` ORDER BY `decision_policy`
```

| decision_policy | txn_count | avg_fraud_score | total_amount |
|---|---|---|---|
| AUTO_APPROVE | 17387 | 0.008279629393192378 | 9.949145303E8 |
| BLOCK_CANDIDATE | 156 | 0.8576535344465103 | 1.910201543E7 |
| REVIEW | 457 | 0.24101295236412162 | 6.209187642E7 |

**Genie:** 判定ポリシー別では、**AUTO_APPROVE** が **17,387件**・平均不正スコア **0.0083**・合計金額 **994,914,530.3円**、**REVIEW** が **457件**・平均不正スコア **0.2410**・合計金額 **62,091,876.42円**、**BLOCK_CANDIDATE** が **156件**・平均不正スコア **0.8577**・合計金額 **19,102,015.43円** でした。件数と金額は **AUTO_APPROVE** が最大で、平均不正スコアは **BLOCK_CANDIDATE** が最も高く、ポリシーの厳しさに応じてスコアが上がっています。

## Q2: 高リスク国宛でブロック候補になっている送金トップ5を金額順で見せて

**Genie（解釈）:** 高リスク国宛で不正スコアが0.60以上のブロック候補となっている送金の中から、金額が大きい順に上位5件の取引情報を見せてほしいということですね。

```sql
WITH `ranked_txns` AS (
  SELECT
    `txn_id`,
    `customer_id`,
    `event_ts`,
    `amount`,
    `payee_country`,
    `channel`,
    `fraud_score`,
    `decision_policy`,
    `reason_codes`,
    `review_status`,
    RANK() OVER (ORDER BY `amount` DESC) AS `amount_rank`
  FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions`
  WHERE `is_high_risk_country` = 1
    AND `fraud_score` >= 0.60
    AND `amount` IS NOT NULL
)
SELECT
  `txn_id`,
  `customer_id`,
  `event_ts`,
  `amount`,
  `payee_country`,
  `channel`,
  `fraud_score`,
  `decision_policy`,
  `reason_codes`,
  `review_status`
FROM `ranked_txns`
WHERE `amount_rank` <= 5
ORDER BY `amount` DESC, `txn_id`
```

| txn_id | customer_id | event_ts | amount | payee_country | channel | fraud_score | decision_policy | reason_codes | review_status |
|---|---|---|---|---|---|---|---|---|---|
| T0000174 | C******452 | 1700120466 | 2081681.24 | XB | web | 0.9925865026443997 | BLOCK_CANDIDATE | [{"feature": "num_txn_24h", "label": "短時間に取引が集中（高頻度）", "contribution": 8.8188, "value": 11.0}, {"feature": "amount_to_avg_ratio", "label": "送金額が平常時の平均を大きく上回る", "contribution": 2.7389, "value": 24.0006}, {"feature": "is_foreign", "label": "海外への送金", "contribution": 1.0663, "value": 1.0}] | PENDING |
| T0015657 | C******605 | 1705363868 | 831119.03 | XC | mobile | 0.8203552809331941 | BLOCK_CANDIDATE | [{"feature": "num_txn_24h", "label": "短時間に取引が集中（高頻度）", "contribution": 7.4824, "value": 10.0}, {"feature": "amount_to_avg_ratio", "label": "送金額が平常時の平均を大きく上回る", "contribution": 2.0215, "value": 13.6762}, {"feature": "device_change", "label": "普段と異なる端末からの操作", "contribution": 0.6561, "value": 1.0}] | PENDING |
| T0057442 | C******011 | 1705860762 | 813335.39 | XC | mobile | 0.9288507561191414 | BLOCK_CANDIDATE | [{"feature": "amount_to_avg_ratio", "label": "送金額が平常時の平均を大きく上回る", "contribution": 5.1135, "value": 10.4081}, {"feature": "num_txn_24h", "label": "短時間に取引が集中（高頻度）", "contribution": 2.6996, "value": 2.0}, {"feature": "device_change", "label": "普段と異なる端末からの操作", "contribution": 1.9266, "value": 1.0}] | PENDING |
| T0045088 | C******024 | 1703623786 | 521520.23 | XC | web | 0.8084227043465608 | BLOCK_CANDIDATE | [{"feature": "amount_to_avg_ratio", "label": "送金額が平常時の平均を大きく上回る", "contribution": 6.151, "value": 21.472}, {"feature": "num_txn_24h", "label": "短時間に取引が集中（高頻度）", "contribution": 2.8286, "value": 2.0}, {"feature": "is_foreign", "label": "海外への送金", "contribution": 1.1998, "value": 1.0}] | PENDING |
| T0021055 | C******011 | 1700637721 | 374217.15 | XC | mobile | 0.9258163522328867 | BLOCK_CANDIDATE | [{"feature": "amount_to_avg_ratio", "label": "送金額が平常時の平均を大きく上回る", "contribution": 5.7539, "value": 8.7226}, {"feature": "num_txn_24h", "label": "短時間に取引が集中（高頻度）", "contribution": 2.5594, "value": 2.0}, {"feature": "device_change", "label": "普段と異なる端末からの操作", "contribution": 1.9934, "value": 1.0}] | PENDING |

**Genie:** 高リスク国宛かつ**ブロック候補**の送金は、金額順で以下の**5件**です。金額は**37.4万円〜208.2万円**で、**全件が審査待ち**、宛先国は**XCが4件、XBが1件**でした。

- **T0000174**: **2,081,681.24円**、宛先 **XB**、チャネル **web**、fraud_score **0.9926**
- **T0015657**: **831,119.03円**、宛先 **XC**、チャネル **mobile**、fraud_score **0.8204**
- **T0057442**: **813,335.39円**、宛先 **XC**、チャネル **mobile**、fraud_score **0.9289**
- **T0045088**: **521,520.23円**、宛先 **XC**、チャネル **web**、fraud_score **0.8084**
- **T0021055**: **374,217.15円**、宛先 **XC**、チャネル **mobile**、fraud_score **0.9258**

## Q3: 海外送金と国内で、レビュー帯（スコア0.10以上）の的中率を比較して

**Genie（解釈）:** 海外送金と国内送金のうち、レビュー帯（不正スコアが0.10以上）の取引件数と、その中で実際に不正だった割合（的中率）を比較しています。

```sql
SELECT CASE WHEN `is_foreign` = 1 THEN '海外送金' WHEN `is_foreign` = 0 THEN '国内' END AS `送金区分`, COUNT(*) AS `レビュー帯件数`, AVG(`is_fraud`) AS `的中率` FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions` WHERE `fraud_score` >= 0.10 AND `is_foreign` IS NOT NULL AND `is_fraud` IS NOT NULL GROUP BY `is_foreign` ORDER BY `is_foreign`
```

| 送金区分 | レビュー帯件数 | 的中率 |
|---|---|---|
| 国内 | 236 | 0.2627118644067797 |
| 海外送金 | 377 | 0.4403183023872679 |

**Genie:** レビュー帯（スコア**0.10以上**）の的中率は、**海外送金の方が高い**です。**国内**は236件で的中率**26.3%**、**海外送金**は377件で的中率**44.0%**でした。件数も的中率も海外送金が上回っており、このレビュー帯では海外送金の方が不正に当たる割合が高い結果です。

## Q3 — フォローアップ: その海外送金のうち、高リスク国宛だけに絞ると的中率はどうなる？

**Genie（解釈）:** You want to see the number of reviewed foreign transactions that are sent to high-risk countries and the precision rate of fraud detection among them.

```sql
SELECT COUNT(*) AS `レビュー帯件数`, AVG(`is_fraud`) AS `的中率` FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions` WHERE `fraud_score` >= 0.10 AND `is_foreign` = 1 AND `is_high_risk_country` = 1 AND `is_fraud` IS NOT NULL
```

| レビュー帯件数 | 的中率 |
|---|---|
| 263 | 0.49809885931558934 |

**Genie:** 海外送金のうち高リスク国宛に絞ると、レビュー帯（fraud_score **0.10以上**）の**的中率は0.4981（約49.8%）**で、対象件数は**263件**です。今回の結果では、高リスク国宛のレビュー帯263件のうち、ほぼ**2件に1件**が実際に不正でした。

## Q4: 時間帯別の不正発生件数は？深夜に偏っている？

**Genie（解釈）:** あなたは時間帯ごとの不正取引件数を見たいです。特に、深夜（0時から5時まで）に不正が多いかどうかを確認したいと考えています。

```sql
WITH `fraud_by_hour` AS (
  SELECT
    `hour_of_day`,
    COUNT(*) AS `fraud_count`
  FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions`
  WHERE `is_fraud` = 1
    AND `hour_of_day` IS NOT NULL
  GROUP BY `hour_of_day`
),
`totals` AS (
  SELECT SUM(`fraud_count`) AS `total_fraud_count`
  FROM `fraud_by_hour`
),
`late_night_summary` AS (
  SELECT
    CASE WHEN `hour_of_day` BETWEEN 0 AND 5 THEN 'late_night_0_5' ELSE 'other_hours' END AS `time_bucket`,
    SUM(`fraud_count`) AS `fraud_count`
  FROM `fraud_by_hour`
  GROUP BY CASE WHEN `hour_of_day` BETWEEN 0 AND 5 THEN 'late_night_0_5' ELSE 'other_hours' END
)
SELECT
  'hourly' AS `result_type`,
  CAST(`f`.`hour_of_day` AS STRING) AS `time_segment`,
  `f`.`fraud_count` AS `fraud_count`,
  try_divide(`f`.`fraud_count`,`t`.`total_fraud_count`) AS `fraud_share`
FROM `fraud_by_hour` `f`
CROSS JOIN `totals` `t`
UNION ALL
SELECT
  'bucket_summary' AS `result_type`,
  `l`.`time_bucket` AS `time_segment`,
  `l`.`fraud_count` AS `fraud_count`,
  try_divide(`l`.`fraud_count`,`t`.`total_fraud_count`) AS `fraud_share`
FROM `late_night_summary` `l`
CROSS JOIN `totals` `t`
ORDER BY `result_type`, `time_segment`
```

| result_type | time_segment | fraud_count | fraud_share |
|---|---|---|---|
| bucket_summary | late_night_0_5 | 117 | 0.29545454545454547 |
| bucket_summary | other_hours | 279 | 0.7045454545454546 |
| hourly | 0 | 15 | 0.03787878787878788 |
| hourly | 1 | 23 | 0.05808080808080808 |
| hourly | 10 | 15 | 0.03787878787878788 |
| hourly | 11 | 14 | 0.03535353535353535 |
| hourly | 12 | 21 | 0.05303030303030303 |
| hourly | 13 | 13 | 0.03282828282828283 |
| hourly | 14 | 15 | 0.03787878787878788 |
| hourly | 15 | 19 | 0.047979797979797977 |

**Genie:** 不正発生は合計**396件**で、このうち深夜帯（0〜5時）は**117件（29.5%）**、それ以外の時間帯は**279件（70.5%）**でした。したがって、**深夜に偏っているとは言えません**。

代表的な時間帯は以下の通りです:
- **22時**: **25件**
- **2時**: **24件**
- **3時**: **24件**
- **1時**: **23件**
- **17時**: **22件**

時間帯別では**22時が最多**で、**6時が5件で最少**でした。

**Genie:** 深夜の定義を0時から5時以外に変更して集計しましょうか？例えば22時から翌朝6時までなど。

## Q5: 端末変更ありの取引は、なしと比べて不正率が何倍高い？

**Genie（解釈）:** You want to see how many transactions involved a device change and how many did not, along with the fraud rates for each group, and then find out how many times higher the fraud rate is when there is a device change compared to when there isn't.

```sql
WITH fraud_rates AS (
  SELECT
    `device_change`,
    COUNT(*) AS `txn_count`,
    AVG(`is_fraud`) AS `fraud_rate`
  FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions`
  WHERE `device_change` IS NOT NULL
    AND `device_change` IN (0, 1)
  GROUP BY `device_change`
)
SELECT
  MAX(CASE WHEN `device_change` = 1 THEN `txn_count` END) AS `txn_count_with_device_change`,
  MAX(CASE WHEN `device_change` = 1 THEN `fraud_rate` END) AS `fraud_rate_with_device_change`,
  MAX(CASE WHEN `device_change` = 0 THEN `txn_count` END) AS `txn_count_without_device_change`,
  MAX(CASE WHEN `device_change` = 0 THEN `fraud_rate` END) AS `fraud_rate_without_device_change`,
  try_divide(MAX(CASE WHEN `device_change` = 1 THEN `fraud_rate` END),NULLIF(MAX(CASE WHEN `device_change` = 0 THEN `fraud_rate` END), 0)) AS `fraud_rate_multiple`
FROM fraud_rates;
```

| txn_count_with_device_change | fraud_rate_with_device_change | txn_count_without_device_change | fraud_rate_without_device_change | fraud_rate_multiple |
|---|---|---|---|---|
| 1128 | 0.17819148936170212 | 16872 | 0.011557610241820768 | 15.417675941080196 |

**Genie:** 端末変更ありの取引の不正率は **0.17819148936170212（約17.82%）**、端末変更なしの取引の不正率は **0.011557610241820768（約1.16%）** で、端末変更ありの不正率は **15.417675941080196倍** 高いです。件数ベースでは、端末変更ありが **1,128件**、端末変更なしが **16,872件** で、端末変更ありは明確に高い不正率が確認できます。

## Q6: いまPENDINGのブロック候補は何件あって、合計いくら止められる可能性がある？

**Genie（解釈）:** いまレビュー状態がPENDINGのブロック候補取引の件数と、その合計金額（円）を知りたいということですね。

```sql
SELECT COUNT(*) AS `pending_block_candidates`, COALESCE(SUM(`amount`), 0) AS `total_amount_yen`
FROM `serverless_stable_h87e9w_catalog`.`fraud`.`scored_transactions`
WHERE `review_status` IS NOT NULL
  AND `review_status` ILIKE '%PENDING%'
  AND `fraud_score` >= 0.60;
```

| pending_block_candidates | total_amount_yen |
|---|---|
| 156 | 1.910201543E7 |

**Genie:** いま **PENDING** のブロック候補は **156件** で、止められる可能性がある合計金額は **19,102,015.43円** です。今回の結果では、**1件あたりの平均は約122,449円** です。

