#!/usr/bin/env bash
# ローカル・エンドツーエンド実行：生データ生成 → Lakeflow(bronze/silver/gold) →
# クラシックML学習+SHAP相当の理由コード → ビジネスインパクト → Lakebase投入 →
# Genie相当のNLクエリ → App動作証跡。実行ログは logs/run_all.log にコミットする。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
LOG=logs/run_all.log

run() { echo -e "\n\n########## $* ##########"; python3 "$@"; }

{
  echo "==== fraud-decision-demo local run @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="
  python3 --version
  echo "numpy $(python3 -c 'import numpy;print(numpy.__version__)')"
  run src/generate_data.py
  run src/pipeline.py
  run src/train_model.py
  run src/business_impact.py
  run src/load_lakebase.py
  run src/genie_queries.py
  run app/local_app.py --demo
  echo -e "\n==== DONE. Outputs committed under outputs/ ===="
} 2>&1 | tee "$LOG"
