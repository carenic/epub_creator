#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""siyuan-epub-builder — build an EPUB 3 book from a SiYuan-exported markdown folder.

Convention
----------
Put the export folder (e.g. ``Programming.md/``) into the workspace and run this
script.  The book and the output file are siblings:

    <workspace>/
    ├── Programming.md/          <- the SiYuan export folder  (input)
    └── Programming.epub         <- generated book            (output)

The table of contents is **reconstructed from the folder contents only** — no
screenshot or side-car outline is needed:

1. every index note (``Matlab.md``) lists its children in document order and that
   order wins;
2. the sibling directory (``Matlab/``) supplies anything the index note missed;
3. directories/files become sections/chapters, to any nesting depth.

Rendered elements: headings, nested lists, GFM task lists, tables (markdown and
raw HTML, including markdown inside cells), syntax-highlighted code blocks,
mermaid diagrams (rendered to PNG), embedded images, blockquotes, intra-book and
external links, SiYuan inline colour styling, and LaTeX math (converted to
MathML).

Usage
-----
    python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py [options] [BOOK_FOLDER ...]

Options
    -w, --workspace DIR   workspace root (default: current directory)
    -b, --book DIR        a book folder to build (may be repeated)
        --check           run epubcheck on the result (needs .tools/, see SKILL.md)
        --clean           discard the cached scratch/mermaid directory first
    -h, --help            show this help

Requires no manual dependency setup: the script re-executes itself through
``uv`` when its Python packages are missing.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

# --------------------------------------------------------------------------
# dependency bootstrap: re-exec under `uv` when packages are unavailable
# --------------------------------------------------------------------------

PKGS = ["markdown-it-py", "mdit-py-plugins", "pygments", "lxml", "pillow",
        "latex2mathml"]


def _ensure_deps() -> None:
    try:
        import lxml  # noqa: F401
        import markdown_it  # noqa: F401
        import mdit_py_plugins  # noqa: F401
        import pygments  # noqa: F401
        import PIL  # noqa: F401
        return
    except ImportError:
        misses = "missing Python packages"
    if os.environ.get("EPUB_BUILDER_BOOTSTRAPPED"):
        sys.exit(f"{misses}; run manually with:\n  uv run --no-project "
                 + " ".join(f"--with {p}" for p in PKGS) + f" python {__file__}")
    uv = shutil.which("uv")
    if not uv:
        sys.exit(f"{misses} and `uv` is not on PATH.\nInstall uv or run:\n  uv run "
                 + " ".join(f"--with {p}" for p in PKGS) + f" python {__file__}")
    env = dict(os.environ, EPUB_BUILDER_BOOTSTRAPPED="1")
    cache = Path(env.get("UV_CACHE_DIR") or Path.cwd() / ".cache" / "uv")
    cache.mkdir(parents=True, exist_ok=True)
    env["UV_CACHE_DIR"] = str(cache)
    argv = [uv, "run", "--no-project", "--quiet"]
    for p in PKGS:
        argv += ["--with", p]
    argv += ["python", str(Path(__file__).resolve()), *sys.argv[1:]]
    os.execve(uv, argv, env)


_ensure_deps()

from lxml import etree, html as lhtml                       # noqa: E402
from markdown_it import MarkdownIt                          # noqa: E402
from mdit_py_plugins.tasklists import tasklists_plugin      # noqa: E402
from pygments import highlight                              # noqa: E402
from pygments.formatters import HtmlFormatter               # noqa: E402
from pygments.lexers import get_lexer_by_name               # noqa: E402
from pygments.util import ClassNotFound                     # noqa: E402
from PIL import Image, ImageDraw, ImageFont                 # noqa: E402

BOOK_SUFFIX = ".md"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".tif",
              ".tiff", ".avif"}
CORE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg"}
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "gif": "image/gif", "svg": "image/svg+xml"}
MAX_IMG_W = 1600        # embedded screenshots
MAX_DIAGRAM_W = 2200    # rendered mermaid diagrams
MATHML_NS = "http://www.w3.org/1998/Math/MathML"

log_lock = False


def log(*a) -> None:
    print(*a, flush=True)


# ==========================================================================
# small helpers
# ==========================================================================


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-") or "x"


def file_slug(text: str, fallback_seed: str = "") -> str:
    """ASCII-only file stem: readable when possible, hashed when the title is
    not representable in ASCII (keeps every href in the package plain ASCII)."""
    ascii_part = re.sub(r"[^a-z0-9]+", "-", slugify(text)).strip("-")
    if len(ascii_part) >= 2:
        return ascii_part[:48]
    seed = fallback_seed or text
    return "n" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:10]


ANCHOR_STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "for", "in", "on", "at", "by"}


def anchor_key(text: str, drop_stopwords: bool = False) -> str:
    """Loose identity of a heading/anchor name: every letter or digit, nothing
    else. Scraped notes link with the *source* site's anchor style
    (`#ii-table-of-abbreviationscommonly-used-acronyms`, `#part-4regulation-of…`)
    while our heading ids use our own slug style, so exact matching is hopeless;
    comparing `anchor_key` on both sides survives punctuation, slashes,
    apostrophes and truncation-free differences. `drop_stopwords` is the last
    resort for a TOC whose label is abbreviated (`…of Final Rule` vs the heading
    `…of the Final Rule`)."""
    t = unicodedata.normalize("NFKC", text).lower()
    if drop_stopwords:
        words = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", t).split()
        t = "".join(w for w in words if w not in ANCHOR_STOPWORDS)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", t)


def resolve_fragment(frag: str, url_ids: dict, slug_ids: dict, anchor_ids: dict,
                     fuzzy_ids: dict | None = None) -> str | None:
    """Resolve a `#fragment` against this page's headings, most precise first:
    verbatim source anchor -> our own slug of the heading text -> loose key ->
    stop-word-insensitive key. Later tiers only ever add matches that would
    otherwise be dropped, so precision can never regress."""
    frag = unquote(frag).strip()
    if not frag:
        return None
    return (url_ids.get(frag) or slug_ids.get(frag.lower())
            or anchor_ids.get(anchor_key(frag))
            or (fuzzy_ids or {}).get(anchor_key(frag, drop_stopwords=True)))


def split_front_matter(text: str):
    meta: dict[str, str] = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm, body = text[3:end], text[end + 4:]
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            return meta, body.lstrip("\n")
    return meta, text


def clean_source_text(text: str) -> str:
    """Strip invisible characters SiYuan leaves behind (zero-width space used
    between the section number and the title, BOM, soft hyphen)."""
    for ch in ("\u200b", "\ufeff", "\u00ad"):
        text = text.replace(ch, "")
    return text


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ==========================================================================
# 1. reconstruct the book tree from the exported folder
# ==========================================================================

# CommonMark link destination: bare destinations may contain *balanced*
# parentheses — SiYuan keeps them, e.g. ``CMake生成器(generator).md``.
LINK_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?\[(?P<label>[^\]]*)\]"
    r"\((?P<href>(?:[^()\s]|\([^()\s]*\))+)\)[ \t]*$",
    re.M,
)


class Node:
    def __init__(self, path: Path, book_dir: Path):
        self.path = path.resolve()
        self.rel = self.path.relative_to(book_dir.resolve())
        self.meta, raw = split_front_matter(read_text(self.path))
        self.body = clean_source_text(raw)
        self.title = (self.meta.get("title") or self.path.stem).strip()
        self.children: list["Node"] = []
        self.doc: str | None = None
        self.num = ""
        self.depth = 0
        self.orphan_links = 0

    @property
    def rel_posix(self) -> str:
        return PurePosixPath(self.rel.as_posix()).as_posix()

    @property
    def slug(self) -> str:
        return file_slug(self.path.stem, self.rel_posix)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


def child_paths(node: Node, book_dir: Path) -> list[Path]:
    """Children in reading order: index-note links first, then whatever else
    lives in the sibling directory (so a missing index note is not fatal)."""
    linked: list[Path] = []
    for m in LINK_RE.finditer(node.body):
        href = m.group("href").strip()
        if href.startswith(("http://", "https://", "mailto:", "tel:", "#", "data:")):
            continue
        raw = unquote(href.split("#")[0])
        if not raw.lower().endswith(".md"):
            continue
        target = (node.path.parent / raw).resolve()
        if not (target.is_file() and target != node.path and target not in linked):
            continue
        if not target.is_relative_to(book_dir.resolve()):   # never escape the book
            continue
        linked.append(target)

    sibling_dir = node.path.parent / node.path.stem
    found = sorted((p.resolve() for p in sibling_dir.glob("*.md") if p.is_file()),
                   key=lambda p: natural_key(p.name)) if sibling_dir.is_dir() else []
    linked_set = set(linked)
    return linked + [p for p in found if p not in linked_set]


