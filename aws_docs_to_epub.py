#!/usr/bin/env python3
"""
aws_docs_to_epub.py
===================

Generate a clean, well-structured EPUB from an AWS documentation guide that
exposes a ``toc-contents.json`` table of contents and per-page Markdown
(``.md``) versions (which the AWS docs site publishes alongside every ``.html``
page).

The pipeline is:

  1. Download ``toc-contents.json`` to discover every page, its title, and its
     position in the navigation hierarchy.
  2. Download each page's Markdown version.
  3. Rewrite internal cross-references (``some-page.md`` links) into intra-EPUB
     anchors so the links keep working offline.
  4. Download and embed any referenced images.
  5. Emit a single concatenated Markdown document in which the heading level of
     every page reflects its depth in the TOC, giving pandoc the information it
     needs to build a properly nested navigation document.
  6. Invoke pandoc to produce an EPUB3 file with a cover, metadata, split
     chapters, and reader-friendly CSS.

The script is intentionally generic: point it at any AWS userguide base URL
via ``--base-url`` (or use one of the presets in ``GUIDES``) and it will build
the matching EPUB. This keeps future refreshes effortless -- re-running the
script re-downloads the latest content and rebuilds the book.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------- #
# Preset guides. Add new entries here to make them one-flag targets.
# --------------------------------------------------------------------------- #
GUIDES: dict[str, dict[str, str]] = {
    "aurora-dsql": {
        "base_url": "https://docs.aws.amazon.com/aurora-dsql/latest/userguide",
        "title": "Amazon Aurora DSQL User Guide",
        "author": "Amazon Web Services",
    },
    "aurora": {
        "base_url": "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide",
        "title": "Amazon Aurora User Guide",
        "author": "Amazon Web Services",
    },
    "rds": {
        "base_url": "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide",
        "title": "Amazon RDS User Guide",
        "author": "Amazon Web Services",
    },
}

USER_AGENT = "aws-docs-to-epub/1.0 (+https://docs.aws.amazon.com)"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Page:
    """A single documentation page discovered from the TOC."""

    href: str                      # e.g. "getting-started.html"
    title: str                     # e.g. "Getting started"
    depth: int                     # nesting depth in the TOC (0 = top level)
    slug: str = ""                 # anchor-safe id derived from href
    markdown: str = ""             # downloaded + rewritten markdown body

    def __post_init__(self) -> None:
        self.slug = slugify_href(self.href)


@dataclass
class Guide:
    base_url: str
    title: str
    author: str
    dated_title: str = ""
    pages: list[Page] = field(default_factory=list)
    images: dict[str, str] = field(default_factory=dict)  # url -> local filename


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify_href(href: str) -> str:
    """Turn a page href into a stable anchor id."""
    name = href.rsplit("/", 1)[-1]
    name = re.sub(r"\.html?$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-z0-9_-]", "-", name)
    return "page-" + name.lower()


def fetch(url: str, *, binary: bool = False, retries: int = 3, timeout: int = 60):
    """Fetch a URL with retries. Returns str (text) or bytes (binary)."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data if binary else data.decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as err:
            last_err = err
            if attempt < retries:
                continue
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def load_toc(base_url: str) -> list[Page]:
    """Download and flatten toc-contents.json into an ordered list of pages."""
    raw = fetch(f"{base_url}/toc-contents.json")
    data = json.loads(raw)
    pages: list[Page] = []
    seen: set[str] = set()

    def walk(items: Iterable[dict], depth: int) -> None:
        for item in items:
            href = item.get("href")
            title = (item.get("title") or "").strip()
            if href and href not in seen and href.endswith(".html"):
                seen.add(href)
                pages.append(Page(href=href, title=title or href, depth=depth))
            if item.get("contents"):
                walk(item["contents"], depth + 1)

    walk(data.get("contents", []), 0)
    return pages


