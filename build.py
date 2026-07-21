#!/usr/bin/env python3
"""Static build for the agentic-gates.dev article pages (deliberately minimal).

Reads content/site.json + content/cta.json + content/posts/*.md, replaces the
{{CTA:<product>}} and {{TOOL:<repo>}} placeholders, renders markdown to HTML and
writes the committed static output:

  blog/index.html            plain article list
  blog/<slug>/index.html     one page per article

Run:  pip install markdown && python3 build.py
Output is committed (GitHub Pages serves the branch as-is, no CI build needed).
"""

from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"

SITE = json.loads((CONTENT / "site.json").read_text(encoding="utf-8"))
CTA = json.loads((CONTENT / "cta.json").read_text(encoding="utf-8"))

MD_EXTENSIONS = ["tables", "fenced_code"]

PAGE_CSS = "/assets/site.css"


def page(title: str, description: str, body: str, canonical: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="canonical" href="{canonical}">
<link rel="stylesheet" href="{PAGE_CSS}">
</head>
<body>
<main>
<header class="topnav">
  <a class="mark" href="/">{SITE["mark_html"]}</a>
  <nav><a href="/blog/">Articles</a></nav>
</header>
{body}
<footer>
  <p>{SITE["footer_note"]}</p>
</footer>
</main>
</body>
</html>
"""


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    m = re.match(r"\s*<!--(.*?)-->\s*", text, re.DOTALL)
    if m:
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        text = text[m.end():]
    return meta, text


def cta_block(product: str) -> str:
    p = CTA["products"][product]
    return (
        '<div class="cta-box">'
        f'<p>{html.escape(p["cta_text"])}.</p>'
        f'<a class="btn" href="{html.escape(p["cta_url"])}">Get in touch &rarr;</a>'
        "</div>"
    )


def replace_placeholders(md_text: str) -> str:
    def cta(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in CTA["products"]:
            raise SystemExit(f"unknown CTA product '{key}' — separation rules violated?")
        return f"@@CTA:{key}@@"  # protect through markdown, swap after render

    def tool(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in CTA["tool_links"]:
            raise SystemExit(f"unknown TOOL key '{key}'")
        return CTA["tool_links"][key]

    md_text = re.sub(r"\{\{CTA:([a-z0-9-]+)\}\}", cta, md_text)
    md_text = re.sub(r"\{\{TOOL:([a-z0-9-]+)\}\}", tool, md_text)
    return md_text


def render_post(path: Path) -> dict[str, str]:
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    slug = meta.get("slug") or path.stem
    title = meta.get("title")
    if not title:
        h1 = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = h1.group(1) if h1 else slug
    body = replace_placeholders(body)
    rendered = markdown.markdown(body, extensions=MD_EXTENSIONS)
    rendered = re.sub(r"(?:<p>)?@@CTA:([a-z0-9-]+)@@(?:</p>)?",
                      lambda m: cta_block(m.group(1)), rendered)
    canonical = f'{SITE["domain"]}/blog/{slug}/'
    out = ROOT / "blog" / slug / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    article = f'<article class="post">{rendered}</article>'
    out.write_text(page(f'{title} · {SITE["site_name"]}',
                        SITE["tagline_blog"], article, canonical), encoding="utf-8")
    return {"slug": slug, "title": title, "date": meta.get("date", "")}


def build_blog_index(posts: list[dict[str, str]]) -> None:
    posts = sorted(posts, key=lambda p: p["date"], reverse=True)
    items = "".join(
        f'<li><a href="/blog/{p["slug"]}/">{html.escape(p["title"])}</a>'
        f'<span class="date">{html.escape(p["date"])}</span></li>'
        for p in posts
    )
    body = f"""<h1 class="pagehead">{html.escape(SITE["tagline_blog"])}</h1>
<ul class="postlist">{items}</ul>"""
    out = ROOT / "blog" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page(f'Articles · {SITE["site_name"]}', SITE["tagline_blog"],
                        body, f'{SITE["domain"]}/blog/'), encoding="utf-8")


def main() -> None:
    posts = [render_post(p) for p in sorted((CONTENT / "posts").glob("*.md"))]
    build_blog_index(posts)
    print(f"built {len(posts)} post(s) + index — {date.today().isoformat()}")


if __name__ == "__main__":
    main()
