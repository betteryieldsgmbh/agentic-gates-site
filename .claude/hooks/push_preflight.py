"""Push preflight: three stages of local checks before anything reaches GitHub.

Owner decision 2026-11-09 (spec-coding change ``push-preflight``, verbatim GO in its
proposal). The three stages:

1. **pre-commit** -- fast checks on the staged files, including a scan of the staged
   files that ship for the repository's internal or customer terms (where the
   repository keeps such a term list).
2. **pre-push** -- a git hook, installed automatically, runs the CI checks that the
   pushed diff affects. The mapping from path patterns to commands lives in the
   repository's config (``.github/preflight.json``), derived from its own CI workflow.
   On success it writes a receipt for the pushed commit
   (``<git common dir>/preflight/<sha>.json``).
3. **session guard** -- a Claude Code ``PreToolUse`` hook refuses ``git push``,
   ``gh pr create`` and the GitHub MCP calls that create a pull request or write files
   unless a passing receipt exists for the commit being pushed. ``--no-verify`` and a
   swapped hooks path are refused outright.

This file is stdlib-only and runs both as ``spec_coding.push_preflight`` and as a plain
script: the provisioner copies it verbatim to ``.claude/hooks/push_preflight.py`` in a
governed repository, because a guard that depends on an installed package fails open in
every fresh container where the package is missing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

VERSION = "1"
RECEIPT_SCHEMA = 1
CONFIG_SCHEMA = 1
CONFIG_REL = ".github/preflight.json"
VENDORED_REL = ".claude/hooks/push_preflight.py"
RECEIPT_DIR = "preflight"
SHIM_MARK = "# spec-coding push-preflight shim"
ZERO_SHA = "0" * 40
PRE_PUSH_FIELDS = 4
DEFAULT_TIMEOUT = 1800
GUARD_TOOLS = (
    "mcp__github__create_pull_request",
    "mcp__github__push_files",
    "mcp__github__create_or_update_file",
)
GUARD_MATCHER = "Bash|" + "|".join(GUARD_TOOLS)
RUN_HINT = 'python3 "$(git rev-parse --show-toplevel)/.claude/hooks/push_preflight.py" run'
RULE_BEGIN = "<!-- spec-coding:preflight:begin -->"
RULE_END = "<!-- spec-coding:preflight:end -->"
OWNER_BEGIN = "<!-- spec-coding:owner-rules:begin -->"
OWNER_END = "<!-- spec-coding:owner-rules:end -->"


# --------------------------------------------------------------------------- git helpers


def git(root: Path, *args: str, check: bool = True) -> str:
    """Run git in ``root`` and return stripped stdout."""
    proc = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def repo_root(start: Path) -> Path | None:
    """The working-tree root that contains ``start``, or None outside a repository."""
    try:
        out = git(start, "rev-parse", "--show-toplevel")
    except (RuntimeError, OSError):
        return None
    return Path(out) if out else None


def common_dir(root: Path) -> Path:
    """The git common directory (shared by all worktrees)."""
    out = git(root, "rev-parse", "--git-common-dir")
    path = Path(out)
    return path if path.is_absolute() else (root / path).resolve()


def hooks_dir(root: Path) -> Path:
    """The directory git reads hooks from (honours ``core.hooksPath``)."""
    out = git(root, "rev-parse", "--git-path", "hooks")
    path = Path(out)
    return path if path.is_absolute() else (root / path).resolve()


def resolve_sha(root: Path, ref: str) -> str | None:
    """The commit sha ``ref`` names in ``root``, or None."""
    try:
        out = git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    except RuntimeError:
        return None
    return out or None


def default_branch(root: Path, config: dict[str, Any] | None = None) -> str:
    """The default branch: config first, then ``origin/HEAD``, then ``main``."""
    branch = config.get("default_branch") if config else None
    if isinstance(branch, str):
        return branch
    out = git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if out.startswith("refs/remotes/origin/"):
        return out.removeprefix("refs/remotes/origin/")
    return "main"


def diff_base(root: Path, sha: str, config: dict[str, Any] | None = None) -> str | None:
    """The merge base of ``sha`` with the remote default branch, or None if unknown."""
    branch = default_branch(root, config)
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        if resolve_sha(root, ref):
            base = git(root, "merge-base", "--end-of-options", sha, ref, check=False)
            if base:
                return base
    return None


def changed_files(root: Path, base: str | None, sha: str) -> list[str] | None:
    """Files that differ between ``base`` and ``sha``; None means "unknown, run all"."""
    if base is None:
        return None
    out = git(
        root,
        "diff",
        "--name-only",
        "--no-renames",
        "--end-of-options",
        f"{base}..{sha}",
        check=False,
    )
    return [line for line in out.splitlines() if line]


# --------------------------------------------------------------------------- config


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob with ``**`` into a regular expression."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns: list[str]) -> bool:
    """True when ``path`` matches one of ``patterns`` (a leading ``!`` excludes)."""
    hit = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if glob_to_regex(pattern[1:]).match(path):
                hit = False
        elif glob_to_regex(pattern).match(path):
            hit = True
    return hit


@dataclass
class Check:
    """One command of a stage, run when a changed file matches its paths."""

    name: str
    run: str
    paths: list[str] = field(default_factory=list)
    timeout: int = DEFAULT_TIMEOUT
    ci: str = ""
    cwd: str = ""
    requires: list[str] = field(default_factory=list)

    def selected(self, files: list[str] | None) -> list[str] | None:
        """Matched files, ``[]`` when unaffected, None when it runs regardless."""
        if not self.paths or files is None:
            return None
        return [f for f in files if matches(f, self.paths)]


class ConfigError(ValueError):
    """The preflight config is missing a field or carries a wrong type."""


def _checks(raw: object, where: str) -> list[Check]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list")
    checks = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{where}[{i}] must be an object")
        name, run = item.get("name", item.get("id")), item.get("run")
        commands = item.get("commands")
        if run is None and isinstance(commands, list) and all(isinstance(c, str) for c in commands):
            # The list form of the reference config (one command per entry): run them in order.
            run = " && ".join(f"( {c} )" for c in commands)
        if not isinstance(name, str) or not isinstance(run, str) or not run.strip():
            raise ConfigError(f"{where}[{i}] needs string fields 'name' and 'run'")
        paths = item.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ConfigError(f"{where}[{i}].paths must be a list of strings")
        timeout = item.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or timeout <= 0:
            raise ConfigError(f"{where}[{i}].timeout must be a positive integer")
        requires = item.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise ConfigError(f"{where}[{i}].requires must be a list of strings")
        cwd = item.get("cwd", "")
        if not isinstance(cwd, str):
            raise ConfigError(f"{where}[{i}].cwd must be a string")
        ci = str(item.get("ci", ""))
        checks.append(Check(name, run, paths, timeout, ci, cwd.strip("/"), requires))
    return checks


_PY = ["**/*.py", "pyproject.toml", "setup.cfg"]
_PY_DEPS = [*_PY, "requirements*.txt", "requirements*.lock"]
_FE = ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.mjs", "**/*.cjs", "**/*.css"]
_FE_CFG = ["package.json", "package-lock.json", "tsconfig*.json", "eslint.config.*", ".prettierrc*"]
_NPM_CI = "if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then npm ci; fi"
_CI_EVIDENCE = "CI evidence (SBOM, license, scorecard), produced on the runner"
_SEMGREP = "a multi-gigabyte install; CI runs the pinned version"
_SECRETS = "needs the effective gitleaks config rendered by CI"


def _npm(name: str, script: str, paths: list[str]) -> dict[str, Any]:
    return {"name": name, "run": f"{_NPM_CI} && {script}", "paths": paths, "requires": ["npm"]}


#: Check sets for the reusable workflows spec-coding publishes. A repository whose CI
#: calls one of them lists the preset under ``extends``; the commands mirror the steps
#: of the reusable workflow (``.github/workflows/<name>.yml`` in spec-coding).
PRESETS: dict[str, dict[str, list[dict[str, Any]]]] = {
    "python-ci": {
        "checks": [
            {"name": "ruff", "run": "ruff check .", "paths": _PY, "requires": ["ruff"]},
            {
                "name": "ruff-format",
                "run": "ruff format --check .",
                "paths": _PY,
                "requires": ["ruff"],
            },
            {"name": "mypy", "run": "python3 -m mypy .", "paths": [*_PY, "mypy.ini"]},
            {
                "name": "bandit",
                "run": "bandit -q -c pyproject.toml -r . --severity-level medium",
                "paths": _PY,
                "requires": ["bandit"],
            },
            {
                "name": "pytest",
                "run": "python3 -m pytest -q -p no:cacheprovider",
                "paths": [*_PY_DEPS, "tests/**", "openspec/**"],
            },
            {
                "name": "spec-coverage",
                "run": "spec-coding coverage",
                "paths": ["openspec/**", "tests/**", "**/*.py"],
                "requires": ["spec-coding"],
            },
            {
                "name": "spec-drift",
                "run": "spec-coding drift",
                "paths": ["openspec/**"],
                "requires": ["spec-coding"],
            },
            {
                "name": "actionlint",
                "run": "actionlint",
                "paths": [".github/workflows/**"],
                "requires": ["actionlint"],
            },
        ],
        "skipped": [
            {"name": "pip-audit", "reason": "queries the online vulnerability database"},
            {"name": "semgrep", "reason": _SEMGREP},
            {"name": "sbom-license", "reason": _CI_EVIDENCE},
            {"name": "secrets-scan", "reason": _SECRETS},
            {"name": "zizmor", "reason": "pinned workflow audit on the runner; actionlint runs"},
            {"name": "diff-coverage", "reason": "needs the coverage report of the CI run"},
        ],
    },
    "frontend-ci": {
        "checks": [
            {
                "name": "npm-ci",
                "run": _NPM_CI,
                "paths": ["package.json", "package-lock.json"],
                "requires": ["npm"],
            },
            _npm("lint", "npm run lint", [*_FE, *_FE_CFG]),
            _npm("format", "npm run format:check", [*_FE, *_FE_CFG, "**/*.json", "**/*.md"]),
            _npm("typecheck", "npm run typecheck", [*_FE, *_FE_CFG]),
            _npm("test", "npm run test:coverage", [*_FE, *_FE_CFG]),
            _npm("reachability", "npm run test:reachability", [*_FE, *_FE_CFG]),
            _npm("bundle-size", "npm run build && npm run size", [*_FE, *_FE_CFG]),
        ],
        "skipped": [
            {"name": "secrets-scan", "reason": _SECRETS},
            {"name": "semgrep", "reason": _SEMGREP},
            {"name": "sbom", "reason": _CI_EVIDENCE},
            {"name": "visual-proof", "reason": "needs the pinned browser container of CI"},
        ],
    },
    "spec-gates": {
        "checks": [
            {
                "name": "spec-drift",
                "run": "spec-coding drift",
                "paths": ["openspec/**"],
                "requires": ["spec-coding"],
            },
            {
                "name": "spec-coverage",
                "run": "spec-coding coverage",
                "paths": ["openspec/**", "tests/**", "**/*.py"],
                "requires": ["spec-coding"],
            },
            {
                "name": "change-hygiene",
                "run": "spec-coding change-hygiene --check",
                "paths": ["openspec/**", "change-reviews/**"],
                "requires": ["spec-coding"],
            },
        ],
        "skipped": [
            {
                "name": "meta-gates",
                "reason": "off in the reusable workflow unless the caller sets run-meta-gates;"
                " a caller that sets it lists `spec-coding meta-gates` under pre_push",
            },
        ],
    },
}


def _preset_entries(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(check entries, skipped entries) contributed by ``extends``."""
    checks: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for ref in config.get("extends", []) or []:
        if isinstance(ref, str):
            name, cwd = ref, ""
        elif isinstance(ref, dict):
            name, cwd = str(ref.get("preset")), str(ref.get("cwd", ""))
        else:
            raise ConfigError("extends entries are a preset name or {preset, cwd}")
        if name not in PRESETS:
            raise ConfigError(f"extends: unknown preset {name!r} (known: {sorted(PRESETS)})")
        prefix = f"{cwd.strip('/')}/" if cwd else ""
        for entry in PRESETS[name]["checks"]:
            item = dict(entry, name=f"{name}:{entry['name']}", ci=f"preset {name}")
            if prefix:
                item["cwd"] = cwd
                item["paths"] = [prefix + p for p in entry["paths"]]
            checks.append(item)
        skipped += [dict(e, name=f"{name}:{e['name']}") for e in PRESETS[name]["skipped"]]
    return checks, skipped


