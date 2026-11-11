# gitum — Git Upstream Manager

*[Русская версия](README.ru.md)*

gitum helps you develop in parallel with an external upstream and stay in sync
with it. It is meant for distribution packages that carry patches, forks with
hundreds of local changes (wine-rebased), and series of changes waiting to be
accepted upstream. At any time it shows where each of your changes stands
relative to upstream.

## Ideas

- **Layers.** Your changes live in ordinary git branches stacked on top of
  an upstream, for example `winehq` → `rebased` (wine-staging) →
  `rebased-basealt` → `rebased-etersoft`. You work in them with plain git.
- **One-pass sync.** `gitum sync` moves the whole stack onto a new upstream
  release in one pass. It works in a private work tree, and all branches
  move in one atomic step at the end. A conflict stops the run without
  touching anything; you resolve it and continue.
- **Every change is accounted for.** Each change is classified as applied
  cleanly, resolved (and by whom: rerere, your rules, an agent, a person),
  already upstream, dropped as a backport, dropped because of its known
  fate, or removed as a patch + revert pair.
- **The journal.** The `gitum/journal` branch is append-only, with one commit
  per published state: layer bases and tips, change ids and fates, series,
  sync reports, and a shared rerere cache. The layer branches are rewritten,
  but the journal and the per-layer history branches `gitum/history/<layer>`
  only grow. That makes them safe to share, and `git log -p` and
  `git bisect` work across your states.
- **Changes have identities and fates.** gitum follows a change through every
  rebase without rewriting commits. Its fate can be: local, sent upstream
  (PR), merged as a given commit, merged with modifications, fixed
  differently, distro-only, backport, disabled. `gitum status --changes`
  shows what each change needs.
- **Series.** A series is a named set of changes on its own branch, with
  dependencies, bugs, merge requests and a state. The journal stores series
  in the wine-staging layout, so they can be imported from and exported to
  wine-staging style directories.

## Requirements

- Python 3.11 or newer
- git 2.45 or newer

## Installation

```sh
pip install ./gitum
```

For ALT Linux, build the RPM package from `gitum.spec` with gear.

## Quick start

```sh
cd wine-rebased
gitum init --upstream winehq=winehq/master --release-tags 'wine-*' \
           --layer rebased=rebased:winehq \
           --layer basealt=rebased-basealt:rebased \
           --layer etersoft=rebased-etersoft:basealt
gitum status
gitum sync --next-release        # the next wine-X.Y tag, all layers
# on a conflict: fix the files in the printed work tree, git add, then
gitum continue                   # or gitum skip / gitum abort
gitum publish                    # push layers, history and journal
```

`init` takes the base of each layer from its `<branch>-START` tag, the tip of
the parent layer, or the merge base with the upstream.

## Configuration

The configuration is `gitum.toml` in the journal. Show it with
`gitum config show`; replace it with `gitum config load FILE`.

```toml
version = 2

[upstream.winehq]
remote = "winehq"
ref = "winehq/master"
release-tags = "wine-*"

[layer.rebased]                 # rebuilt from wine-staging, not cherry-picked
base = "upstream:winehq"
branch = "rebased"
generator = "staging"
staging-repo = "../wine-staging"
staging-ref = "v{version}"
post-command = "autoreconf -f && tools/make_requests"

[layer.basealt]
base = "layer:rebased"
branch = "rebased-basealt"
compat-start-tag = "rebased-basealt-START"

[layer.etersoft]
base = "layer:basealt"
branch = "rebased-etersoft"
annihilate-reverts = true

[resolve]
chain = ["rerere", "rules", "agent"]
rules = "tools/conflict-resolve.py"
agent = "tools/conflict-agent.sh"
review-unless = ["rerere"]       # anything else goes through candidate/*

[publish]
remote = "origin"
check = "./build.sh"             # must pass before the branches move

[series]
subject-lint = '^[a-z0-9_./-]+: [A-Z].*\.$'

[hooks]                          # get JSON events on stdin
change = "tools/notify-bugs.py"
run = "tools/notify-chat.sh"
publish = "tools/set-version-branches.sh"
```

