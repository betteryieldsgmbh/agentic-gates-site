"""Prose gate: lint Markdown/text against the styleguide hard style rules.

The styleguide owns the rules; this checker is the mechanical enforcement of the
subset that can be caught by pattern. Consuming repos (for example the marketing
content repo) vendor this file plus ``prose_rules.json`` and run it in CI so
every draft is held to the same voice.

Checks (ERROR unless noted):
- em/en dashes, and (WARNING) a spaced hyphen used as a dash
- banned phrases: smooth editorial transitions and marketing hype
- (WARNING) not-X-but-Y antithesis definitions
- (WARNING) staccato openers: consecutive lines starting "Not "/"Nicht "
- (WARNING) informal German address (du/dein/...) in the text
- (WARNING) vague filler words (irgendwie/irgendwas/...)

Usage::

    python prose_gate.py posts/*.md            # ERRORs fail, warnings print
    python prose_gate.py --strict posts/*.md    # warnings fail too
    python prose_gate.py --rules other.json FILE

Exit codes: 0 clean, 1 findings at the failing level, 2 bad invocation.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ERROR = "ERROR"
WARNING = "WARNING"
_FENCE = "```"


@dataclass(frozen=True)
class Finding:
    """One rule violation located in a file."""

    path: str
    line: int
    severity: str
    rule: str
    message: str
    snippet: str


def load_rules(path: Path) -> dict[str, Any]:
    """Load the rule data file."""
    return json.loads(path.read_text(encoding="utf-8"))


def prose_lines(text: str) -> list[tuple[int, str]]:
    """Return (1-based line number, text) for prose lines only.

    Fenced code blocks and HTML comment blocks are skipped so code samples and
    the front-matter header never trip the checker.
    """
    out: list[tuple[int, str]] = []
    in_code = False
    in_comment = False
    for index, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith(_FENCE):
            in_code = not in_code
            continue
        if "<!--" in stripped:
            in_comment = True
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if not in_code:
            out.append((index, raw))
    return out


def _dash_findings(
    path: str, line: int, text: str, dash: dict[str, Any]
) -> list[Finding]:
    """Flag em/en dashes (error) and a spaced hyphen used as a dash (warning)."""
    found: list[Finding] = []
    for char in dash.get("forbidden_chars", []):
        if char in text:
            found.append(
                Finding(
                    path, line, ERROR, "dash", "em/en dash is forbidden", text.strip()
                )
            )
            break
    if dash.get("flag_spaced_hyphen") and re.search(r"\S \- \S", text):
        found.append(
            Finding(
                path,
                line,
                WARNING,
                "dash-spaced",
                "hyphen used as a dash",
                text.strip(),
            )
        )
    return found


def paragraphs(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Group prose lines into paragraphs so phrases split by wrapping still match.

    Returns (first line number, whitespace-joined text) per blank-line-delimited
    block; hard-wrapped prose is rejoined so a phrase spanning two lines is seen
    as one string.
    """
    blocks: list[tuple[int, str]] = []
    start = 0
    buffer: list[str] = []
    for line, text in lines:
        if text.strip():
            if not buffer:
                start = line
            buffer.append(text.strip())
        elif buffer:
            blocks.append((start, " ".join(buffer)))
            buffer = []
    if buffer:
        blocks.append((start, " ".join(buffer)))
    return blocks


def _phrase_findings(
    path: str, line: int, text: str, rules: dict[str, Any]
) -> list[Finding]:
    """Flag banned phrases (error) and antithesis definitions (warning)."""
    found: list[Finding] = []
    lowered = text.lower()
    for entry in rules.get("banned_phrases", []):
        if re.search(entry["pattern"], lowered):
            found.append(
                Finding(path, line, ERROR, "banned-phrase", entry["why"], text.strip())
            )
    for entry in rules.get("antithesis", []):
        if re.search(entry["pattern"], lowered):
            found.append(
                Finding(path, line, WARNING, "antithesis", entry["why"], text.strip())
            )
    return found


def _informal_findings(
    path: str, line: int, text: str, words: list[str]
) -> list[Finding]:
    """Flag informal German address used as whole words."""
    lowered = text.lower()
    for word in words:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            message = f"informal German address '{word}'; use formal or impersonal"
            return [Finding(path, line, WARNING, "informal-de", message, text.strip())]
    return []


def _vague_findings(
    path: str, line: int, text: str, words: list[str]
) -> list[Finding]:
    """Flag vague filler words used as whole words."""
    lowered = text.lower()
    for word in words:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            message = f"vague filler '{word}'; use a concrete word"
            return [Finding(path, line, WARNING, "vague-filler", message, text.strip())]
    return []


def _staccato_findings(
    path: str, lines: list[tuple[int, str]], openers: list[str]
) -> list[Finding]:
    """Flag consecutive non-empty lines that both start with a banned opener."""
    found: list[Finding] = []
    previous_hit = False
    for line, text in lines:
        body = text.lstrip(">*-# ").lower()
        hit = any(body.startswith(op) for op in openers)
        if hit and previous_hit:
            found.append(
                Finding(
                    path,
                    line,
                    WARNING,
                    "staccato",
                    "staccato antithesis opener",
                    text.strip(),
                )
            )
        previous_hit = hit if text.strip() else previous_hit
    return found


def check_file(path: Path, rules: dict[str, Any]) -> list[Finding]:
    """Run every check against a single file and return its findings."""
    name = str(path)
    lines = prose_lines(path.read_text(encoding="utf-8"))
    findings: list[Finding] = []
    dash = rules.get("dash", {})
    informal = rules.get("informal_de", [])
    vague = rules.get("vague_fillers", [])
    for line, text in lines:
        findings += _dash_findings(name, line, text, dash)
        findings += _informal_findings(name, line, text, informal)
        findings += _vague_findings(name, line, text, vague)
    for start, block in paragraphs(lines):
        findings += _phrase_findings(name, start, block, rules)
    findings += _staccato_findings(name, lines, rules.get("staccato_openers", []))
    return findings


def _print(findings: list[Finding]) -> None:
    """Print findings grouped by file, most severe first."""
    order = {ERROR: 0, WARNING: 1}
    for finding in sorted(findings, key=lambda f: (f.path, order[f.severity], f.line)):
        print(
            f"{finding.path}:{finding.line} [{finding.severity}] {finding.rule}: {finding.message}"
        )
        print(f"    {finding.snippet[:100]}")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the checks, and return the process exit code."""
    parser = argparse.ArgumentParser(
        description="Lint prose against the styleguide hard rules."
    )
    parser.add_argument("files", nargs="+", help="Markdown/text files to check")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as failures"
    )
    default_rules = Path(__file__).resolve().parent / "prose_rules.json"
    parser.add_argument(
        "--rules", default=str(default_rules), help="path to prose_rules.json"
    )
    args = parser.parse_args(argv)

    rules = load_rules(Path(args.rules))
    findings: list[Finding] = []
    for name in args.files:
        findings += check_file(Path(name), rules)
    _print(findings)
    errors = sum(1 for f in findings if f.severity == ERROR)
    warnings = len(findings) - errors
    print(
        f"\n[prose-gate] {errors} error(s), {warnings} warning(s) in {len(args.files)} file(s)"
    )
    fail = errors > 0 or (args.strict and warnings > 0)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