def build_tree(path: Path, book_dir: Path, depth: int = 0) -> Node:
    node = Node(path, book_dir)
    node.depth = depth
    for child in child_paths(node, book_dir):
        node.children.append(build_tree(child, book_dir, depth + 1))
    return node


def flat_chapters(book_dir: Path, root_path: Path) -> list[Node]:
    """No index notes at all: every markdown file under the export folder is a
    chapter, ordered by its ``date`` front matter and then by name."""
    paths = [p for p in book_dir.rglob("*.md") if p.is_file() and p.resolve() != root_path.resolve()]
    nodes = [Node(p, book_dir) for p in paths]
    nodes.sort(key=lambda n: (n.meta.get("date", ""), natural_key(n.rel_posix)))
    return nodes


def find_root_doc(book_dir: Path, name: str) -> Path | None:
    cand = book_dir / f"{name}.md"
    if cand.is_file():
        return cand
    tops = sorted((p for p in book_dir.glob("*.md") if p.is_file()), key=lambda p: natural_key(p.name))
    if tops:
        for p in tops:
            if p.stem.lower() in ("index", "readme", "index.md"):
                return p
        return tops[0]
    inner = sorted((p for p in book_dir.rglob("*.md") if p.is_file()), key=lambda p: natural_key(p.as_posix()))
    return inner[0] if inner else None


# ==========================================================================
# 2. math: pull LaTeX out before markdown parsing, put MathML back after
# ==========================================================================

FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1", re.S)
BLOCK_MATH_RE = re.compile(r"(?<!\\)\$\$(?!\$)(.+?)(?<!\\)\$\$", re.S)
INLINE_MATH_RE = re.compile(r"(?<![\$\\])\$(?![\s$])((?:[^\n$\\]|\\.)+?)(?<![\s\\])\$(?!\$)")


DROPPED_LINKS: list[str] = []          # hrefs degraded to plain text (reported)


def _math_segments(src: str):
    """Yield ``(text, in_code)`` so math is only extracted from prose."""
    fenced = False
    for line in src.split("\n"):
        if FENCE_RE.match(line):
            fenced = not fenced
            yield line + "\n", True
            continue
        if fenced:
            yield line + "\n", True
            continue
        pos = 0
        for m in INLINE_CODE_RE.finditer(line):
            yield line[pos:m.start()], False
            yield m.group(0), True
            pos = m.end()
        yield line[pos:], False
        yield "\n", False


def looks_like_math(body: str) -> bool:
    """Guard for `$…$`: prose uses the dollar sign too, and a pair of currency
    amounts reads as a perfect inline-math span ('$532 million at a 7 percent
    discount rate (cost savings:$'), which used to turn regulatory cost text
    into italic pseudo-math. Block `$$…$$` is unambiguous and skips this."""
    if re.search(r"\\[a-zA-Z]+", body):                       # \alpha, \frac, \text
        return True
    if re.search(r"[\\^_{}=+*/<>≤≥≠±×÷∑∫√∞]", body):          # operators / sub-superscripts
        return True
    if not re.search(r"\s", body):                            # a single bare token: $x$, $3.14$
        return True
    # Whitespace inside: only accept when every alphabetic run is math-short
    # ($a b$, $max(a, b)$); 'million', 'percent', 'discount', 'costs' are prose.
    return all(len(w) <= 3 for w in re.findall(r"[A-Za-z]+", body))


def extract_math(src: str) -> tuple[str, dict[str, tuple[str, bool]]]:
    table: dict[str, tuple[str, bool]] = {}
    out: list[str] = []
    counter = 0

    def _block(m):
        nonlocal counter
        body = m.group(1).strip()
        # A `$$…$$` pair is usually a formula, but a stray pair around prose
        # would render as unreadable pseudo-math ('Thisparagraphisprose'); so a
        # block must look like math too, and must not swallow a whole chapter.
        if not looks_like_math(body) or body.count("\n") > 40 or len(body) > 4000:
            return m.group(0)
        key = f"@@MATHBLOCK{counter}@@"
        table[key] = (body, True)
        counter += 1
        return key

    def _inline(m):
        nonlocal counter
        if not looks_like_math(m.group(1).strip()):
            return m.group(0)
        key = f"@@MATHINLINE{counter}@@"
        table[key] = (m.group(1).strip(), False)
        counter += 1
        return key

    def _substitute(text: str) -> str:
        if "$" not in text:
            return text
        text = BLOCK_MATH_RE.sub(_block, text)
        return INLINE_MATH_RE.sub(_inline, text)

    # Prose chunks are joined before substitution: a `$$…$$` block spans several
    # lines, so matching line by line would never see its closing delimiter.
    # Fenced/inline code still breaks the run, so `$` inside code is untouched.
    buf: list[str] = []
    for text, in_code in _math_segments(src):
        if in_code:
            if buf:
                out.append(_substitute("".join(buf)))
                buf = []
            out.append(text)
        else:
            buf.append(text)
    if buf:
        out.append(_substitute("".join(buf)))
    return "".join(out), table


def latex_to_mathml(latex: str, display: bool) -> str:
    try:
        from latex2mathml.converter import convert
        body = convert(latex, display="block" if display else "inline")
    except Exception as exc:                                     # pragma: no cover
        log(f"    ! LaTeX 转换失败 ({exc}): {latex[:60]}")
        return f'<code class="math-src">{html.escape(latex)}</code>'
    if "xmlns" not in body.split(">", 1)[0]:
        body = body.replace("<math", f'<math xmlns="{MATHML_NS}"', 1)
    return body


def restore_math(fragment: str, table: dict[str, tuple[str, bool]]) -> str:
    for key, (latex, display) in table.items():
        ml = latex_to_mathml(latex, display)
        if display:
            fragment = fragment.replace(f"<p>{key}</p>", f'<div class="math-block">{ml}</div>')
            fragment = fragment.replace(key, f'<div class="math-block">{ml}</div>')
        else:
            fragment = fragment.replace(key, ml)
    return fragment


# ==========================================================================
# 3. markdown rendering helpers
# ==========================================================================

md = (
    MarkdownIt("commonmark", {"html": True, "xhtmlOut": True, "breaks": False})
    .enable(["table", "strikethrough"])
    .use(tasklists_plugin, enabled=True)
)

CODE_ALIASES = {"shell": "bash", "sh": "bash", "ps1": "powershell", "txt": "text",
                "plaintext": "text", "ts": "typescript", "js": "javascript",
                "c++": "cpp", "console": "text", "conf": "ini", "yml": "yaml",
                "md": "markdown", "py": "python", "rs": "rust", "golang": "go"}

formatter = HtmlFormatter(nowrap=True, cssclass="hl")


def highlight_code(code: str, lang: str) -> str:
    """Highlight a fence; unknown/absent languages stay plain (many bare fences
    in these notes are ASCII diagrams where colouring would only add noise)."""
    lang = (lang or "").strip().lower()
    lexer = None
    if lang and lang not in ("text", "txt", "plaintext", "none"):
        try:
            lexer = get_lexer_by_name(CODE_ALIASES.get(lang, lang))
        except ClassNotFound:
            lexer = None
    if lexer is None:
        return '<pre class="code"><code>' + html.escape(code) + "</code></pre>"
    return '<pre class="code"><code>' + highlight(code, lexer, formatter).rstrip("\n") + "</code></pre>"


md.options["highlight"] = lambda code, lang, attrs="": highlight_code(code, lang)


def inline_md(text: str) -> str:
    return md.renderInline(text)


BLOCK_CLOSE_RE = re.compile(
    r"^[ \t]*</(?:table|div|figure|figcaption|details|section|blockquote|dl|ul|ol|p)>[ \t]*$",
    re.M,
)


def unpin_html_blocks(text: str) -> str:
    """markdown-it keeps a raw HTML block open until a blank line, so markdown
    written directly after e.g. ``</table>`` would be swallowed verbatim."""
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        if BLOCK_CLOSE_RE.match(line):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.strip():
                out.append("")
    return "\n".join(out)


# ---- SiYuan inline styling ------------------------------------------------

