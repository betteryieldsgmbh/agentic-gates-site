"""Tests for the styleguide prose gate (tools/prose_gate.py)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("prose_gate", _HERE / "prose_gate.py")
assert _spec and _spec.loader
prose_gate = importlib.util.module_from_spec(_spec)
# Register before exec so the @dataclass in prose_gate can resolve its module.
sys.modules["prose_gate"] = prose_gate
_spec.loader.exec_module(prose_gate)
RULES = json.loads((_HERE / "prose_rules.json").read_text(encoding="utf-8"))


def _check(tmp_path: Path, text: str) -> list:
    path = tmp_path / "post.md"
    path.write_text(text, encoding="utf-8")
    return prose_gate.check_file(path, RULES)


def test_flags_em_dash(tmp_path: Path) -> None:
    findings = _check(tmp_path, "A sentence with an em dash — here.\n")
    assert any(f.rule == "dash" and f.severity == "ERROR" for f in findings)


def test_banned_phrase_across_wrapped_lines(tmp_path: Path) -> None:
    # "The danger is" is split by a hard line wrap; paragraph matching must catch it.
    text = "It always answers. The\ndanger is that it answers plausibly.\n"
    findings = _check(tmp_path, text)
    assert any(f.rule == "banned-phrase" and f.severity == "ERROR" for f in findings)


def test_truth_is_needs_comma(tmp_path: Path) -> None:
    substantive = _check(tmp_path, "For the AI, the truth is the document itself.\n")
    filler = _check(tmp_path, "The truth is, nobody checked the value.\n")
    assert not any(f.rule == "banned-phrase" for f in substantive)
    assert any(f.rule == "banned-phrase" for f in filler)


def test_antithesis_is_warning(tmp_path: Path) -> None:
    findings = _check(
        tmp_path, "It works not because it is smart, but because it is checked.\n"
    )
    assert any(f.rule == "antithesis" and f.severity == "WARNING" for f in findings)


def test_comma_not_antithesis(tmp_path: Path) -> None:
    findings = _check(tmp_path, "The tool checks the values, not the wording.\n")
    assert any(f.rule == "antithesis" and f.severity == "WARNING" for f in findings)


def test_is_not_it_is_antithesis(tmp_path: Path) -> None:
    findings = _check(
        tmp_path, "The failure is not a missed bug. It is a fabricated proof line.\n"
    )
    assert any(f.rule == "antithesis" and f.severity == "WARNING" for f in findings)


def test_german_comma_nicht_antithesis(tmp_path: Path) -> None:
    findings = _check(tmp_path, "Die Severity gilt als Hinweis, nicht als Urteil.\n")
    assert any(f.rule == "antithesis" and f.severity == "WARNING" for f in findings)


def test_informal_german(tmp_path: Path) -> None:
    findings = _check(tmp_path, "Pruefe deine Werte, bevor du sie freigibst.\n")
    assert any(f.rule == "informal-de" for f in findings)


def test_vague_filler(tmp_path: Path) -> None:
    findings = _check(tmp_path, "Das läuft irgendwie schief.\n")
    assert any(f.rule == "vague-filler" and f.severity == "WARNING" for f in findings)


def test_clean_text_passes(tmp_path: Path) -> None:
    text = (
        "The datasheet says 2.7 volts. The summary says 3.3 volts. Nobody caught it.\n"
    )
    assert _check(tmp_path, text) == []


def test_code_fence_is_skipped(tmp_path: Path) -> None:
    text = "```\nThe danger is real in this code sample.\n```\nClean prose here.\n"
    assert not any(f.rule == "banned-phrase" for f in _check(tmp_path, text))
