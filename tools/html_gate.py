"""HTML gate: check this static site's markup, links and legal duties mechanically.

This repo is a public landing page that carries legally binding text (Impressum and
Datenschutzerklaerung under German law). A broken tag, a dead link in the Impressum or a
page that stops linking to the legal notice is a liability, and on a hand-edited static
site nothing catches it. This gate does.

Checks (all ERROR; the gate has no advisory level by design -- a legal page either
carries its mandatory content or it does not):

- structure: DOCTYPE, ``<html lang>``, a non-empty ``<title>``, charset and viewport meta
- markup: every opened element closed in the right order, no duplicate ``id``
- links: internal targets resolve on disk, ``#anchors`` resolve to an id on the target
  page, external links use https, ``mailto:`` addresses are well formed
- legal: every page reaches the Impressum and the Datenschutzerklaerung, and the
  Impressum carries every item Section 5 DDG requires

External links are NOT fetched. A gate that reaches the network is a gate that goes red
when somebody else's server is slow, and a gate people learn to ignore stops protecting
anything. Reachability of third-party pages is a monitoring question, not a merge gate.

Usage::

    python tools/html_gate.py                 # check every *.html in the repo root
    python tools/html_gate.py index.html      # check specific files

Exit codes: 0 clean, 1 findings, 2 bad invocation.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

# Elements that never have a closing tag, so the nesting check must not expect one.
VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

# Pages whose presence the law requires, and which every other page must reach.
LEGAL_PAGES = ("impressum.html", "datenschutz.html")

# What Section 5 DDG requires in an Impressum. Each entry is a label plus the patterns
# that evidence it; one match is enough. Kept as patterns rather than exact strings so
# rewording the page does not break the gate, while removing a whole item does.
IMPRESSUM_REQUIREMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Firmenname und Rechtsform", (r"\bGmbH\b", r"\bUG\b", r"\bAG\b")),
    ("Postanschrift", (r"\b\d{5}\s+\w",)),
    ("Vertretungsberechtigter", (r"Vertreten durch", r"Geschäftsführer")),
    ("E-Mail-Kontakt", (r"[\w.+-]+\s*(?:@|\[at\])\s*[\w.-]+\.\w{2,}",)),
    ("Registergericht", (r"Amtsgericht", r"Registergericht")),
    ("Registernummer", (r"\bHRB\s*\d+", r"\bHRA\s*\d+")),
    ("Umsatzsteuer-Identifikationsnummer", (r"\bDE\d{9}\b",)),
)

MAILTO_PATTERN = re.compile(r"^mailto:[\w.+-]+@[\w.-]+\.\w{2,}$")


@dataclass(frozen=True)
class Finding:
    """One problem located in a file."""

    path: str
    line: int
    rule: str
    message: str

    def render(self) -> str:
        """Return the one-line report form."""
        return f"{self.path}:{self.line} [{self.rule}] {self.message}"


class PageParser(HTMLParser):
    """Collect the structural facts one page states about itself.

    Subclassing the stdlib parser keeps this dependency-free. It is lenient by design:
    it records what it sees and leaves every judgement to the check functions, so a
    parse never raises on markup this gate is supposed to REPORT rather than crash on.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.unclosed: list[tuple[str, int]] = []
        self.stray_close: list[tuple[str, int]] = []
        self.ids: list[tuple[str, int]] = []
        self.links: list[tuple[str, int]] = []
        self.has_doctype = False
        self.html_lang: str | None = None
        self.metas: list[dict[str, str]] = []
        self.title: str = ""
        self._in_title = False

    def handle_decl(self, decl: str) -> None:
        """Record the DOCTYPE declaration."""
        if decl.lower().startswith("doctype"):
            self.has_doctype = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record an opened element, its id, and any link it points at."""
        values = {name: (value or "") for name, value in attrs}
        line = self.getpos()[0]
        if "id" in values:
            self.ids.append((values["id"], line))
        for attr in ("href", "src"):
            if attr in values:
                self.links.append((values[attr], line))
        if tag == "html":
            self.html_lang = values.get("lang")
        elif tag == "meta":
            self.metas.append(values)
        elif tag == "title":
            self._in_title = True
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, line))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle a self-closing tag without pushing it on the nesting stack."""
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        """Pop the nesting stack, recording anything closed out of order."""
        if tag == "title":
            self._in_title = False
        if tag in VOID_ELEMENTS:
            return
        if tag not in [name for name, _ in self.stack]:
            self.stray_close.append((tag, self.getpos()[0]))
            return
        # Everything opened after this tag was never closed.
        while self.stack:
            name, line = self.stack.pop()
            if name == tag:
                break
            self.unclosed.append((name, line))

    def handle_data(self, data: str) -> None:
        """Accumulate the document title."""
        if self._in_title:
            self.title += data


def parse_page(path: Path) -> PageParser:
    """Parse one HTML file and return the collected facts."""
    parser = PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def check_structure(path: Path, page: PageParser) -> list[Finding]:
    """Return findings about the document's required structural elements."""
    found: list[Finding] = []
    name = path.name
    if not page.has_doctype:
        found.append(Finding(name, 1, "structure", "missing <!DOCTYPE html>"))
    if not page.html_lang:
        found.append(
            Finding(name, 1, "structure", "<html> has no lang attribute (screen readers, SEO)")
        )
    if not page.title.strip():
        found.append(Finding(name, 1, "structure", "missing or empty <title>"))
    if not any("charset" in meta for meta in page.metas):
        found.append(Finding(name, 1, "structure", "missing <meta charset>"))
    if not any(meta.get("name") == "viewport" for meta in page.metas):
        found.append(Finding(name, 1, "structure", "missing <meta name=viewport> (mobile layout)"))
    return found


