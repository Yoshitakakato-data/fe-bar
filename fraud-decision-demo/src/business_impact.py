"""ビジネスインパクト算出：現状(ルール＋属人レビュー) vs 提案(ML＋理由コード)。

同一のホールドアウト取引に対して2つの運用シナリオを適用し、
KPI（調査時間・誤検知による顧客摩擦・不正阻止額・ネット金額インパクト・判断標準化率）
を算出して年換算する。数値の前提はすべて common.py に集約。

出力:
  outputs/business_impact.txt   … エグゼクティブ向けKPIレポート
  outputs/business_impact.json  … App/デッキが読む機械可読サマリ
"""
from __future__ import annotations
import csv
import json
import os

from common import (OUT_DIR, DATA_DIR, REVIEW_THRESHOLD, BLOCK_THRESHOLD,
                    FALSE_POSITIVE_COST_JPY, ANALYST_MIN_BASELINE,
                    ANALYST_MIN_WITH_REASONS, ANALYST_COST_PER_MIN_JPY,
                    RECOVERY_RATE)


def _load():
    with open(os.path.join(DATA_DIR, "scored_transactions.csv")) as f:
        rows = list(csv.DictReader(f))
    # gold全体件数（年換算スケール用）
    with open(os.path.join(DATA_DIR, "gold_features.csv")) as f:
        total = sum(1 for _ in f) - 1
    return rows, total


def _current_rule_flag(r):
    """現状の広めのルールベース検知（低精度・大量アラート）。"""
    return (int(r["is_foreign"]) == 1
            or int(r["is_new_payee"]) == 1
            or float(r["amount_to_avg_ratio"]) >= 3.0
            or int(r["num_txn_24h"]) >= 6
            or int(r["is_high_risk_country"]) == 1)


