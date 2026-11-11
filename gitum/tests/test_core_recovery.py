"""Recovery from a gitum killed at any point (GITUM_TEST_CRASH ends the process like kill -9)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import gitum as gitum_pkg
from gitum.journal import JOURNAL_REF
from gitum.repo import Repo as GitumRepo

from core_helpers import INIT_ARGS, Repo, make_stack

SRC = str(Path(gitum_pkg.__file__).resolve().parents[1])
BRANCHES = ('rebased', 'rebased-basealt', 'rebased-etersoft')


def run(work, *args, crash=None):
	env = {**os.environ, 'PYTHONPATH': SRC}
	env.pop('GITUM_TEST_CRASH', None)
	if crash:
		env['GITUM_TEST_CRASH'] = crash
	res = subprocess.run([sys.executable, '-m', 'gitum', '--repo', str(work.path), '--json', *args],
			     capture_output=True, text=True, env=env)
	data = json.loads(res.stdout) if res.stdout.strip() else None
	return res.returncode, data


def release(up, version, files=None):
	up.commit('upstream change for %s' % version, files or {'up-%s' % version: version + '\n'})
	up.commit('Release %s' % version, {'VERSION': version + '\n'})
	up.run('tag', 'v' + version)


@pytest.fixture
def stack(tmp_path):
	up, work = make_stack(tmp_path)
	assert run(work, *INIT_ARGS)[0] == 0
	return up, work


def expected_after_sync(tmp_path, version='1.1'):
	"""Branch subjects after an uninterrupted sync of a fresh copy of the stack."""
	up, work = make_stack(tmp_path / 'reference')
	assert run(work, *INIT_ARGS)[0] == 0
	release(up, version)
	assert run(work, 'sync', '--next-release')[0] == 0
	return {b: work.subjects('v%s..%s' % (version, b)) for b in BRANCHES}


def state(work, version='1.1'):
	return {b: work.subjects('v%s..%s' % (version, b)) for b in BRANCHES}


def assert_clean(work):
	g = GitumRepo(work.path)
	assert g.load_op() is None
	assert not (g.gitum_dir / 'committing.json').exists()
	assert not list((g.gitum_dir / 'wt').glob('*')) if (g.gitum_dir / 'wt').exists() else True
	assert work.git.out('worktree', 'list').count('\n') == 0


@pytest.mark.parametrize('crash', ['cherry-pick:3', 'cherry-pick:3:after', 'cherry-pick:5:after',
				   'worktree:2:after', 'range-diff:2:after', 'commit-tree:2'])
def test_sync_killed_and_continued(stack, tmp_path, crash):
	up, work = stack
	release(up, '1.1')
	before = {b: work.rev(b) for b in BRANCHES}
	code, _ = run(work, 'sync', '--next-release', crash=crash)
	assert code == 137
	# nothing visible changed
	assert {b: work.rev(b) for b in BRANCHES} == before
	code, st = run(work, 'status')
	assert st['operation'] and 'interrupted' in st['operation']['message']
	code, data = run(work, 'continue')
	assert code == 0, data
	assert state(work) == expected_after_sync(tmp_path)
	assert_clean(work)


def test_sync_killed_after_the_commit(stack, tmp_path):
	"""Killed right after the refs moved: the next command finishes the cleanup."""
	up, work = stack
	release(up, '1.1')
	code, _ = run(work, 'sync', '--next-release', crash='update-ref:1:after')
	assert code == 137
	assert state(work) == expected_after_sync(tmp_path)
	code, st = run(work, 'status')
	assert st['interrupted_commit'] == 'sync'
	code, data = run(work, 'continue')
	assert code == 0 and 'finished the interrupted sync' in data['message']
	assert_clean(work)
	journal = GitumRepo(work.path).journal()
	assert journal.layer('etersoft').tip == work.rev('rebased-etersoft')


def test_checked_out_branch_killed_before_the_refs_moved(stack, tmp_path):
	"""The user work tree was already updated when gitum died: it is put back."""
	up, work = stack
	release(up, '1.1')
	work.run('checkout', '-q', 'rebased-etersoft')
	old_tip = work.rev('rebased-etersoft')
	code, _ = run(work, 'sync', '--next-release', crash='update-ref:1')
	assert code == 137
	assert work.rev('rebased-etersoft') == old_tip
	assert not work.git.is_clean()			# updated ahead of the branch
	code, data = run(work, 'continue')
	assert code == 0, data
	assert work.git.is_clean() and work.read('VERSION') == '1.1\n'
	assert work.git.out('symbolic-ref', 'HEAD') == 'refs/heads/rebased-etersoft'
	assert_clean(work)


def test_conflict_then_killed_while_continuing(stack, tmp_path):
	up, work = stack
	release(up, '1.1', {'base': 'line1\nUPSTREAM\nline3\nline4\nline5\n'})
	code, data = run(work, 'sync', '--next-release')
	assert code == 2
	wt = Repo(data['worktree'], init=False)
	wt.write('base', 'line1\nETER+UPSTREAM\nline3\nline4\nline5\n')
	wt.run('add', 'base')
	code, _ = run(work, 'continue', crash='update-ref:1')
	assert code == 137
	code, data = run(work, 'continue')
	assert code == 0, data
	assert work.show('rebased-etersoft', 'base') == 'line1\nETER+UPSTREAM\nline3\nline4\nline5\n'
	assert_clean(work)


def test_private_work_tree_lost(stack, tmp_path):
	up, work = stack
	release(up, '1.1')
	code, _ = run(work, 'sync', '--next-release', crash='cherry-pick:4')
	assert code == 137
	op = GitumRepo(work.path).load_op()
	import shutil
	shutil.rmtree(op['worktree'])
	code, data = run(work, 'continue')
	assert code == 0, data
	assert state(work) == expected_after_sync(tmp_path)
	assert_clean(work)


def test_leftover_work_tree_is_removed(stack, tmp_path):
	"""A gitum killed inside prepare leaves its work tree; the next locked command removes it."""
	up, work = stack
	work.run('checkout', '-q', '-b', 'series/s', 'v1.0')
	work.commit('s: Change.', {'s': '1\n'})
	work.run('checkout', '-q', '--detach')
	assert run(work, 'series', 'add', 's', '--base', 'upstream:winehq')[0] == 0
	code, _ = run(work, 'prepare', 's', crash='cherry-pick:1:after')
	assert code == 137
	assert list((GitumRepo(work.path).gitum_dir / 'wt').iterdir())
	assert run(work, 'save')[0] == 0
	assert_clean(work)
	assert run(work, 'prepare', 's')[0] == 0


def test_pull_killed(stack, tmp_path):
	up, work = stack
	bare = Repo(tmp_path / 'origin.git', bare=True)
	work.run('remote', 'add', 'origin', str(bare.path))
	cfg = tmp_path / 'c.toml'
	cfg.write_text(GitumRepo(work.path).journal().config_text + '\n[publish]\nremote = "origin"\n')
	assert run(work, 'config', 'load', str(cfg))[0] == 0
	assert run(work, 'publish')[0] == 0
	other = Repo(tmp_path / 'other', init=False)
	work.git.at(tmp_path).run('clone', '-q', str(bare.path), str(other.path))
	other.git = other.git.at(other.path)
	other.run('remote', 'add', 'winehq', str(up.path))
	other.run('fetch', '-q', 'winehq')
	assert run(other, 'pull')[0] == 0
	other.run('checkout', '-q', 'rebased-etersoft')
	release(up, '1.1')
	assert run(work, 'sync', '--next-release', '--publish')[0] == 0
	code, _ = run(other, 'pull', crash='update-ref:1')
	assert code == 137
	code, data = run(other, 'pull')
	assert code == 0, data
	assert {b: other.rev(b) for b in BRANCHES} == {b: work.rev(b) for b in BRANCHES}
	assert other.git.is_clean() and other.read('VERSION') == '1.1\n'
	assert other.rev(JOURNAL_REF) == work.rev(JOURNAL_REF)


def test_killed_before_anything_started(stack, tmp_path):
	up, work = stack
	release(up, '1.1')
	code, _ = run(work, 'sync', '--next-release', crash='worktree:1:after')
	assert code == 137
	code, st = run(work, 'status')
	assert st['operation'] is None
	assert run(work, 'sync', '--next-release')[0] == 0
	assert state(work) == expected_after_sync(tmp_path)
	assert_clean(work)
