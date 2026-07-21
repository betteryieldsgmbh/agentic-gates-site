#!/usr/bin/env python3
"""Static build for the agentic-gates.dev blog + email-gate pages.

Reads content/site.json + content/cta.json + content/posts/*.md, replaces the
{{CTA:<product>}} and {{TOOL:<repo>}} placeholders, renders markdown to HTML and
writes the committed static output:

  blog/index.html            article list + newsletter email field
  blog/<slug>/index.html     one page per article
  get/<repo>/index.html      email gate ("enter email -> see the repo link")
  beta/index.html            beta-tester signup

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
  <a class="mark" href="/">agentic<span class="g">-gates</span></a>
  <nav><a href="/blog/">Blog</a> <a href="/beta/">Beta</a></nav>
</header>
{body}
<footer>
  <p>{SITE["footer_note"]}</p>
</footer>
</main>
</body>
</html>
"""


def email_form(form_action: str, source: str, extra_hidden: dict[str, str],
               button: str, reveal_html: str | None = None) -> str:
    """A small progressive form. POSTs to the configured mail-service endpoint
    (fetch, no-cors) if one is set, then reveals the follow-up content locally.
    Soft gate by design: the repos are public; this catches the funnel majority."""
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in ({"PRODUCT": SITE["product"], "SOURCE": source} | extra_hidden).items()
    )
    reveal = (
        f'<div class="reveal" hidden>{reveal_html}</div>' if reveal_html
        else '<div class="reveal" hidden><p class="ok">Thanks — check your inbox to confirm '
             '(double opt-in). You can unsubscribe anytime.</p></div>'
    )
    return f"""<form class="capture" data-action="{html.escape(form_action)}" method="post" novalidate>
  {hidden}
  <div class="row">
    <input type="email" name="EMAIL" required placeholder="you@company.com" aria-label="Email address">
    <button type="submit">{html.escape(button)}</button>
  </div>
  <label class="consent"><input type="checkbox" required>
    I agree to receive emails from betteryields GmbH. Double opt-in; unsubscribe anytime.
    See the <a href="/datenschutz.html">privacy policy</a>.</label>
</form>
{reveal}
<script>
(function () {{
  var form = document.currentScript.previousElementSibling.previousElementSibling;
  var reveal = document.currentScript.previousElementSibling;
  form.addEventListener("submit", function (ev) {{
    ev.preventDefault();
    if (!form.reportValidity()) return;
    var action = form.getAttribute("data-action");
    if (action) {{
      try {{ fetch(action, {{ method: "POST", mode: "no-cors", body: new FormData(form) }}); }}
      catch (e) {{ /* soft gate: reveal regardless */ }}
    }}
    form.hidden = true;
    reveal.hidden = false;
  }});
}})();
</script>"""


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
        f'<a class="btn" href="{html.escape(p["cta_url"])}">Become a beta tester &rarr;</a>'
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
    nl = SITE["newsletter"]
    body = f"""<h1 class="pagehead">{html.escape(SITE["tagline_blog"])}</h1>
<ul class="postlist">{items}</ul>
<section class="signup">
  <h2>{html.escape(nl["heading"])}</h2>
  <p>{html.escape(nl["sub"])}</p>
  {email_form(nl["form_action"], "newsletter", {}, "Notify me")}
</section>"""
    out = ROOT / "blog" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page(f'Blog · {SITE["site_name"]}', SITE["tagline_blog"],
                        body, f'{SITE["domain"]}/blog/'), encoding="utf-8")


def build_gate_pages() -> None:
    gate = SITE["tool_gate"]
    for key, repo in gate["repos"].items():
        reveal = (
            f'<p class="ok">Here is the repo:</p>'
            f'<p><a class="btn" href="{html.escape(repo["github_url"])}">'
            f'{html.escape(repo["display"])} on GitHub &rarr;</a></p>'
            '<p class="small">We also emailed you the link (after you confirm — double opt-in).</p>'
        )
        body = f"""<h1 class="pagehead">Get {html.escape(repo["display"])} — free</h1>
<p class="lede">{html.escape(repo["pitch"])}</p>
<p>Enter your email and the repo link appears right here (and lands in your inbox).</p>
{email_form(gate["form_action"], "tool-gate", {"TOOL": key},
            "Show me the repo", reveal_html=reveal)}"""
        out = ROOT / "get" / key / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page(f'Get {repo["display"]} · {SITE["site_name"]}',
                            repo["pitch"], body,
                            f'{SITE["domain"]}/get/{key}/'), encoding="utf-8")


def build_beta_page() -> None:
    b = SITE["beta"]
    body = f"""<h1 class="pagehead">{html.escape(b["heading"])}</h1>
<p class="lede">{html.escape(b["sub"])}</p>
{email_form(b["form_action"], "beta-signup", {}, "Become a beta tester")}"""
    out = ROOT / "beta" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page(f'Beta · {SITE["site_name"]}', b["heading"], body,
                        f'{SITE["domain"]}/beta/'), encoding="utf-8")


def main() -> None:
    posts = [render_post(p) for p in sorted((CONTENT / "posts").glob("*.md"))]
    build_blog_index(posts)
    build_gate_pages()
    build_beta_page()
    print(f"built {len(posts)} post(s), {len(SITE['tool_gate']['repos'])} gate page(s), "
          f"blog index, beta page — {date.today().isoformat()}")


if __name__ == "__main__":
    main()