SIYUAN_COLORS = {
    "1": "sy-c1", "2": "sy-c2", "3": "sy-c3", "4": "sy-c4", "5": "sy-c5",
    "6": "sy-c6", "7": "sy-c7", "8": "sy-c8", "9": "sy-c9", "10": "sy-c10",
    "11": "sy-c11", "12": "sy-c12", "13": "sy-c13",
}
SIYUAN_CARDS = {
    "var(--b3-card-error-background)": "sy-card-error",
    "var(--b3-card-warning-background)": "sy-card-warn",
    "var(--b3-card-info-background)": "sy-card-info",
    "var(--b3-card-success-background)": "sy-card-ok",
}


def style_to_classes(style: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"(color|background-color)\s*:\s*([^;]+)", style or ""):
        prop, val = m.group(1), m.group(2).strip()
        card = SIYUAN_CARDS.get(val)
        if card:
            out.append(card + ("-bg" if prop == "background-color" else ""))
            continue
        cm = re.search(r"var\(--b3-font-(?:color|background)(\d+)\)", val)
        if cm and cm.group(1) in SIYUAN_COLORS:
            out.append(SIYUAN_COLORS[cm.group(1)] + ("-bg" if prop == "background-color" else ""))
    return out


# ==========================================================================
# 4. assets: images and mermaid diagrams
# ==========================================================================


def make_image_writer(oebps: Path):
    used: set[str] = {p.name for p in (oebps / "images").iterdir()} if (oebps / "images").is_dir() else set()

    def write(src: Path, book_dir: Path, limit: int = MAX_IMG_W) -> str:
        rel = src.relative_to(book_dir)
        owner = rel.parent.name
        if owner == "assets" and rel.parent.parent != PurePosixPath("."):
            owner = rel.parent.parent.name
        stem = file_slug(src.stem, rel.as_posix())
        ext = src.suffix.lower()
        name = f"{slugify(owner)[:24]}-{stem}" if owner not in ("", ".", "assets") else stem
        if ext not in CORE_IMAGE_EXTS:
            ext = ".png"                     # webp/bmp/... are not EPUB core types
        candidate, n = f"{name}{ext}", 2
        while candidate in used:
            candidate, n = f"{name}-{n}{ext}", n + 1
        used.add(candidate)
        dst = oebps / "images" / candidate
        try:
            with Image.open(src) as im:
                if ext == ".png" and im.mode not in ("RGB", "RGBA", "L", "P"):
                    im = im.convert("RGBA")
                if im.width > limit:
                    im = im.resize((limit, max(1, int(im.height * limit / im.width))), Image.LANCZOS)
                if im.mode == "P":
                    im = im.convert("RGBA" if "transparency" in im.info else "RGB")
                if im.mode in ("RGBA", "LA") and ext in (".jpg", ".jpeg"):
                    im = im.convert("RGB")
                im.save(dst, optimize=True)
        except Exception as exc:
            log(f"    ! copy as-is {src.name}: {exc}")
            shutil.copy2(src, dst)
        return f"images/{candidate}"

    return write


def collect_mermaid_blocks(book_dir: Path) -> list[str]:
    blocks: list[str] = []
    for f in sorted(book_dir.rglob("*.md")):
        for m in re.finditer(r"^```mermaid[ \t]*\n(.*?)^```", read_text(f), re.S | re.M):
            blocks.append(m.group(1).strip())
    return list(dict.fromkeys(blocks))


def render_mermaid(blocks: list[str], work: Path, oebps: Path) -> dict[str, str]:
    if not blocks:
        return {}
    tmp = work / "mermaid"
    tmp.mkdir(parents=True, exist_ok=True)
    (work / "mmdc.json").write_text(json.dumps({
        "theme": "neutral",
        "flowchart": {"htmlLabels": False, "useMaxWidth": False},
        "sequence": {"useMaxWidth": False},
        "themeVariables": {
            "fontFamily": "Noto Sans CJK SC, Source Han Sans SC, PingFang SC, Microsoft YaHei, sans-serif",
            "fontSize": "16px",
        },
    }), encoding="utf-8")
    (work / "puppeteer.json").write_text(json.dumps({
        "args": ["--no-sandbox", "--disable-setuid-sandbox", "--font-render-hinting=none"]
    }), encoding="utf-8")

    bun = shutil.which("bun")
    mapping: dict[str, str] = {}
    for i, code in enumerate(blocks):
        mmd, png = tmp / f"d{i:03d}.mmd", tmp / f"d{i:03d}.png"
        mmd.write_text(code + "\n", encoding="utf-8")
        name = f"diagram-{i:03d}.png"
        dst = oebps / "images" / name
        if not png.exists():
            if not bun:
                log("    ! 未找到 bun，跳过 mermaid 渲染")
                continue
            log(f"    · mermaid 图 {i:03d} …")
            r = subprocess.run([
                bun, "x", "--bun", "@mermaid-js/mermaid-cli@11",
                "-i", str(mmd), "-o", str(png),
                "-p", str(work / "puppeteer.json"),
                "-c", str(work / "mmdc.json"),
                "-b", "white", "-s", "3",
            ], capture_output=True, text=True, timeout=1800)
            if r.returncode != 0 or not png.exists():
                log(f"    ! mermaid 图 {i:03d} 渲染失败，源码将以代码块保留")
                log((r.stdout or "")[-800:], (r.stderr or "")[-800:])
                continue
        with Image.open(png) as im:
            im = im.convert("RGB")
            if im.width > MAX_DIAGRAM_W:
                im = im.resize((MAX_DIAGRAM_W, int(im.height * MAX_DIAGRAM_W / im.width)), Image.LANCZOS)
            im.quantize(colors=128, method=Image.MEDIANCUT).save(dst, optimize=True, bits=8)
        mapping[code] = f"images/{name}"
    return mapping


# ==========================================================================
# 5. XHTML post-processing
# ==========================================================================

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "col",
    "colgroup", "dd", "del", "div", "dl", "dt", "em", "figcaption", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd", "li", "mark",
    "ol", "p", "pre", "q", "s", "samp", "small", "span", "strong", "sub",
    "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul", "var",
}
KEEP_ATTRS = {"a": {"href"}, "img": {"src", "alt"}, "td": {"colspan", "rowspan"},
              "th": {"colspan", "rowspan", "scope"}, "colgroup": {"span"},
              "ol": {"start"}}


def in_highlighted_code(el) -> bool:
    p = el.getparent()
    while p is not None:
        if p.tag == "pre" and "code" in (p.get("class") or ""):
            return True
        p = p.getparent()
    return False


def clean_attrs(root) -> None:
    """Drop SiYuan bookkeeping, unknown tags and unsafe attributes."""
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if in_highlighted_code(el):
            continue
        if el.tag not in ALLOWED_TAGS:
            el.tag = "span"
        classes = (el.get("class") or "").split()
        style = el.get("style")
        if style:
            classes += style_to_classes(style)
        keep = {}
        for a in KEEP_ATTRS.get(el.tag, set()):
            if el.get(a) is not None:
                keep[a] = el.get(a)
        el.attrib.clear()
        el.attrib.update(keep)
        if classes:
            el.set("class", " ".join(dict.fromkeys(classes)))


def cell_markdown(cell) -> None:
    """Render markdown written inside a raw-HTML table cell."""
    if any(e.tag in ("strong", "em", "code", "a", "del") for e in cell.iter()):
        return
    for br in cell.iter("br"):
        br.tail = "\n" + (br.tail or "")
    text = cell.text_content()
    if not re.search(r"\*\*|`|~~|\[[^\]]+\]\(|^\s*[-*] ", text, re.M):
        return
    for child in list(cell):
        cell.remove(child)
    cell.text = None
    for i, line in enumerate(text.split("\n")):
        if i:
            cell.append(lhtml.fragment_fromstring("<br/>"))
        cell.append(lhtml.fragment_fromstring(f"<span>{inline_md(line)}</span>"))


def unwrap_heading_links(root) -> list[tuple]:
    """Headings in scraped documents are often wrapped in links; unwrap them and
    keep the fragments so external '#' links can be re-pointed inside the book."""
    captured = []
    for h in root.iter("h1", "h2", "h3", "h4", "h5", "h6"):
        frags = [a.get("href", "").split("#")[-1] for a in h.iter("a")
                 if "#" in (a.get("href") or "")]
        captured.append(frags)
        for a in list(h.iter("a")):
            text = a.text_content()
            parent = a.getparent()
            if parent is None:
                continue
            pos = parent.index(a)
            parent.remove(a)
            if text.strip():
                parent.insert(pos, lhtml.fragment_fromstring(f"<span>{html.escape(text)}</span>"))
    return captured


