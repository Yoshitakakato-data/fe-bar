# 不正送金レビュー支援 — エンドツーエンド・データジャーニー（金融 / クラシックML）

> **業界**: 金融サービス（リテールバンキング）
> **顧客課題**: 不正検知モデルの予測が業務ユーザに理解しにくく、「本当に送金を止めるべきか」の
> 最終判断が**属人化**している。誤検知で正当な顧客の送金を止めれば摩擦・離反を招き、
> 見逃せば損失。判断のばらつきと調査工数が増大している。
> **解決**: クラシックML（勾配ブースティング）の予測に**SHAP相当の理由コード（なぜ止めるべきか）**を付け、
> **しきい値ポリシー**で判断を標準化し、**Databricks App** 上で「理由＋期待金額価値(EV)＋判断」を1画面に。
> **Genie** で全体傾向を日本語探索。**属人的判断を、説明可能で標準化された運用へ。**

これは6コンポーネントを一気通貫でつないだプロトタイプ。**ローカルで実際に実行し、出力をテキストでコミット**してある（`outputs/`, `logs/`）。同一ロジックの **Databricks ネイティブ版**（`databricks/`, `app/app.py`）も同梱。

---

## 🧭 6コンポーネントのマッピング（要件充足）

| 要件 | 本デモの中身 | 実行証拠（コミット済みテキスト） |
|---|---|---|
| **Lakeflow**（取り込み） | 合成送金生データを bronze→silver→gold に宣言的変換 | `logs/run_all.log`（pipeline行）、`databricks/01_lakeflow_ingest.py` |
| **Unity Catalog**（統制） | カタログ/スキーマ/Volume/PIIマスキング/権限/リネージ | `databricks/02_unity_catalog_setup.sql` |
| **Lakebase**（運用） | スコア＋理由コードを低遅延ストアへ同期、Appが読み書き | `data/lakebase.sqlite`、`outputs/app_demo_output.txt`、`databricks/04_lakebase_sync.md` |
| **クラシックML**（知能化） | 勾配ブースティング＋**加法的な理由コード**（SHAP相当） | `outputs/model_metrics.txt`、`outputs/reason_codes_examples.txt` |
| **Genie Room**（自然言語） | 業務ユーザの日本語質問6問→SQL→実データ回答 | `outputs/genie_query_results.txt`、`databricks/05_genie_space_setup.md` |
| **Databricks Apps**（公開） | レビュー支援コックピット（理由＋EV＋判断＋KPI） | `outputs/app_demo_output.txt`、`app/local_app.py`、`app/app.py` |

---

## 📊 ビジネス成果（年換算 / `outputs/business_impact.txt` より）

現状（広いルール＋理由なしの属人レビュー） vs 提案（ML＋理由コード＋しきい値ポリシー）:

| KPI | インパクト |
|---|---|
| **ネット金額改善** | **約 +1.0億円 / 年** |
| **顧客摩擦コスト削減**（誤検知） | 約 2.3億円 / 年（誤検知 約7.3万件/年 削減） |
| **調査時間削減** | 約 1.7万時間 / 年（レビュー 5,719→497件、-91%） |
| **判断標準化率** | **97.7%**（しきい値で機械的に確定、残りのみ人が理由コード付きで判断） |
| モデル性能 | ROC-AUC ≈ 0.89（ホールドアウト） |

> 数値の前提（誤検知コスト・調査分数・人件費）は `src/common.py` に一元化。合成データに基づく。

---

## ▶️ ローカルで再現（依存: Python3 + numpy のみ）

```bash
./run_all.sh          # 全工程を実行し、logs/run_all.log と outputs/ を再生成
python3 app/local_app.py   # レビュー支援アプリ -> http://127.0.0.1:8000
```

各ステップ個別実行も可能（`src/generate_data.py` → `pipeline.py` → `train_model.py` →
`business_impact.py` → `load_lakebase.py` → `genie_queries.py`）。

## 🧱 Databricks で動かす
`databricks/README.md` に配備手順と **ローカル↔Databricks の1:1対応表** を記載。

## 📑 プレゼン資料
`presentation/deck.pdf`（16:9・7枚。ビジネス成果を冒頭に、KPI定量化、購入者/スポンサー価値）。
編集元は `presentation_src/deck.html`。修正後は Chrome headless で再出力:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-pdf-header-footer \
  --print-to-pdf=presentation/deck.pdf "file://$PWD/presentation_src/deck.html"
```

## ⚠️ データについて
すべて**合成データ**（`src/generate_data.py`）。実在の顧客・PII・実データは一切含まない。

## 📂 構成
```
src/         ローカル実装（データ生成・パイプライン・ML・理由コード・KPI・Genie）
app/         local_app.py(ローカル) / app.py+app.yaml(Databricks Apps)
databricks/  Lakeflow/UC/ML(SHAP)/Lakebase/Genie のネイティブ実装と配備ガイド
outputs/     実行結果（メトリクス・理由コード・KPI・Genie回答・App証跡）★実行証拠
logs/        run_all.log（全工程の実行ログ）★実行証拠
data/        合成データと運用ストア(sqlite)
presentation/ 経営層向けデッキ（PDF）
presentation_src/ デッキの編集元HTML
```
