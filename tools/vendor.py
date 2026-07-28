"""Generic vendoring engine: pull files verbatim from a source git checkout and
pin them with a manifest, plus a ``--check`` drift gate.

A vendored file is a byte-identical copy of a file owned by another repo, plus a
manifest (``<name>.vendor.json``) that records the source repo, the exact commit,
and each file's SHA-256. This makes the copy a reproducible pull rather than a
hand-made duplicate: ``--check`` fails the moment a vendored copy drifts from its
source (the source moved on and this repo was not re-pulled, or a vendored file
was edited by hand). ``pull_styleguide.py`` and ``pull_speccoding.py`` are thin
configs over this engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VendorConfig:
    """One vendoring relationship: which source repo, which files, which manifest."""

    name: str  # human label, e.g. "styleguide"
    manifest_name: str  # e.g. "styleguide.vendor.json"
    files: tuple[tuple[str, str], ...]  # (source_path_in_repo, vendored_name_under_tools)
    source_candidates: tuple[str, ...]  # checkout paths tried when --source is absent
    note: str  # manifest note
    puller: str  # this puller's path, for messages


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def git_head_commit(repo: Path) -> str:
    """Return the full commit SHA at the repo's HEAD."""
    return _git(repo, "rev-parse", "HEAD")


def git_is_dirty(repo: Path) -> bool:
    """Return True when the repo has uncommitted changes to tracked files."""
    return bool(_git(repo, "status", "--porcelain"))


def commit_exists(repo: Path, commit: str) -> bool:
    """True if ``commit`` is a real commit object in ``repo`` (force-push detection)."""
    try:
        _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
        return True
    except subprocess.CalledProcessError:
        return False


