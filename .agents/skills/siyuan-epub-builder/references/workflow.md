# Workflow notes, EPUB 3 rules and troubleshooting

Deep reference for `siyuan-epub-builder`. `SKILL.md` is enough for the normal
"drop a folder in, get an epub" case; read this when something looks wrong or the
builder needs changing.

## 1. Environment

| Need | Provided by |
| --- | --- |
| Python packages | the script itself, re-executing through `uv run --no-project --with …` |
| `uv` | Cherry's bundled toolchain (on `PATH`) |
| mermaid rendering | `bun x --bun @mermaid-js/mermaid-cli@11` (bun is bundled) |
| Chrome for mermaid | auto-downloaded by Puppeteer into `~/.cache/puppeteer` on first use |
| epubcheck | `scripts/setup_epubcheck.sh` → `<workspace>/.tools/` |

**`uv` cache and the file sandbox.** Under a workspace-write sandbox the default
cache at `~/.cache/uv` is read-only, which makes `uv` fail with
`Could not acquire lock … Read-only file system`. The builder therefore forces
`UV_CACHE_DIR` to `<cwd>/.cache/uv`. Keep that behaviour if you refactor the
bootstrap; never rely on the user-level cache.

**Mermaid sanity check.** A single-diagram render should take ~2 s once the
package and Chrome are cached:

```bash
printf 'flowchart TD\n  A["👤 用户"] --> B{"结果?"}\n' > /tmp/t.mmd
bun x --bun @mermaid-js/mermaid-cli@11 -i /tmp/t.mmd -o /tmp/t.png \
  -p puppeteer.json -c mmdc.json -b white -s 3
```

`mmdc.json` must contain `"flowchart": {"htmlLabels": false}` — with HTML labels
mermaid emits `<foreignObject>`, which rasterises badly and breaks CJK fonts.
`puppeteer.json` must contain `--no-sandbox`.

## 2. Why each EPUB rule is in the builder

- **`mimetype` first, uncompressed.** `zipfile` writes it with `ZIP_STORED`
  before anything else. Getting this wrong is a hard epubcheck failure.
- **`container.xml` points at `OEBPS/package.opf`**; `nav.xhtml` sits in `OEBPS/`
  while every content document sits in `OEBPS/text/`, so nav links are
  `text/x.xhtml` and content-to-content links are bare file names.
- **One `<h1>` per content document.** The note's own `# Title` is stripped and
  re-emitted from the tree, so a note that forgot its heading still gets one.
- **Only EPUB core media types in the manifest** (PNG/JPEG/GIF/SVG). webp, bmp,
  tiff and avif are re-encoded to PNG — otherwise epubcheck reports an unsupported
  media type or a missing fallback.
- **No `<style>`/`style=` attributes.** SiYuan emits
  `style="color: var(--b3-font-color8)"`, which is meaningless in a reading
  system; `style_to_classes()` maps the known custom properties to real CSS
  classes and the sanitiser drops everything else.
- **Attribute whitelist.** Raw HTML tables carry `data-*`, `class`, `id` noise.
  `clean_attrs()` keeps only `href`, `src`, `alt`, `colspan`, `rowspan`, `scope`,
  `span`, `start`, and re-attaches the mapped SiYuan classes. Note the ordering:
  classes are collected *before* `attrib.clear()`.
- **Markdown after raw HTML blocks.** markdown-it keeps an HTML block open until
  a blank line, so prose following `</table>` would be swallowed verbatim;
  `unpin_html_blocks()` inserts the blank line.
- **Images are never inline block content.** An `<img>` sitting inside a
  paragraph that also holds text is pulled out into a following `<figure>`, since
  block markup may not be nested in a `<p>`.
- **`white-space: pre-wrap` on `pre.code`.** Many fences in these notes are ASCII
  diagrams; without wrapping they overflow (there is no horizontal scrolling in
  e-readers).

## 3. Tree reconstruction gotchas

- **Balanced parentheses in link destinations.** `LINK_RE` must accept
  `…/CMake生成器(generator).md`. A `[^)\s]+` character class truncates at the
  first `)` and the whole link is dropped, which silently demotes that note to
  filesystem ordering — the symptom is a chapter appearing at the *end* of its
  section instead of in place.
- **Percent-encoded, space-and-`+`-bearing names.** `unquote()` handles `%20`
  but deliberately does *not* turn `+` into a space, which matters for
  `VSCode与Zed官宣拥抱Vite+.md`.
- **Zero-width spaces.** SiYuan writes `U+200B` between a section number and its
  title (`[1. ](…)​[Introduction](…)`); `clean_source_text()` strips it, otherwise
  it leaks into headings, ids and the nav.
