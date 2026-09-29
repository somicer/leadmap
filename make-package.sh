#!/usr/bin/env bash
# Build a portable leadmap-YYYYMMDD.tgz for installing on another server.
# Leaves out everything server-specific or secret: config.yaml (password), database,
# browser profile, logs, debug dumps, exports and the virtualenv.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$(dirname "$APP_DIR")/leadmap-$(date +%Y%m%d).tgz}"
NAME="$(basename "$APP_DIR")"

tar czf "$OUT" -C "$(dirname "$APP_DIR")" \
  --exclude="$NAME/.venv" --exclude="$NAME/data" --exclude="$NAME/debug" \
  --exclude="$NAME/logs" --exclude="$NAME/exports" --exclude="$NAME/config.yaml" \
  --exclude="$NAME/doc.txt" --exclude="$NAME/.git" --exclude="$NAME/.gitignore" --exclude="__pycache__" --exclude=".pytest_cache" \
  --transform "s|^$NAME|leadmap|" \
  "$NAME"

echo "Package: $OUT ($(du -h "$OUT" | cut -f1))"
echo
echo "On the new server:"
echo "  scp $OUT root@NEW_SERVER:/root/"
echo "  ssh root@NEW_SERVER"
echo "  cd /root && tar xzf $(basename "$OUT") && cd leadmap && ./install.sh"
