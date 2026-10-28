# 不正送金レビュー支援 — エンドツーエンド・データジャーニー（金融 / クラシックML）

> **業界**: 金融サービス（リテールバンキング／送金不正対策）
> **顧客課題**: 不正検知モデルの予測が業務ユーザに理解しにくく、「本当に送金を止めるべきか」の
> 最終判断が**属人化**している。誤検知で正当な顧客の送金を止めれば摩擦・離反を招き、
> 見逃せば損失。判断のばらつきと調査工数が増大している。
> **解決**: クラシックML（勾配ブースティング）の予測に**SHAP の理由コード（なぜ止めるべきか）**を付け、
> **しきい値ポリシー**で判断を標準化し、**Databricks App** 上で「理由＋期待金額価値(EV)＋判断」を1画面に。
> **Genie** で全体傾向を日本語探索。**属人的判断を、説明可能で標準化された運用へ。**

| 誰のための価値か | 役職 | その人の評価指標（スコアカード） | 本デモが動かす数値 |
|---|---|---|---|
| エグゼクティブ・スポンサー | **CRO（最高リスク責任者）** | 不正損失額・顧客摩擦コスト・規制当局/内部監査の指摘件数 | ネット **+1.0億円/年**、摩擦コスト **−2.3億円/年**、全判断に理由コード（監査証跡） |
| ドメインオーナー | **不正対策部長（Head of Fraud Operations）** | アラート処理件数・1件あたり処理時間・誤検知率・判断のばらつき | レビュー **−91%**、**14→5分/件**、誤検知 **−99.8%**、判断標準化率 **97.7%** |

**2層で実行済み**：
1. **Databricks 実ワークスペース（FEVM）で全コンポーネントを実行**し、API 応答を証跡としてコミット → [`evidence/databricks/`](evidence/databricks/README.md)
2. 同じロジックの**ローカル版**（Python3 + numpy のみ）も実行し、出力をコミット → `outputs/`, `logs/`

---

## 🧭 6コンポーネントのマッピング（要件充足）

| 要件 | 本デモの中身 | Databricks 実行証跡 | 実装 |
|---|---|---|---|
| **Lakeflow**（取り込み） | 合成送金生データを bronze→silver→gold に宣言的変換＋期待値チェック | [`03_lakeflow_pipeline.json`](evidence/databricks/03_lakeflow_pipeline.json)：COMPLETED、各60,000行、期待値違反0 | `databricks/01_lakeflow_ingest.py` |
| **Unity Catalog**（統制） | スキーマ/Volume/**列マスク（顧客ID）**/**行フィルタ（海外送金は海外チームのみ）**/リネージ | [`05_unity_catalog_part_b.json`](evidence/databricks/05_unity_catalog_part_b.json)：所属前 海外0行→所属後 2,034行、[`10_final_verification.json`](evidence/databricks/10_final_verification.json)：リネージ | `databricks/02_unity_catalog_setup.sql` |
| **クラシックML**（知能化） | GBDT＋**SHAP 理由コード**、MLflow→UC モデル登録 | [`04_ml_training.json`](evidence/databricks/04_ml_training.json)：ROC-AUC 0.9034、加法性誤差 1.5e-11、`fraud_gbdt` v1 | `databricks/03_train_model.py` |
| **Lakebase**（運用） | 判断が必要な取引を Lakebase Autoscaling（Postgres）へ synced table で同期、App が読み書き | [`07_lakebase_sync.json`](evidence/databricks/07_lakebase_sync.json)：Postgres 613行 = Delta 613行、ONLINE | `databricks/04_lakebase_sync.md`, `07_lakebase_sql.py` |
| **Genie**（自然言語） | Genie Space を作成し、業務の日本語質問6問＋フォローアップを**実会話**。SQL は Genie が生成 | [`outputs/genie_live_transcript.md`](outputs/genie_live_transcript.md)、[`09_genie_conversations.json`](evidence/databricks/09_genie_conversations.json) | `databricks/05_genie_space_setup.md` |
| **Databricks Apps**（公開） | レビュー支援コックピット（理由コード＋EV＋**LLM対応メモ**＋判断の書き戻し） | [`08_databricks_app.json`](evidence/databricks/08_databricks_app.json)：SUCCEEDED、起動時自己チェックで Lakebase 読取・書込 | `app/app.py` |
| ＋ GenAI（発展） | `ai_query` で理由コード＋EV を3文の対応メモに整形（判断は ML＋EV のまま） | [`06_llm_action_memo.json`](evidence/databricks/06_llm_action_memo.json)：50件生成 | `databricks/06_action_memo.sql` |