def git_show(repo: Path, commit: str, path: str) -> bytes | None:
    """Return the bytes of ``path`` as of ``commit``, or None if it does not exist there."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{path}"],
            capture_output=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def source_bytes(repo: Path, commit: str, path: str) -> bytes:
    """Return the bytes of ``path`` as committed, falling back to the working tree.

    Pull and ``--check`` must read the SAME bytes or the check is meaningless. ``check``
    compares against the git object, so the pull has to copy the git object too: on a
    checkout with ``core.autocrlf`` (any Windows machine) the working-tree copy has CRLF
    line endings while the object has LF, which made every freshly pulled file report as
    drifted. The fallback covers a source that is not a git checkout at all.
    """
    committed = git_show(repo, commit, path)
    if committed is not None:
        return committed
    return (repo / path).read_bytes()


def slug_from_url(url: str) -> str:
    """Reduce a git remote URL to its ``owner/repo`` slug.

    Handles both URL forms (``https://host/owner/repo.git``) and scp-style SSH
    remotes (``git@host:owner/repo.git``) by treating ``:`` as a path break.
    """
    normalized = url.rstrip("/").removesuffix(".git").replace(":", "/")
    parts = normalized.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else url


def git_repo_slug(repo: Path) -> str:
    """Return the ``owner/repo`` slug from the repo's origin remote."""
    return slug_from_url(_git(repo, "remote", "get-url", "origin"))


def discover_source_repo(config: VendorConfig, explicit: str | None) -> Path:
    """Locate the source checkout, preferring an explicit path."""
    candidates = [explicit] if explicit else list(config.source_candidates)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if all((path / source_rel).is_file() for source_rel, _ in config.files):
            return path
    tried = ", ".join(c for c in candidates if c)
    raise SystemExit(f"{config.name} checkout not found (tried: {tried})")


def build_manifest(
    config: VendorConfig, source_repo: Path, commit: str, slug: str
) -> dict[str, object]:
    """Build the manifest describing each vendored file and its source SHA."""
    files = [
        {
            "source": source_rel,
            "vendored": vendored_name,
            # Hash the COMMITTED bytes, matching what the drift check compares against.
            "sha256": hashlib.sha256(source_bytes(source_repo, commit, source_rel)).hexdigest(),
        }
        for source_rel, vendored_name in config.files
    ]
    return {
        "note": config.note,
        "source_repo": slug,
        "source_commit": commit,
        "files": files,
    }


def write_manifest(config: VendorConfig, target_dir: Path, manifest: dict[str, object]) -> None:
    """Write the manifest atomically as pretty JSON with a trailing newline."""
    path = target_dir / config.manifest_name
    body = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    # Write to a sibling temp file then rename, so an interrupted run never
    # leaves a half-written manifest that the --check gate would misread.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def pull(config: VendorConfig, source_repo: Path, target_dir: Path, commit: str, slug: str) -> None:
    """Copy each source file verbatim into tools/ and refresh the manifest."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for source_rel, vendored_name in config.files:
        content = source_bytes(source_repo, commit, source_rel)
        dest = target_dir / vendored_name
        dest.parent.mkdir(parents=True, exist_ok=True)  # vendored_name may be a subpath
        dest.write_bytes(content)
        print(f"[OK] {source_rel} -> {vendored_name}")
    write_manifest(config, target_dir, build_manifest(config, source_repo, commit, slug))
    print(f"[OK] manifest pinned to {slug}@{commit[:12]}")


def _read_manifest(config: VendorConfig, path: Path) -> dict[str, object] | str:
    """Return the parsed manifest dict, or an error message when unusable."""
    if not path.is_file():
        return f"missing manifest {config.manifest_name}; run the pull first"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"{config.manifest_name}: unreadable ({exc})"
    if not isinstance(data, dict):
        return f"{config.manifest_name}: malformed (expected a JSON object)"
    return data


def check(config: VendorConfig, source_repo: Path, target_dir: Path, commit: str) -> list[str]:
    """Return drift messages; an empty list means the copies are in sync."""
    manifest = _read_manifest(config, target_dir / config.manifest_name)
    if isinstance(manifest, str):
        return [manifest]
    drift: list[str] = []
    pinned = manifest.get("source_commit")
    if pinned != commit:
        shown = pinned[:12] if isinstance(pinned, str) else repr(pinned)
        drift.append(
            f"manifest pins {shown} but {config.name} HEAD is {commit[:12]}; re-pull to catch up"
        )
    # The pinned commit must still exist: a force-push that rewrites history can
    # remove it, which SHA-pinning the files alone would not catch (external review).
    pin_ok = isinstance(pinned, str) and commit_exists(source_repo, pinned)
    if isinstance(pinned, str) and not pin_ok:
        drift.append(
            f"pinned commit {pinned[:12]} not found in {config.name} source (rewritten history?)"
        )
    for source_rel, vendored_name in config.files:
        source = source_repo / source_rel
        vendored = target_dir / vendored_name
        if not vendored.is_file():
            drift.append(f"{vendored_name}: missing vendored copy")
            continue
        # Verify the vendored copy against the file AS OF THE PINNED COMMIT, not the
        # mutable working tree, so working-tree edits cannot mask real drift.
        if pin_ok:
            at_pin = git_show(source_repo, str(pinned), source_rel)
            if at_pin is None:
                drift.append(f"{source_rel}: not present at pinned commit {str(pinned)[:12]}")
            elif hashlib.sha256(at_pin).hexdigest() != sha256_file(vendored):
                drift.append(f"{vendored_name}: differs from {config.name} at the pinned commit")
        elif not source.is_file():
            drift.append(f"{source_rel}: missing in {config.name} source")
        elif sha256_file(vendored) != sha256_file(source):
            drift.append(f"{vendored_name}: content differs from {config.name} source")
    return drift


def _resolve(config: VendorConfig, args: argparse.Namespace) -> tuple[Path, Path, str, str]:
    """Resolve source repo, target dir, commit and slug from CLI args."""
    source_repo = discover_source_repo(config, args.source)
    target_dir = Path(args.target).resolve()
    commit = args.commit or git_head_commit(source_repo)
    slug = args.repo_slug or git_repo_slug(source_repo)
    if not args.commit and not args.allow_dirty and git_is_dirty(source_repo):
        raise SystemExit(
            f"{config.name} checkout has uncommitted changes; commit them or pass "
            "--allow-dirty to pin the current HEAD anyway"
        )
    return source_repo, target_dir, commit, slug


def run_cli(config: VendorConfig, argv: list[str] | None = None) -> None:
    """Parse arguments and run either the pull or the drift check for one config."""
    default_target = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=f"Vendor {config.name} files into tools/.")
    parser.add_argument("--source", help=f"path to the {config.name} checkout")
    parser.add_argument(
        "--target", default=str(default_target), help="tools/ dir receiving the copies"
    )
    parser.add_argument("--commit", help="override the pinned commit (skips git)")
    parser.add_argument("--repo-slug", help="override the source repo slug")
    parser.add_argument("--allow-dirty", action="store_true", help="allow a dirty source")
    parser.add_argument("--check", action="store_true", help="verify only, write nothing")
    args = parser.parse_args(argv)

    source_repo, target_dir, commit, slug = _resolve(config, args)
    if args.check:
        drift = check(config, source_repo, target_dir, commit)
        if drift:
            print(f"{config.name} vendor drift:", file=sys.stderr)
            for message in drift:
                print(f"  - {message}", file=sys.stderr)
            raise SystemExit(1)
        print(f"[OK] vendored {config.name} copies are in sync")
        return
    pull(config, source_repo, target_dir, commit, slug)
