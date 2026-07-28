"""Tests for the HTML gate.

Every rule gets a sabotage test: a page that breaks exactly that rule must produce
exactly that finding. A gate nobody has watched fail is not evidence, it is decoration
-- so the point of this file is proving the gate can go RED, not that it goes green.

The final test pins the real site: the pages as committed must pass. That is what turns
a later green run into a statement about the site rather than about the checker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import html_gate

REPO_ROOT = Path(__file__).resolve().parent.parent

# A minimal page that satisfies every rule. Tests copy it and break one thing.
GOOD_PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test</title>
</head>
<body>
<main>
  <a href="/impressum.html">Impressum</a>
  <a href="/datenschutz.html">Datenschutz</a>
</main>
</body>
</html>
"""

# An Impressum carrying every Section 5 DDG item the gate requires.
GOOD_IMPRESSUM = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Impressum</title>
</head>
<body>
<main>
  <p>Betteryields GmbH<br>Musterstrasse 1<br>10707 Berlin</p>
  <h2>Vertreten durch</h2>
  <p>Dr. Aaron Hutzler</p>
  <p>E-Mail: info [at] betteryields.ai</p>
  <p>Amtsgericht Berlin-Charlottenburg<br>HRB 272078 B<br>USt-IdNr.: DE453623272</p>
  <a href="/datenschutz.html">Datenschutz</a>
</main>
</body>
</html>
"""


def build_site(root: Path, index: str = GOOD_PAGE, impressum: str | None = None) -> Path:
    """Write a complete three-page site into *root* and return it."""
    (root / "index.html").write_text(index, encoding="utf-8")
    (root / "impressum.html").write_text(impressum or GOOD_IMPRESSUM, encoding="utf-8")
    (root / "datenschutz.html").write_text(
        GOOD_PAGE.replace("<title>Test</title>", "<title>Datenschutz</title>").replace(
            '<a href="/datenschutz.html">Datenschutz</a>', '<a href="/impressum.html">Impressum</a>'
        ),
        encoding="utf-8",
    )
    return root


def run(root: Path) -> list[html_gate.Finding]:
    """Check every page of the site at *root*."""
    return html_gate.check_site(root, sorted(root.glob("*.html")))


def rules(findings: list[html_gate.Finding]) -> set[str]:
    """Return the set of rule names present in *findings*."""
    return {f.rule for f in findings}


def test_clean_site_has_no_findings(tmp_path: Path) -> None:
    """The reference site passes: the gate is not red by construction."""
    assert run(build_site(tmp_path)) == []


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        ("<!DOCTYPE html>\n", "missing <!DOCTYPE html>"),
        ('<meta charset="utf-8">\n', "missing <meta charset>"),
        ("<title>Test</title>\n", "missing or empty <title>"),
    ],
)
def test_structure_sabotage(tmp_path: Path, removed: str, expected: str) -> None:
    """Removing a required structural element is reported."""
    findings = run(build_site(tmp_path, index=GOOD_PAGE.replace(removed, "")))
    assert any(expected in f.message for f in findings), [f.render() for f in findings]


def test_missing_lang_attribute_is_reported(tmp_path: Path) -> None:
    """An <html> without lang is reported (screen readers rely on it)."""
    findings = run(build_site(tmp_path, index=GOOD_PAGE.replace('<html lang="de">', "<html>")))
    assert any("no lang attribute" in f.message for f in findings)


def test_missing_viewport_is_reported(tmp_path: Path) -> None:
    """A page without the viewport meta is reported (it breaks the mobile layout)."""
    page = GOOD_PAGE.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n', ""
    )
    assert any("viewport" in f.message for f in run(build_site(tmp_path, index=page)))


def test_unclosed_tag_is_reported(tmp_path: Path) -> None:
    """An element that is never closed is reported with its opening line."""
    findings = run(build_site(tmp_path, index=GOOD_PAGE.replace("</main>\n", "")))
    assert any("<main> is never closed" in f.message for f in findings)


def test_stray_closing_tag_is_reported(tmp_path: Path) -> None:
    """A closing tag with no open element is reported."""
    findings = run(build_site(tmp_path, index=GOOD_PAGE.replace("</main>", "</section></main>")))
    assert any("not open" in f.message for f in findings)


def test_void_element_needs_no_closing_tag(tmp_path: Path) -> None:
    """<br> and friends must not be reported as unclosed (they never close)."""
    page = GOOD_PAGE.replace("<main>", "<main><p>a<br>b<hr><img src=/impressum.html></p>")
    assert "markup" not in rules(run(build_site(tmp_path, index=page)))


def test_duplicate_id_is_reported(tmp_path: Path) -> None:
    """Two elements sharing an id are reported (anchors become ambiguous)."""
    page = GOOD_PAGE.replace("<main>", '<main id="x"><div id="x"></div>')
    assert any("duplicate id" in f.message for f in run(build_site(tmp_path, index=page)))


def test_dead_internal_link_is_reported(tmp_path: Path) -> None:
    """A link to a file that does not exist is reported."""
    page = GOOD_PAGE.replace('href="/impressum.html">Impressum', 'href="/agb.html">AGB')
    findings = run(build_site(tmp_path, index=page))
    assert any("target does not exist: /agb.html" in f.message for f in findings)


def test_dead_anchor_is_reported(tmp_path: Path) -> None:
    """A fragment with no matching id is reported, on this page and across pages."""
    page = GOOD_PAGE.replace("<main>", '<main><a href="#nope">x</a><a href="/impressum.html#no">y</a>')
    messages = [f.message for f in run(build_site(tmp_path, index=page))]
    assert any("#nope has no matching id" in m for m in messages)
    assert any("#no has no id in impressum.html" in m for m in messages)


def test_live_anchor_is_accepted(tmp_path: Path) -> None:
    """A fragment that does resolve is not reported."""
    page = GOOD_PAGE.replace("<main>", '<main id="top"><a href="#top">top</a>')
    assert "link" not in rules(run(build_site(tmp_path, index=page)))


def test_insecure_http_link_is_reported(tmp_path: Path) -> None:
    """An http:// link is reported; https:// is accepted."""
    page = GOOD_PAGE.replace("<main>", '<main><a href="http://example.com">x</a>')
    assert any("insecure http://" in f.message for f in run(build_site(tmp_path, index=page)))
    ok = GOOD_PAGE.replace("<main>", '<main><a href="https://example.com">x</a>')
    assert "link" not in rules(run(build_site(tmp_path, index=ok)))


