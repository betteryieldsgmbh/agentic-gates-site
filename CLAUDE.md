# agentic-gates-site

## OBERSTE REGEL: Kein manueller Eingriff des Owners (Owner 2026-10-31)

Wortlaut: "oberste regel: jegliches manuelles eingefreigen von mir muss vermiden werden."

Kein Klick, kein Befehl, keine Freigabe, keine Einstellung durch den Owner. Die Sitzung erledigt jeden Schritt selbst und nimmt bei einer Sperre den nächsten zulässigen Weg. Bleibt ein Schritt trotzdem beim Owner, ist das ein Fehler im Ablauf: die Ursache wird dauerhaft beseitigt (Rechte, Automatik, Einstellung), damit derselbe Eingriff nie wieder nötig ist, und mit allen versuchten Wegen gemeldet. Schutzvorrichtungen werden dabei nicht umgangen.

<!-- spec-coding:preflight:begin -->

### Three stages of local checks before GitHub (owner 2026-11-09)

Owner wording: "1 a und integriere das auch in spec coding und schau dass das in
allen repos mit CI läuft". Nothing reaches GitHub that has not passed the checks
the repository's own CI would run on it:

1. **pre-commit** -- fast checks on the staged files, including the scan of the
   staged files that ship for this repository's internal or customer terms.
2. **pre-push** -- a git hook (installed by the SessionStart hook) runs the CI
   checks the pushed diff affects, as mapped in `.github/preflight.json`, and
   writes a receipt for the pushed commit under `.git/preflight/<sha>.json`.
3. **session guard** -- `.claude/hooks/push_preflight.py guard` (`PreToolUse`)
   refuses `git push`, `gh pr create`, `create_pull_request`, `push_files` and
   `create_or_update_file` unless a passing receipt exists for that commit.

Before a push, run `python3 .claude/hooks/push_preflight.py run` on the clean
HEAD, fix what it reports, commit, run again. `--no-verify`, a swapped
`core.hooksPath` and a hand-written receipt are routes around a guard and are
never taken. A CI check that cannot run locally (a secret, a service) is listed
under `skipped` in the config with its reason, never silently dropped.
<!-- spec-coding:preflight:end -->
