# gitum design

*[Русская версия](design.ru.md) · [Manual](manual.md)*

This document records why gitum is built the way it is: the problems it grew
from, the decisions taken and the options rejected. How to use it and what
each command does is in the [manual](manual.md); this document does not
repeat it and links to it instead.

## The task

Develop your own changes in parallel with somebody else's upstream and stay
in sync with it conveniently and visibly: at any moment you see where each of
your changes stands relative to the upstream.

The requirements come from three real scenarios:

- **A fork with a robot (wine-rebased).** A stack of layers winehq → rebased
  (wine-staging) → rebased-basealt → rebased-etersoft with hundreds of own
  patches. An update by cron for every release, a conflict resolution chain
  rerere → rules → agent, a review of the candidate, reports to the bug
  tracker, export to RPM.
- **Series for upstream (wine-kloder).** About a hundred series, each on its
  own branch from the upstream master. An internal MR first, then an
  upstream MR, strictly one at a time. One series lives in two variants (on
  master and in our layer). Upstream accepts patches with modifications.
  Several sessions work in different worktrees.
- **A distribution package (ALT).** The fate of every change: a PR, accepted
  in release X, fixed differently, distro-only. Jumps to release tags,
  backports with `Origin:`, several upstreams in one repository (vendored).

The decision was to develop gitum further even if only its name survived.
That is what happened: gitum 2 was written from scratch.

## What wine-staging taught us

wine-staging is a working example of the same approach: about 120 series in
parallel with the upstream and almost 2000 "Rebase against SHA" commits.

- A series is a directory `patches/<series>/`: `definition` (`Fixes:`,
  `Depends:`, `Disabled:` with a reason) plus `0001-*.patch`.
- The base is one upstream sha for the whole set.
- Series accepted upstream are removed by hand in a rebase commit; broken
  ones get `Disabled: true`.
- The history of the repository is the history of the states of the set.

What it lacks: a link from a series to its MRs and bugs (beyond `Fixes`),
automatic detection of acceptance (let alone with modifications), variants
of a series on different bases, a record of conflict resolutions, a
machine-readable report, and work in git branches: patches are edited as
files, and a series is built in a wine tree to test it.

Hence two decisions. **We take** the series directory and `definition` format
(gitum reads and writes this layout), the `Depends` graph, the base as an
explicit sha, and a journal commit per state. **We add** the series as an
ordinary git branch (gitum generates the directory in the journal),
metadata, variants on bases, tracking of acceptance and a record of
conflicts.

## The model: why three entities

- **A layer** is for the robot: a branch on top of an upstream or another
  layer, hundreds of commits that need not be sorted into series. An
  existing fork can be taken under gitum at once, without sorting it first.
- **A series** is for working with upstream: its own base, dependencies,
  MRs, a state, a queue. It is the unit that is sent and accepted.
