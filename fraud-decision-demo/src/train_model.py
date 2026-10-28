"""モデル学習・評価・スコアリング・理由コード生成。

出力:
  outputs/model_metrics.txt          … 評価指標（AUC/適合率/再現率/混同行列/上位K精度/大域重要度）
  outputs/reason_codes_examples.txt  … 加法的分解が厳密に一致する検算つき理由コード例
  data/scored_transactions.csv       … ホールドアウト取引のスコア＋判定＋理由コード(JSON)
"""
from __future__ import annotations
import csv
import json
import os
import numpy as np

from common import (FEATURES, FEATURE_LABELS_JP, OUT_DIR, DATA_DIR, SEED,
                    REVIEW_THRESHOLD, BLOCK_THRESHOLD)
from gbdt import GBDTClassifier, _sigmoid
from pipeline import load_gold


def roc_auc(y, s):
    """順位法(Mann-Whitney U)でAUCを計算（sklearn不要）。"""
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    # 同値のタイ補正
    _tie_correct(s, order, ranks)
    n_pos = y.sum()
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    sum_ranks_pos = ranks[y == 1].sum()
    auc = (sum_ranks_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
    return float(auc)


def _tie_correct(s, order, ranks):
    ss = s[order]
    i = 0
    n = len(ss)
    while i < n:
        j = i
        while j + 1 < n and ss[j + 1] == ss[i]:
            j += 1
        if j > i:
            avg = (ranks[order[i]] + ranks[order[j]]) / 2.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1


def confusion(y, pred):
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    return tp, fp, tn, fn


def reason_codes(model, x, top_k=3):
    """1件の理由コードを返す。fraud方向(正の寄与)の上位を人間可読ラベルにする。"""
    baseline, contrib = model.explain(x)
    items = sorted(contrib.items(), key=lambda kv: kv[1], reverse=True)
    codes = []
    for fidx, c in items[:top_k]:
        if c <= 0:
            break
        fname = FEATURES[fidx]
        codes.append({
            "feature": fname,
            "label": FEATURE_LABELS_JP[fname],
            "contribution": round(float(c), 4),
            "value": round(float(x[fidx]), 4),
        })
    return baseline, contrib, codes


def main():
    X, y, rows = load_gold()
    rng = np.random.default_rng(SEED)
    idx = rng.permutation(len(y))
    n_test = int(len(y) * 0.3)
    test_idx, train_idx = idx[:n_test], idx[n_test:]
    Xtr, ytr = X[train_idx], y[train_idx]
    Xte, yte = X[test_idx], y[test_idx]

    print(f"[train] train={len(ytr):,} (fraud {ytr.sum()})  test={len(yte):,} (fraud {yte.sum()})")
    model = GBDTClassifier(n_trees=150, learning_rate=0.15, max_depth=3, min_samples_leaf=200)
    model.fit(Xtr, ytr)

    ste = model.predict_proba(Xte)
    auc = roc_auc(yte, ste)

    # しきい値ごとの指標
    lines = []
    lines.append("=" * 68)
    lines.append("不正送金検知モデル — 評価レポート (holdout test)")
    lines.append("=" * 68)
    lines.append(f"アルゴリズム        : Gradient Boosting Decision Trees (numpy実装, logloss)")
    lines.append(f"木の本数/深さ/lr    : 150 / 3 / 0.15")
    lines.append(f"学習/テスト件数     : {len(ytr):,} / {len(yte):,}")
    lines.append(f"テスト不正率        : {100*yte.mean():.2f}%  (件数 {int(yte.sum())})")
    lines.append(f"ROC-AUC             : {auc:.4f}")
    lines.append("")
    lines.append(f"{'threshold':>10} {'precision':>10} {'recall':>10} {'F1':>8} {'TP':>6} {'FP':>6} {'FN':>6}")
    for thr in [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]:
        pred = (ste >= thr).astype(int)
        tp, fp, tn, fn = confusion(yte, pred)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        marker = "  <- BLOCK" if abs(thr - BLOCK_THRESHOLD) < 1e-9 else (
                 "  <- REVIEW" if abs(thr - REVIEW_THRESHOLD) < 1e-9 else "")
        lines.append(f"{thr:>10.2f} {prec:>10.3f} {rec:>10.3f} {f1:>8.3f} {tp:>6} {fp:>6} {fn:>6}{marker}")

    # 上位K精度（アナリストが1日にレビューできる件数の想定）
    lines.append("")
    lines.append("Precision@K （スコア上位K件の不正的中率）:")
    order = np.argsort(-ste)
    for k in [50, 100, 200, 500]:
        topk = order[:k]
        p_at_k = yte[topk].mean()
        lines.append(f"  Precision@{k:<4} = {p_at_k:.3f}  （上位{k}件中 不正 {int(yte[topk].sum())}件）")

    # 大域的な特徴量重要度（|寄与|の平均）
    imp = np.zeros(len(FEATURES))
    sample_for_imp = test_idx[:2000]
    for i in sample_for_imp:
        _, contrib = model.explain(X[i])
        for f, c in contrib.items():
            imp[f] += abs(c)
    imp /= max(1, len(sample_for_imp))
    order_imp = np.argsort(-imp)
    lines.append("")
    lines.append("大域的特徴量重要度（平均|寄与|, logit空間）:")
    for f in order_imp:
        lines.append(f"  {imp[f]:.4f}  {FEATURES[f]:<22} {FEATURE_LABELS_JP[FEATURES[f]]}")

    with open(os.path.join(OUT_DIR, "model_metrics.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))

    # ---- スコアリング＋理由コード（ホールドアウト取引を「レビュー待ち」とみなす）----
    scored_path = os.path.join(DATA_DIR, "scored_transactions.csv")
    fieldnames = ["txn_id", "customer_id", "event_ts", "hour_of_day", "amount",
                  "payee_country", "channel", "amount_to_avg_ratio", "is_new_payee",
                  "is_foreign", "is_high_risk_country", "num_txn_24h", "device_change",
                  "is_fraud", "fraud_score", "decision_policy", "reason_codes"]
    with open(scored_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for j, i in enumerate(test_idx):
            score = float(ste[j])
            _, _, codes = reason_codes(model, X[i])
            if score >= BLOCK_THRESHOLD:
                policy = "BLOCK_CANDIDATE"
            elif score >= REVIEW_THRESHOLD:
                policy = "REVIEW"
            else:
                policy = "AUTO_APPROVE"
            r = rows[i]
            w.writerow({
                "txn_id": r["txn_id"], "customer_id": r["customer_id"],
                "event_ts": r["event_ts"], "hour_of_day": r["hour_of_day"],
                "amount": r["amount"], "payee_country": r["payee_country"],
                "channel": r["channel"],
                "amount_to_avg_ratio": r["amount_to_avg_ratio"],
                "is_new_payee": r["is_new_payee"],
                "is_foreign": r["is_foreign"],
                "is_high_risk_country": r["is_high_risk_country"],
                "num_txn_24h": r["num_txn_24h"],
                "device_change": r["device_change"],
                "is_fraud": r["is_fraud"],
                "fraud_score": round(score, 6),
                "decision_policy": policy,
                "reason_codes": json.dumps(codes, ensure_ascii=False),
            })
    print(f"[train] scored holdout -> {scored_path} ({len(test_idx):,} rows)")

    # ---- 検算つき理由コード例（加法性の証明）----
    ex_lines = []
    ex_lines.append("理由コード例 — 加法的分解の検算 (baseline + Σcontribution = logit)")
    ex_lines.append("=" * 68)
    # 高スコアの不正例と、誤検知しやすい正常例を数件
    hi = order[:6]
    for rank, i_local in enumerate(hi, 1):
        gi = test_idx[i_local]
        x = X[gi]
        baseline, contrib, codes = reason_codes(model, x, top_k=4)
        raw = model.decision_function(x.reshape(1, -1))[0]
        recon = baseline + sum(contrib.values())
        p = float(_sigmoid(np.array([raw]))[0])
        r = rows[gi]
        ex_lines.append("")
        ex_lines.append(f"[例{rank}] txn={r['txn_id']} cust={r['customer_id']} "
                        f"amount={float(r['amount']):,.0f}円 dest={r['payee_country']} "
                        f"実ラベル={'不正' if int(r['is_fraud']) else '正常'}")
        ex_lines.append(f"    fraud_score = {p:.4f}   （logit={raw:.4f}）")
        ex_lines.append(f"    検算: baseline({baseline:.4f}) + Σcontrib({sum(contrib.values()):.4f}) "
                        f"= {recon:.4f}  vs logit {raw:.4f}  -> 誤差 {abs(recon-raw):.2e}")
        ex_lines.append(f"    上位理由:")
        for c in codes:
            ex_lines.append(f"      + {c['contribution']:+.4f}  {c['label']}  (値={c['value']})")
    with open(os.path.join(OUT_DIR, "reason_codes_examples.txt"), "w") as f:
        f.write("\n".join(ex_lines) + "\n")
    print(f"[train] reason code examples -> outputs/reason_codes_examples.txt")

    return scored_path


if __name__ == "__main__":
    main()