def strip_backlinks(root) -> None:
    """Scraped notes carry '¶' / 'Back to Citation' / 'Back to top' round-trip
    links that are meaningless once the page lives in a book."""
    noise = {"¶", "↩", "#", "", "back to citation", "back to top", "回到顶部", "返回顶部"}
    for a in list(root.iter("a")):
        if (a.text_content() or "").strip().lower() in noise:
            parent = a.getparent()
            if parent is not None:
                parent.remove(a)


def convert_task_boxes(root) -> None:
    for inp in list(root.iter("input")):
        done = "checked" in inp.attrib
        span = lhtml.fragment_fromstring(
            f'<span class="task{" done" if done else ""}">{"☑" if done else "☐"}</span>')
        parent = inp.getparent()
        if parent is not None:
            parent.replace(inp, span)


def nearest_figure(el):
    p = el.getparent()
    while p is not None:
        if p.tag == "figure":
            return p
        p = p.getparent()
    return None


class LinkResolver:
    """Maps markdown note references (relative, percent-encoded, addressed from
    any note directory or from the notebook root) onto XHTML documents."""

    def __init__(self, book_name: str) -> None:
        self.by_rel: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        self.base_dir = PurePosixPath(".")
        self.book_name = book_name

    def add(self, rel_posix: str, doc: str) -> None:
        key = PurePosixPath(rel_posix).as_posix()
        self.by_rel[key] = doc
        self.by_name.setdefault(PurePosixPath(key).name, doc)

    def lookup(self, href: str) -> str | None:
        path = unquote(href.split("#")[0])
        if not path:
            return None
        for candidate in (PurePosixPath(path), self.base_dir / path):
            norm = PurePosixPath(posixpath.normpath(candidate.as_posix())).as_posix()
            if norm in self.by_rel:
                return self.by_rel[norm]
            for prefix in (f"{self.book_name}/", ""):   # root-relative references
                if prefix + norm in self.by_rel:
                    return self.by_rel[prefix + norm]
        return self.by_name.get(PurePosixPath(path).name)


CALLOUT_RE = re.compile(r"^\s*\[!\s*(?P<kind>[A-Za-z]+)\s*\]\s*")
CALLOUT_KINDS = {"note": "note", "info": "note", "abstract": "note", "todo": "note",
                 "tip": "tip", "success": "tip", "important": "important",
                 "warning": "warning", "caution": "caution", "danger": "danger",
                 "question": "note", "example": "note", "quote": "note", "failure": "danger"}
CALLOUT_LABELS = {"note": "Note", "tip": "Tip", "important": "Important",
                  "warning": "Warning", "caution": "Caution", "danger": "Danger"}


def convert_callouts(root) -> None:
    """Turn GitHub/Obsidian alerts (`> [!NOTE] Title`) into styled callouts — the
    raw marker otherwise leaks into the text as '[!NOTE] ✏️ Summary …'."""
    for bq in root.iter("blockquote"):
        p = bq.find("p")
        if p is None:
            continue
        holder = p
        if not (p.text or "").strip():
            holder = next((el for el in p.iter() if el is not p and (el.text or "").strip()), None)
        if holder is None:
            continue
        m = CALLOUT_RE.match(holder.text or "")
        kind = CALLOUT_KINDS.get(m.group("kind").lower()) if m else None
        if not kind:
            continue
        title, _, body = (holder.text or "")[m.end():].partition("\n")
        holder.text = body.lstrip("\n")
        bq.set("class", f"callout callout-{kind}")
        label = title.strip() or CALLOUT_LABELS.get(kind, kind.title())
        bq.insert(0, lhtml.fragment_fromstring(
            f'<p class="callout-title">{html.escape(label)}</p>'))
        if not p.text_content().strip():
            bq.remove(p)


def process_fragment(frag: str, chapter_slug: str, image_map: dict,
                     link_map: LinkResolver | None, math_table: dict) -> str:
    root = lhtml.fragment_fromstring(f"<div id='__root'>{frag}</div>")

    convert_task_boxes(root)
    strip_backlinks(root)
    heading_hrefs = unwrap_heading_links(root)
    clean_attrs(root)
    convert_callouts(root)

    # ---- tables ----------------------------------------------------------
    for table in root.iter("table"):
        for cell in table.iter("td", "th"):
            cell_markdown(cell)
        if table.find(".//thead") is None:
            body = table.find("tbody")
            if body is not None and len(body):
                thead = lhtml.fragment_fromstring("<thead></thead>")
                thead.append(body[0])
                table.insert(0, thead)
        table.set("class", "table")
        wrap = lhtml.fragment_fromstring('<div class="table-wrap"></div>')
        table.addprevious(wrap)
        wrap.append(table)

    # ---- images ----------------------------------------------------------
    for img in list(root.iter("img")):
        src = img.get("src", "")
        alt = (img.get("alt") or "").strip()
        target = None
        if src.startswith("images/"):
            target = src            # already an asset we generated (mermaid)
        else:
            target = image_map.get(unquote(src)) or image_map.get(Path(unquote(src)).name)
        if not target:
            log(f"    · 未找到图片，已移除: {src}")
            parent = img.getparent()
            if parent is not None:
                parent.remove(img)
            continue
        img.set("src", "../" + target)
        img.set("alt", alt)
        fig = nearest_figure(img)
        if fig is not None:
            fig.set("class", "fig")
            if fig.find("figcaption") is None and alt:
                fig.append(lhtml.fragment_fromstring(
                    f"<figcaption>{html.escape(alt)}</figcaption>"))
        else:
            parent = img.getparent()
            if parent is not None and parent.tag == "p" and len(parent) == 1 \
                    and not (parent.text or "").strip():
                parent.set("class", "fig-para")
            elif parent is not None and parent.tag == "p":
                newfig = lhtml.fragment_fromstring('<figure class="fig"></figure>')
                parent.addnext(newfig)
                newfig.append(img)
                if alt:
                    newfig.append(lhtml.fragment_fromstring(
                        f"<figcaption>{html.escape(alt)}</figcaption>"))
            else:
                img.set("class", "inline-img")

    # ---- headings: stable ids --------------------------------------------
    url_ids: dict[str, str] = {}          # exact source fragment -> our id
    anchor_ids: dict[str, str] = {}       # loose anchor_key() -> our id
    fuzzy_ids: dict[str, str] = {}        # stop-word-insensitive key -> our id
    slug_ids: dict[str, str] = {}         # our own slug of the heading text -> id
    used: set[str] = set()
    for h, frags in zip(root.iter("h1", "h2", "h3", "h4", "h5", "h6"), heading_hrefs):
        text = h.text_content().strip()
        # MathML is substituted only at the very end (the sanitiser would strip
        # it), so a heading containing math still carries @@MATH…@@ here: slug
        # the LaTeX instead of the placeholder.
        label = text
        for key, (latex, _display) in (math_table or {}).items():
            label = label.replace(key, latex)
        label = re.sub(r"@@MATH(?:INLINE|BLOCK)\d+@@", "", label).strip() or text
        base = f"{chapter_slug}-{file_slug(label, label)[:40]}"
        hid, n = base, 2
        while hid in used:
            hid, n = f"{base}-{n}", n + 1
        used.add(hid)
        h.set("id", hid)
        # Two maps with different precision: an exact slug match beats a loose
        # key match, so documents that repeat a heading name ('A. Introduction'
        # under two parents) still link to the right one.
        for cand in {slugify(label), file_slug(label, label)}:
            slug_ids.setdefault(cand, hid)
        key = anchor_key(label)
        if key:
            anchor_ids.setdefault(key, hid)
        fkey = anchor_key(label, drop_stopwords=True)
        if fkey:
            fuzzy_ids.setdefault(fkey, hid)
        for f in frags:
            if f and f not in url_ids:
                url_ids[f] = hid
                anchor_ids.setdefault(anchor_key(f), hid)

    # ---- links -----------------------------------------------------------
    def drop_dead_link(a) -> None:
        """Keep the visible label, discard the link: a dangling in-book anchor
        would otherwise make epubcheck fail with RSC-012."""
        text = a.text_content()
        parent = a.getparent()
        if parent is None:
            return
        DROPPED_LINKS.append(a.get("href", ""))
        pos = parent.index(a)
        parent.remove(a)
        if text.strip():
            parent.insert(pos, lhtml.fragment_fromstring(f"<span>{html.escape(text)}</span>"))

    for a in list(root.iter("a")):
        href = a.get("href", "")
        for attr in ("target", "rel", "title", "class"):
            a.attrib.pop(attr, None)
        resolved = link_map.lookup(href) if link_map else None
        if resolved:
            a.set("href", resolved)
        elif href.startswith("#"):
            target = resolve_fragment(href[1:], url_ids, slug_ids, anchor_ids, fuzzy_ids)
            if target:
                a.set("href", "#" + target)
            else:
                drop_dead_link(a)
                continue
        elif href.split("#")[0].startswith(("http://", "https://", "mailto:", "tel:")):
            if "#" in href:
                target = resolve_fragment(href.split("#")[-1], url_ids, slug_ids,
                                          anchor_ids, fuzzy_ids)
                if target:
                    a.set("href", "#" + target)
                    continue
            a.set("href", href)
        else:
            drop_dead_link(a)
            continue
        if not a.get("href"):
            a.set("href", "#")

    # ---- drop a leading separator rule -----------------------------------
    for el in list(root)[:2]:
        if el.tag == "hr":
            root.remove(el)
            break

    inner = "".join(etree.tostring(c, encoding="unicode", method="xml") for c in root)
    inner = inner.replace("<span></span>", "").replace("<span/>", "")
    return restore_math(inner, math_table)


