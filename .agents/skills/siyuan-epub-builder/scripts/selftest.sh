#!/usr/bin/env bash
# Regression self-test for build_epub.py.
#
#   bash .agents/skills/siyuan-epub-builder/scripts/selftest.sh [-w WORKSPACE]
#
# Builds a throwaway book in a temp directory that reproduces every failure mode
# found so far (scraped anchors, duplicate heading names, percent-encoded and
# abbreviated anchors, currency read as math, `$$` around prose, multi-line
# block math, `$` inside code fences, GitHub callouts, a long cover chapter
# list), then checks the output and runs epubcheck. Run it after touching the
# builder — it is much faster than rediscovering a bug through a real book.
#
# `-w` names the workspace that holds `.tools/` (epubcheck) and `.cache/uv`; the
# fixture book itself is always built in a temp directory. KEEP=1 keeps it.
set -uo pipefail

WS="$PWD"
while [ $# -gt 0 ]; do
  case "$1" in
    -w|--workspace) WS="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "! 未知参数: $1" >&2; exit 2 ;;
  esac
done
WS="$(cd "$WS" && pwd)"
SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$SKILL/scripts/build_epub.py"
CHECK="$SKILL/scripts/check_epub.sh"

pass=0; fail=0
ok()   { pass=$((pass + 1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { fail=$((fail + 1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
want() { # want <description> <needle> <file...>
  local desc="$1" needle="$2"; shift 2
  if grep -qF -- "$needle" "$@" 2>/dev/null; then ok "$desc"; else bad "$desc (缺少: $needle)"; fi
}
wantnot() { # wantnot <description> <forbidden needle> <file...>
  local desc="$1" needle="$2"; shift 2
  if grep -qF -- "$needle" "$@" 2>/dev/null; then bad "$desc (不应出现: $needle)"; else ok "$desc"; fi
}

wanttext() { # wanttext <description> <needle> <file>  — tags stripped first, so
             # highlighted markup (Pygments splits `$HOME` into spans) can't hide text
  local desc="$1" needle="$2" file="$3" plain="$3.plain"
  sed 's/<[^>]*>//g' "$file" > "$plain" 2>/dev/null
  if grep -qF -- "$needle" "$plain" 2>/dev/null; then ok "$desc"; else bad "$desc (缺少: $needle)"; fi
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/siyuan-epub-selftest.XXXXXX")"
cleanup() { [ "${KEEP:-0}" = "1" ] || rm -rf "$TMP"; }
trap cleanup EXIT

BOOK="$TMP/Selftest.md"
mkdir -p "$BOOK/Selftest"
cat > "$BOOK/Selftest.md" <<'MD'
---
title: Selftest
date: 2025-01-01T00:00:00+08:00
---

# Selftest

[Anchors](Selftest/Anchors.md)

[Math](Selftest/Math.md)

[Part One](Selftest/Part%20One.md)

[Part Two](Selftest/Part%20Two.md)

[Part Three](Selftest/Part%20Three.md)

[Part Four](Selftest/Part%20Four.md)

[Part Five](Selftest/Part%20Five.md)

[Part Six](Selftest/Part%20Six.md)
MD

cat > "$BOOK/Selftest/Anchors.md" <<'MD'
---
title: Anchors
---

# Anchors

## Note's

First occurrence — a first-wins loose match would wrongly land here.

## Notes

Second occurrence — the exact slug `#notes` must land on THIS heading.

## Café

Accent target.

## F. General Overview of the Final Rule

Stop-word target.

- [exact](#notes)
- [encoded](#caf%C3%A9)
- [abbreviated](#f-general-overview-of-final-rule)
- [dead](#sectno-reference-820.1)

> [!NOTE] Selftest Callout
> Body with currency $1,200 and $3,400 in one sentence.
MD

cat > "$BOOK/Selftest/Math.md" <<'MD'
---
title: Math
---

# Math

Inline math $E = mc^2$ stays math.

A real block formula:

$$
\int_0^1 x^2 \, dx = \frac{1}{3}
$$

A stray delimiter pair around prose must stay readable text:

$$
This paragraph is prose, not a formula.
It must stay readable text.

More prose that is long enough to look nothing like a formula.
$$

Currency in prose: the rule costs about $532 million at a 7 percent discount
rate (savings: $540M, costs: $8.2M) and about $561M overall.

A fence must stay untouched:

```sh
echo $HOME and $PATH
```
MD

part() { # part <file-stem> <title>
  printf -- '---\ntitle: %s\n---\n\n# %s\n\nText of part %s.\n' "$2" "$2" "$1" \
    > "$BOOK/Selftest/Part $1.md"
}
part One   "Part One With A Name Long Enough To Need Wrapping"
part Two   "Part Two"
part Three "Part Three"
part Four  "Part Four"
part Five  "Part Five"
part Six   "Part Six"

echo "▸ 构建回归用书 (临时目录 $TMP)"
if ! UV_CACHE_DIR="$WS/.cache/uv" python3 "$BUILD" -w "$TMP" --clean > "$TMP/build.log" 2>&1; then
  bad "build_epub.py 构建失败"; tail -20 "$TMP/build.log" | sed 's/^/    /'
  echo; echo "结果: $pass 通过 / $fail 失败"; exit 1
fi
ok "构建成功"

T="$TMP/.build/Selftest/OEBPS/text"
ANCH="$(ls "$T"/*anchors*.xhtml 2>/dev/null | head -1)"
MATH="$(ls "$T"/*math*.xhtml 2>/dev/null | head -1)"

echo "▸ 锚点解析"
if [ -n "$ANCH" ]; then
  want    "重名标题: #notes 命中第二个标题"      'href="#s1-anchors-notes">exact' "$ANCH"
  want    "重名标题: 同名但拼写不同的 id 独立"    'id="s1-anchors-note-s"' "$ANCH"
  want    "百分号编码锚点 #caf%C3%A9 解析"       'href="#s1-anchors-caf"' "$ANCH"
  want    "缩写 TOC 标签 (…of Final Rule) 解析"  'href="#s1-anchors-f-general-overview-of-the-final-rule"' "$ANCH"
  want    "无目标锚点降级为纯文本"               '<span>dead</span>' "$ANCH"
  wantnot "无目标锚点不残留 href"                'href="#sectno-reference-820.1"' "$ANCH"
  want    "GitHub alert 转为 callout"            'class="callout callout-note"' "$ANCH"
  want    "callout 标题单独成行"                 'class="callout-title">Selftest Callout' "$ANCH"
  want    "货币文本保持原样"                     '$1,200 and $3,400' "$ANCH"
  wantnot "货币未被当作数学"                     '<math' "$ANCH"
else
  bad "找不到 anchors 页面"
fi

echo "▸ 数学与代码"
if [ -n "$MATH" ]; then
  n_math=$(grep -o '<math' "$MATH" | wc -l | tr -d ' ')
  n_block=$(grep -o 'class="math-block"' "$MATH" | wc -l | tr -d ' ')
  [ "$n_math" = "2" ] && ok "行内公式 + 真公式转 MathML (2 个 <math>)" \
                      || bad "MathML 数量应为 2，实际 $n_math"
  [ "$n_block" = "1" ] && ok "只有真公式成为 math-block (共 1 个)" \
                       || bad "math-block 数量应为 1，实际 $n_block"
  wanttext '$$ 包住的散文保持为文本' 'This paragraph is prose, not a formula.' "$MATH"
  wanttext '货币金额保持为文本'      '$532 million' "$MATH"
  wanttext '围栏代码里的 $ 未被处理' '$HOME and $PATH' "$MATH"
else
  bad "找不到 math 页面"
fi

echo "▸ 封面"
COVER="$TMP/.build/Selftest/OEBPS/images/cover.png"
if [ -f "$COVER" ]; then
  if python3 - "$COVER" <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read(33)
assert d[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
w, h = struct.unpack(">II", d[16:24])
assert (w, h) == (1600, 2400), f"unexpected size {w}x{h}"
PY
  then ok "封面 PNG 生成且为 1600×2400"; else bad "封面 PNG 尺寸异常"; fi
else
  bad "封面 PNG 缺失"
fi

echo "▸ epubcheck"
if bash "$CHECK" -w "$WS" "$TMP/Selftest.epub" > "$TMP/check.log" 2>&1; then
  ok "epubcheck 无错误无警告"
else
  bad "epubcheck 报错"; grep -E 'ERROR|WARNING|FATAL' "$TMP/check.log" | head -10 | sed 's/^/    /'
fi

echo
if [ "$fail" = "0" ]; then
  echo -e "\033[32m结果: $pass 通过 / 0 失败\033[0m"
else
  echo -e "\033[31m结果: $pass 通过 / $fail 失败\033[0m"
  [ "${KEEP:-0}" = "1" ] || echo "  提示: KEEP=1 重跑可保留临时目录排查"
fi
exit $((fail > 0))
