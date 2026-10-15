#!/usr/bin/env bash
# Validate one or more EPUB files with the local epubcheck in <workspace>/.tools/.
#
#   bash .agents/skills/siyuan-epub-builder/scripts/check_epub.sh [BOOK.epub ...] [-w WORKSPACE]
#
# With no argument every *.epub in the workspace is checked.
set -euo pipefail

WS="$PWD"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -w|--workspace) WS="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
WS="$(cd "$WS" && pwd)"

JAVA="$(find "$WS/.tools" -type f -path '*/bin/java' -print -quit 2>/dev/null || true)"
JAR="$(find "$WS/.tools" -name epubcheck.jar -print -quit 2>/dev/null || true)"
if [ ! -x "$JAVA" ] || [ -z "$JAR" ]; then
  echo "! 未找到本地 epubcheck，先运行：" >&2
  echo "    bash .agents/skills/siyuan-epub-builder/scripts/setup_epubcheck.sh '$WS'" >&2
  exit 1
fi

if [ ${#ARGS[@]} -eq 0 ]; then
  mapfile -t ARGS < <(find "$WS" -maxdepth 1 -name '*.epub' -print | sort)
fi
[ ${#ARGS[@]} -gt 0 ] || { echo "! 没有待检查的 epub" >&2; exit 1; }

status=0
for book in "${ARGS[@]}"; do
  echo "──── $(basename "$book") ────"
  if ! "$JAVA" -jar "$JAR" "$book"; then status=1; fi
  echo
done
exit $status