def main():
    rows, total = _load()
    n = len(rows)
    fraud_total_amt = sum(float(r["amount"]) for r in rows if int(r["is_fraud"]) == 1)

    # ---- 現状: ルールでフラグ→全件を理由コードなしで手レビュー ----
    a_flagged = [r for r in rows if _current_rule_flag(r)]
    a_review_ct = len(a_flagged)
    a_tp_amt = sum(float(r["amount"]) for r in a_flagged if int(r["is_fraud"]) == 1)  # レビューで阻止できる不正額
    a_friction_ct = sum(1 for r in a_flagged if int(r["is_fraud"]) == 0)               # 正当なのに保留=摩擦
    a_time_min = a_review_ct * ANALYST_MIN_BASELINE
    a_friction_cost = a_friction_ct * FALSE_POSITIVE_COST_JPY
    a_analyst_cost = a_time_min * ANALYST_COST_PER_MIN_JPY
    a_net = a_tp_amt * RECOVERY_RATE - a_friction_cost - a_analyst_cost

    # ---- 提案: MLスコア＋しきい値ポリシー＋理由コードで高速レビュー ----
    review_band = [r for r in rows if REVIEW_THRESHOLD <= float(r["fraud_score"])]
    b_review_ct = len(review_band)                                                     # 人が見る件数
    b_tp_amt = sum(float(r["amount"]) for r in review_band if int(r["is_fraud"]) == 1) # 阻止できる不正額
    # 摩擦=自動ブロック帯(>=BLOCK)で正当を止めてしまう件数（理由コードでREVIEW帯の正当は解放）
    b_friction_ct = sum(1 for r in rows
                        if float(r["fraud_score"]) >= BLOCK_THRESHOLD and int(r["is_fraud"]) == 0)
    b_time_min = b_review_ct * ANALYST_MIN_WITH_REASONS
    b_friction_cost = b_friction_ct * FALSE_POSITIVE_COST_JPY
    b_analyst_cost = b_time_min * ANALYST_COST_PER_MIN_JPY
    b_net = b_tp_amt * RECOVERY_RATE - b_friction_cost - b_analyst_cost

    # 判断標準化率: しきい値ポリシーで機械的に決まる割合（自動承認 + 自動ブロック帯）
    auto_ct = sum(1 for r in rows if float(r["fraud_score"]) < REVIEW_THRESHOLD
                  or float(r["fraud_score"]) >= BLOCK_THRESHOLD)
    standardization_rate = auto_ct / n

    scale = (365.0 / 90.0) * (total / n)   # ホールドアウト(90日の30%サンプル) -> 年間全量

    def y(v):  # 年換算
        return v * scale

    kpi = {
        "assumptions": {
            "false_positive_cost_jpy": FALSE_POSITIVE_COST_JPY,
            "analyst_min_baseline": ANALYST_MIN_BASELINE,
            "analyst_min_with_reasons": ANALYST_MIN_WITH_REASONS,
            "analyst_cost_per_min_jpy": ANALYST_COST_PER_MIN_JPY,
            "review_threshold": REVIEW_THRESHOLD,
            "block_threshold": BLOCK_THRESHOLD,
            "holdout_rows": n,
            "annualize_scale": round(scale, 3),
        },
        "current": {
            "review_count": a_review_ct,
            "fraud_blocked_jpy": a_tp_amt,
            "friction_count": a_friction_ct,
            "review_hours": a_time_min / 60.0,
            "friction_cost_jpy": a_friction_cost,
            "analyst_cost_jpy": a_analyst_cost,
            "net_jpy": a_net,
        },
        "proposed": {
            "review_count": b_review_ct,
            "fraud_blocked_jpy": b_tp_amt,
            "friction_count": b_friction_ct,
            "review_hours": b_time_min / 60.0,
            "friction_cost_jpy": b_friction_cost,
            "analyst_cost_jpy": b_analyst_cost,
            "net_jpy": b_net,
            "standardization_rate": standardization_rate,
        },
        "delta_annualized": {
            "review_hours_saved": y((a_time_min - b_time_min) / 60.0),
            "friction_count_reduced": y(a_friction_ct - b_friction_ct),
            "friction_cost_saved_jpy": y(a_friction_cost - b_friction_cost),
            "analyst_cost_saved_jpy": y(a_analyst_cost - b_analyst_cost),
            "net_impact_gain_jpy": y(b_net - a_net),
            "fraud_blocked_proposed_jpy": y(b_tp_amt),
        },
    }

    with open(os.path.join(OUT_DIR, "business_impact.json"), "w") as f:
        json.dump(kpi, f, ensure_ascii=False, indent=2)

    # ---- テキストレポート ----
    def yen(v):
        return f"{v:,.0f}円"

    L = []
    L.append("=" * 72)
    L.append("ビジネスインパクト — 現状(ルール＋属人レビュー) vs 提案(ML＋理由コード)")
    L.append("=" * 72)
    L.append(f"対象: ホールドアウト {n:,}件（90日の30%サンプル）／年換算係数 x{scale:.2f}")
    L.append(f"前提: 誤検知コスト {FALSE_POSITIVE_COST_JPY:,}円/件, "
             f"調査 現状{ANALYST_MIN_BASELINE}分 → 提案{ANALYST_MIN_WITH_REASONS}分/件, "
             f"人件費 {ANALYST_COST_PER_MIN_JPY}円/分")
    L.append(f"しきい値: REVIEW>={REVIEW_THRESHOLD}, BLOCK>={BLOCK_THRESHOLD}")
    L.append("")
    L.append(f"{'指標':<28}{'現状':>18}{'提案':>18}")
    L.append("-" * 72)
    L.append(f"{'人手レビュー件数':<26}{a_review_ct:>18,}{b_review_ct:>18,}")
    L.append(f"{'総調査時間(h)':<28}{a_time_min/60.0:>18,.0f}{b_time_min/60.0:>18,.0f}")
    L.append(f"{'誤検知(顧客摩擦)件数':<24}{a_friction_ct:>18,}{b_friction_ct:>18,}")
    L.append(f"{'不正阻止額':<27}{yen(a_tp_amt):>18}{yen(b_tp_amt):>18}")
    L.append(f"{'誤検知コスト':<26}{yen(a_friction_cost):>18}{yen(b_friction_cost):>18}")
    L.append(f"{'調査人件費':<27}{yen(a_analyst_cost):>18}{yen(b_analyst_cost):>18}")
    L.append(f"{'ネット金額インパクト':<24}{yen(a_net):>18}{yen(b_net):>18}")
    L.append("")
    L.append(f"判断標準化率（しきい値で機械的に確定する割合）: {standardization_rate*100:.1f}%")
    L.append("")
    L.append("―― 年換算のビジネス成果（提案 − 現状）――")
    d = kpi["delta_annualized"]
    L.append(f"  調査時間削減        : {d['review_hours_saved']:,.0f} 時間/年")
    L.append(f"  誤検知件数削減      : {d['friction_count_reduced']:,.0f} 件/年")
    L.append(f"  顧客摩擦コスト削減  : {yen(d['friction_cost_saved_jpy'])}/年")
    L.append(f"  調査人件費削減      : {yen(d['analyst_cost_saved_jpy'])}/年")
    L.append(f"  不正阻止額(提案)    : {yen(d['fraud_blocked_proposed_jpy'])}/年")
    L.append(f"  ネット改善額        : {yen(d['net_impact_gain_jpy'])}/年")

    with open(os.path.join(OUT_DIR, "business_impact.txt"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    return kpi


if __name__ == "__main__":
    main()