def pre_push_checks(config: dict[str, Any]) -> list[Check]:
    """Preset checks plus the repository's own, minus any named under ``skipped``."""
    preset, _ = _preset_entries(config)
    checks = _checks(preset, "extends") + _checks(config.get("pre_push"), "pre_push")
    off = {e.get("name") for e in config.get("skipped", []) if isinstance(e, dict)}
    return [c for c in checks if c.name not in off]


def skipped_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every CI check that does not run locally, with its reason."""
    _, preset = _preset_entries(config)
    own = [e for e in config.get("skipped", []) if isinstance(e, dict)]
    names = {e.get("name") for e in own}
    return own + [e for e in preset if e["name"] not in names]


def load_config(root: Path) -> dict[str, Any] | None:
    """The parsed config of ``root``, or None when the repository has none."""
    path = root / CONFIG_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{CONFIG_REL}: {exc}") from exc
    validate_config(data)
    config: dict[str, Any] = data
    return config


def validate_config(data: object) -> None:
    """Raise :class:`ConfigError` when ``data`` does not follow schema 1."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")
    if data.get("schema") != CONFIG_SCHEMA:
        raise ConfigError(f"config 'schema' must be {CONFIG_SCHEMA}")
    _checks(data.get("pre_push"), "pre_push")
    if not isinstance(data.get("extends", []), list):
        raise ConfigError("extends must be a list")
    _preset_entries(data)
    pre_commit = data.get("pre_commit", {})
    if not isinstance(pre_commit, dict):
        raise ConfigError("pre_commit must be an object")
    _checks(pre_commit.get("checks"), "pre_commit.checks")
    terms = pre_commit.get("terms")
    if terms is not None:
        if not isinstance(terms, dict) or not isinstance(terms.get("file"), str):
            raise ConfigError("pre_commit.terms needs a string field 'file'")
        if not isinstance(terms.get("paths", []), list):
            raise ConfigError("pre_commit.terms.paths must be a list")
    skipped = data.get("skipped", [])
    if not isinstance(skipped, list):
        raise ConfigError("skipped must be a list")
    for i, item in enumerate(skipped):
        if not isinstance(item, dict) or not isinstance(item.get("reason"), str):
            raise ConfigError(f"skipped[{i}] needs a string field 'reason'")