# --------------------------------------------------------------------------- #
# Markdown transformation
# --------------------------------------------------------------------------- #
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ATX_HEADING_RE = re.compile(r"^(#{1,6})\s", re.MULTILINE)
# Bare HTML anchor tags that some guides embed, e.g. <a name="x"></a> or
# <a id="x"></a> (optionally with a closing </a>). Matched case-insensitively.
ANCHOR_TAG_RE = re.compile(
    r"<a\s+(?:name|id)=\"[^\"]*\"\s*>\s*</a>|<a\s+(?:name|id)=\"[^\"]*\"\s*/?>",
    re.IGNORECASE,
)


def demote_headings(md: str, shift: int) -> str:
    """Shift every ATX heading down by `shift` levels (capped at h6)."""
    if shift <= 0:
        return md

    def repl(m: re.Match) -> str:
        hashes = m.group(1)
        new_level = min(len(hashes) + shift, 6)
        return "#" * new_level + " "

    return ATX_HEADING_RE.sub(repl, md)


def rewrite_body(
    page: Page,
    guide: Guide,
    href_to_slug: dict[str, str],
) -> str:
    """Rewrite image + internal links and demote headings for one page."""
    md = page.markdown

    # 0. Strip bare HTML anchor tags (e.g. <a name="sec"></a> or <a id="sec">)
    #    that some AWS guides embed in their Markdown. Because raw_html is
    #    disabled in the pandoc reader (so literal angle brackets in code stay
    #    literal), these would otherwise render as visible "<a name=...>" text.
    #    Our link rewriting already redirects fragments to page-level anchors,
    #    so the inline targets are not needed.
    md = ANCHOR_TAG_RE.sub("", md)

    # 1. Images: capture, assign a local filename, rewrite path to media/<name>.
    def img_repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        abs_src = urljoin(guide.base_url, src)
        local = guide.images.get(abs_src)
        if local is None:
            local = make_image_name(abs_src, len(guide.images))
            guide.images[abs_src] = local
        return f"![{alt}](media/{local})"

    md = IMG_RE.sub(img_repl, md)

    # 2. Internal links to other pages / in-page fragments -> intra-epub anchors.
    #    AWS source pages use two fragment styles:
    #      * same-page section anchors (e.g. "#getting-started-prereqs") that
    #        pandoc does not reproduce as heading ids, and
    #      * cross-page links (e.g. "other-page.md" or "other-page.md#sec").
    #    To guarantee no dead links we resolve every internal link to a page
    #    anchor: cross-page links point to the target page's top anchor, and
    #    unresolved same-page fragments point to the current page's top anchor.
    def link_repl(m: re.Match) -> str:
        text, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "mailto:")):
            return m.group(0)
        # Pure in-page fragment -> current page's top anchor.
        if target.startswith("#"):
            return f"[{text}](#{page.slug})"
        base = target.split("#", 1)[0]
        html_name = re.sub(r"\.md$", ".html", base, flags=re.IGNORECASE)
        html_name = html_name.rsplit("/", 1)[-1]
        slug = href_to_slug.get(html_name)
        if slug:
            return f"[{text}](#{slug})"
        # Unknown internal target (not in this guide) -> drop the link, keep text.
        if not base.startswith(("http://", "https://")):
            return text
        return m.group(0)

    md = LINK_RE.sub(link_repl, md)

    # 3. Determine the page's top heading level from TOC depth.
    #    Top-level pages become H1 (new chapter); nested pages shift down so the
    #    generated navigation mirrors the site hierarchy.
    #    The page's own markdown already starts at "# Title"; we shift by depth.
    md = demote_headings(md, page.depth)

    # 4. Prepend an explicit anchored heading so cross-links land correctly and
    #    titles always match the TOC (markdown H1 sometimes differs slightly).
    level = min(page.depth + 1, 6)
    heading = "#" * level
    anchor = f'{heading} {escape_md(page.title)} {{#{page.slug}}}\n\n'

    # Strip the page's original leading H1/top heading to avoid duplicate title
    # (we replace it with our anchored, TOC-accurate heading).
    md = strip_leading_heading(md)

    return anchor + md.strip() + "\n"


