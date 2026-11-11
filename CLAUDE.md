# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

gitum (Git Upstream Manager) keeps your changes developed in parallel with an external upstream and syncs them with it.
- The code is the package `gitum/gitum/`. Its design and the decisions taken are in `docs/design-v2.md` (Russian); read it before changing the gitum 2 code. All stages of the plan in section 9 of the design are implemented: 0 foundation, 1 sync, 2 changes/series/upstream check/forges, 3 upstream work (`upstreamwork.py`), 4 packaging (`packaging.py`: generated layers, spec/quilt/staging export and import, HTML reports).

User docs: `README.md` (English) and `README.ru.md` (Russian) describe gitum 2 and must stay in sync with each other. `NOTES` (untracked) is an old list of known problems, in Russian.

Packaged for ALT Linux via gear. The working branch is `sisyphus`. Remotes: `git.github` (github.com/Etersoft/gitum) and `pub.gitlab` (gitlab.eterfund.ru/etersoft/gitum); publish with `gpush`.

## Commands

`pyproject.toml` lives in the `gitum/` subdirectory. gitum has no Python dependencies; run the tests with the system `python3`.

```sh
cd gitum
python3 -m pytest                                        # full suite
python3 -m pytest tests/test_core_sync.py::test_conflict_continue   # single test
python3 -m gitum --repo /path/to/repo status             # gitum 2 CLI from source
python3 -m pyflakes gitum tests                          # lint
```

Package build: `.gear/rules` is `tar: gitum` (it tars the `gitum/` subdir), and `gitum.spec` uses the pyproject macros. Build with `gear-hsh --commit -- ~/tmp/claude/hasher`; new files must be `git add`ed first, because gear takes only tracked files. The version lives in `gitum/gitum/__init__.py` (`__version__`, read by pyproject) and must match `Version:` in `gitum.spec`.

## gitum 2 architecture

- **No GitPython.** `git.py` has `Git` (subprocess runner with a non-interactive env: `GIT_EDITOR=true`, no prompts) and `RefTransaction` (`update-ref --stdin` with old-value checks).
  - Branches change **only** through such a transaction at the end of an operation.
  - **Crash safety is a hard requirement** (README "Reliability"):
    - Every operation must be resumable or undoable after a kill at any point.
    - Save `operation.json` before acting. Mark each step (`picking`/`head`, `gen.applying`/`head`) so that `Sync._recover` can redo it.
    - Move refs only through `repo.committing(tr, moves, kind=...)`. It writes the `committing.json` marker, updates clean user work trees first and undoes them on failure; `repo.recover()` (run on every lock) finishes or undoes an interrupted commit and removes leftover private work trees.
    - Write state files with `repo.write_json`. Move a layer together with the journal through `state.move_layer`.
    - Test new paths with `GITUM_TEST_CRASH="<git cmd>:<n>[:after]"` (`tests/test_core_recovery.py`).
  - **Performance:** almost all the time is spent in git processes (5–12 ms each on a wine-sized repository); Python itself is under 5%. Never run git once per object in a loop. Use the batch helpers instead: `read_blobs` (cat-file --batch), `Journal.read_files`, `commit_infos`, `diffs`, `hash_blobs`, `patch_ids`.
  - Work happens in private work trees under `<common dir>/gitum/wt/`; never reset or clean a user's work tree.
  - A branch checked out in a clean user work tree moves together with it (`repo.check_checkouts` before the transaction, `repo.follow_checkouts` after it, via `read-tree -m -u old new`). A dirty work tree means `NeedsHuman`.
  - Every external command gets its environment from `repo.hook_env()` (sync adds layer, run and target via `Sync._env`). The variables, the lock file format and the publish event are a documented interface in README "Commands that gitum runs": keep them stable.
- **Configuration.** `config.py` parses `gitum.toml` (stdlib `tomllib`): upstreams, and layers whose base is `upstream:<name>` or `layer:<name>`.
- **Journal.** `journal.py` manages the `gitum/journal` branch, one commit per state, built with a temporary index (`JournalWriter`).
  - Each state commit's first parent is the previous state. The layer tips are extra parents, so rebased-away commits stay reachable.
  - Always walk the journal with `--first-parent`.
  - `gitum/history/<layer>` is a linear branch of commit-tree'd layer trees, for `log -p` and bisect between states.