# ==========================================================================
# 6. CSS, cover, documents
# ==========================================================================

CSS = """\
@charset "utf-8";

@page { margin: 5% 5%; }

body {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", "SimSun",
               Georgia, "Times New Roman", serif;
  line-height: 1.78;
  margin: 0 4%;
  padding: 0;
  text-align: justify;
  overflow-wrap: break-word;
  word-wrap: break-word;
  color: #1b1b1b;
  background: #ffffff;
}

p { margin: 0.55em 0; }

h1, h2, h3, h4, h5, h6 {
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC",
               "Microsoft YaHei", Helvetica, Arial, sans-serif;
  line-height: 1.35;
  font-weight: 700;
  color: #14304d;
  text-align: left;
  page-break-after: avoid;
  break-after: avoid;
}
h1 { font-size: 1.7em; margin: 1.1em 0 0.9em; }
h2 { font-size: 1.4em; margin: 1.5em 0 0.6em; border-bottom: 1px solid #d8e0e8; padding-bottom: 0.25em; }
h3 { font-size: 1.18em; margin: 1.3em 0 0.5em; }
h4 { font-size: 1.06em; margin: 1.2em 0 0.4em; }
h5, h6 { font-size: 1em; margin: 1.1em 0 0.4em; color: #34506b; }

a { color: #0b62a4; text-decoration: none; }

ul, ol { margin: 0.5em 0 0.5em 1.5em; padding: 0; }
li { margin: 0.22em 0; }
li > ul, li > ol { margin: 0.2em 0 0.2em 1.2em; }

blockquote {
  margin: 0.9em 0;
  padding: 0.55em 1em;
  border-left: 4px solid #9dbdd8;
  background: #f4f8fb;
  color: #2c3e50;
}
blockquote p { margin: 0.35em 0; }

blockquote.callout { border-radius: 5px; border-left-width: 5px; }
blockquote.callout .callout-title {
  font-weight: 700; margin: 0 0 0.4em 0; letter-spacing: 0.01em;
}
blockquote.callout-note { border-left-color: #4a90c4; background: #f1f7fd; }
blockquote.callout-note .callout-title { color: #23557d; }
blockquote.callout-tip { border-left-color: #3f9e6a; background: #f0faf4; }
blockquote.callout-tip .callout-title { color: #22633f; }
blockquote.callout-important { border-left-color: #7b62c9; background: #f5f2fd; }
blockquote.callout-important .callout-title { color: #4a3a86; }
blockquote.callout-warning { border-left-color: #d09a2c; background: #fdf8ec; }
blockquote.callout-warning .callout-title { color: #8a6210; }
blockquote.callout-caution, blockquote.callout-danger { border-left-color: #c8564f; background: #fdf3f2; }
blockquote.callout-caution .callout-title, blockquote.callout-danger .callout-title { color: #8d3730; }

hr { border: 0; border-top: 1px solid #d5dde5; margin: 1.6em 0; }

code, kbd, samp, pre {
  font-family: "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono CJK SC",
               Consolas, Menlo, monospace;
}
code {
  font-size: 0.9em;
  background: #f2f4f7;
  border: 1px solid #e3e7ec;
  border-radius: 3px;
  padding: 0.05em 0.3em;
  overflow-wrap: break-word;
}

pre.code {
  font-size: 0.8em;
  line-height: 1.6;
  background: #f7f9fb;
  border: 1px solid #dfe5ec;
  border-radius: 5px;
  padding: 0.7em 0.85em;
  margin: 1em 0;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  word-wrap: break-word;
  tab-size: 4;
  text-align: left;
  page-break-inside: avoid;
}
pre.code code { background: none; border: 0; padding: 0; font-size: 1em; white-space: pre-wrap; }

.table-wrap { margin: 1em 0; }
table.table { border-collapse: collapse; width: 100%; font-size: 0.85em; line-height: 1.5; }
table.table th, table.table td {
  border: 1px solid #cfd8e0;
  padding: 0.4em 0.55em;
  text-align: left;
  vertical-align: top;
  overflow-wrap: break-word;
}
table.table thead th { background: #eef3f8; font-weight: 700; }
table.table tbody tr:nth-child(even) { background: #fafbfd; }

figure.fig, p.fig-para { margin: 1.1em 0; text-align: center; page-break-inside: avoid; }
figure.fig img, p.fig-para img { max-width: 100%; height: auto; border-radius: 4px; }
figure.fig figcaption {
  font-size: 0.82em;
  color: #5b6b7b;
  margin-top: 0.4em;
  font-family: "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", sans-serif;
}
img { max-width: 100%; height: auto; }

.task { font-family: "DejaVu Sans Mono", monospace; margin-right: 0.35em; color: #4a5b6b; }
.task.done { color: #1e7e34; }

.sy-c1 { color: #2f6fb5; } .sy-c2 { color: #1a9e8f; } .sy-c3 { color: #1f6fb2; }
.sy-c4 { color: #b8860b; } .sy-c5 { color: #8e44ad; } .sy-c8 { color: #c0392b; }
.sy-c9 { color: #d35400; } .sy-c10 { color: #7a5cc0; } .sy-c11 { color: #7a5cc0; }
.sy-c12 { color: #0f8b8d; } .sy-c13 { color: #1f6fb2; }
.sy-card-ok, .sy-card-ok-bg { background: #e8f5e9; color: #1b5e20; }
.sy-card-warn, .sy-card-warn-bg { background: #fff8e1; color: #8d6e00; }
.sy-card-info, .sy-card-info-bg { background: #e3f2fd; color: #0d47a1; }
.sy-card-error, .sy-card-error-bg { background: #fdecea; color: #a4282a; }
.sy-c1-bg, .sy-c2-bg, .sy-c3-bg, .sy-c4-bg, .sy-c5-bg, .sy-c8-bg,
.sy-c9-bg, .sy-c10-bg, .sy-c11-bg, .sy-c12-bg, .sy-c13-bg { background: #fdf3d8; }

math { font-size: 1.02em; }
.math-block { display: block; text-align: center; margin: 1em 0; }
.math-src { font-size: 0.95em; }

body.cover { margin: 0; padding: 0; text-align: center; }
body.cover img { max-width: 100%; max-height: 100%; }

body.titlepage { text-align: center; font-family: "Noto Sans CJK SC", "PingFang SC", sans-serif; }
body.titlepage .book-title { font-size: 2.5em; margin: 1.8em 0 0.3em; font-weight: 700; color: #14304d; letter-spacing: 0.04em; }
body.titlepage .book-sub { font-size: 1em; color: #5b6b7b; }
body.titlepage .rule { width: 32%; margin: 1.5em auto; border-top: 2px solid #9dbdd8; }
body.titlepage .part-list { list-style: none; margin: 0; padding: 0; font-size: 1.05em; line-height: 2.1; }
body.titlepage .colophon { margin-top: 3em; font-size: 0.8em; color: #8b98a5; }

body.part h1 { font-size: 2em; margin-top: 0.4em; }
body.part.d2 h1 { font-size: 1.6em; }
.part-kicker {
  font-family: "Noto Sans CJK SC", sans-serif;
  font-size: 0.82em;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: #7d8ea0;
  margin: 1.4em 0 0.2em;
}
.child-toc { list-style: none; margin: 1.4em 0 0 0; padding: 0; }
.child-toc li { margin: 0.5em 0; padding-left: 1.6em; text-indent: -1.6em; }
.child-toc .num { color: #7d8ea0; margin-right: 0.6em; }

nav#toc ol { list-style: none; margin-left: 0.3em; padding-left: 0; }
nav#toc ol ol { margin-left: 1.2em; }
nav#toc li { margin: 0.35em 0; }
nav#toc > ol > li { margin-top: 0.9em; font-weight: 700; }
nav#toc > ol > li > ol > li { font-weight: 400; }

@media (prefers-color-scheme: dark) {
  body { color: #e3e3e3; background: #121212; }
  h1, h2, h3, h4, h5, h6 { color: #9ec8ee; }
  h2 { border-bottom-color: #333c45; }
  a { color: #6fb6ea; }
  code { background: #1e2329; border-color: #303740; color: #e6e6e6; }
  pre.code { background: #1a1f25; border-color: #303740; }
  blockquote { background: #1a2026; border-left-color: #3f6b90; color: #cfd8e0; }
  blockquote.callout-note { background: #16222c; border-left-color: #4a90c4; }
  blockquote.callout-note .callout-title { color: #8ec4ea; }
  blockquote.callout-tip { background: #14231b; border-left-color: #3f9e6a; }
  blockquote.callout-tip .callout-title { color: #8ed6ac; }
  blockquote.callout-important { background: #1e1a2c; border-left-color: #7b62c9; }
  blockquote.callout-important .callout-title { color: #bda9f0; }
  blockquote.callout-warning { background: #26200f; border-left-color: #d09a2c; }
  blockquote.callout-warning .callout-title { color: #ecc46a; }
  blockquote.callout-caution, blockquote.callout-danger { background: #2a1715; border-left-color: #c8564f; }
  blockquote.callout-caution .callout-title,
  blockquote.callout-danger .callout-title { color: #f0a49d; }
  table.table th, table.table td { border-color: #39414a; }
  table.table thead th { background: #1e252c; }
  table.table tbody tr:nth-child(even) { background: #171c21; }
  hr { border-top-color: #333c45; }
  .sy-card-ok, .sy-card-ok-bg { background: #14301a; color: #a5d6a7; }
  .sy-card-warn, .sy-card-warn-bg { background: #2f2a12; color: #ffe082; }
  .sy-card-info, .sy-card-info-bg { background: #10243a; color: #90caf9; }
  .sy-card-error, .sy-card-error-bg { background: #34191a; color: #ef9a9a; }
  .sy-c1-bg, .sy-c2-bg, .sy-c3-bg, .sy-c4-bg, .sy-c5-bg, .sy-c8-bg,
  .sy-c9-bg, .sy-c10-bg, .sy-c11-bg, .sy-c12-bg, .sy-c13-bg { background: #2c2413; }
  .sy-c8 { color: #ff9b8a; }
  figure.fig figcaption { color: #9aa7b3; }
  body.titlepage .colophon { color: #7c8894; }
}
"""


