"""クラシックML：勾配ブースティング決定木（二値分類, logloss）をnumpyのみで実装。

さらに「木の経路寄与（Saabas法）」で1件ごとの特徴量寄与を厳密に計算する。
これは TreeSHAP の前身にあたる加法的分解で、
    logit(p) = baseline + Σ_feature contribution[feature]
が厳密に成り立つ（誤差なし）。この contribution を業務ユーザ向けの
「理由コード（なぜ止めるべきか）」に変換する。

Databricks移植版(databricks/03_train_model.py)では scikit-learn の
GradientBoostingClassifier + shap.TreeExplainer を使う。役割・出力は同じ。
"""
from __future__ import annotations
import numpy as np


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


class _Node:
    __slots__ = ("value", "feat", "thr", "left", "right")

    def __init__(self, value):
        self.value = value      # このノードでの予測値（残差の平均）
        self.feat = None        # 分割に使う特徴量index
        self.thr = None         # 分割しきい値
        self.left = None
        self.right = None

    def is_leaf(self):
        return self.feat is None


class _RegTree:
    """残差に対する最小二乗回帰木。分割は分散減少（SSE最小化）で選ぶ。"""

    def __init__(self, max_depth=3, min_samples_leaf=200):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.root = None

    def fit(self, X, target):
        self.root = self._build(X, target, depth=0)
        return self

    def _build(self, X, t, depth):
        node = _Node(float(t.mean()))
        if depth >= self.max_depth or len(t) < 2 * self.min_samples_leaf:
            return node
        best = self._best_split(X, t)
        if best is None:
            return node
        feat, thr = best
        mask = X[:, feat] <= thr
        if mask.sum() < self.min_samples_leaf or (~mask).sum() < self.min_samples_leaf:
            return node
        node.feat, node.thr = feat, thr
        node.left = self._build(X[mask], t[mask], depth + 1)
        node.right = self._build(X[~mask], t[~mask], depth + 1)
        return node

    def _best_split(self, X, t):
        n = len(t)
        best_gain = 1e-12
        best = None
        total_sse = _sse(t.sum(), (t * t).sum(), n)
        for feat in range(X.shape[1]):
            xf = X[:, feat]
            order = np.argsort(xf, kind="mergesort")
            xs = xf[order]
            ts = t[order]
            csum = np.cumsum(ts)
            csum2 = np.cumsum(ts * ts)
            tot = csum[-1]
            tot2 = csum2[-1]
            # 分割点候補：値が変わる境目のみ
            change = np.where(np.diff(xs) > 0)[0]
            for i in change:
                nl = i + 1
                nr = n - nl
                if nl < self.min_samples_leaf or nr < self.min_samples_leaf:
                    continue
                sse_l = _sse(csum[i], csum2[i], nl)
                sse_r = _sse(tot - csum[i], tot2 - csum2[i], nr)
                gain = total_sse - (sse_l + sse_r)
                if gain > best_gain:
                    best_gain = gain
                    best = (feat, 0.5 * (xs[i] + xs[i + 1]))
        return best

    def predict(self, X):
        return np.array([self._pred_row(self.root, X[i]) for i in range(X.shape[0])])

    def _pred_row(self, node, x):
        while not node.is_leaf():
            node = node.left if x[node.feat] <= node.thr else node.right
        return node.value

    def contributions(self, x):
        """1件の経路寄与を返す: (bias, {feat_index: delta}).
        bias(=root.value) と各特徴のdeltaの総和が leaf.value に厳密一致する。"""
        node = self.root
        bias = node.value
        contrib = {}
        while not node.is_leaf():
            parent_val = node.value
            child = node.left if x[node.feat] <= node.thr else node.right
            contrib[node.feat] = contrib.get(node.feat, 0.0) + (child.value - parent_val)
            node = child
        return bias, contrib


class GBDTClassifier:
    def __init__(self, n_trees=150, learning_rate=0.15, max_depth=3,
                 min_samples_leaf=200):
        self.n_trees = n_trees
        self.lr = learning_rate
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.trees = []
        self.f0 = 0.0

    def fit(self, X, y, verbose=True):
        p = np.clip(y.mean(), 1e-6, 1 - 1e-6)
        self.f0 = float(np.log(p / (1 - p)))
        F = np.full(len(y), self.f0, dtype=float)
        for m in range(self.n_trees):
            residual = y - _sigmoid(F)                       # 負の勾配（logloss）
            tree = _RegTree(self.max_depth, self.min_samples_leaf).fit(X, residual)
            F += self.lr * tree.predict(X)
            self.trees.append(tree)
            if verbose and (m + 1) % 30 == 0:
                ll = _logloss(y, _sigmoid(F))
                print(f"[gbdt] tree {m+1:3d}/{self.n_trees}  train_logloss={ll:.4f}")
        return self

    def decision_function(self, X):
        F = np.full(X.shape[0], self.f0, dtype=float)
        for tree in self.trees:
            F += self.lr * tree.predict(X)
        return F

    def predict_proba(self, X):
        return _sigmoid(self.decision_function(X))

    def explain(self, x):
        """1件の加法的分解を返す: (baseline_logit, {feat_index: contribution}).
        baseline + Σcontribution = decision_function(x) が厳密に成立。"""
        baseline = self.f0
        contrib = {}
        for tree in self.trees:
            bias, c = tree.contributions(x)
            baseline += self.lr * bias
            for f, d in c.items():
                contrib[f] = contrib.get(f, 0.0) + self.lr * d
        return baseline, contrib


def _sse(s, s2, n):
    # 二乗和誤差 = Σt^2 - (Σt)^2 / n
    if n <= 0:
        return 0.0
    return s2 - (s * s) / n


def _logloss(y, p):
    p = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())
