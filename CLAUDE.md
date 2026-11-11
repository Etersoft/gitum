# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

gitum (Git Upstream Manager) keeps your changes developed in parallel with an external upstream and syncs them with it.
- The code is the package `gitum/gitum/`. Its design and the decisions taken are in `docs/design-v2.md` (Russian); read it before changing the gitum 2 code. Stages 0 (foundation) and 1 (sync) are done; stages 2–4 follow the plan in section 9 of the design.

User docs: `README.md` (English) and `README.ru.md` (Russian), still describing gitum 1. `NOTES` (untracked) is an old list of known problems, in Russian.

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
  - Work happens in private work trees under `<common dir>/gitum/wt/`; never reset or clean a user's work tree.
  - A branch checked out in a user work tree moves only with `--update-checked-out`, via `read-tree -m -u old new`.
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
- **Shared rerere.** `rrcache.py` keeps the rerere cache in the journal under `rr-cache/`.
- **Hooks.** `hooks.py` passes JSON events on stdin; hook failures never fail a run.
- **Publishing.** `publish.py` pushes layers with a lease and the journal/history without force. Commits others pushed since our last publish (`refs/gitum/published/...`) are cherry-picked on top. `pull` fast-forwards the journal and moves the branches.
- **Exit codes** (`errors.py`): 0 ok, 1 error, 2 `NeedsHuman` (conflict, review, diverged), 10 updated (with `--detailed-exitcode`), 75 busy. Every command takes `--json`.

Tests:
- `tests/core_helpers.py` builds an upstream with release tags and a work clone with layers `rebased` → `rebased-basealt` → `rebased-etersoft` and their `-START` tags, like wine-rebased.
- The autouse `git_env` fixture in `tests/conftest.py` isolates git from the user's config.