- **Links may be addressed from the note's own directory *or* from the notebook
  root.** `LinkResolver.lookup()` tries the current-directory resolution, then
  the notebook-root prefix, then a bare-filename match.
- **Never escape the book folder.** A link pointing outside `book_dir` is ignored
  rather than turned into a node.

## 4. Verifying the result

Automated:

```bash
bash .agents/skills/siyuan-epub-builder/scripts/setup_epubcheck.sh
bash .agents/skills/siyuan-epub-builder/scripts/check_epub.sh
```

Visual — screenshot a built page with the Chromium that mermaid already
downloaded. **Do not pass `--user-data-dir`** (profile creation hangs), and give
each invocation a timeout:

```bash
CHROME=~/.cache/puppeteer/chrome/*/chrome-linux64/chrome
timeout 90 $CHROME --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
  --screenshot=shot.png --window-size=900,3000 \
  "file://$PWD/.build/<Book>/OEBPS/text/<page>.xhtml"
```

Worth eyeballing: `nav.xhtml` (TOC order vs. the notebook tree), the title page,
one code-heavy chapter, one chapter with a mermaid diagram, and the largest
chapter (tables + images). Then read the PNG back with an image tool.

Useful one-liners for a built book:

```bash
# every diagram actually resolved?
grep -o 'diagram-[0-9]*\.png' .build/<Book>/OEBPS/text/*.xhtml | sort -u
# any mermaid fence left unrendered?
grep -l mermaid .build/<Book>/OEBPS/text/*.xhtml
# images referenced vs. images shipped
grep -ho 'src="\.\./images/[^"]*"' .build/<Book>/OEBPS/text/*.xhtml | sort -u | wc -l
ls .build/<Book>/OEBPS/images | wc -l
```

## 5. Common failures

Before debugging a real book, run the regression self-test — it covers every
failure mode listed below on a throwaway book in seconds:

```bash
bash .agents/skills/siyuan-epub-builder/scripts/selftest.sh -w <workspace>
```

| Symptom | Cause / fix |
| --- | --- |
| `Could not acquire lock … Read-only file system` | `UV_CACHE_DIR` points outside the writable workspace; the builder now redirects it to `<cwd>/.cache/uv` |
| A chapter sits at the end of its section | its index link failed to parse — check for parentheses or unusual characters in the `.md` name |
| `未找到图片，已移除: …` for a real screenshot | the image is referenced with a path the map does not know; the map holds both the book-relative path and the bare filename |
| mermaid fails with `Failed to launch the browser` | Chrome not downloaded yet, or a stale lock; delete `~/.cache/puppeteer` and rerun |
| Cover text renders as boxes | no CJK font found; install `fonts-noto-cjk` |
| epubcheck: `item … has no fallback` | a non-core image format slipped through; the builder converts webp/bmp/tiff/avif to PNG |
| epubcheck: `RSC-012 Fragment identifier is not defined` | a `#anchor` found no heading: link text is kept as plain text, so a real hit means the anchor and the heading disagree — check `resolve_fragment()` (verbatim source anchor → our slug → loose `anchor_key()` → stop-word-insensitive key) rather than exact matching |
| A scraped TOC entry sits as plain text | its label differs from the heading (`…of Final Rule` vs `…of the Final Rule`, `#sectno-reference-820.1` vs no such heading); add a tier to `resolve_fragment()` only if the intended target is actually derivable from the label |
| epubcheck: `OPF-014 The property "mathml" should be declared` | a page containing `<math>` was listed without `properties="mathml"`; the manifest loop now detects `<math` in the built document |
| `$` amounts show up as italic pseudo-math | `looks_like_math()` rejected them (a `$…$` pair spanning prose, e.g. two currency figures); extend that guard rather than the regexes |
| `$$…$$` block left verbatim | block math must be matched over the whole prose run — `extract_math()` joins consecutive non-code chunks before substituting, because a `$$` block spans several lines |
| Prose inside `$$…$$` turned into italic pseudo-math | `looks_like_math()` also gates block math, so a stray delimiter pair around prose stays readable text |
| TOC missing a whole section | the root index note was not found; `find_root_doc()` expects `<bookname>.md` inside the folder, else any top-level `.md` |

## 6. Extending

- New fence languages: add an alias to `CODE_ALIASES` (Pygments already knows
  the target lexer).
- New SiYuan custom properties: extend `SIYUAN_COLORS` / `SIYUAN_CARDS` **and**
  add the matching rules to `CSS`.
- Different cover art: `make_cover()` accepts title, subtitle and the list of
  top-level sections; it uses `NotoSansCJK-*.ttc` when present.
- The scratch tree under `<workspace>/.build/<Book>/` is a complete unpacked
  EPUB — hand-edit and re-zip it there when experimenting, then move the change
  back into the script.