- **A change** is for the distribution: every commit has its own
  [fate](manual.md#the-fate-of-a-change) that survives any rebase.

Layers are stacked because real forks are built that way: each layer adds
its own on top of the previous one, and they have to move together.

## The journal: why the state is stored this way

What the journal holds is in the manual:
[what is rewritten and what only grows](manual.md#what-is-rewritten-and-what-only-grows).
This section is about why.

- **The journal only grows.** Publishing it is a fast-forward, no force-push,
  so it is safe to share with colleagues and robots. Layer branches are
  rewritten as before, but the journal is the source of truth.
- **A visible branch `gitum/journal`,** not a hidden ref: it shows in
  `git log`, and it is published and fetched with ordinary tools.
- **Layer tips are extra parents of a journal commit.** After a rebase the
  old tips are no longer reachable from branches and would be lost at
  `git gc`. This way any past state is restored exactly, from the original
  commits, while the main line is read with `--first-parent`.
- **A history branch per layer** `gitum/history/<layer>` is made of commits of
  the ready tip trees (`commit-tree`), so `git log -p` and `git bisect` work
  between our states. gitum 1 applied patches for that; now the tree is
  taken as it is, and it costs almost nothing.
- **Every series and every change is a separate file.** Two sessions editing
  different series create journal commits that merge without conflicts
  (`pull` merges them with `git merge-tree`). That is parallel work without
  locking the whole repository. Concurrent edits of the same series give
  exit code 2, and a person decides.
- **The configuration lives in the journal,** not in the work tree: it is
  shared by all clones and changes together with the state it describes.

## Decisions

The decisions were taken on 2026-11-11, when work on gitum 2 began, and
refined along the way.

1. **A thin wrapper over `git` (subprocess), no GitPython.** Everything gitum
   needs (rebase, cherry-pick with rerere, merge-tree, patch-id,
   update-ref --stdin) exists only in git itself. Libraries either wrap the
   same git process, or lack these operations, or bring a compiled
   dependency. The wrapper is transparent: the run log shows every git
   command.
2. **Python ≥ 3.11 and no dependencies** besides git: `tomllib` is in the
   standard library. The package installs and tests in any distribution.
3. **gitum 1 is not supported:** neither its commands nor converting old
   repositories. Compatibility with its model (one layer, upstream moved one
   commit at a time) would hold the new one back.
4. **Exit codes for automation:** 0 success (10 updated, with
   `--detailed-exitcode`), 1 error, 2 a person is needed, 75 busy. No
   command asks anything: where input is needed, it exits with 2 and says
   what exactly is needed. Every command takes `--json`.
5. **A change's identity is kept in the journal** (`changes/<id>.json`), not
   in a commit trailer. A mandatory `Gitum-Id` in every commit was planned at
   first, but it would have meant rewriting hundreds of existing commits.
   Now a change is recognized by its trailer if it has one, by the sha
   mapping of a sync, by patch-id and by subject.
6. **Reliability is the main requirement.** Any operation can be interrupted
   at any point; afterwards it can be continued or undone, and the next
   command finishes or rolls back an interrupted commit by itself. How it
   works is in [Reliability](manual.md#reliability) in the manual. Every new
   path is tested by killing gitum at a chosen git call
   (`GITUM_TEST_CRASH`).
7. **Work happens only in private work trees.** gitum never resets or cleans
   somebody's work tree. A branch checked out in a clean work tree moves
   together with it; with local changes there, exit code 2.
8. **References move in one transaction** at the end of an operation
   (`update-ref --stdin` with checks of the old values): either all branches,
   the journal and the history are updated, or nothing is.
9. **Generated layers are rebuilt, not rebased** (`generator = "staging"` or
   `"command"`). A layer made of wine-staging series is better built anew on
   the new base than moved commit by commit. The staging generator
   reproduced the robot's `rebased-11.20` layer completely except
   `configure`, which depends on the autoconf version.
10. **Python is not the bottleneck.** Almost all the time goes to git
    processes (5–12 ms each on a wine-sized repository); Python itself takes
    under 5%. So the one rule is: never run git once per object in a loop,
    read in batches (`cat-file --batch`, one `log` for many commits). For
    example, this took `status` from 1.34 to 0.28 s.
11. **The full-cycle tests run in the package build** (`%check`): only git,
    no network, temporary directories.

## Decisions about working with upstream

- **gitum does not create or edit MRs itself.** It prepares the branch and
  the text and pushes the branch to a fork; a person or an agent opens the
  MR. Every forge has its own rules and permissions, and a mistake in
  somebody else's project is expensive.
- **Acceptance with modifications is confirmed by content.** An upstream
  commit with a similar subject and diff is only `suspect`; `merged-modified`
  is set only when a three-way apply onto the upstream changes nothing. A
  false "accepted" would silently drop our change.
- **A stale upstream ref is visible.** A series branch may stand on a newer
  upstream than the local ref, and then foreign commits would become part
  of the series. Without a fetch this cannot be known for sure, so gitum
  looks for commits by people who commit to the upstream (other than you)
  and warns with exit code 2.
- **Bugs are a list with trackers** (`etersoft:19979`, `winehq:60741`), not a
  part of the series name. The series name is at once a branch, a patch
  directory and the MR branch in a public fork. The relation is not one to
  one (one bug, three series; a series with bugs in two trackers), and
  closing a bug as a duplicate must not break branches and links.
- **The MR text with images is kept with the series.** The internal MR and
  the upstream MR are on different forges, images have to be uploaded
  again, and internal notes must not leak. So the text and images live in
  the journal, the internal part is marked, and the URLs of the images
  uploaded by whoever opens the MR are remembered by content. gitum itself
  uploads nothing and handles no tokens: writing to somebody else's service
  on the user's behalf is not its job, and tokens are a needless risk.
  Import reads public MRs only.
- **Hooks instead of integrations.** gitum does not write to trackers or
  chats: it passes JSON events on stdin to hooks, and a failing hook never
  fails the operation.

## What gitum will not do

- It does not create MRs/PRs itself: only preparation, the text and a push to
  a fork.
- No build or notification system of its own: only checks and hooks.
- It does not replace git: branches stay ordinary branches, gitum only keeps
  them in sync with the journal.
- No logic special to one project: everything wine needed is done with
  general mechanisms (generators, rules, hooks, trackers).

## History

- **gitum 1 (2012).** One layer of patches over one upstream, branches
  `upstream`/`rebased`/`mainline`/`patches`, the upstream moved one commit
  at a time, GitPython. Ideas carried over to gitum 2: the journal as a
  branch whose history is the history of states, and a linear history with
  `git log -p` and bisect between states.
- **gitum 2.0.0 (2026).** Written from scratch in five stages: the foundation
  (git wrapper, transactions, locks, journal), sync, changes and series,
  working with upstream, packaging. All stages are implemented.

Checked on real data (a wine-rebased clone): the basealt (15 patches) and
etersoft (156 patches) layers moved from 11.19 to 11.21 gave patches
identical to the robot's result (154 of 156; the robot later redid the two
missing ones). `check-upstream` found the taskschd patch accepted in
wine-11.5.
