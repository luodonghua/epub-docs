#!/usr/bin/env bash
#
# refresh.sh -- one-command refresh of the AWS documentation EPUB(s).
#
# This wrapper exists so a future refresh is effortless: run `./refresh.sh`
# and it re-downloads the latest docs and rebuilds the EPUB with today's build
# date baked into the title (e.g. "Amazon Aurora DSQL User Guide (20261019)").
#
# Usage:
#   ./refresh.sh                 # rebuild the default guide (aurora-dsql)
#   ./refresh.sh aurora-dsql     # rebuild a named preset guide
#   ./refresh.sh --base-url https://docs.aws.amazon.com/<svc>/latest/userguide \
#                --title "My Guide"   # rebuild an arbitrary AWS userguide
#
# Output: <guide>-<YYYY-MM-DD>.epub in this directory, plus a stable
# <guide>-latest.epub symlink pointing at the newest build.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEN="$SCRIPT_DIR/aws_docs_to_epub.py"
DATE="$(date +%Y-%m-%d)"

# --- dependency checks -----------------------------------------------------
if ! command -v pandoc >/dev/null 2>&1; then
  echo "pandoc not found. Install it with: sudo apt-get install -y pandoc" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found." >&2
  exit 1
fi

# --- resolve the guide name for the output filename ------------------------
# Default to the 'aurora-dsql' preset when no args are given.
if [[ $# -eq 0 ]]; then
  set -- --guide aurora-dsql
fi

# Derive a filename stem from the args (preset name, or host from --base-url).
STEM="aws-docs"
if [[ "${1:-}" != --* ]]; then
  STEM="$1"                       # positional preset name, e.g. aurora-dsql
  set -- --guide "$1"
else
  # scan flags for --guide / --base-url to build a sensible stem
  prev=""
  for arg in "$@"; do
    case "$prev" in
      --guide)    STEM="$arg" ;;
      --base-url) STEM="$(echo "$arg" | sed -E 's#https?://([^/]+)/.*#\1#; s#[^a-zA-Z0-9]+#-#g')" ;;
    esac
    prev="$arg"
  done
fi

OUT="$SCRIPT_DIR/${STEM}-${DATE}.epub"

echo "==> Building EPUB: $OUT"
python3 "$GEN" "$@" -o "$OUT"

# --- update the stable 'latest' symlink ------------------------------------
LATEST="$SCRIPT_DIR/${STEM}-latest.epub"
ln -sf "$(basename "$OUT")" "$LATEST"
echo "==> Updated symlink: $LATEST -> $(basename "$OUT")"

# --- optional validation if epubcheck is present ---------------------------
if command -v epubcheck >/dev/null 2>&1; then
  echo "==> Validating with epubcheck ..."
  epubcheck "$OUT" 2>&1 | tail -3
fi

echo "==> Done. Copy '$OUT' to your iPad (AirDrop, Files, or Apple Books)."
