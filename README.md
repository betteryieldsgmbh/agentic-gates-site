# agentic-gates.dev — static site (GitHub Pages)

Landing page + a few static article pages for **agentic-gates**. Served straight from this
branch by GitHub Pages (CNAME `agentic-gates.dev`), no CI build — generated HTML is committed.

**Deliberately minimal** (owner decision 2026-07-21): no blog machinery, no email capture,
no beta page. Just the landing, the articles and direct links to the GitHub repos. The
article CTA is a mail link (`info@betteryields.ai`).

## Layout

```
index.html                  launching-soon landing (hand-maintained)
impressum.html / datenschutz.html / legal.css
content/site.json           config (domain, taglines, footer)
content/cta.json            CTA + tool links used by the placeholders (see below)
content/posts/*.md          articles (markdown, HTML-comment front-matter)
build.py                    static generator (pip install markdown; python3 build.py)
assets/site.css             shared chrome for generated pages
blog/                       GENERATED — do not edit by hand, rerun build.py
```

## Publishing an article

Canonical marketing content lives in the internal marketing repo under `docs/marketing/`
(strategy, post workflow, `cta.json`, `posts/`).

1. Copy the post markdown into `content/posts/` (front-matter: `title · product · slug · date`).
2. If the canonical `cta.json` changed, mirror the **agentic-gates entries only** into
   `content/cta.json`. Strict brand separation: this site must not mention the other
   product anywhere (and vice versa).
3. `python3 build.py`, review the diff, commit. Placeholders: `{{CTA:agentic-gates}}` →
   CTA box (mail link), `{{TOOL:<repo>}}` → direct GitHub repo link.

Note: `ai-build-gate` still needs its GitHub rename + the repos need to be made public
(separate step, per the marketing handoff).