def pygments_css() -> str:
    light = HtmlFormatter(style="friendly").get_style_defs("pre.code")
    dark = HtmlFormatter(style="monokai").get_style_defs("pre.code")
    dark = "\n".join("  " + line for line in dark.splitlines() if line.strip())
    return (
        "/* syntax highlighting (light) */\n" + light
        + "\n\n@media (prefers-color-scheme: dark) {\n"
        + "  pre.code { color: #f8f8f2; }\n" + dark + "\n}\n"
    )


def find_font(bold: bool = False) -> str:
    for cand in (
        f"/usr/share/fonts/opentype/noto/NotoSansCJK-{'Bold' if bold else 'Regular'}.ttc",
        f"/usr/share/fonts/truetype/noto/NotoSansCJK-{'Bold' if bold else 'Regular'}.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    ):
        if Path(cand).exists():
            return cand
    return ""


def _wrap_lines(draw, text: str, font, max_w: int) -> list[str]:
    """Greedy wrap: break at spaces when there are any (latin titles must never
    be cut mid-word — 'Medical Devi/ces ABC'), otherwise per character, which is
    what CJK needs."""
    lines: list[str] = []
    cur = ""
    for token in re.findall(r"\S+\s*", text):
        if cur and draw.textlength((cur + token).rstrip(), font=font) > max_w:
            lines.append(cur.rstrip())
            cur = ""
        cur += token
        while draw.textlength(cur.rstrip(), font=font) > max_w and len(cur.strip()) > 1:
            cut = len(cur.rstrip())
            while cut > 1 and draw.textlength(cur[:cut], font=font) > max_w:
                cut -= 1
            lines.append(cur[:cut].rstrip())
            cur = cur[cut:].lstrip()
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _ellipsize(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    cut = len(text)
    while cut > 1 and draw.textlength(text[:cut] + "…", font=font) > max_w:
        cut -= 1
    return text[:cut].rstrip() + "…"


def _fit_block(draw, text: str, sizes: list[int], bold: bool, max_w: int,
               max_lines: int) -> tuple[list[str], object]:
    """Largest font size from `sizes` whose wrap fits in `max_lines`; overflow is
    ellipsized, so nothing can ever run off the cover."""
    path = find_font(True) if bold else find_font(False)
    font, lines = ImageFont.load_default(), [text]
    for size in sizes:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        lines = _wrap_lines(draw, text, font, max_w)
        if len(lines) <= max_lines:
            return lines, font
    lines = lines[:max_lines]
    lines[-1] = _ellipsize(draw, lines[-1] + "…", font, max_w)
    return lines, font


def make_cover(path: Path, title: str, subtitle: str, parts: list[str]) -> None:
    W, H = 1600, 2400
    img = Image.new("RGB", (W, H), "#0f2537")
    d = ImageDraw.Draw(img)
    top, bottom = (18, 42, 62), (7, 18, 29)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    d.rectangle([0, 0, W, 20], fill=(92, 172, 232))
    d.rectangle([86, 86, W - 86, H - 86], outline=(52, 92, 124), width=3)

    reg, bold = find_font(False), find_font(True)
    if not reg and not bold:
        log("    ! 未找到 CJK 字体，封面文字可能显示为方块")
    small_font = ImageFont.truetype(reg, 42) if reg else ImageFont.load_default()

    def center(text, font, y, fill):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)

    inner_w = W - 320
    title_lines, title_font = _fit_block(d, title, [196, 168, 144, 122, 104], True, inner_w, 3)
    n_parts = min(len(parts), 8)
    # Shrink rather than truncate: a chapter title must stay complete. With many
    # parts there is no room for a second line, so fit them on one.
    max_plines = 2 if n_parts <= 5 else 1
    part_blocks: dict[int, tuple[list[str], object]] = {}
    for i, p in enumerate(parts[:n_parts], 1):
        part_blocks[i] = _fit_block(d, f"{i:02d}   {p}",
                                    [62, 54, 46, 40, 34, 29], False, inner_w, max_plines)

    line_h = int(title_font.size * 1.18) if hasattr(title_font, "size") else 230
    y = 380
    for line in title_lines:
        center(line, title_font, y, (240, 246, 252))
        y += line_h
    d.line([(W * 0.34, y + 60), (W * 0.66, y + 60)], fill=(92, 172, 232), width=4)
    if subtitle:
        y += 120
        for line in _wrap_lines(d, subtitle, small_font, inner_w)[:2]:
            center(line, small_font, y, (168, 200, 226))
            y += 62

    y = max(y + 260, 1080)
    bottom = H - 380                       # stay clear of the footer line
    slot = max(64.0, (bottom - y) / n_parts) if n_parts else 0.0
    for i in range(1, n_parts + 1):
        lines, pfont = part_blocks[i]
        line_h = min(80.0, slot / max(1, len(lines)))
        yy = y + max(0.0, (slot - line_h * len(lines)) / 2)
        for line in lines:
            center(line, pfont, yy, (206, 224, 240))
            yy += line_h
        y += slot

    center("EPUB 3", small_font, H - 300, (102, 134, 162))
    img.save(path, optimize=True)


def wrap_document(title: str, body_inner: str, lang: str, epub_type: str | None = None,
                  body_class: str | None = None) -> str:
    attrs = f' epub:type="{epub_type}"' if epub_type else ""
    if body_class:
        attrs += f' class="{body_class}"'
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}">\n'
        "<head>\n  <meta charset=\"utf-8\"/>\n"
        f"  <title>{html.escape(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="../css/style.css"/>\n'
        '  <link rel="stylesheet" type="text/css" href="../css/pygments.css"/>\n'
        "</head>\n<body" + attrs + ">\n" + body_inner + "\n</body>\n</html>\n"
    )


# ==========================================================================
# 7. the build itself
# ==========================================================================


