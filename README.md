# agentic-gates-site

The landing page for [agentic-gates.dev](https://agentic-gates.dev/), served by GitHub
Pages from `main`. Static HTML and CSS, no build step: what is in this repo is what is
live.

| File | What it is |
|---|---|
| `index.html` | the landing page, styles inlined |
| `impressum.html` | legal notice (Section 5 DDG) |
| `datenschutz.html` | privacy notice |
| `legal.css` | shared styles for the two legal pages |
| `CNAME` | the custom domain for GitHub Pages |

## Gates

The pages carry legally binding text and are edited by hand, so the checks are mechanical
rather than a matter of remembering. `.github/workflows/gates.yml` runs on every push and
pull request, and `main` requires it.

| Gate | What it protects |
|---|---|
| `tools/html_gate.py` | markup is well formed, no duplicate ids, every internal link and anchor resolves, external links use https, every page reaches the Impressum and the Datenschutzerklaerung, and the Impressum carries every Section 5 DDG item |
| `tools/prose_gate.py` | the shared writing rules, vendored from the styleguide repo (no em dashes, no marketing hype, consistent voice) |
| `python -m pytest tools/` | the gates' own sabotage tests: each rule has a test proving it reports a page that breaks it |

Run them locally the way CI does:

```bash
python tools/html_gate.py && python tools/prose_gate.py *.html && python -m pytest tools/ -q
```

External links are deliberately not fetched. A gate that reaches the network goes red when
somebody else's server is slow, and a gate people learn to ignore stops protecting anything.

## Vendored files

`prose_gate.py`, `prose_rules.json` and `test_prose_gate.py` are verbatim copies from
[`betteryieldsgmbh/styleguide`](https://github.com/betteryieldsgmbh/styleguide), the single
source of truth for the writing rules. `styleguide.vendor.json` pins the exact commit and
the SHA-256 of each copy.

Do not edit them by hand. To pick up styleguide changes:

```bash
python tools/pull_styleguide.py --source ../styleguide
```

To verify the copies still match their pin:

```bash
python tools/pull_styleguide.py --check --source ../styleguide
```

The check needs a local styleguide checkout, so it runs on a developer machine rather than
in CI: the styleguide repo is private and this one is public, and wiring a cross-repo token
into a public repo to lint a landing page is a worse trade than checking drift locally.
