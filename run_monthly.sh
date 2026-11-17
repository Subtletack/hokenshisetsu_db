#!/bin/zsh
set -euo pipefail

BASE_DIR="$HOME/hokenshisetsu_auto"
PY_BIN="/opt/homebrew/bin/python3.12"
SNAP=$(date +%Y%m)
LOG_FILE="$BASE_DIR/logs/run_${SNAP}.log"

cd "$BASE_DIR"

{
  echo "=== Start snapshot=${SNAP} at $(date '+%Y-%m-%d %H:%M:%S') ==="
  "$PY_BIN" subprojects/hokenshisetsu/scripts/download_raw.py --snapshot "$SNAP" --mode all --download-japanpost
  "$PY_BIN" subprojects/hokenshisetsu/scripts/build_nationwide.py --snapshot "$SNAP"
  "$PY_BIN" subprojects/hokenshisetsu/scripts/verify_nationwide.py --snapshot "$SNAP"
  "$PY_BIN" subprojects/hokenshisetsu/scripts/export_public_release.py --snapshot "$SNAP"
  echo "=== Finished snapshot=${SNAP} at $(date '+%Y-%m-%d %H:%M:%S') ==="
} >> "$LOG_FILE" 2>&1
