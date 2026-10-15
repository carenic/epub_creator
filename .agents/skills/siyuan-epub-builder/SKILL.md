---
name: siyuan-epub-builder
description: Build one polished, EPUB 3–conformant e-book from a SiYuan (思源笔记) exported markdown folder. Use when the user drops a «book.md» export folder into a workspace and asks to merge its markdown notes into an .epub (合并成一本电子书 / 制作 epub / 导出 epub), or asks to rebuild, restyle, extend or re-validate such a book. Handles rebuilding the table of contents from the folder contents alone, code highlighting, mermaid flowcharts, embedded images, tables and LaTeX math.
version: 1.0.0
---

# SiYuan → EPUB 3 builder

Turns a SiYuan markdown export folder into a single EPUB 3 book, with the table
of contents reconstructed **from the folder contents** — no screenshot, outline
file or manual ordering is required.

## Workspace convention

The book folder and the generated book are siblings in the workspace root:

```
<workspace>/
├── Programming.md/            ← the SiYuan export folder (input, never modified)
│   ├── Programming.md         ← root index note
│   └── Programming/           ← the notebook tree
├── Programming.epub           ← generated book (output)
└── .build/Programming/        ← scratch: OEBPS/, mermaid cache  (safe to delete)
```

The output name is always the folder name with `.md` replaced by `.epub`.

## Quick start

```bash
# from the workspace root, with the export folder already in place
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py
```

That is the whole workflow. The script re-executes itself through `uv` to obtain
its Python dependencies, so no virtualenv or `pip install` is needed. It
auto-discovers every `*.md/` folder in the workspace; pass folder names to build
only some of them:

```bash
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py Programming.md
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py -w /path/to/ws --clean --check
```

The **first** run downloads the Python packages (~1 min) and, when a mermaid
diagram is present, Chrome for Puppeteer (~150 MB); later runs reuse
`<workspace>/.cache/uv` and `~/.cache/puppeteer` and take a few seconds. Give the
first invocation a generous timeout — a short one looks like a hang.

| Flag | Effect |
| --- | --- |
| `-w, --workspace DIR` | workspace root (default: current directory) |
| `-b, --book DIR` | build one specific book folder (repeatable) |
| `--clean` | drop the cached scratch dir first (also clears the mermaid cache) |
| `--check` | run epubcheck on the result |

## How the tree is rebuilt

1. **Index notes win.** A note such as `Matlab.md` lists its children as links
   (``[Wavelet Toolbox Functions](Matlab/Wavelet%20Toolbox%20Functions.md)``).
   Those links, in document order, become the section/chapter order.
2. **The filesystem fills the gaps.** Anything present in the sibling directory
   (`Matlab/`) but missing from the link list is appended, so a note without an
   index still builds.
3. **No index at all** falls back to a flat chapter list ordered by the `date`
   front-matter field, then by name.
4. Nesting depth is unlimited: every node becomes one XHTML page, nodes with
   children additionally get a generated child table of contents, and the nav
   document mirrors the whole tree.

Link destinations are parsed with CommonMark's balanced-parenthesis rule, so
names like `CMake生成器(generator).md` and `Python Type Hints (类型注解).md`
resolve correctly — a naive `[^)]+` pattern silently drops them and scrambles the
order.

## What gets rendered

- headings (stable unique ids), nested lists, GFM task lists (`☑`/`☐`),
  blockquotes, horizontal rules
- tables, both GFM and raw HTML — markdown written inside cells is rendered too,
  and a `<thead>` is synthesised when the export omits one
- fenced code blocks with Pygments highlighting (language aliases such as
  `shell`→bash, `ts`→typescript are mapped); unknown languages stay plain
- **mermaid** flowcharts/sequence diagrams rendered to PNG through
  `bun x @mermaid-js/mermaid-cli` (needs `bun`; the first run downloads Chrome
  into `~/.cache/puppeteer`). If rendering is impossible the diagram source is
  kept as a code block instead of failing the build.
- embedded images from every `assets/` folder, downscaled and re-encoded;
  non-core formats (webp/bmp/tiff/avif) are converted to PNG so epubcheck stays
  happy
- LaTeX math via `$…$` / `$$…$$` converted to MathML (multi-line `$$…$$` blocks
  included) and declared as `properties="mathml"` in the manifest. Math is only
  extracted from prose — fenced blocks and inline code are skipped, delimiters
  that hug whitespace (e.g. `$auth.id OR $`) are left alone, and a `$…$` pair
  that only spans prose (`$532 million … cost savings:$`) is treated as currency,
  not math, so shell snippets and cost figures are never mangled
- GitHub/Obsidian alerts (`> [!NOTE] Title`) become styled callouts instead of
  leaking a literal `[!NOTE]` marker into the text
- SiYuan inline styling: `var(--b3-font-colorN)` / `var(--b3-card-*-background)`
  spans become CSS classes instead of being stripped
- intra-book `.md` links become links to the built pages; external links are
  kept; in-page anchors are resolved in tiers — verbatim source anchor, then our
  own slug of the heading text, then a loose alnum-only key, then a
  stop-word-insensitive key — so a scraped
  `#ii-table-of-abbreviationscommonly-used-acronyms` still lands on its heading;
  anchors with no target and dead local links degrade to plain text, never to a
  dangling `#fragment` (epubcheck RSC-012)
- a generated cover (word-wrapped, auto-shrinking title/chapter list), a title
  page, a nested nav document and a dark-mode CSS variant

## Optional per-book configuration

Create `epub.json` inside the book folder, or `<Book>.epub.json` next to it, to
override what is auto-derived:

```json
{
  "title": "Programming",
  "subtitle": "编程技术笔记合集",
  "author": "…",
  "language": "zh-CN",
  "publisher": "…",
  "description": "…",
  "identifier": "urn:uuid:…"
}
```

Everything is optional: the title comes from the root note's front matter (or
the folder name), the subtitle from the top-level section names, and the
language is detected from the CJK ratio.

## Validating

```bash
# one-time: install epubcheck + a JRE into <workspace>/.tools/ (nothing global)
bash .agents/skills/siyuan-epub-builder/scripts/setup_epubcheck.sh

# then either
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py --check
# or, for books that already exist
bash .agents/skills/siyuan-epub-builder/scripts/check_epub.sh
```

Target: `0 fatals / 0 errors / 0 warnings`. See `references/workflow.md` for the
pitfalls each build rule exists to prevent and for troubleshooting.

### Self-test (run after any change to the builder)

```bash
bash .agents/skills/siyuan-epub-builder/scripts/selftest.sh [-w WORKSPACE]
```

Builds a throwaway book in a temp directory that reproduces every failure mode
found so far — scraped / percent-encoded / abbreviated / duplicate-name anchors,
anchors with no target, currency read as math, `$$` around prose, multi-line
block math, `$` inside a code fence, GitHub callouts, a long cover chapter list —
then asserts the expected output and runs epubcheck. `-w` names the workspace
holding `.tools/` and `.cache/uv` (the fixture itself stays in the temp dir, and
`KEEP=1` keeps it for debugging). It finishes in seconds and is the cheapest way
to prove a fix generalises instead of patching one book; each assertion is
mutation-tested, so removing a guard makes it fail.