def test_malformed_mailto_is_reported(tmp_path: Path) -> None:
    """A mailto: without a real address is reported; a valid one passes."""
    bad = GOOD_PAGE.replace("<main>", '<main><a href="mailto:info@">x</a>')
    assert any("malformed mailto" in f.message for f in run(build_site(tmp_path, index=bad)))
    good = GOOD_PAGE.replace("<main>", '<main><a href="mailto:info@betteryields.ai">x</a>')
    assert "link" not in rules(run(build_site(tmp_path, index=good)))


def test_page_without_legal_links_is_reported(tmp_path: Path) -> None:
    """A page that stops linking to the legal pages is reported, per page."""
    bare = GOOD_PAGE.replace('<a href="/impressum.html">Impressum</a>', "").replace(
        '<a href="/datenschutz.html">Datenschutz</a>', ""
    )
    messages = [f.message for f in run(build_site(tmp_path, index=bare))]
    assert any("does not link to impressum.html" in m for m in messages)
    assert any("does not link to datenschutz.html" in m for m in messages)


def test_bare_slash_counts_as_a_link_to_index(tmp_path: Path) -> None:
    """href="/" resolves to index.html rather than being reported as dead."""
    page = GOOD_PAGE.replace("<main>", '<main><a href="/">home</a>')
    assert "link" not in rules(run(build_site(tmp_path, index=page)))


@pytest.mark.parametrize(
    ("removed", "label"),
    [
        ("Betteryields GmbH", "Firmenname"),
        ("Vertreten durch", "Vertretungsberechtigter"),
        ("Amtsgericht Berlin-Charlottenburg", "Registergericht"),
        ("HRB 272078 B", "Registernummer"),
        ("DE453623272", "Umsatzsteuer"),
        ("info [at] betteryields.ai", "E-Mail"),
        ("10707 Berlin", "Postanschrift"),
    ],
)
def test_impressum_missing_item_is_reported(tmp_path: Path, removed: str, label: str) -> None:
    """Dropping any Section 5 DDG item from the Impressum is reported by name."""
    findings = run(build_site(tmp_path, impressum=GOOD_IMPRESSUM.replace(removed, "")))
    assert any(
        f.rule == "legal" and label in f.message for f in findings
    ), f"removing {removed!r} produced {[f.render() for f in findings]}"


def test_missing_impressum_file_is_reported(tmp_path: Path) -> None:
    """A site with no Impressum at all is reported."""
    build_site(tmp_path)
    (tmp_path / "impressum.html").unlink()
    assert any("has no Impressum" in f.message for f in run(tmp_path))


def test_unreadable_file_is_reported_not_raised(tmp_path: Path) -> None:
    """A file that cannot be decoded produces a finding instead of a traceback."""
    build_site(tmp_path)
    (tmp_path / "index.html").write_bytes(b"\xff\xfe\x00broken")
    assert "read" in rules(run(tmp_path))


def test_cli_rejects_a_missing_file(tmp_path: Path) -> None:
    """A named file that does not exist is a usage error (exit 2), not a silent pass."""
    assert html_gate.main([str(tmp_path / "nope.html")]) == 2


def test_cli_returns_one_on_findings(tmp_path: Path) -> None:
    """The CLI exits 1 when it finds something, which is what makes CI go red."""
    build_site(tmp_path, index=GOOD_PAGE.replace("<!DOCTYPE html>\n", ""))
    assert html_gate.main(["--root", str(tmp_path)]) == 1


def test_the_real_site_passes() -> None:
    """The committed pages of this site pass every rule.

    This is the test that gives the green CI run meaning: it checks the actual product,
    not a fixture. If a future edit breaks the markup or drops a legal item, this fails.
    """
    findings = html_gate.check_site(REPO_ROOT, sorted(REPO_ROOT.glob("*.html")))
    assert findings == [], [f.render() for f in findings]