def strip_leading_heading(md: str) -> str:
    """Remove the first ATX heading (the page's own title) if present."""
    lines = md.lstrip().splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue
        if re.match(r"^#{1,6}\s", line):
            return "\n".join(lines[i + 1:])
        break
    return md


def escape_md(text: str) -> str:
    return text.replace("[", r"\[").replace("]", r"\]")


def urljoin(base: str, src: str) -> str:
    if src.startswith(("http://", "https://")):
        return src
    if src.startswith("/"):
        # scheme + host
        m = re.match(r"^(https?://[^/]+)", base)
        return (m.group(1) if m else base) + src
    return f"{base}/{src.lstrip('./')}"


def available_memory_bytes() -> int | None:
    """Best-effort available RAM in bytes (Linux /proc/meminfo), else None."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    # value is in kB
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def total_memory_bytes() -> int | None:
    """Best-effort total RAM in bytes (Linux /proc/meminfo), else None."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def free_swap_bytes() -> int:
    """Best-effort free swap in bytes (Linux /proc/meminfo), else 0."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("SwapFree:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def make_image_name(url: str, index: int) -> str:
    ext = os.path.splitext(url.split("?", 1)[0])[1] or ".img"
    h = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"img{index:03d}-{h}{ext}"


# --------------------------------------------------------------------------- #
# Build pipeline
# --------------------------------------------------------------------------- #
def download_pages(guide: Guide, verbose: bool = True) -> None:
    total = len(guide.pages)
    for i, page in enumerate(guide.pages, 1):
        md_url = f"{guide.base_url}/{re.sub(r'.html$', '.md', page.href)}"
        if verbose:
            print(f"  [{i:3d}/{total}] {page.href}", file=sys.stderr)
        page.markdown = fetch(md_url)


def download_images(guide: Guide, media_dir: Path, verbose: bool = True) -> None:
    if not guide.images:
        return
    media_dir.mkdir(parents=True, exist_ok=True)
    for url, local in guide.images.items():
        if verbose:
            print(f"  image: {url}", file=sys.stderr)
        try:
            data = fetch(url, binary=True)
            (media_dir / local).write_bytes(data)
        except RuntimeError as err:
            print(f"  WARNING: could not download image {url}: {err}", file=sys.stderr)


def build_markdown(guide: Guide) -> str:
    href_to_slug = {p.href: p.slug for p in guide.pages}
    parts: list[str] = []
    for page in guide.pages:
        parts.append(rewrite_body(page, guide, href_to_slug))
    return "\n\n".join(parts)


# Pandoc reader extension string. raw_html and tex math are disabled so that
# literal angle brackets in code/placeholders and '$' in shell samples are
# treated as text, not markup.
PANDOC_READER = (
    "markdown+pipe_tables+backtick_code_blocks+fenced_code_attributes"
    "-smart-tex_math_dollars-tex_math_single_backslash"
    "-tex_math_double_backslash-raw_tex-raw_html"
    "-auto_identifiers-gfm_auto_identifiers"
)


def _page_to_html(args: tuple[str, str]) -> tuple[str, str]:
    """Convert one page's rewritten markdown to an HTML fragment.

    Converting each page independently (rather than concatenating all pages
    into one giant markdown document) is essential: a markdown quirk on one
    AWS page -- e.g. an ASCII-art table inside a code block, or a bare '------'
    divider -- can otherwise corrupt pandoc's parser state and silently drop
    the headings/anchors of *later* pages. Per-page conversion isolates that
    state so every page's anchor is preserved.
    """
    slug, md = args
    proc = subprocess.run(
        ["pandoc", "-f", PANDOC_READER, "-t", "html"],
        input=md, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc (page {slug}) failed: {proc.stderr}")
    return slug, proc.stdout


def build_body_html(guide: Guide, verbose: bool = True) -> str:
    """Produce a single HTML document body with one <section> per page.

    Each page becomes a sectioned HTML fragment; top-level pages (depth 0) are
    marked so pandoc splits the EPUB into chapters there.
    """
    href_to_slug = {p.href: p.slug for p in guide.pages}
    rendered: dict[str, str] = {}

    tasks = [(p.slug, rewrite_body(p, guide, href_to_slug)) for p in guide.pages]

    # Free the downloaded markdown now that each page's rewritten source has
    # been captured in `tasks`: on large guides (~1000 pages) keeping every
    # page's raw markdown resident adds hundreds of MB of needless pressure
    # that competes with the final pandoc packaging step on small-RAM hosts.
    for p in guide.pages:
        p.markdown = ""

    # Parallelise the per-page pandoc conversions (CPU/subprocess bound), but
    # scale the worker count by available memory as well as CPU. Each pandoc
    # worker is a separate GHC-runtime process; spawning 16 at once on a small
    # host (e.g. ~4 GiB) wastes memory and can push the box into swap. Budget
    # ~256 MiB of headroom per concurrent pandoc process.
    cpu_cap = min(16, (os.cpu_count() or 4) * 2)
    avail = available_memory_bytes()
    if avail is not None:
        mem_cap = max(1, int(avail // (256 * 1024 * 1024)))
        max_workers = max(1, min(cpu_cap, mem_cap))
    else:
        max_workers = cpu_cap
    if verbose:
        print(f"  per-page conversion with {max_workers} worker(s)",
              file=sys.stderr)
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for i, (slug, html) in enumerate(ex.map(_page_to_html, tasks), 1):
            rendered[slug] = html
            if verbose and i % 100 == 0:
                print(f"  converted {i}/{len(tasks)} pages", file=sys.stderr)

    # Assemble in TOC order. Each fragment already contains the page's leveled
    # heading (H1 for top-level pages, deeper for nested), so pandoc rebuilds
    # the section hierarchy and splits chapters at each H1.
    parts: list[str] = [rendered[page.slug] for page in guide.pages]
    rendered.clear()
    return "\n".join(parts)



DEFAULT_CSS = """\
/* Reader-friendly styling tuned for iPad / Apple Books */
body { font-family: -apple-system, "Helvetica Neue", serif; line-height: 1.5;
       margin: 0 5%; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; font-weight: 600;
       -webkit-hyphens: none; hyphens: none; }