- **Repository.** `repo.py` provides the flock lock (`Busy`, exit 75) with owner info, `operation.json` for resumable operations, run log directories and private work trees, all under `<common dir>/gitum/`.
- **State commands.** `state.py` implements `init` (a layer's base comes from its `compat-start-tag`, the parent tip or the merge base), `save` (records work done with plain git) and `status`.
- **Sync.** `sync.py` holds the whole operation state in `self.op`, saved after every step; `continue`/`skip`/`abort` reload it.
  - Each change is classified as annihilated revert pair, `dropped-backport` (its `Origin:` / `cherry picked from` commit is in the new base; checked before patch-id), `dropped-upstream` (`git cherry`), `picked`, `resolved` (with the resolver recorded), `emptied` or `skipped`.
  - Resolvers run in the order rerere → `resolve.rules` → `resolve.agent` (shell commands, env `GITUM_*`).
  - A result needs review when it used a resolver outside `review-unless` + rerere/manual; it then goes to `candidate/*` and waits for `gitum accept`.
- **Change identity.** `changes.py` stores `changes/<id>.json` in the journal; commits are not rewritten.
  - The `Tracker` matches new commits to ids in this order: `Gitum-Id` trailer, sync sha mapping, same sha, patch-id, subject.
  - It records events and fates. Changes with a fate in `DROP_FATES` are dropped by sync (`dropped-marked`) once the condition holds.
- **Series.** `series.py` keeps `series/<name>/{definition, *.patch, meta.json}`, compatible with the wine-staging layout.
  - Import/export of that layout. Series may sit at any depth (`export_dir`).
  - Export touches only `definition` and the patch files in the series manifest (`patch_files`: every name it ever wrote); everything else in a series directory belongs to people or to other series sharing it.
  - A dependent series starts at its dependency's tip, and changes with a dependency's patch-id are excluded. Patch numbers continue after the dependencies exported into the same directory.
  - `checkout` builds a branch with dependencies first.
  - States are listed in `series.STATES`. Every metadata change sets `updated`; state changes go to `history`.
  - Claims live in `<common dir>/gitum/claims/`; `check_claims` guards series operations.
  - Queues are per `target` and are paused through `queue/<target>.json` in the journal.
- **Upstream check.** `upstreamcheck.py` matches by patch-id, then scores candidates touching the same files by subject + diff similarity. `merged-modified` requires verified content (`content_in`: a three-way apply onto upstream changes nothing). A similar but unverified match is `suspect` and is never recorded or dropped automatically.
- **Forges.** `forge.py` reads GitHub/GitLab/Forgejo REST APIs (tokens from env) and supports JSON import/export.
- **Generated layers.** A layer with `generator = "staging"` (`staging-repo`, `staging-ref` = `v{version}`, `staging-exclude`) or `"command"` is rebuilt on the new base instead of cherry-picked.
  - `post-command` runs afterwards (autoreconf etc.).
  - On wine-rebased this reproduced the robot's `rebased-11.20` except `configure`, whose content depends on the autoconf version.
- **Shared rerere.** `rrcache.py` keeps the rerere cache in the journal under `rr-cache/`.
- **Hooks.** `hooks.py` passes JSON events on stdin; hook failures never fail a run.
- **Publishing.** `publish.py` pushes layers with a lease and the journal/history without force. Commits others pushed since our last publish (`refs/gitum/published/...`) are cherry-picked on top. `pull` fast-forwards the journal, or merges diverged journals with `git merge-tree` when they touched different files, and moves the branches.
- **Exit codes** (`errors.py`): 0 ok, 1 error, 2 `NeedsHuman` (conflict, review, diverged), 10 updated (with `--detailed-exitcode`), 75 busy. Every command takes `--json`.

Tests:
- `tests/core_helpers.py` builds an upstream with release tags and a work clone with layers `rebased` → `rebased-basealt` → `rebased-etersoft` and their `-START` tags, like wine-rebased.
- The autouse `git_env` fixture in `tests/conftest.py` isolates git from the user's config.