Upstream options:
- `rewritten = true`: the upstream is regenerated rather than continued, like
  a branch built by patchinstall.
- `prefix = "third-party/lib"`: the upstream is vendored inside your tree.

## Commands

All commands take `--json` and `--repo PATH`. Exit codes:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error |
| 2 | needs a person: conflict, review, diverged history |
| 10 | something was updated (with `--detailed-exitcode`) |
| 75 | the repository is busy |

Nothing asks questions interactively.

| Command | What it does |
|---|---|
| `init` | Create the journal from existing branches. |
| `status [--changes] [--series [--variants]]` | Upstreams, layers, operations in progress, locks, the "change → fate → action" table, series as a tree by dependencies (state, base lag, claims, diverged variants). |
| `sync [--to REF \| --next-release] [--step] [--candidate auto\|always\|never] [--publish] [--find-culprit] [--allow-rewrite]` | Move the stack onto a new upstream. |
| `continue`, `skip`, `abort` | Resume, drop the conflicting change, or cancel a stopped sync. |
| `accept`, `drop-candidate` | Publish or drop a result waiting for review in `candidate/*`. |
| `save` | Record work done on layer branches with plain git. |
| `publish`, `pull` | Share the state. Commits others pushed meanwhile are put on top; journals of parallel sessions merge. |
| `change list\|show\|set` | Changes and their fates: `gitum change set ID fate=merged upstream_commit=SHA`. |
| `check-upstream [layer:X\|series:Y] [--since REF] [--apply]` | Find our changes upstream: patch-id, then subject and diff similarity. A match counts as accepted with edits only when the content is verified (our change applied onto upstream changes nothing); anything else is `suspect` and waits for a person. |
| `series add\|save\|list\|show\|set\|remove\|export\|import-staging\|import-json\|checkout\|claim\|release` | Series of changes. States: draft → review (internal) → queued → sent (version vN) → approved → merged, rejected or superseded. A series may sit at any depth of an export directory (`export_dir`); export touches only `definition` and `*.patch`. `claim` marks a series as worked on by a session (`GITUM_SESSION`); other sessions get exit code 75 for it. |
| `port`, `variants` | A series on another base, and how its variants differ. |
| `queue [next\|add\|pause\|resume] [--target T]`, `queue --format md` | Upstream submission queues, one per target (series field `target`). One series at a time until it is approved or merged, dependencies first. `pause --reason` stops a queue. Generates `UPSTREAM-QUEUE.md`; its titles come from `[queue.markdown]`. |
| `prepare SERIES [--push FORK] [--force]` | Upstream form of a series: fresh base, new dates one second apart, no Co-Authored-By/Gitum-Id trailers, subject check, path remapping for vendored trees, a range-diff against the saved series. Dependencies must be merged first (`--force` includes them). You open the merge request yourself; `forge refresh` warns if its head differs from the prepared branch. |
| `backport SHA\|URL --into LAYER` | Take an upstream commit with an `Origin:` trailer; it is dropped automatically once the base contains it. |
| `branches [--remote FORK] [--prune]` | Series and upstream branches; stale ones after merge. |
| `forge refresh\|import-json\|export-json` | Merge request states from GitHub, GitLab or Forgejo, or as JSON from your own scripts. |
| `export`, `import` | Patch sets for RPM spec files (Patch lines between `# BEGIN gitum patches` / `# END gitum patches`), quilt, wine-staging. |
| `log [CHANGE\|series:X\|layer:X]`, `diff STATE1 [STATE2]`, `report [RUN] [--html FILE]` | History and reports. |
| `mirror NAME` | A shared mirror of an upstream in `~/.cache/gitum` instead of throwaway clones. |