h1 { page-break-before: always; margin-top: 1em; font-size: 1.7em; }
h2 { font-size: 1.4em; margin-top: 1.3em; }
h3 { font-size: 1.2em; }
code, pre { font-family: "SF Mono", Menlo, Consolas, monospace; }
pre { background: #f4f4f4; padding: 0.8em; border-radius: 6px;
      overflow-x: auto; font-size: 0.85em; white-space: pre-wrap;
      word-wrap: break-word; }
code { background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px;
       font-size: 0.88em; }
pre code { background: transparent; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 1em 0;
        font-size: 0.85em; }
th, td { border: 1px solid #ccc; padding: 0.4em 0.6em; text-align: left;
         vertical-align: top; }
th { background: #f0f0f0; }
blockquote { border-left: 4px solid #d0d0d0; margin: 1em 0; padding: 0.2em 1em;
             color: #444; background: #fafafa; }
img { max-width: 100%; height: auto; }
a { color: #0066c0; text-decoration: none; }
"""


def write_support_files(workdir: Path, guide: Guide) -> tuple[Path, Path]:
    css_path = workdir / "style.css"
    css_path.write_text(DEFAULT_CSS, encoding="utf-8")

    today = _dt.date.today().isoformat()
    datestamp = _dt.date.today().strftime("%Y%m%d")
    guide.dated_title = f"{guide.title} ({datestamp})"
    meta_path = workdir / "metadata.xml"
    meta_path.write_text(
        f"""<dc:date>{today}</dc:date>
<dc:publisher>{xml_escape(guide.author)}</dc:publisher>
<dc:description>Offline EPUB build of {xml_escape(guide.title)} generated {today} from {xml_escape(guide.base_url)}.</dc:description>
<dc:rights>(c) Amazon Web Services. Documentation content belongs to AWS. This EPUB is an unofficial offline copy for personal reading.</dc:rights>
""",
        encoding="utf-8",
    )
    return css_path, meta_path


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def run_pandoc(
    workdir: Path,
    body_html: Path,
    css_path: Path,
    meta_path: Path,
    output: Path,
    guide: Guide,
) -> None:
    cmd = [
        "pandoc",
        str(body_html),
        "-f", "html",
        "-t", "epub3",
        "-o", str(output),
        "--toc",
        "--toc-depth=3",
        "--split-level=1",
        "--epub-metadata", str(meta_path),
        "--css", str(css_path),
        f"--metadata=title:{guide.dated_title or guide.title}",
        f"--metadata=author:{guide.author}",
        "--metadata=lang:en",
        "--standalone",
    ]
    # embed media directory resources via --resource-path
    cmd += ["--resource-path", str(workdir)]

    # Bound pandoc's GHC heap and let the runtime return freed memory to the
    # OS promptly. On small-RAM hosts the single packaging pass for a large
    # guide (~1000 pages) is what triggers the OOM killer (SIGKILL / exit -9).
    # Capping the heap just under available memory turns a hard kernel kill
    # into a graceful pandoc heap-overflow error we can report clearly, and
    # `--idle-gc`/compacting GC reduce the resident peak.
    avail = available_memory_bytes()
    if avail is not None:
        # Pandoc may legitimately need to grow into swap to package a very
        # large guide on a small-RAM host. Allow the heap to use available
        # RAM *plus* free swap, keeping ~256 MiB of total headroom so the
        # kernel is never driven to SIGKILL. Floor at 512 MiB.
        budget = avail + free_swap_bytes() - (256 * 1024 * 1024)
        heap_mb = max(512, int(budget // (1024 * 1024)))
        # -M<n>m  : cap total heap so we fail gracefully instead of OOM-kill.
        # -c      : use the compacting collector for the oldest generation,
        #           which lowers the resident peak (less fragmentation) at a
        #           small CPU cost -- the right trade on a memory-starved host.
        cmd += ["+RTS", f"-M{heap_mb}m", "-c", "-RTS"]
    print("  running pandoc (packaging EPUB)...", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        rc = proc.returncode
        if rc == -9:
            total = total_memory_bytes()
            total_gib = f"{total / (1024**3):.1f} GiB" if total else "unknown"
            raise RuntimeError(
                "pandoc was killed by the OS (SIGKILL / exit -9) while "
                "packaging the EPUB. This is an out-of-memory condition: the "
                f"host has only {total_gib} RAM and this guide is too large to "
                "package in one pass. Remedies: add swap "
                "(e.g. `sudo fallocate -l 4G /swapfile && sudo chmod 600 "
                "/swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`), "
                "run on a host with more RAM, or close other memory-hungry "
                "processes, then re-run."
            )
        if proc.stderr and "Heap exhausted" in proc.stderr:
            raise RuntimeError(
                "pandoc exhausted its bounded heap while packaging the EPUB "
                "(the guide needs more memory than is available on this host). "
                "Add swap or use a host with more RAM, then re-run."
            )
        raise RuntimeError(f"pandoc failed with exit code {rc}")
    if proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)


def title_page_html(guide: Guide) -> str:
    """Render the front-matter / title page as an HTML fragment."""
    today = _dt.date.today().isoformat()
    datestamp = _dt.date.today().strftime("%Y%m%d")
    return (
        f"<h1>{xml_escape(guide.title)} ({datestamp})</h1>\n"
        f"<p><em>Unofficial offline edition</em></p>\n"
        f"<p>Generated <strong>{today}</strong> from "
        f'<a href="{xml_escape(guide.base_url)}">{xml_escape(guide.base_url)}</a></p>\n'
        f"<p>Source: Amazon Web Services documentation. "
        f"This copy is for personal/offline reading only.</p>\n"
    )


def generate(guide: Guide, output: Path, keep_workdir: bool = False) -> Path:
    output = output.resolve()
    print(f"Fetching TOC from {guide.base_url} ...", file=sys.stderr)
    guide.pages = load_toc(guide.base_url)
    print(f"Discovered {len(guide.pages)} pages.", file=sys.stderr)

    print("Downloading markdown pages ...", file=sys.stderr)
    download_pages(guide)

    print("Converting pages to HTML (isolated per page) ...", file=sys.stderr)
    body_fragment = build_body_html(guide)

    workdir = Path(tempfile.mkdtemp(prefix="aws-epub-"))
    try:
        # Prepend the title page, then wrap as a complete HTML document.
        full_html = (
            "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"/></head>\n<body>\n"
            + title_page_html(guide)
            + "\n"
            + body_fragment
            + "\n</body></html>\n"
        )
        body_html = workdir / "body.html"
        body_html.write_text(full_html, encoding="utf-8")

        # Release the large in-memory HTML buffers now that they live on disk.
        # For a ~1000-page guide these strings are tens of MB each; holding
        # them during the final pandoc pass needlessly competes for RAM on a
        # memory-starved host.
        del full_html, body_fragment
        import gc as _gc
        _gc.collect()

        print(f"Downloading {len(guide.images)} image(s) ...", file=sys.stderr)
        download_images(guide, workdir / "media")

        css_path, meta_path = write_support_files(workdir, guide)

        output.parent.mkdir(parents=True, exist_ok=True)
        run_pandoc(workdir, body_html, css_path, meta_path, output, guide)
        print(f"\nEPUB written to: {output}", file=sys.stderr)
        return output
    finally:
        if keep_workdir:
            print(f"Work directory kept at: {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an EPUB from an AWS documentation guide.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--guide", choices=sorted(GUIDES), default="aurora-dsql",
        help="Preset guide to build (default: aurora-dsql).",
    )
    parser.add_argument(
        "--base-url",
        help="Override: base userguide URL (no trailing slash). "
             "Use for guides not in the preset list.",
    )
    parser.add_argument("--title", help="Override: book title.")
    parser.add_argument("--author", default="Amazon Web Services",
                        help="Book author/publisher (default: Amazon Web Services).")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output .epub path (default: outputs/<guide>-<date>.epub).",
    )
    parser.add_argument("--keep-workdir", action="store_true",
                        help="Keep the temporary build directory for debugging.")
    args = parser.parse_args(argv)

    if args.base_url:
        base_url = args.base_url.rstrip("/")
        title = args.title or base_url.split("/")[2] + " documentation"
        author = args.author
        name = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    else:
        preset = GUIDES[args.guide]
        base_url = preset["base_url"]
        title = args.title or preset["title"]
        author = args.author or preset["author"]
        name = args.guide

    if shutil.which("pandoc") is None:
        print("ERROR: pandoc is not installed. Install it with "
              "'sudo apt-get install -y pandoc'.", file=sys.stderr)
        return 2

    # Default output lands in the repo's outputs/ directory so generated books
    # stay grouped and out of the project root. An explicit -o path is honored
    # as given.
    default_out = (
        Path(__file__).resolve().parent
        / "outputs"
        / f"{name}-{_dt.date.today().isoformat()}.epub"
    )
    output = args.output or default_out
    guide = Guide(base_url=base_url, title=title, author=author)
    try:
        generate(guide, output, keep_workdir=args.keep_workdir)
    except Exception as err:  # noqa: BLE001
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
