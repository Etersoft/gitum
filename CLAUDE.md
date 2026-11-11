# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

gitum (Git Upstream Manager) keeps a downstream fork of a git project in sync with upstream: it pulls upstream commits one by one into your working branch and keeps your own changes available as a set of ready-to-submit patches. User docs: `README.md` (English) and `README.ru.md` (Russian); see also https://wiki.etersoft.ru/GitUM. `NOTES` (untracked) is a list of known problems and wishes, written in Russian.

Packaged for ALT Linux via gear. The working branch is `sisyphus`; `p7` holds backports. `git.github` (github.com/Etersoft/gitum) is the up-to-date public remote.

## Runtime constraints

- Python >= 3.9, GitPython >= 3.1, git. Code is indented with tabs.
- Diffs and patch contents are handled as **bytes** (`_diff()` uses `stdout_as_string=False`, patches are written with `write_bytes`) so binary files, CRLF and non-UTF-8 text survive. Patches are applied with `git am --keep-cr`. Keep it that way; tests cover it.
- Where GitPython can't feed stdin (`hash-object`, `mktree` in `_make_orphan_branch`) or an editor is needed (`commit -e` in an interactive update), git runs via `subprocess`. Non-interactive `git rebase` gets `GIT_EDITOR=true` (`NO_EDITOR_ENV`), because modern `rebase --continue` would otherwise open an editor.

## Commands

`pyproject.toml` lives in the `gitum/` subdirectory. GitPython is not installed system-wide on the dev machine; the venv `~/tmp/claude/gitum-venv` (created with `--system-site-packages`) has it.

```sh
cd gitum
python3 -m pytest                                        # full suite
python3 -m pytest tests/test_local.py::test_restore      # single test
python3 -m gitupstream --repo /path/to/repo status       # run CLI from source
pip install .                                            # installs the `gitum` entry point
```

Tests build real repos in `tmp_path`.
- The autouse `git_env` fixture in `tests/conftest.py` isolates git from the user's config (HOME, `defaultBranch=master`, `GIT_EDITOR=true`).
- The `Work` helper wraps a repo. The `work` fixture is a gitum repo with branches `master`/`rebased`/`dev`/`patches` tracking `merge`.
- The number of conflicts in a merge scenario depends on the git merge backend, so make upstream test changes either clearly separate from or clearly overlapping with local ones.

Package build: `.gear/rules` is `tar: gitum` (it tars the `gitum/` subdir), and `gitum.spec` uses `%pyproject_build` / `%pyproject_install` / `%pyproject_run_pytest`. Build with `gear-hsh --commit`. The version lives in `gitupstream/__init__.py` (`__version__`, read by pyproject) and must match `Version:` in `gitum.spec`.

## Architecture

- `gitupstream/cli.py`: argparse CLI, `main(argv) -> int`. Each subcommand (`create`, `merge`, `update`, `restore`, `remove`, `clone`, `pull`, `push`, `status`) maps to one `GitUpstream` method. A `GitUmException` gives exit code 1; the method has already printed the details.
- `gitum/gitupstream/gitupstream.py`: the `GitUpstream` class, which holds all the logic.
- `constants.py`: default branch names. `errors.py`: exception hierarchy.

### The four branches

`create` sets up four branches (names are configurable):

| Default name | Role |
|---|---|
| `upstream` | Mirror of the tracked upstream (`origin/master` by default). |
| `rebased` | Our patches rebased on top of `upstream`. The user works here. **Local only, never pushed**: it is regenerated from `patches`. |
| `mainline` (the "current" branch) | Linear history with one commit per upstream commit merged and per local change. This is the publishable branch. Its tree must always equal `rebased`'s tree. |
| `patches` | Orphan branch whose tree has only `*.patch` files (`git format-patch upstream..rebased`), `_upstream_commit_` (upstream sha) and `_current_patch_` (the mainline commit that produced this state). One commit per state change, so history can be restored from it. |

If the names are not the defaults, they are stored in `.gitum-config` on the orphan `gitum-config` branch, created with `hash-object`/`mktree`/`commit-tree`. `_load_config` falls back to the defaults when that branch is missing.

### Core flows

- **merge** (pull upstream commits): for each new upstream commit, a three-stage pipeline runs. `_stage1` merges the commit into `upstream`. `_stage2` rebases `rebased` onto it and diffs the old and new `rebased` heads. `_stage3` applies that diff to `mainline` as a commit carrying the upstream author and message. `_save_repo_state` then regenerates `patches`. On conflict, the progress (saved branch heads, index, stage) goes to `<git_dir>/.gitum-state`, and `merge --continue/--skip/--abort` resumes, skips or rolls back via `_load_state`.
- **update** (record local work done on `rebased`): if `rebased` only gained new commits since the last saved head, they are cherry-picked one by one onto `mainline`. Otherwise (history was rewritten) the whole `mainline`↔`rebased` diff goes in as one commit (`_diffapply`).
- **restore**: rebuilds `rebased` (and with `--full`, the other branches) from the `patches` history (`_gen_rebased` applies the `*.patch` files with `git am` on top of `_upstream_commit_`).
- **clone/pull/push**: share `upstream`, `mainline`, `patches` and `gitum-config` with a remote gitum repo. `pull` hard-resets the local branches to the remote ones, then replays `_current_patch_` entries via `git am` (`_pull_commits`).

State files live in `repo.git_dir` (`_state_path`), not a hardcoded `.git/`. Concurrent use of one repo (several worktrees or sessions) is still unsafe: gitum checks out and resets the shared branches. Invariant checks: before mutating, most commands verify that `mainline` is still at the head saved in `.curent_mainline` (`_check_mainline`) and compare `rebased` with `.curent_rebased`. If these files are missing, the repository is not a gitum repo (`NotGitumRepo`). Other state files: `.gitum-remote` (default remote for push/pull `--track`) and `.gitum-mbranch` (default merge branch).
