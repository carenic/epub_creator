#!/usr/bin/env bash
# Install a local epubcheck into <workspace>/.tools/ so `build_epub.py --check`
# can validate the generated books. Nothing is installed system-wide.
#
#   bash .agents/skills/siyuan-epub-builder/scripts/setup_epubcheck.sh [WORKSPACE]
#
# Afterwards the java and jar locations are recorded in .tools/.jre_path and
# .tools/.epubcheck_path as paths RELATIVE to .tools/ (e.g.
# `epubcheck-5.2.1/epubcheck.jar`), so the records stay valid when the
# workspace is moved or copied. Readers prefix them with <workspace>/.tools/.
set -euo pipefail

WS="${1:-$PWD}"
WS="$(cd "$WS" && pwd)"
TOOLS="$WS/.tools"
mkdir -p "$TOOLS"
cd "$TOOLS"

EPUBCHECK_VERSION="${EPUBCHECK_VERSION:-5.2.1}"
JRE_MAJOR="${JRE_MAJOR:-21}"

# ---- epubcheck ------------------------------------------------------------
if [ -z "$(find . -name epubcheck.jar -print -quit 2>/dev/null)" ]; then
  echo "▸ 下载 epubcheck $EPUBCHECK_VERSION …"
  curl -fsSL -o epubcheck.zip \
    "https://github.com/w3c/epubcheck/releases/download/v$EPUBCHECK_VERSION/epubcheck-$EPUBCHECK_VERSION.zip"
  unzip -q -o epubcheck.zip
  rm -f epubcheck.zip
else
  echo "· epubcheck 已存在"
fi

# ---- JRE ------------------------------------------------------------------
if [ -z "$(find . -type f -path '*/bin/java' -print -quit 2>/dev/null)" ]; then
  echo "▸ 查找 Azul Zulu JRE $JRE_MAJOR …"
  URL=$(curl -fsSL https://cdn.azul.com/zulu/bin/ \
        | grep -oE "zulu${JRE_MAJOR}\.[0-9.]+-ca-jre${JRE_MAJOR}[0-9.]*-linux_x64\.tar\.gz" \
        | sort -uV | tail -1)
  if [ -z "$URL" ]; then
    echo "! 未能从 Azul CDN 解析到 JRE 包；系统已安装 java 时可直接使用它" >&2
    exit 1
  fi
  echo "  $URL"
  curl -fsSL -o jre.tar.gz "https://cdn.azul.com/zulu/bin/$URL"
  tar xzf jre.tar.gz
  rm -f jre.tar.gz
else
  echo "· JRE 已存在"
fi

JAVA="$(find . -type f -path '*/bin/java' -print -quit)"
JAR="$(find . -name epubcheck.jar -print -quit)"
if [ -z "$JAVA" ] || [ -z "$JAR" ]; then
  echo "! 未能在 $TOOLS 下定位 java 或 epubcheck.jar" >&2
  exit 1
fi
# `find .` yields './x/y'; strip the prefix so the records are relative to .tools/
JAVA="${JAVA#./}"
JAR="${JAR#./}"
printf '%s\n' "$JAVA" > .jre_path
printf '%s\n' "$JAR" > .epubcheck_path

echo
echo "✓ java : $TOOLS/$JAVA"
echo "✓ jar  : $TOOLS/$JAR"
echo "· 位置已记录到 .tools/.jre_path 与 .tools/.epubcheck_path（相对路径）"
"$TOOLS/$JAVA" -version 2>&1 | head -1
