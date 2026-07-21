# agentic-gates.dev — static site (GitHub Pages)

Landing page + blog + email-gate pages for **agentic-gates**. Served straight from this
branch by GitHub Pages (CNAME `agentic-gates.dev`), no CI build — generated HTML is committed.

## Layout

```
index.html                  launching-soon landing (hand-maintained)
impressum.html / datenschutz.html / legal.css
content/site.json           THE config: form endpoints, repo links, taglines
content/cta.json            gates slice of the marketing CTA source (see below)
content/posts/*.md          articles (markdown, HTML-comment front-matter)
build.py                    static generator (pip install markdown; python3 build.py)
assets/site.css             shared chrome for generated pages
blog/ get/ beta/            GENERATED — do not edit by hand, rerun build.py
```

## Content pipeline (from the marketing session)

Canonical marketing content lives in **`betteryieldsgmbh/aisen-platform`** under
`docs/marketing/` (strategy, post workflow, `cta.json`, `posts/`). Publishing an article here:

1. Copy the post markdown from `aisen-platform/docs/marketing/posts/` into `content/posts/`
   (add the HTML-comment front-matter `title · product · slug · date · cta` if missing).
2. If `docs/marketing/cta.json` changed, mirror the **agentic-gates entries only** into
   `content/cta.json`. **Never** copy the aisen entries — strict brand separation:
   this site must not mention aisen anywhere (and vice versa).
3. `python3 build.py`, review the diff, commit.

Beta → live flip: edit `content/cta.json` once (status/cta_text/cta_url), rebuild — every
article's CTA updates. Never hardcode a CTA in an article.

## Email capture (soft gate) — wiring the mail service

Forms on `/blog/`, `/get/<repo>/`, `/beta/` POST `EMAIL` + hidden `PRODUCT` / `SOURCE`
(`newsletter` | `tool-gate` | `beta-signup`) / `TOOL` fields to the endpoint configured in
`content/site.json` (`newsletter.form_action`, `tool_gate.form_action`, `beta.form_action`),
then reveal the follow-up locally (repo link / thanks). The gate is **soft by design**: repos
are public; posts/blog never link GitHub directly, always these pages.

**Owner steps to complete the funnel** (until then forms reveal but store nothing):

1. Create the mail-service account (recommended: **Brevo** — EU, double opt-in, free tier,
   contact attributes). Create a form (or use the API endpoint) per source, or one form with
   the hidden attributes mapped to contact fields `PRODUCT` / `SOURCE` / `TOOL`.
2. Enable **double opt-in**; set the confirmation mail for `tool-gate` submissions to include
   the repo link ("here is your link" mail per the handoff).
3. Paste the form endpoint URL(s) into `content/site.json` → `python3 build.py` → commit.
4. Have the privacy page reviewed: `datenschutz.html` needs a newsletter/double-opt-in section
   naming the mail provider (processor). Not added here — legal text is owner-reviewed.

Note: `content/site.json` points the gate reveals at `github.com/betteryieldsgmbh/ai-build-gate`
and `.../agentic-gates` — `ai-build-gate` still needs its GitHub rename + repos need to be made
public (separate step, per the handoff).
