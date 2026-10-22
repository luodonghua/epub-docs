---
name: aws-docs-epub
description: Generate or refresh an EPUB e-book from an AWS documentation guide (e.g. Aurora DSQL) for offline reading on an iPad/Kindle. Use when the user wants to build, rebuild, or update an AWS docs EPUB.
---

# AWS Docs → EPUB

Generate a clean, well-structured EPUB from an AWS documentation guide so it can
be read offline on an iPad (Apple Books), Kindle, or any EPUB reader.

The project directory contains:
- `aws_docs_to_epub.py` — the generator (pandoc-based pipeline).
- `refresh.sh` — one-command wrapper that builds with today's date in the title.

## How it works

AWS documentation publishes, alongside every `.html` page, a clean `.md`
version and a `toc-contents.json` table of contents. The generator:
1. Downloads `toc-contents.json` to discover every page + the nav hierarchy.
2. Downloads each page's Markdown.
3. Rewrites internal cross-links and same-page fragments to intra-EPUB anchors
   (so no links are dead offline) and embeds images.
4. Demotes headings per TOC depth so the EPUB nav mirrors the website.
5. Runs pandoc to produce a validated EPUB3 with a dated title such as
   `Amazon Aurora DSQL User Guide (20261019)`, reader-friendly CSS, a nested
   table of contents, and split chapters.

## What to do

Interpret the user request in `$ARGUMENTS`. Then:

1. **Verify tooling.** Ensure `pandoc` and `python3` are installed. If pandoc is
   missing, install it: `sudo apt-get install -y pandoc`. Optionally install
   `epubcheck` for validation.

2. **Build.** From the project directory, run the refresh wrapper:
   - Default (Aurora DSQL):
     ```bash
     ./refresh.sh aurora-dsql
     ```
   - A different preset: `./refresh.sh <preset-name>`
   - An arbitrary AWS userguide (not yet a preset):
     ```bash
     ./refresh.sh --base-url https://docs.aws.amazon.com/<service>/latest/userguide \
                  --title "Human Readable Title"
     ```
   The output is `<stem>-<YYYY-MM-DD>.epub` plus a `<stem>-latest.epub` symlink.

3. **Add a new preset (optional).** If the user will refresh a new guide
   repeatedly, add it to the `GUIDES` dict near the top of
   `aws_docs_to_epub.py` so it becomes a one-word target:
   ```python
   GUIDES = {
       "aurora-dsql": {...},
       "<new-name>": {
           "base_url": "https://docs.aws.amazon.com/<svc>/latest/userguide",
           "title": "<Title>",
           "author": "Amazon Web Services",
       },
   }
   ```

4. **Validate & report.** If `epubcheck` is available, run it and confirm
   `0 errors`. Report the output path, page/chapter counts, and the dated title.

## Future refresh (low effort)

When the docs change, the user just re-runs `./refresh.sh aurora-dsql`. The
build date in the title makes it obvious which copy is newer. No manual page
lists to maintain — the TOC is re-fetched every run, so new/removed pages are
picked up automatically.

## Request

$ARGUMENTS
