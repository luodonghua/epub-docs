# AWS Docs → EPUB

Generate a clean, offline-readable **EPUB** from an AWS documentation guide for
comfortable reading on an iPad (Apple Books), Kindle, or any EPUB reader.

Built-in presets:
- `aurora-dsql` — **Amazon Aurora DSQL User Guide**
- `aurora` — **Amazon Aurora User Guide**

The build date is baked into the book title — e.g.
`Amazon Aurora DSQL User Guide (20261019)` — so you can always tell at a glance
whether your copy is current.

## Quick start

```bash
# Build / refresh a preset with today's date
./refresh.sh aurora-dsql     # Amazon Aurora DSQL
./refresh.sh aurora          # Amazon Aurora (large: ~1000 pages, a few minutes)
```

This produces:
- `aurora-dsql-YYYY-MM-DD.epub` — the dated build
- `aurora-dsql-latest.epub` — a symlink to the newest build

Copy the `.epub` to your iPad via AirDrop, the Files app, or Apple Books.

## Refreshing when the docs change

Just run the same command again:

```bash
./refresh.sh aurora-dsql
```

Every run re-fetches the live table of contents (`toc-contents.json`), so newly
added or removed pages are picked up automatically — there is no page list to
maintain by hand. The dated title tells old and new builds apart.

## Building other AWS guides

Any AWS guide that exposes `toc-contents.json` and per-page `.md` versions works.
One-off:

```bash
./refresh.sh \
  --base-url https://docs.aws.amazon.com/<service>/latest/userguide \
  --title "Human Readable Title"
```

To make a guide a permanent one-word target, add it to the `GUIDES` dictionary
near the top of `aws_docs_to_epub.py`:

```python
GUIDES = {
    "aurora-dsql": { ... },
    "my-guide": {
        "base_url": "https://docs.aws.amazon.com/<svc>/latest/userguide",
        "title": "My Guide",
        "author": "Amazon Web Services",
    },
}
```

Then: `./refresh.sh my-guide`.

## How it works

1. Downloads `toc-contents.json` to discover every page and the navigation
   hierarchy (titles + nesting).
2. Downloads each page's clean Markdown (`.md`) version.
3. Rewrites internal cross-references and same-page fragments into intra-EPUB
   anchors so links keep working offline, and embeds referenced images.
4. Demotes headings according to TOC depth so the EPUB's navigation mirrors the
   website's structure.
5. Converts **each page independently** to HTML before assembling the book.
   This isolation is important for large guides: a markdown quirk on one page
   (e.g. an ASCII-art table inside a code block, or a bare `------` divider)
   can otherwise corrupt the parser and silently drop the anchors of later
   pages. Per-page conversion keeps every page's anchor intact.
6. Runs **pandoc** to package a validated EPUB3 with a dated title,
   reader-friendly CSS (tuned for iPad), a nested table of contents, and
   per-chapter splitting.

## Direct script usage

```bash
python3 aws_docs_to_epub.py --guide aurora-dsql -o aurora-dsql.epub
python3 aws_docs_to_epub.py --base-url <url> --title "<title>" -o out.epub
python3 aws_docs_to_epub.py --help
```

## Requirements

- `python3` (3.9+)
- `pandoc` — `sudo apt-get install -y pandoc`
- `epubcheck` *(optional, for validation)* — `sudo apt-get install -y epubcheck`

## Kiro skill

A Kiro skill is provided at `.kiro/skills/aws-docs-epub/SKILL.md`. Invoke it with:

```
/aws-docs-epub refresh the Aurora DSQL book
```

## Notes

The documentation content belongs to Amazon Web Services. These EPUBs are
unofficial offline copies for personal reading.

## License

The **code and tooling** in this repository are licensed under the
[MIT License](LICENSE).

This license covers only the source code (e.g. `aws_docs_to_epub.py`,
`refresh.sh`, the Kiro skill). It does **not** cover AWS documentation
content, which the tool downloads at runtime from `docs.aws.amazon.com`. That
content is © Amazon Web Services and is governed by the
[AWS Site Terms](https://aws.amazon.com/terms/). Generated EPUB files are
unofficial offline copies for personal use and are not redistributed by this
repository. See [NOTICE](NOTICE) for details.