## Commands that gitum runs

gitum runs your commands for conflict resolution, checks, generated layers
and hooks. None of them is interactive. They all get the same `GITUM_*`
environment:

| Variable | Value |
|---|---|
| `GITUM_REPO` | the main work tree of the repository |
| `GITUM_COMMON_DIR` | the git common directory |
| `GITUM_WORKTREE` | the private work tree of the operation (sync only) |
| `GITUM_RUN`, `GITUM_RUN_DIR` | the run id and its log directory |
| `GITUM_LAYER` | the layer being processed |
| `GITUM_UPSTREAM`, `GITUM_TARGET`, `GITUM_TARGET_TAG`, `GITUM_VERSION` | the upstream, the target commit, its release tag and the version from the tag (`wine-11.20` → `11.20`) |
| `GITUM_LOCK_FILE` | the repository lock file |
| `GITUM_LOCK_HELD` | set to the lock file while gitum holds the lock |

| Command | Working directory | Additional variables | Result |
|---|---|---|---|
| `resolve.rules`, `resolve.agent` | the private work tree with the conflict | `GITUM_COMMIT`, `GITUM_SUBJECT`, `GITUM_FILES` (one per line), `GITUM_CONFLICT_DIR`, `GITUM_RESOLVER` | exit 0 with all conflicts resolved and `git add`ed |
| `publish.check` | the private work tree at the top of each leaf layer | `GITUM_TIP` | exit 0, or the branches stay unchanged |
| `generator = "command"` | the private work tree at the new base | `GITUM_BASE` | new commits on HEAD |
| `post-command` of a generated layer | the private work tree | | commits; leftovers are committed by gitum |
| `hooks.change`, `hooks.run`, `hooks.publish` | the main work tree | `GITUM_EVENT` | JSON event on stdin; failures never fail the run |

Hooks:
- `hooks.change` gets one event per change that was not simply applied.
- `hooks.run` gets the whole sync report (`event` is `sync` or `candidate`).
- `hooks.publish` runs after a successful `gitum publish`. Its event looks like this:

```json
{"event": "publish", "remote": "origin", "journal": "<sha>",
 "upstreams": {"winehq": {"ref": "winehq/master", "sha": "<sha>", "tag": "wine-11.20", "version": "11.20"}},
 "layers": {"etersoft": {"branch": "rebased-etersoft", "old": "<sha before the push>", "new": "<sha>",
                         "base": "<sha>", "start_tag": "rebased-etersoft-START"}},
 "integrated": {"etersoft": ["subjects of commits put on top"]}}
```

**The lock** is a stable interface for your own scripts.
- It is an exclusive `flock` on `$GIT_COMMON_DIR/gitum/lock`.
- While it is held, the file contains the owner as JSON: `{"pid", "host", "user", "session", "since"}`. The session comes from `GITUM_SESSION`.
- Commands run by gitum get `GITUM_LOCK_HELD`, and a `gitum` started from them does not lock again.

**Checked-out branches.** A layer branch checked out in a clean work tree
moves together with its work tree. If that work tree has local changes,
gitum stops with exit code 2 and changes nothing.

## Development

```sh
cd gitum
python3 -m pytest
python3 -m gitum --help
```

The design and the decisions behind it are in
[docs/design-v2.md](docs/design-v2.md) (in Russian).

## Links

- Project page (in Russian): <https://wiki.etersoft.ru/GitUM>
- Article on Habr about gitum 1 (in Russian, 2013): <https://habr.com/ru/sandbox/60279/>
- Presentation at Protva 2012 (Pavel Shilovsky, Vitaly Lipatov):
  [Presentation Gitum at Protva 2012.odp](https://wiki.etersoft.ru/Файл:Презентация_Gitum_на_Протве_2012.odp)
- Source code: <https://github.com/Etersoft/gitum>

## License

GPL-2.0-or-later. See [gitum/COPYING](gitum/COPYING).