def config_digest(root: Path) -> str:
    """sha256 of the config file bytes; a changed config invalidates old receipts."""
    path = root / CONFIG_REL
    data = path.read_bytes() if path.is_file() else b""
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- receipts


def receipt_path(root: Path, sha: str) -> Path:
    """Where the receipt for ``sha`` lives."""
    return common_dir(root) / RECEIPT_DIR / f"{sha}.json"


def read_receipt(root: Path, sha: str) -> tuple[bool, str]:
    """(valid, reason): a receipt is valid when it passed, names ``sha`` and the config."""
    path = receipt_path(root, sha)
    if not path.is_file():
        return False, f"no receipt for {sha[:12]}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, f"receipt for {sha[:12]} is unreadable"
    if not isinstance(data, dict) or data.get("schema") != RECEIPT_SCHEMA:
        return False, f"receipt for {sha[:12]} has an unknown schema"
    if data.get("sha") != sha:
        return False, f"receipt for {sha[:12]} names another commit"
    if data.get("passed") is not True:
        return False, f"receipt for {sha[:12]} records a failed run"
    if data.get("config_sha256") != config_digest(root):
        return False, f"receipt for {sha[:12]} was written under another {CONFIG_REL}"
    return True, "ok"


def write_receipt(root: Path, payload: dict[str, Any]) -> Path:
    """Write ``payload`` as the receipt of ``payload['sha']``."""
    path = receipt_path(root, payload["sha"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- running


def _shell() -> list[str]:
    bash = shutil.which("bash")
    return [bash, "-c"] if bash else ["sh", "-c"]


def run_checks(
    root: Path,
    checks: list[Check],
    files: list[str] | None,
    out: TextIO = sys.stderr,
) -> tuple[bool, list[dict[str, Any]]]:
    """Run every affected check; return (all passed, per-check records)."""
    records: list[dict[str, Any]] = []
    passed = True
    for check in checks:
        hit = check.selected(files)
        if hit == []:
            records.append({"name": check.name, "status": "not-affected"})
            continue
        missing = [tool for tool in check.requires if shutil.which(tool) is None]
        if missing:
            print(f"[preflight] [FAIL] {check.name}: tool not installed: {missing}", file=out)
            records.append({"name": check.name, "status": "failed", "missing": missing})
            passed = False
            continue
        prefix = f"{check.cwd}/" if check.cwd else ""
        args = [f.removeprefix(prefix) for f in hit or []]
        command = check.run.replace("{files}", " ".join(shlex.quote(f) for f in args))
        print(f"[preflight] {check.name}: {command}", file=out, flush=True)
        start = time.monotonic()
        try:
            proc = subprocess.run(  # nosec B603
                [*_shell(), command],
                cwd=root / check.cwd if check.cwd else root,
                timeout=check.timeout,
                check=False,
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = 124
        seconds = round(time.monotonic() - start, 1)
        status = "passed" if code == 0 else "failed"
        marker = "[OK]" if code == 0 else "[FAIL]"
        print(f"[preflight] {marker} {check.name} ({seconds}s, exit {code})", file=out)
        records.append({"name": check.name, "status": status, "exit": code, "seconds": seconds})
        if code != 0:
            passed = False
    return passed, records


def tree_is_clean(root: Path) -> bool:
    """True when no tracked file differs from HEAD (untracked files are allowed)."""
    out = git(root, "status", "--porcelain", "--untracked-files=no", check=False)
    return out == ""


def preflight_sha(
    root: Path,
    sha: str,
    out: TextIO = sys.stderr,
    force: bool = False,
) -> int:
    """Run the pre-push stage for ``sha`` (must be the clean HEAD) and write a receipt."""
    config = load_config(root)
    if config is None:
        print(f"[preflight] no {CONFIG_REL}: nothing to check", file=out)
        return 0
    if not force:
        valid, _ = read_receipt(root, sha)
        if valid:
            print(f"[preflight] [OK] receipt for {sha[:12]} already passed", file=out)
            return 0
    head = resolve_sha(root, "HEAD")
    if sha != head or not tree_is_clean(root):
        print(
            f"[preflight] [FAIL] {sha[:12]} is not the clean checked-out HEAD; check it"
            " out without local edits and run the preflight there.",
            file=out,
        )
        return 1
    base = diff_base(root, sha, config)
    files = changed_files(root, base, sha)
    checks = pre_push_checks(config)
    passed, records = run_checks(root, checks, files, out)
    payload = {
        "schema": RECEIPT_SCHEMA,
        "tool_version": VERSION,
        "sha": sha,
        "tree": git(root, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{tree}}"),
        "base": base,
        "changed_files": len(files) if files is not None else None,
        "config": CONFIG_REL,
        "config_sha256": config_digest(root),
        "checks": records,
        "skipped": skipped_entries(config),
        "passed": passed,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = write_receipt(root, payload)
    if passed:
        print(f"[preflight] [OK] all affected checks passed; receipt {path}", file=out)
        return 0
    print("[preflight] [FAIL] fix the failed checks, commit, and run again", file=out)
    return 1


def pre_push(root: Path, stdin_text: str, out: TextIO = sys.stderr) -> int:
    """The git pre-push hook body: every pushed commit needs a passing receipt."""
    code = 0
    for entry in stdin_text.splitlines():
        parts = entry.split()
        if len(parts) != PRE_PUSH_FIELDS:
            continue
        local_ref, local_sha = parts[0], parts[1]
        if local_sha == ZERO_SHA or local_ref.startswith("refs/tags/"):
            continue  # a deletion or a tag push carries no new commit to check
        code = max(code, preflight_sha(root, local_sha, out))
    return code


# --------------------------------------------------------------------------- pre-commit


def load_terms(root: Path, rel: str) -> list[str]:
    """Terms from a JSON list (or ``{"terms": [...]}``) or one term per line."""
    path = root / rel
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if rel.endswith(".json"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("terms", [])
        return [t.strip() for t in data if isinstance(t, str) and t.strip()]
    terms = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def term_findings(text: str, terms: list[str]) -> list[str]:
    """Terms that occur in ``text`` as whole words, case-insensitive."""
    found = []
    for term in terms:
        pattern = r"(?<![\w])" + re.escape(term) + r"(?![\w])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            found.append(term)
    return found


def staged_files(root: Path) -> list[str]:
    """Paths added, copied, modified or renamed in the index."""
    out = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", check=False)
    return [line for line in out.splitlines() if line]


def staged_text(root: Path, rel: str) -> str:
    """The staged content of ``rel``, decoded leniently."""
    proc = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(root), "show", "--end-of-options", f":{rel}"],
        capture_output=True,
        check=False,
    )
    return proc.stdout.decode("utf-8", errors="replace")


def pre_commit(root: Path, out: TextIO = sys.stderr) -> int:
    """The git pre-commit hook body: term scan of shipped files, then fast checks."""
    config = load_config(root)
    if config is None:
        return 0
    stage = config.get("pre_commit", {}) or {}
    files = staged_files(root)
    code = 0
    terms_cfg = stage.get("terms")
    if terms_cfg:
        terms = load_terms(root, terms_cfg["file"])
        scope = terms_cfg.get("paths", [])
        term_list_file = terms_cfg["file"]
        for rel in files:
            if rel == term_list_file or (scope and not matches(rel, scope)):
                continue
            hits = term_findings(staged_text(root, rel), terms)
            if hits:
                print(f"[preflight] [FAIL] {rel} carries listed terms: {hits}", file=out)
                code = 1
    passed, _ = run_checks(root, _checks(stage.get("checks"), "pre_commit.checks"), files, out)
    return code if passed else 1


# --------------------------------------------------------------------------- session guard


def _split_commands(command: str) -> list[list[str]]:
    """Split a shell line into simple commands at ``&&``, ``||``, ``;``, ``|`` and newlines."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|\n"):
            commands.append([token])
            commands.append([])
        else:
            commands[-1].append(token)
    return [c for c in commands if c]


def _git_subcommand(argv: list[str]) -> tuple[str | None, list[str], str | None, bool]:
    """(subcommand, its args, -C dir, hooks path overridden) of a git argv."""
    i, cdir, hooks_override = 1, None, False
    while i < len(argv):
        arg = argv[i]
        if arg == "-C" and i + 1 < len(argv):
            cdir = argv[i + 1]
            i += 2
        elif arg == "-c" and i + 1 < len(argv):
            if argv[i + 1].lower().startswith("core.hookspath"):
                hooks_override = True
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg, argv[i + 1 :], cdir, hooks_override
    return None, [], cdir, hooks_override


def _strip_env(argv: list[str]) -> list[str]:
    """Drop leading ``VAR=value`` assignments and ``env``/``command`` prefixes."""
    i = 0
    while i < len(argv) and (
        re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i]) or argv[i] in ("env", "command")
    ):
        i += 1
    return argv[i:]


def _push_targets(root: Path, args: list[str]) -> tuple[list[str], str | None]:
    """(shas a ``git push`` would publish, refusal) for its arguments."""
    positional = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--all", "--mirror", "--branches"):
            return [], f"git push {arg} publishes many branches; push one branch at a time"
        if arg in ("--delete", "-d"):
            return [], None
        if arg in ("--repo", "-o", "--push-option", "--receive-pack", "--exec"):
            i += 2
            continue
        if not arg.startswith("-"):
            positional.append(arg)
        i += 1
    refspecs = positional[1:]
    if not refspecs:
        sha = resolve_sha(root, "HEAD")
        return ([sha] if sha else []), None
    shas = []
    for spec in refspecs:
        source = spec.lstrip("+").split(":", 1)[0]
        if not source or spec.startswith("refs/tags/") or source.startswith("refs/tags/"):
            continue  # ":branch" deletes; a tag carries no new branch head
        sha = resolve_sha(root, source)
        if sha is None:
            return [], f"cannot resolve '{source}' locally to check its receipt"
        shas.append(sha)
    return shas, None


def _refuse_missing(root: Path, shas: list[str], route: str) -> str | None:
    for sha in shas:
        valid, why = read_receipt(root, sha)
        if not valid:
            return (
                f"[push-preflight] {route} refused: {why}. Run the preflight on the"
                f" clean HEAD first ({RUN_HINT}), fix what it reports, then retry."
                " --no-verify is not a route."
            )
    return None


def _governed(start: Path) -> Path | None:
    root = repo_root(start)
    if root is None or not (root / CONFIG_REL).is_file():
        return None
    return root


def guard_bash(command: str, cwd: Path) -> str | None:
    """The refusal for a Bash command, or None."""
    here = cwd
    preflight_ran = False
    for simple in _split_commands(command):
        if simple[0] in ("&&", "||", ";", "|", "&"):
            if simple[0] != "&&":
                preflight_ran = False
            continue
        argv = _strip_env(simple)
        if not argv:
            continue
        if argv[0] == "cd" and len(argv) > 1:
            target = Path(os.path.expanduser(argv[1]))
            here = target if target.is_absolute() else here / target
            continue
        if any(a.endswith("push_preflight.py") for a in argv[:3]) and "run" in argv:
            preflight_ran = True
            continue
        if Path(argv[0]).name == "git":
            sub, args, cdir, hooks_override = _git_subcommand(argv)
            where = (
                here if cdir is None else (Path(cdir) if Path(cdir).is_absolute() else here / cdir)
            )
            if sub not in ("push", "commit"):
                continue
            root = _governed(where)
            if root is None:
                continue
            if "--no-verify" in args or (sub == "commit" and "-n" in args) or hooks_override:
                return (
                    f"[push-preflight] git {sub} past the hooks is refused"
                    " (--no-verify, -n, core.hooksPath)."
                )
            if sub == "commit" or preflight_ran:
                continue
            shas, refusal = _push_targets(root, args)
            if refusal:
                return f"[push-preflight] {refusal}"
            reason = _refuse_missing(root, shas, "git push")
            if reason:
                return reason
        elif Path(argv[0]).name == "gh" and argv[1:3] == ["pr", "create"]:
            root = _governed(here)
            if root is None or preflight_ran:
                continue
            head = None
            for flag in ("--head", "-H"):
                if flag in argv:
                    idx = argv.index(flag)
                    head = argv[idx + 1] if idx + 1 < len(argv) else None
            sha = _branch_sha(root, head.split(":")[-1]) if head else resolve_sha(root, "HEAD")
            if sha is None:
                return f"[push-preflight] gh pr create refused: cannot resolve '{head}' locally"
            reason = _refuse_missing(root, [sha], "gh pr create")
            if reason:
                return reason
    return None


def _branch_sha(root: Path, branch: str) -> str | None:
    """The pushed head of ``branch``: the remote-tracking ref first, then the local one."""
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", branch):
        sha = resolve_sha(root, ref)
        if sha:
            return sha
    return None


def _remote_matches(root: Path, owner: str, repo: str) -> bool:
    url = git(root, "remote", "get-url", "origin", check=False).lower()
    url = url.removesuffix(".git").rstrip("/")
    slug = f"{owner}/{repo}".lower()
    return url.endswith((f"/{slug}", f":{slug}"))


def find_checkout(cwd: Path, owner: str, repo: str) -> Path | None:
    """A local governed checkout of ``owner/repo``: cwd's repository or a sibling."""
    root = repo_root(cwd)
    candidates = [root] if root else []
    base = root.parent if root else cwd
    with contextlib.suppress(OSError):
        candidates += sorted(p for p in base.iterdir() if (p / ".git").exists())
    for cand in candidates:
        if cand and (cand / CONFIG_REL).is_file() and _remote_matches(cand, owner, repo):
            return cand
    return None


def _receipt_shas(root: Path, limit: int = 50) -> list[str]:
    folder = common_dir(root) / RECEIPT_DIR
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files[:limit]]


def _content_at(root: Path, sha: str, rel: str) -> str | None:
    proc = subprocess.run(  # nosec B603 B607
        ["git", "-C", str(root), "show", "--end-of-options", f"{sha}:{rel}"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def guard_mcp(tool: str, data: dict[str, Any], cwd: Path) -> str | None:
    """The refusal for a GitHub MCP call, or None."""
    owner, repo = data.get("owner"), data.get("repo")
    if not isinstance(owner, str) or not isinstance(repo, str):
        return None
    root = find_checkout(cwd, owner, repo)
    if root is None:
        return None
    if tool == "mcp__github__create_pull_request":
        head = str(data.get("head", "")).split(":")[-1]
        sha = _branch_sha(root, head) if head else None
        if sha is None:
            return f"[push-preflight] create_pull_request refused: '{head}' is not known locally"
        return _refuse_missing(root, [sha], "create_pull_request")
    if tool == "mcp__github__push_files":
        files = data.get("files") or []
        wanted = {f.get("path"): f.get("content") for f in files if isinstance(f, dict)}
    else:
        wanted = {data.get("path"): data.get("content")}
    wanted = {k: v for k, v in wanted.items() if isinstance(k, str)}
    for sha in _receipt_shas(root):
        if not read_receipt(root, sha)[0]:
            continue
        if all(_content_at(root, sha, str(rel)) == content for rel, content in wanted.items()):
            return None
    return (
        f"[push-preflight] {tool.removeprefix('mcp__github__')} refused: the files are not"
        " identical to any commit with a passing preflight receipt. Commit them locally,"
        f" run the preflight ({RUN_HINT}), then push with git or send exactly that content."
    )


def guard(payload: dict[str, Any]) -> str | None:
    """The refusal for a PreToolUse payload, or None to allow."""
    tool = payload.get("tool_name")
    data = payload.get("tool_input") or {}
    if not isinstance(data, dict):
        return None
    cwd = Path(payload.get("cwd") or os.getcwd())
    try:
        if tool == "Bash" and isinstance(data.get("command"), str):
            return guard_bash(data["command"], cwd)
        if tool in GUARD_TOOLS:
            return guard_mcp(tool, data, cwd)
    except ConfigError as exc:
        return f"[push-preflight] {CONFIG_REL} is invalid ({exc}); fix it before pushing"
    return None


def main_guard(stdin: TextIO = sys.stdin) -> int:
    """PreToolUse entry: exit 2 with the reason on stderr refuses the call."""
    try:
        payload = json.loads(stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        reason = guard(payload)
    except (OSError, RuntimeError) as exc:
        reason = f"[push-preflight] guard could not check the push ({exc}); refused"
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


# --------------------------------------------------------------------------- provisioning


SHIM = """#!/bin/sh
{mark} ({hook})
# Installed by spec-coding (push_preflight.py install-hooks). A hook that was here
# before is kept as {hook}.local and runs after the preflight.
root=$(git rev-parse --show-toplevel) || exit 1
py=$(command -v python3 || command -v python)
{body}
"""

PRE_PUSH_BODY = """input=$(cat)
printf '%s\\n' "$input" | "$py" "$root/.claude/hooks/push_preflight.py" pre-push "$@" || exit 1
if [ -x "$0.local" ]; then printf '%s\\n' "$input" | "$0.local" "$@" || exit 1; fi
exit 0"""

PRE_COMMIT_BODY = """"$py" "$root/.claude/hooks/push_preflight.py" pre-commit || exit 1
if [ -x "$0.local" ]; then "$0.local" "$@" || exit 1; fi
exit 0"""


def install_hooks(root: Path, out: TextIO = sys.stderr) -> list[str]:
    """Install the pre-push and pre-commit shims; keep a foreign hook as ``.local``."""
    folder = hooks_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    done = []
    for hook, body in (("pre-push", PRE_PUSH_BODY), ("pre-commit", PRE_COMMIT_BODY)):
        path = folder / hook
        text = SHIM.format(mark=SHIM_MARK, hook=hook, body=body)
        if path.exists():
            current = path.read_text(encoding="utf-8", errors="replace")
            if current == text:
                continue
            if SHIM_MARK not in current:
                local = folder / f"{hook}.local"
                if not local.exists():
                    path.replace(local)
                    local.chmod(0o755)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        done.append(str(path))
    for name in done:
        print(f"[preflight] installed {name}", file=out)
    return done


def _hook_entry(command: str) -> dict[str, Any]:
    return {"type": "command", "command": command}


def merge_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Add the guard (PreToolUse) and the hook installer (SessionStart), idempotently."""
    script = '"$CLAUDE_PROJECT_DIR/.claude/hooks/push_preflight.py"'
    wanted = {
        "PreToolUse": (GUARD_MATCHER, f"python3 {script} guard"),
        "SessionStart": (None, f"python3 {script} install-hooks --quiet"),
    }
    hooks = settings.setdefault("hooks", {})
    for event, (matcher, command) in wanted.items():
        groups = hooks.setdefault(event, [])
        present = any(
            "push_preflight.py" in str(h.get("command", ""))
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
            if isinstance(h, dict)
        )
        if present:
            for g in groups:
                for h in g.get("hooks", []):
                    if "push_preflight.py" in str(h.get("command", "")):
                        h["command"] = command
                        if matcher is not None:
                            g["matcher"] = matcher
            continue
        group: dict[str, Any] = {"hooks": [_hook_entry(command)]}
        if matcher is not None:
            group = {"matcher": matcher, **group}
        groups.append(group)
    return settings


def rule_block() -> str:
    """The preflight rule text, read from the packaged owner-rules block."""
    here = Path(__file__).resolve().parent
    for cand in (here / "assets" / "owner-rules.consumer.md", here / "owner-rules.consumer.md"):
        if cand.is_file():
            text = cand.read_text(encoding="utf-8")
            start, end = text.find(RULE_BEGIN), text.find(RULE_END)
            if start != -1 and end != -1:
                return text[start : end + len(RULE_END)]
    raise FileNotFoundError("owner-rules.consumer.md with the preflight block not found")


def provision_rules(text: str, preflight: str) -> str:
    """Put the preflight rule block into an instruction file.

    An existing preflight block is refreshed in place. Otherwise the block goes inside
    the owner-rules block, just before its end marker, when the file carries one, and
    at the end of the file when it does not. The rest of the owner-rules block is left
    as it is: refreshing it is a separate rollout with its own review.
    """
    if RULE_BEGIN in text and RULE_END in text:
        start = text.index(RULE_BEGIN)
        end = text.index(RULE_END) + len(RULE_END)
        return text[:start] + preflight + text[end:]
    if OWNER_BEGIN in text and OWNER_END in text:
        end = text.index(OWNER_END)
        return text[:end] + preflight + "\n\n" + text[end:]
    return text.rstrip("\n") + "\n\n" + preflight + "\n"


def install(root: Path, rules_file: str | None = None, out: TextIO = sys.stderr) -> int:
    """Provision a repository: vendored script, settings, rule text, config, git hooks."""
    target = root / VENDORED_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != target.resolve():
        shutil.copyfile(source, target)
    settings_path = root / ".claude" / "settings.json"
    raw = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else "{}"
    indent: str | int = "\t" if "\n\t" in raw else 2  # keep the file's own indentation
    settings_path.write_text(
        json.dumps(merge_settings(json.loads(raw)), indent=indent, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    names = [rules_file] if rules_file else ["CLAUDE.md", "AGENTS.md"]
    preflight = rule_block()
    for name in names:
        path = root / name
        if path.is_file():
            path.write_text(
                provision_rules(path.read_text(encoding="utf-8"), preflight),
                encoding="utf-8",
            )
            break
    else:
        (root / names[0]).write_text(preflight + "\n", encoding="utf-8")
    config = root / CONFIG_REL
    if not config.is_file():
        config.parent.mkdir(parents=True, exist_ok=True)
        scaffold = {
            "schema": CONFIG_SCHEMA,
            "source": "derive from .github/workflows/*.yml",
            "pre_commit": {"checks": []},
            "pre_push": [],
            "skipped": [],
        }
        config.write_text(json.dumps(scaffold, indent=2) + "\n", encoding="utf-8")
    install_hooks(root, out)
    print(f"[preflight] provisioned {root}", file=out)
    return 0


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """``push-preflight {run,pre-push,pre-commit,guard,install-hooks,install,check-config}``."""
    parser = argparse.ArgumentParser(prog="push-preflight", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the pre-push checks for HEAD and write a receipt")
    run.add_argument("--force", action="store_true", help="rerun although a receipt exists")
    push = sub.add_parser("pre-push", help="git pre-push hook body (reads refs on stdin)")
    push.add_argument("hook_args", nargs="*", help="remote name and url, as git passes them")
    sub.add_parser("pre-commit", help="git pre-commit hook body")
    sub.add_parser("guard", help="Claude Code PreToolUse hook body")
    hooks = sub.add_parser("install-hooks", help="install the git shims")
    hooks.add_argument("--quiet", action="store_true")
    inst = sub.add_parser("install", help="provision a repository")
    inst.add_argument("--rules-file", default=None)
    sub.add_parser("check-config", help="validate the config")
    parser.add_argument("--path", default=".", help="repository directory")
    args = parser.parse_args(argv)

    if args.cmd == "guard":
        return main_guard()
    root = repo_root(Path(args.path).resolve())
    if root is None:
        print("[preflight] not inside a git repository", file=sys.stderr)
        return 0 if args.cmd in ("install-hooks", "pre-commit") else 1
    try:
        if args.cmd == "run":
            sha = resolve_sha(root, "HEAD")
            return 1 if sha is None else preflight_sha(root, sha, force=args.force)
        if args.cmd == "pre-push":
            return pre_push(root, sys.stdin.read())
        if args.cmd == "pre-commit":
            return pre_commit(root)
        if args.cmd == "install-hooks":
            if (root / CONFIG_REL).is_file():
                install_hooks(root, io.StringIO() if args.quiet else sys.stderr)
            return 0
        if args.cmd == "install":
            return install(root, args.rules_file)
        if args.cmd == "check-config":
            load_config(root)
            print(f"[preflight] [OK] {CONFIG_REL} is valid", file=sys.stderr)
            return 0
    except ConfigError as exc:
        print(f"[preflight] [FAIL] {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
