"""共通設定：パス・特徴量定義・ビジネスコストパラメータ・しきい値。

このファイルの定数は「ローカル版」と「Databricks移植版」の両方で同じ意味を持つ。
Databricks側では同じ列名・同じしきい値・同じコスト前提を使うことで、
ローカルで得た数値がそのまま説明可能になる。
"""
from __future__ import annotations
import os

# ---- パス ----
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "outputs")
LOG_DIR = os.path.join(ROOT, "logs")
LAKEBASE_DB = os.path.join(DATA_DIR, "lakebase.sqlite")  # 運用ストア(Lakebase相当)

for _d in (DATA_DIR, OUT_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

# ---- 再現性 ----
SEED = 42

# ---- 特徴量（モデル入力）----
# 業務ユーザに説明しやすい名前にしてある（理由コードにそのまま出る）
FEATURES = [
    "amount_to_avg_ratio",   # 普段の平均送金額に対する倍率
    "amount_log",            # 送金額(log)
    "is_new_payee",          # 初めての送金先か
    "is_foreign",            # 海外送金か
    "is_high_risk_country",  # 高リスク国宛か
    "hour_risk",             # 深夜帯など時間帯のリスク度(0-1)
    "num_txn_24h",           # 直近24時間の取引件数(velocity)
    "device_change",         # 普段と違う端末か
    "account_age_days",      # 口座開設からの日数
    "sec_since_last_txn",    # 直前取引からの経過秒(log)
]

# 理由コードで人間可読ラベルに変換する辞書
FEATURE_LABELS_JP = {
    "amount_to_avg_ratio": "送金額が平常時の平均を大きく上回る",
    "amount_log": "送金額が高額",
    "is_new_payee": "初めての送金先",
    "is_foreign": "海外への送金",
    "is_high_risk_country": "高リスク国宛の送金",
    "hour_risk": "普段利用しない時間帯（深夜など）",
    "num_txn_24h": "短時間に取引が集中（高頻度）",
    "device_change": "普段と異なる端末からの操作",
    "account_age_days": "口座開設からの経過期間が短い",
    "sec_since_last_txn": "直前取引からの間隔が異常",
}

# ---- 意思決定しきい値（不正確率）----
# レビュー運用：>= BLOCK_THRESHOLD は「要ブロック検討」、
# >= REVIEW_THRESHOLD は「アナリストレビュー」、それ未満は自動承認。
REVIEW_THRESHOLD = 0.10
BLOCK_THRESHOLD = 0.60

# ---- ビジネスコスト前提（円）----
# エグゼクティブに説明する数値の根拠。すべてここで一元管理。
FALSE_POSITIVE_COST_JPY = 3200      # 誤検知1件あたりの顧客摩擦コスト(問合せ対応+機会損失+離反リスク)
ANALYST_MIN_BASELINE = 14.0         # 理由コードなしでの1件あたり調査時間(分)
ANALYST_MIN_WITH_REASONS = 5.0      # SHAP理由コードありでの1件あたり調査時間(分)
ANALYST_COST_PER_MIN_JPY = 60       # アナリスト人件費(円/分) 相当
RECOVERY_RATE = 1.0                 # ブロックできた不正送金額の回収率(=阻止額)

# 年換算係数（デモの生成期間 -> 年間）。生成は90日想定 -> x (365/90)
ANNUALIZE = 365.0 / 90.0
