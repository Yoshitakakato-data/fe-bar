# ⑤ Genie Room — 自然言語で不正アラートを探索

業務ユーザ（不正調査アナリスト／リスク管理部門）が **日本語で質問**し、Genie が
`scored_transactions` に対する SQL を生成して回答する。SHAP理由コード（モデルの「なぜ」）と
役割分担し、Genie は **集計・傾向・セグメント探索の「なぜ全体でこうなっているか」** を担う。

**実行済み**: `deploy/deploy.py --steps genie` が Genie Space を API（`POST /api/2.0/genie/spaces`）で作成し、
下の6問＋フォローアップを `start-conversation` / `messages` で投げ、**Genie が生成した SQL と回答**を
`../outputs/genie_live_transcript.md`（生データ: `../evidence/databricks/09_genie_conversations.json`）に保存している。

参考（ローカル版）: `src/genie_queries.py` は同じ質問を人手の SQL で sqlite に投げるオフライン用の代替で、Genie ではない。

---

## Genie Space 設定

1. Databricks UI → **Genie → New Space**
2. 対象テーブル: `fraud_demo.fraud.scored_transactions`（＋必要なら `gold_features`）
3. SQL Warehouse: Serverless を指定
4. **一般指示（Instructions）** に業務文脈を与える：

```
このスペースは銀行の不正送金レビューを支援する。
- fraud_score は不正確率(0-1)。decision_policy は AUTO_APPROVE/REVIEW/BLOCK_CANDIDATE。
- reason_codes は各取引がなぜ高リスクかを説明するJSON（SHAP寄与）。
- is_foreign=1 は海外送金、is_high_risk_country=1 は高リスク国宛。
- 「的中率(precision)」= レビュー対象のうち実際に不正だった割合 = AVG(is_fraud)。
- 金額は円。ユーザは不正調査アナリストとリスク管理者。
```

5. **サンプル質問（Sample Questions）** に以下を登録（`src/genie_queries.py` と同一）：

| # | 自然言語の質問 |
|---|---|
| Q1 | 判定ポリシー別の件数と平均不正スコア、合計金額は？ |
| Q2 | 高リスク国宛でブロック候補の送金トップ5を金額順で |
| Q3 | 海外送金と国内で、レビュー帯の的中率(precision)を比較して |
| Q4 | 時間帯別の不正発生件数は？深夜に偏っている？ |
| Q5 | 端末変更ありの取引は、なしと比べて不正率が何倍高い？ |
| Q6 | いまPENDINGのブロック候補は何件、合計いくら止められる？ |

6. **Certified Answer** に Q1/Q3 を登録し、経営報告で使う定義を固定する。

---

## 実会話の結果（抜粋）
- Q3 海外 vs 国内のレビュー帯的中率: **44.0% vs 26.3%** → フォローアップ「高リスク国宛だけでは？」: **49.8%**
- Q5 端末変更ありの不正率: **15.4倍**
- Q4 深夜（0〜5時）の不正: 全体の **29.5%** で「偏っているとは言えない」（最多は22時）
- Q6 PENDING のブロック候補: **156件・約1,910万円**

Genie は列マスク・行フィルタ適用済みの `scored_transactions` を、質問したユーザの権限で読む
（顧客IDはマスクされたまま回答に出る＝ガバナンスが自然言語の経路にも効いている）。
