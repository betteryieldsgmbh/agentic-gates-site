"""Pull the vendored styleguide prose gate into this repo from the styleguide repo.

The styleguide repo (``betteryieldsgmbh/styleguide``) is the single source of truth for
the writing rules. This site ships verbatim copies plus a manifest
(``styleguide.vendor.json``) pinning the exact commit, so the copy is a reproducible
pull rather than a hand-made duplicate that quietly diverges.

Only the prose gate is vendored. The marketing-specific parts of the styleguide (the
Sprachtypen personas that feed the voice judge) do not apply to a landing page.

Usage::

    python tools/pull_styleguide.py            # pull, refresh the manifest
    python tools/pull_styleguide.py --check     # verify copies are in sync

``--check`` needs a local styleguide checkout, so it runs where one exists (a developer
machine) rather than in this repo's CI: the styleguide repo is private and this repo is
public, and wiring a cross-repo token into a public repo to lint a landing page is a
worse trade than checking drift locally.

See ``vendor.py`` for the engine; this module only declares the config.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vendor import VendorConfig, run_cli

CONFIG = VendorConfig(
    name="styleguide",
    manifest_name="styleguide.vendor.json",
    files=(
        ("tools/prose_gate.py", "prose_gate.py"),
        ("tools/prose_rules.json", "prose_rules.json"),
        ("tools/test_prose_gate.py", "test_prose_gate.py"),
    ),
    source_candidates=(
        "/workspace/styleguide",
        "../styleguide",
        "~/styleguide",
        "~/coding/styleguide",
    ),
    note=(
        "Vendored from the styleguide repo. Do not edit the vendored files "
        "by hand; run tools/pull_styleguide.py to refresh."
    ),
    puller="tools/pull_styleguide.py",
)


if __name__ == "__main__":
    run_cli(CONFIG)