def detect_lang(texts: list[str]) -> str:
    sample = "".join(texts)[:200000]
    if not sample:
        return "en"
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return "zh-CN" if cjk / max(1, len(sample)) > 0.02 else "en"


def load_config(ws: Path, book_dir: Path, name: str) -> dict:
    for cand in (ws / f"{name}.epub.json", book_dir / "epub.json", book_dir / ".epub.json"):
        if cand.is_file():
            try:
                cfg = json.loads(read_text(cand))
                log(f"  · 读取配置 {cand.name}")
                return cfg if isinstance(cfg, dict) else {}
            except Exception as exc:
                log(f"  ! 配置解析失败 {cand}: {exc}")
    return {}


def plan_documents(root: Node) -> list[Node]:
    """Pre-order walk, assigning hierarchical numbers and unique doc names."""
    nodes: list[Node] = []

    def rec(node: Node, num: str, depth: int) -> None:
        node.num = num
        node.depth = depth
        stem = f"{num.replace('.', '-')}-{node.slug}"
        node.doc = f"text/{stem[:72]}.xhtml"
        nodes.append(node)
        for i, child in enumerate(node.children, 1):
            rec(child, f"{num}.{i}", depth + 1)

    for i, child in enumerate(root.children, 1):
        rec(child, str(i), 1)
    seen: dict[str, int] = {}
    for n in nodes:
        if n.doc in seen:
            seen[n.doc] += 1
            n.doc = n.doc[:-6] + f"-{seen[n.doc]}.xhtml"
        else:
            seen[n.doc] = 1
    return nodes


def prepare_body(node: Node, child_docs: list[Node]) -> str:
    """Strip the note's own H1 (re-emitted as the page heading), the index-note
    link list (replaced by a generated child TOC) and the leading rule."""
    text = node.body
    text = re.sub(r"^#\s+.*?\n", "", text, count=1, flags=re.M)
    if child_docs:
        keep_lines = []
        for line in text.split("\n"):
            m = LINK_RE.match(line)
            if m and m.group("href").split("#")[0].lower().endswith(".md"):
                continue
            keep_lines.append(line)
        text = "\n".join(keep_lines)
    text = re.sub(r"\A\s*(?:-{3,}|\*{3,}|_{3,})\s*\n", "", text, count=1)
    return text