def check_markup(path: Path, page: PageParser) -> list[Finding]:
    """Return findings about unbalanced tags and duplicate ids."""
    found: list[Finding] = []
    name = path.name
    for tag, line in page.unclosed + page.stack:
        found.append(Finding(name, line, "markup", f"<{tag}> is never closed"))
    for tag, line in page.stray_close:
        found.append(Finding(name, line, "markup", f"</{tag}> closes an element that is not open"))
    seen: dict[str, int] = {}
    for value, line in page.ids:
        if value in seen:
            found.append(
                Finding(name, line, "markup", f'duplicate id "{value}" (first at line {seen[value]})')
            )
        else:
            seen[value] = line
    return found


def _resolve_internal(root: Path, target: str) -> Path:
    """Return the file an internal href points at, mapping a bare / to index.html."""
    clean = target.split("?", 1)[0].split("#", 1)[0]
    if clean in ("", "/"):
        return root / "index.html"
    return root / clean.lstrip("/")


def check_links(path: Path, page: PageParser, root: Path, anchors: dict[str, set[str]]) -> list[Finding]:
    """Return findings about links that do not resolve or use an unsafe scheme."""
    found: list[Finding] = []
    name = path.name
    for target, line in page.links:
        if target.startswith("mailto:"):
            if not MAILTO_PATTERN.match(target):
                found.append(Finding(name, line, "link", f"malformed mailto: {target}"))
        elif target.startswith("http://"):
            found.append(Finding(name, line, "link", f"insecure http:// link, use https: {target}"))
        elif target.startswith(("https://", "tel:", "data:")):
            continue
        elif target.startswith("#"):
            if target[1:] and target[1:] not in anchors.get(name, set()):
                found.append(Finding(name, line, "link", f"anchor {target} has no matching id"))
        else:
            resolved = _resolve_internal(root, target)
            if not resolved.is_file():
                found.append(Finding(name, line, "link", f"target does not exist: {target}"))
                continue
            fragment = target.partition("#")[2]
            if fragment and fragment not in anchors.get(resolved.name, set()):
                found.append(
                    Finding(name, line, "link", f"anchor #{fragment} has no id in {resolved.name}")
                )
    return found


def check_legal_reachability(path: Path, page: PageParser, root: Path) -> list[Finding]:
    """Return findings when a page does not link to the legal pages it is not itself.

    German law requires the Impressum to be reachable from every page of the site.
    Enforcing it per page is what makes a future page inherit the duty automatically.
    """
    found: list[Finding] = []
    targets = {_resolve_internal(root, target).name for target, _ in page.links}
    for legal in LEGAL_PAGES:
        if path.name == legal or legal in targets:
            continue
        if not (root / legal).is_file():
            continue
        found.append(
            Finding(path.name, 1, "legal", f"page does not link to {legal} (must be reachable)")
        )
    return found


def check_impressum_content(root: Path) -> list[Finding]:
    """Return findings for Section 5 DDG items missing from the Impressum."""
    impressum = root / "impressum.html"
    if not impressum.is_file():
        return [Finding("impressum.html", 1, "legal", "the site has no Impressum (Section 5 DDG)")]
    text = impressum.read_text(encoding="utf-8")
    return [
        Finding("impressum.html", 1, "legal", f"Section 5 DDG item missing: {label}")
        for label, patterns in IMPRESSUM_REQUIREMENTS
        if not any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    ]


def collect_anchors(pages: dict[str, PageParser]) -> dict[str, set[str]]:
    """Return {page name: set of ids} so cross-page anchors can be validated."""
    return {name: {value for value, _ in page.ids} for name, page in pages.items()}


def check_site(root: Path, paths: list[Path]) -> list[Finding]:
    """Run every check over the given pages and return all findings, sorted."""
    pages: dict[str, PageParser] = {}
    findings: list[Finding] = []
    for path in paths:
        try:
            pages[path.name] = parse_page(path)
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(Finding(path.name, 1, "read", f"cannot read: {exc}"))
    anchors = collect_anchors(pages)
    for path in paths:
        page = pages.get(path.name)
        if page is None:
            continue
        findings.extend(check_structure(path, page))
        findings.extend(check_markup(path, page))
        findings.extend(check_links(path, page, root, anchors))
        findings.extend(check_legal_reachability(path, page, root))
    findings.extend(check_impressum_content(root))
    return sorted(findings, key=lambda f: (f.path, f.line, f.rule))


def main(argv: list[str] | None = None) -> int:
    """Check the site's HTML files; return 0 clean, 1 on findings, 2 on bad invocation."""
    parser = argparse.ArgumentParser(prog="html-gate", description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="HTML files (default: every *.html in --root)")
    parser.add_argument("--root", default=".", help="site root (default: current directory)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"[html-gate] not a directory: {root}", file=sys.stderr)
        return 2
    paths = [Path(f).resolve() for f in args.files] if args.files else sorted(root.glob("*.html"))
    missing = [p for p in paths if not p.is_file()]
    if missing:
        print(f"[html-gate] no such file: {missing[0]}", file=sys.stderr)
        return 2
    if not paths:
        print(f"[html-gate] no HTML files found in {root}", file=sys.stderr)
        return 2

    findings = check_site(root, paths)
    for finding in findings:
        print(finding.render())
    print(f"\n[html-gate] {len(findings)} finding(s) in {len(paths)} file(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