---

## 💬 Genie の実回答（Databricks 上の実会話より）

| 業務の質問（日本語） | Genie の回答 | 運用への示唆 |
|---|---|---|
| 海外送金と国内で、レビュー帯の的中率を比較して | 海外 **44.0%** vs 国内 **26.3%** | レビュー資源を海外送金に厚く |
| → 高リスク国宛だけに絞ると？（フォローアップ） | **49.8%**（263件） | 高リスク国宛は2件に1件が実不正。優先キュー化 |
| 端末変更ありは不正率が何倍？ | **15.4倍** | 端末変更時の追加認証ルールの根拠 |
| 深夜に偏っている？ | 0〜5時は不正の29.5%で**偏りは限定的**、最多は22時 | 「深夜だから怪しい」という属人ルールの見直し |
| PENDING のブロック候補は何件・いくら？ | **156件・約1,910万円** | 当日の処理優先度の判断材料 |

---

## 📊 ビジネス成果（年換算 / `outputs/business_impact.txt` より）

現状（広いルール＋理由なしの属人レビュー） vs 提案（ML＋理由コード＋しきい値ポリシー）:

| KPI | インパクト | 主に効く役職 |
|---|---|---|
| **ネット金額改善** | **約 +1.0億円 / 年** | CRO |
| **顧客摩擦コスト削減**（誤検知） | 約 2.3億円 / 年（誤検知 約7.3万件/年 削減） | CRO |
| **調査時間削減** | 約 1.7万時間 / 年（レビュー 5,719→497件、-91%） | 不正対策部長 |
| **判断標準化率** | **97.7%**（しきい値で機械的に確定、残りのみ人が理由コード付きで判断） | 不正対策部長 |
| モデル性能 | ROC-AUC ≈ 0.89（ローカル）/ 0.90（Databricks） | — |

> 数値の前提（誤検知コスト・調査分数・人件費）は `src/common.py` に一元化。合成データに基づく。
> 年換算KPIはローカル版ホールドアウトで算出（Databricks 版との差は [`evidence/databricks/README.md`](evidence/databricks/README.md#ローカル版との数値の違い)）。

---

## ▶️ 再現

**Databricks（実ワークスペース）**
```bash
databricks auth login --host <workspace-url> -p <profile>
python3 databricks/deploy/deploy.py --profile <profile>   # UC→Lakeflow→ML→ガバナンス→LLMメモ→Lakebase→App→Genie→最終確認
```

**ローカル（依存: Python3 + numpy のみ）**
```bash
./run_all.sh               # 全工程を実行し、logs/run_all.log と outputs/ を再生成
python3 app/local_app.py   # レビュー支援アプリ -> http://127.0.0.1:8000
```

## 🛠️ 作り方と試行錯誤
AI（Isaac / Claude Code）との進め方、実際に起きた失敗と修正、トレードオフは [`BUILD_NOTES.md`](BUILD_NOTES.md)。

## 📑 プレゼン資料
`presentation/deck.pdf`（16:9。ビジネス成果を冒頭に、KPI定量化、CRO／不正対策部長それぞれの価値、Databricks 実行証跡）。
編集元は `presentation_src/deck.html`。修正後は Chrome headless で再出力:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-pdf-header-footer \
  --print-to-pdf=presentation/deck.pdf "file://$PWD/presentation_src/deck.html"
```

## ⚠️ データについて
すべて**合成データ**（`src/generate_data.py`）。実在の顧客・PII・実データは一切含まない。

## 📂 構成
```
src/              ローカル実装（データ生成・パイプライン・ML・理由コード・KPI・Genie相当）
app/              local_app.py(ローカル) / app.py+app.yaml(Databricks Apps, Lakebase接続)
databricks/       Lakeflow/UC/ML(SHAP)/Lakebase/Genie/LLMメモ のネイティブ実装
databricks/deploy/ 実ワークスペースへのデプロイ＋証跡取得スクリプト
evidence/         Databricks 実行証跡（API応答・実行ログ）★
outputs/          ローカル実行結果＋ Genie 実会話ログ（genie_live_transcript.md）★
logs/             run_all.log（ローカル全工程の実行ログ）
data/             合成データと運用ストア(sqlite)
presentation/     経営層向けデッキ（PDF）
presentation_src/ デッキの編集元HTML
```