def build_book(book_dir: Path, ws: Path, args) -> Path:
    name = book_dir.name[: -len(BOOK_SUFFIX)]
    out_epub = ws / f"{name}.epub"
    work = ws / ".build" / name
    oebps = work / "OEBPS"
    cfg = load_config(ws, book_dir, name)

    if args.clean and work.exists():
        shutil.rmtree(work)
    if oebps.exists():
        shutil.rmtree(oebps)
    for sub in ("text", "css", "images"):
        (oebps / sub).mkdir(parents=True, exist_ok=True)
    (work / "META-INF").mkdir(parents=True, exist_ok=True)

    log(f"\n▸ [{name}] 重建目录树 …")
    root_path = find_root_doc(book_dir, name)
    if root_path is None:
        raise SystemExit(f"  ! {book_dir} 中没有找到 markdown 文件")
    root = build_tree(root_path, book_dir)
    sections = root.children
    if not sections:
        log("  · 索引笔记没有子链接，按文件系统顺序平铺为章节")
        for i, n in enumerate(flat_chapters(book_dir, root_path), 1):
            n.depth = 1
            n.num = str(i)
            root.children.append(n)
        sections = root.children
    if not sections:
        raise SystemExit(f"  ! {book_dir} 中没有可用的章节")
    for s in sections:
        log(f"    {s.title}: {len(s.children)} 个子节点")

    nodes = plan_documents(root)
    all_nodes = list(root.walk())

    title = (cfg.get("title") or root.meta.get("title") or name).strip()
    subtitle = (cfg.get("subtitle") or "").strip()
    if not subtitle:
        names = [s.title for s in sections]
        subtitle = " · ".join(names[:5]) + (" …" if len(names) > 5 else "")
    lang = cfg.get("language") or detect_lang([n.body for n in all_nodes]) or "zh-CN"

    log("▸ 渲染 mermaid 流程图 …")
    mermaid_map = render_mermaid(collect_mermaid_blocks(book_dir), work, oebps)
    log(f"    {len(mermaid_map)} 张")

    log("▸ 收集图片 …")
    write_image = make_image_writer(oebps)
    image_map: dict[str, str] = {}
    for f in sorted(book_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTS:
            continue
        if any(part.startswith(".") for part in f.relative_to(book_dir).parts):
            continue
        rel = f.relative_to(book_dir).as_posix()
        if rel in image_map:
            continue
        target = write_image(f, book_dir)
        image_map[rel] = target
        image_map.setdefault(f.name, target)
    log(f"    {len({v for v in image_map.values()})} 张")

    link_map = LinkResolver(book_dir.name[: -len(BOOK_SUFFIX)])
    for n in nodes:
        link_map.add(n.rel_posix, Path(n.doc).name)

    docs: list[tuple[str, str, str]] = []
    cover_rel = "images/cover.png"
    make_cover(oebps / "images" / "cover.png", title, subtitle, [s.title for s in sections])
    docs.append(("text/cover.xhtml", "封面", wrap_document(
        "封面", f'<div><img src="../{cover_rel}" alt="{html.escape(title)}"/></div>',
        lang, epub_type="cover", body_class="cover")))
    docs.append(("text/titlepage.xhtml", "书名页", wrap_document(
        "书名页",
        f'<p class="book-title">{html.escape(title)}</p>\n'
        + (f'<p class="book-sub">{html.escape(subtitle)}</p>\n' if subtitle else "")
        + '<div class="rule"></div>\n'
        '<ul class="part-list">'
        + "".join(f"<li>{html.escape(s.title)}</li>" for s in sections)
        + "</ul>\n"
        + f'<p class="colophon">由思源笔记 (SiYuan) 导出内容构建 · EPUB 3</p>',
        lang, epub_type="titlepage", body_class="titlepage")))

    log("▸ 转换章节 …")
    for node in nodes:
        src = unpin_html_blocks(prepare_body(node, node.children))
        fig_counter = 0

        def _mm(m, _n=node):
            nonlocal fig_counter
            code = m.group(1).strip()
            target = mermaid_map.get(code)
            if not target:
                return m.group(0)
            fig_counter += 1
            heads = re.findall(r"^#{1,6}[ \t]+(.+?)[ \t]*$", src[:m.start()], re.M)
            label = re.sub(r"[*`\\]", "", heads[-1]).strip() if heads else ""
            cap = f"图 {_n.num}.{fig_counter}" + (f" · {label}" if label else "")
            return (f'\n<figure class="fig"><img src="{target}" alt="{html.escape(cap)}"/>'
                    f"<figcaption>{html.escape(cap)}</figcaption></figure>\n\n")

        src = re.sub(r"^```mermaid[ \t]*\n(.*?)^```[ \t]*$", _mm, src, flags=re.S | re.M)
        src, math_table = extract_math(src)
        frag = md.render(src)

        slug = f"s{node.num.replace('.', '-')}-{node.slug}"
        link_map.base_dir = PurePosixPath(PurePosixPath(node.rel_posix).parent.as_posix())
        inner = process_fragment(frag, slug, image_map, link_map, math_table)

        head = ""
        if node.children:
            kicker = f"PART {node.num}" if node.depth == 1 else f"SECTION {node.num}"
            items = "".join(
                f'<li><a href="{Path(c.doc).name}"><span class="num">{c.num}</span>'
                f"{html.escape(c.title)}</a></li>" for c in node.children)
            head = (f'<p class="part-kicker">{kicker}</p>\n'
                    f'<h1 id="{slug}-title">{html.escape(node.title)}</h1>\n'
                    f'<ul class="child-toc">{items}</ul>\n')
            body_class = "part" if node.depth == 1 else "part d2"
            etype = "part"
        else:
            head = f'<h1 id="{slug}-title">{html.escape(node.title)}</h1>\n'
            body_class, etype = "chapter", "chapter"
        docs.append((node.doc, node.title,
                     wrap_document(node.title, head + inner, lang, epub_type=etype,
                                   body_class=body_class)))
        log(f"    ✓ {node.num} {node.title}")
        if DROPPED_LINKS:
            uniq = sorted(set(DROPPED_LINKS))
            shown = ", ".join(uniq[:3]) + (f" … 共 {len(uniq)} 个" if len(uniq) > 3 else "")
            log(f"      · {len(DROPPED_LINKS)} 个链接/锚点无目标，已降级为纯文本: {shown}")
            DROPPED_LINKS.clear()

    # ---- navigation -------------------------------------------------------
    def nav_list(items: list[Node]) -> str:
        out = []
        for n in items:
            sub = f"\n<ol>{nav_list(n.children)}</ol>" if n.children else ""
            out.append(f'<li><a href="{n.doc}">{html.escape(n.title)}</a>{sub}</li>')
        return "\n".join(out)

    nav_inner = (
        '<nav epub:type="toc" id="toc" role="doc-toc">\n<h1>目录</h1>\n<ol>\n'
        + nav_list(sections) + "\n</ol>\n</nav>\n"
        '<nav epub:type="landmarks" id="landmarks" hidden="hidden">\n<h1>导航</h1>\n<ol>\n'
        '  <li><a epub:type="cover" href="text/cover.xhtml">封面</a></li>\n'
        '  <li><a epub:type="titlepage" href="text/titlepage.xhtml">书名页</a></li>\n'
        '  <li><a epub:type="toc" href="nav.xhtml">目录</a></li>\n'
        f'  <li><a epub:type="bodymatter" href="{sections[0].doc}">正文</a></li>\n'
        "</ol>\n</nav>")
    nav_head = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops" '
                f'xml:lang="{lang}" lang="{lang}">\n<head>\n  <meta charset="utf-8"/>\n'
                "  <title>目录</title>\n"
                '  <link rel="stylesheet" type="text/css" href="css/style.css"/>\n'
                "</head>\n<body>\n")
    (oebps / "nav.xhtml").write_text(nav_head + nav_inner + "\n</body>\n</html>\n", encoding="utf-8")

    for rel, _t, content in docs:
        (oebps / rel).write_text(content, encoding="utf-8")
    (oebps / "css" / "style.css").write_text(CSS, encoding="utf-8")
    (oebps / "css" / "pygments.css").write_text(pygments_css(), encoding="utf-8")

    # ---- package.opf ------------------------------------------------------
    uid = cfg.get("identifier") or "urn:uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"siyuan-epub:{name}"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    docs_order = [("text/cover.xhtml", 1), ("text/titlepage.xhtml", 2)]
    for n in nodes:
        docs_order.append((n.doc, 4))
    doc_map = {d[0]: d for d in docs}

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="css/style.css" media-type="text/css"/>',
        '<item id="pygc" href="css/pygments.css" media-type="text/css"/>',
        f'<item id="cover-image" href="{cover_rel}" media-type="image/png" properties="cover-image"/>',
    ]
    for img in sorted((oebps / "images").iterdir()):
        if img.name == "cover.png":
            continue
        manifest.append(f'<item id="img-{file_slug(img.stem, img.name)}" href="images/{img.name}" '
                        f'media-type="{MEDIA_TYPES.get(img.suffix.lstrip(".").lower(), "image/png")}"/>')
    spine = ['<itemref idref="nav"/>']
    for i, (rel, _prio) in enumerate(docs_order, 1):
        # MathML is a "foreign" content type in EPUB 3: every document that uses
        # it must advertise the property in the manifest (epubcheck OPF-014).
        body = doc_map.get(rel)
        props = ' properties="mathml"' if body and "<math" in body[2] else ""
        manifest.append(f'<item id="doc{i}" href="{rel}" media-type="application/xhtml+xml"{props}/>')
        if rel == "text/cover.xhtml":
            spine.insert(0, f'<itemref idref="doc{i}"/>')
        elif rel == "text/titlepage.xhtml":
            spine.insert(1, f'<itemref idref="doc{i}"/>')
        else:
            spine.append(f'<itemref idref="doc{i}"/>')

    metas = [
        f'    <dc:identifier id="pub-id">{html.escape(uid)}</dc:identifier>',
        f'    <dc:title id="main-title">{html.escape(title)}</dc:title>',
        '    <meta refines="#main-title" property="title-type">main</meta>',
        f'    <dc:language>{lang}</dc:language>',
        f'    <dc:date>{datetime.now().strftime("%Y-%m-%d")}</dc:date>',
        f'    <meta property="dcterms:modified">{now}</meta>',
    ]
    if cfg.get("author"):
        metas.append(f'    <dc:creator>{html.escape(str(cfg["author"]))}</dc:creator>')
    if cfg.get("publisher"):
        metas.append(f'    <dc:publisher>{html.escape(str(cfg["publisher"]))}</dc:publisher>')
    desc = cfg.get("description") or subtitle
    if desc:
        metas.append(f'    <dc:description>{html.escape(desc)}</dc:description>')
    metas.append('    <dc:source>思源笔记 (SiYuan) 导出</dc:source>')

    (oebps / "package.opf").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="pub-id" xml:lang="{lang}">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        + "\n".join(metas) + "\n  </metadata>\n  <manifest>\n    "
        + "\n    ".join(manifest) + "\n  </manifest>\n  <spine>\n    "
        + "\n    ".join(spine) + "\n  </spine>\n</package>\n", encoding="utf-8")

    (work / "mimetype").write_text("application/epub+zip", encoding="utf-8")
    (work / "META-INF" / "container.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles>\n'
        '    <rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/>\n'
        '  </rootfiles>\n</container>\n', encoding="utf-8")

    log("▸ 打包 …")
    if out_epub.exists():
        out_epub.unlink()
    with zipfile.ZipFile(out_epub, "w") as z:
        z.write(work / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
        for f in sorted(work.rglob("*")):
            if f.is_dir() or f.name == "mimetype" or f.suffix == ".json":
                continue
            if "mermaid" in f.relative_to(work).parts:
                continue
            z.write(f, f.relative_to(work).as_posix(), compress_type=zipfile.ZIP_DEFLATED)
    log(f"✓ {out_epub}  {out_epub.stat().st_size / 1024 / 1024:.2f} MB  "
        f"({len(sections)} 部分 / {len(nodes)} 页)")
    return out_epub


# ==========================================================================
# 8. epubcheck
# ==========================================================================


def find_epubcheck(ws: Path):
    java = next(iter(sorted(ws.glob(".tools/**/bin/java"))), None)
    jar = next(iter(sorted(ws.glob(".tools/**/epubcheck.jar"))), None)
    if java is None:
        java = Path(shutil.which("java") or "")
    return (java if java and java.exists() else None), (jar if jar and jar.exists() else None)


def run_epubcheck(epub: Path, ws: Path) -> bool:
    java, jar = find_epubcheck(ws)
    if not (java and jar):
        log("  ! 未找到 epubcheck（运行 scripts/setup_epubcheck.sh 安装到 .tools/）")
        return True
    r = subprocess.run([str(java), "-jar", str(jar), str(epub)],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    log("\n".join("    " + line for line in out.splitlines()[-25:]))
    ok = bool(re.search(r"0 errors", out)) and bool(re.search(r"0 fatals", out))
    log(f"  {'✓ 校验通过' if ok else '✗ 校验未通过'}: {epub.name}")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("books", nargs="*", help="book folders (default: every *.md folder)")
    ap.add_argument("-w", "--workspace", default=".", help="workspace root")
    ap.add_argument("-b", "--book", action="append", default=[], help="book folder (repeatable)")
    ap.add_argument("--check", action="store_true", help="run epubcheck afterwards")
    ap.add_argument("--clean", action="store_true", help="discard cached scratch dir first")
    args = ap.parse_args()

    ws = Path(args.workspace).expanduser().resolve()
    if not ws.is_dir():
        sys.exit(f"workspace not found: {ws}")

    requested = list(args.books) + list(args.book)
    books: list[Path] = []
    for r in requested:
        p = Path(r)
        p = (ws / p).resolve() if not p.is_absolute() else p.resolve()
        if not p.is_dir():
            sys.exit(f"book folder not found: {p}")
        books.append(p)
    if not books:
        books = sorted(p for p in ws.iterdir()
                       if p.is_dir() and p.name.endswith(BOOK_SUFFIX) and p.name != BOOK_SUFFIX
                       and not p.name.startswith("."))
    if not books:
        sys.exit(f"no '*{BOOK_SUFFIX}' folder found in {ws}\n"
                 f"put the SiYuan export folder (e.g. «MyBook.md/») there and re-run.")

    log(f"工作区: {ws}\n待构建: {', '.join(b.name for b in books)}")
    results = []
    for b in books:
        results.append(build_book(b, ws, args))
    if args.check:
        log("\n▸ epubcheck …")
        for epub in results:
            run_epubcheck(epub, ws)


if __name__ == "__main__":
    main()
