#!/usr/bin/env bash
# Annotate every root-level *.py file, then prove nothing broke.
#
#   arena/annotate_root.sh            # dry run: report what would change
#   arena/annotate_root.sh --write    # annotate in place + audit
#
# Exit status is non-zero if any file was rejected, or if the audit finds a
# syntax error, an unimported annotation name, or a semantic change.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}

write=()
if [[ "${1:-}" == "--write" ]]; then
  write=(--write)
  shift
fi

echo "== infer =="
"$PY" arena/run_all.py "${write[@]}" --batch 100 "$@"

echo
echo "== coverage =="
"$PY" arena/annotation_stats.py

echo
echo "== audit =="
"$PY" arena/audit_annotations.py --rev HEAD
