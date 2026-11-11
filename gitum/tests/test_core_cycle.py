"""End-to-end cycles: several upstream releases, several people and robots, the whole workflow.

Each test follows one story from the beginning to the end with real
repositories and the gitum command line, checking the state after every
step. They are slower than the unit tests but show that the parts work
together.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import gitum as gitum_pkg
from gitum import changes as changes_mod
from gitum.cli import main
from gitum.errors import EXIT_NEEDS_HUMAN
from gitum.journal import JOURNAL_REF, history_ref
from gitum.repo import Repo as GitumRepo

from core_helpers import Repo, make_upstream

SRC = str(Path(gitum_pkg.__file__).resolve().parents[1])
LAYERS = ('rebased', 'rebased-basealt', 'rebased-etersoft')


def g(repo, *args, expect=0):
	"""Run gitum in-process with --json; check the exit code and return the data."""
	import io
	import contextlib
	buf = io.StringIO()
	with contextlib.redirect_stdout(buf):
		code = main(['--repo', str(repo.path), '--json', *args])
	data = json.loads(buf.getvalue()) if buf.getvalue().strip() else None
	assert code == expect, (args, code, data)
	return data


def g_killed(repo, *args, crash):
	"""Run gitum as a process that dies like kill -9 at the given git call."""
	env = {**os.environ, 'PYTHONPATH': SRC, 'GITUM_TEST_CRASH': crash}
	res = subprocess.run([sys.executable, '-m', 'gitum', '--repo', str(repo.path), *args], env=env,
			     capture_output=True)
	assert res.returncode == 137, res.stderr.decode()


def clone(src, path, origin='origin'):
	r = Repo(path, init=False)
	Repo(path.parent, init=False).git.run('clone', '-q', '-o', origin, str(src), str(path))
	r.git = r.git.at(path)
	return r


def staging_patch(scratch, base, files, subject):
	scratch.run('checkout', '-q', '--detach', base)
	scratch.commit(subject, files)
	return scratch.git.run('format-patch', '--stdout', '-1', 'HEAD').stdout


def results(data, layer):
	return {r['subject']: r for r in data['report']['layers'][layer]['results']}


def no_leftovers(repo):
	gr = GitumRepo(repo.path)
	assert gr.load_op() is None
	assert not (gr.gitum_dir / 'committing.json').exists()
	wt = gr.gitum_dir / 'wt'
	assert not wt.exists() or not list(wt.iterdir())


@pytest.fixture
def world(tmp_path):
	"""Upstream with v1.0, a wine-staging repo, a shared remote and the robot's repository."""
	up = make_upstream(tmp_path / 'upstream')
	scratch = clone(up.path, tmp_path / 'scratch')
	staging = Repo(tmp_path / 'wine-staging')
	staging.write('patches/aaa-Base/definition', 'Fixes: [1] base\n')
	staging.write('patches/aaa-Base/0001-aaa.patch', staging_patch(scratch, 'v1.0', {'aaa': 'a\n'}, 'aaa: Base.'))
	staging.run('add', '-A')
	staging.commit('Release v1.0')
	staging.run('tag', 'v1.0')
	origin = Repo(tmp_path / 'origin.git', bare=True)
	robot = clone(up.path, tmp_path / 'robot', origin='winehq')
	robot.run('remote', 'add', 'origin', str(origin.path))
	robot.run('checkout', '-q', '-b', 'rebased', 'v1.0')
	robot.run('am', '-q', str(staging.path / 'patches/aaa-Base/0001-aaa.patch'))
	robot.run('checkout', '-q', '-b', 'rebased-basealt')
	robot.commit('basealt: tweak', {'basealt': 'alt\n'})
	robot.run('checkout', '-q', '-b', 'rebased-etersoft')
	robot.commit('etersoft: feature (eterbug #100)', {'eter': 'eter\n'})
	robot.commit('etersoft: change base line', {'base': 'line1\nETER\nline3\nline4\nline5\n'})
	robot.run('checkout', '-q', '--detach')
	events = tmp_path / 'events.jsonl'
	rules = tmp_path / 'rules.sh'
	rules.write_text('#!/bin/sh\nexit 1\n')				# our rules know nothing yet
	rules.chmod(0o755)
	agent = tmp_path / 'agent.sh'
	agent.write_text('#!/bin/sh\n[ -e "$GITUM_REPO/../agent-on" ] || exit 1\n'
			 'git checkout --theirs -- $GITUM_FILES && git add -- $GITUM_FILES\n')
	agent.chmod(0o755)
	cfg = tmp_path / 'gitum.toml'
	cfg.write_text('''version = 2
[upstream.winehq]
remote = "winehq"
ref = "winehq/master"
release-tags = "v*"

[layer.staging]
base = "upstream:winehq"
branch = "rebased"
generator = "staging"
staging-repo = "%s"

[layer.basealt]
base = "layer:staging"
branch = "rebased-basealt"

[layer.etersoft]
base = "layer:basealt"
branch = "rebased-etersoft"
annihilate-reverts = true

[resolve]
chain = ["rerere", "rules", "agent"]
rules = "%s"
agent = "%s"

[publish]
remote = "origin"

[hooks]
change = "cat >> %s; echo >> %s"
''' % (staging.path, rules, agent, events, events))
	g(robot, 'init', '--config', str(cfg))
	g(robot, 'publish')
	return {'up': up, 'scratch': scratch, 'staging': staging, 'origin': origin, 'robot': robot,
		'events': events, 'tmp': tmp_path}


def new_release(w, version, upstream_files=None, staging_files=None):
	"""An upstream release and the matching wine-staging release."""
	up, st = w['up'], w['staging']
	up.commit('upstream: work for %s' % version, upstream_files or {'up-%s' % version: version + '\n'})
	up.commit('Release %s' % version, {'VERSION': version + '\n'})
	up.run('tag', 'v' + version)
	if staging_files:
		for name, data in staging_files.items():
			st.write(name, data)
	st.run('add', '-A')
	st.commit('Release v%s' % version)
	st.run('tag', 'v' + version)


def test_fork_through_three_releases(world):
	w = world
	up, robot, tmp = w['up'], w['robot'], w['tmp']

	# a developer works in another clone and pushes with plain git
	dev = clone(w['origin'].path, tmp / 'dev')
	dev.run('remote', 'add', 'winehq', str(up.path))
	dev.run('fetch', '-q', 'winehq')
	g(dev, 'pull')
	dev.run('checkout', '-q', 'rebased-etersoft')
	dev.commit('etersoft: developer fix (eterbug #200)', {'devfix': 'd\n'})
	dev.run('push', '-q', 'origin', 'rebased-etersoft')
	feature_id = {c['subject']: c['id'] for c in changes_mod.load_all(GitumRepo(robot.path).journal()).values()}[
		'etersoft: feature (eterbug #100)']

	# release 1.1: upstream took our feature as is, and changed the line we change too
	up.commit('etersoft: feature (eterbug #100)', {'eter': 'eter\n'})
	new_release(w, '1.1', {'base': 'line1\nUPSTREAM\nline3\nline4\nline5\n'})
	data = g(robot, 'sync', '--next-release', '--publish', expect=EXIT_NEEDS_HUMAN)
	assert data['files'] == ['base']			# rerere and rules could not help
	st = g(robot, 'status')
	assert st['operation']['stopped_at']
	wt = Repo(data['worktree'], init=False)
	wt.write('base', 'line1\nETER+UPSTREAM\nline3\nline4\nline5\n')
	wt.run('add', 'base')
	data = g(robot, 'continue')
	eter = results(data, 'etersoft')
	assert eter['etersoft: feature (eterbug #100)']['result'] == 'dropped-upstream'
	assert eter['etersoft: change base line']['resolver'] == 'manual'
	assert data['published']['integrated'] == {'etersoft': ['etersoft: developer fix (eterbug #200)']}
	assert robot.subjects('rebased-basealt..rebased-etersoft') == [
		'etersoft: change base line', 'etersoft: developer fix (eterbug #200)']
	feature = changes_mod.Tracker(GitumRepo(robot.path).journal()).get(feature_id)
	assert feature['fate'] == 'merged' and feature['gone']
	events = [json.loads(l) for l in w['events'].read_text().splitlines() if l.strip()]
	assert {(e['layer'], e['result']) for e in events} >= {('etersoft', 'dropped-upstream'), ('etersoft', 'resolved')}
	no_leftovers(robot)

	# release 1.2: the robot is killed in the middle and continues
	new_release(w, '1.2')
	g_killed(robot, 'sync', '--next-release', '--publish', crash='cherry-pick:3:after')
	assert g(robot, 'status')['operation']['message'].startswith('interrupted')
	data = g(robot, 'continue')
	assert data['report']['target'] == 'winehq v1.2'
	g(robot, 'publish')
	no_leftovers(robot)

	# release 1.3: a conflict only the agent resolves; that result goes to a review first
	(tmp / 'agent-on').write_text('')
	new_release(w, '1.3', {'devfix': 'upstream\n'})
	data = g(robot, 'sync', '--next-release', '--publish', expect=EXIT_NEEDS_HUMAN)
	assert 'review' in data['error']
	assert robot.rev('candidate/rebased-etersoft') and w['origin'].rev('candidate/rebased-etersoft')
	data = g(robot, 'accept', '--publish')
	assert results(data, 'etersoft')['etersoft: developer fix (eterbug #200)']['resolver'] == 'agent'
	assert not w['origin'].rev('candidate/rebased-etersoft')
	no_leftovers(robot)

	# the developer gets everything, and their checked-out branch follows
	data = g(dev, 'pull')
	assert data['changed']
	assert {b: dev.rev(b) for b in LAYERS} == {b: robot.rev(b) for b in LAYERS}
	assert dev.git.is_clean() and dev.read('VERSION') == '1.3\n'

	# the journal tells the story; the history branch is bisectable state by state
	journal = GitumRepo(robot.path).journal()
	subjects = [robot.git.commit_info(c)['subject'] for c in journal.history()]
	assert [s for s in subjects if s.startswith('gitum: sync')] == [
		'gitum: sync to winehq v1.3', 'gitum: sync to winehq v1.2', 'gitum: sync to winehq v1.1']
	assert robot.git.count(history_ref('etersoft')) >= 4
	for version in ('1.1', '1.2', '1.3'):
		assert any(robot.git.show_file(c, 'VERSION') == (version + '\n').encode()
			   for c in robot.git.rev_list(history_ref('etersoft')))
	assert journal.upstream('winehq')['tag'] == 'v1.3'
	assert len(journal.list('runs')) == 3
	html = tmp / 'report.html'
	g(robot, 'report', '--html', str(html))
	assert 'Sync to winehq v1.3' in html.read_text()
	assert w['origin'].rev(JOURNAL_REF) == robot.rev(JOURNAL_REF)


class FakeForge(http.server.BaseHTTPRequestHandler):
	"""A GitLab API with merge requests in FakeForge.mrs: iid -> data (approved, state, ...)."""
	mrs = {}

	def do_GET(self):
		parts = self.path.rstrip('/').split('/')
		if parts[-1] == 'approvals':
			data = {'approved': self.mrs.get(int(parts[-2]), {}).get('approved', False)}
		else:
			data = self.mrs.get(int(parts[-1]))
		self.send_response(200 if data is not None else 404)
		self.send_header('Content-Type', 'application/json')
		self.end_headers()
		self.wfile.write(json.dumps(data or {}).encode())

	def log_message(self, *args):
		pass


def test_series_from_branch_to_upstream(world):
	w = world
	up, robot, tmp = w['up'], w['robot'], w['tmp']
	server = http.server.HTTPServer(('127.0.0.1', 0), FakeForge)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	forge_url = 'http://127.0.0.1:%d' % server.server_port
	fork = Repo(tmp / 'fork.git', bare=True)
	robot.run('remote', 'add', 'fork', str(fork.path))
	try:
		cfg = tmp / 'cfg.toml'
		cfg.write_text(GitumRepo(robot.path).journal().config_text +
			       '\n[forge.wine]\ntype = "gitlab"\nurl = "%s"\n'
			       '[series]\npr-forge = "wine"\nsubject-lint = \'^[a-z0-9_./-]+: [A-Z].*\\.$\'\n' % forge_url)
		g(robot, 'config', 'load', str(cfg))

		# two series: helper, and use-helper on top of it
		robot.run('checkout', '-q', '-b', 'series/helper', 'v1.0')
		robot.commit('ntdll: Add a helper.', {'ntdll': 'helper\n'})
		robot.run('checkout', '-q', '-b', 'series/use-helper')
		robot.commit('ntdll: Use the helper.', {'ntdll': 'helper\nuse\n'})
		robot.run('checkout', '-q', '--detach')
		g(robot, 'series', 'add', 'helper', '--base', 'upstream:winehq')
		g(robot, 'series', 'add', 'use-helper', '--base', 'upstream:winehq', '--depends', 'helper')
		assert [c['subject'] for c in g(robot, 'series', 'show', 'use-helper')['changes']] == ['ntdll: Use the helper.']
		g(robot, 'queue', 'add', 'helper')
		g(robot, 'queue', 'add', 'use-helper')
		assert g(robot, 'queue')['queues']['upstream']['next'] == 'helper'

		# a variant for our builds; and our builds need the helper now: it goes into the etersoft layer
		g(robot, 'port', 'helper', '--to', 'layer:etersoft')
		assert not g(robot, 'variants', 'helper')['layer:etersoft']['diverged']
		robot.run('checkout', '-q', 'rebased-etersoft')
		robot.run('cherry-pick', 'series/helper')
		robot.run('checkout', '-q', '--detach')
		g(robot, 'save')

		# send helper upstream; the dependent series waits
		data = g(robot, 'prepare', 'helper', '--push', 'fork')
		assert fork.rev('helper') == data['tip']
		g(robot, 'prepare', 'use-helper', expect=EXIT_NEEDS_HUMAN)
		g(robot, 'series', 'set', 'helper', 'state=sent', 'pr=wine/wine!11')
		FakeForge.mrs = {11: {'state': 'opened', 'sha': data['tip'], 'approved': True}}
		g(robot, 'forge', 'refresh')
		q = g(robot, 'queue')['queues']['upstream']
		assert [r['state'] for r in q['rows'] if r['name'] == 'helper'] == ['approved']
		assert q['next'] is None			# use-helper waits for helper to be merged

		# upstream takes it with an edit; the forge says merged
		accepted = up.commit('ntdll: Add a helper.', {'ntdll': 'helper\n', 'ntdll.h': 'void helper(void);\n'})
		new_release(w, '1.1')
		robot.run('fetch', '-q', 'winehq')
		FakeForge.mrs = {11: {'state': 'merged', 'sha': data['tip'], 'merge_commit_sha': accepted,
				      'merged_at': '2026-11-10T12:00:00Z'}}
		g(robot, 'forge', 'refresh')
		helper = g(robot, 'series', 'show', 'helper')
		assert helper['state'] == 'merged' and helper['upstream_commit'] == accepted
		# check-upstream verifies the content and records the change itself
		data = g(robot, 'check-upstream', 'series:helper', '--apply')
		assert data['results'][0]['status'] == 'merged-modified'
		assert data['results'][0]['content_in_upstream'] is True

		# the next sync drops the backported helper from the layer by itself
		data = g(robot, 'sync', '--next-release')
		dropped = {s: r['result'] for s, r in results(data, 'etersoft').items()}
		assert dropped['ntdll: Add a helper.'] in ('dropped-marked', 'dropped-upstream', 'emptied')
		assert 'ntdll: Add a helper.' not in robot.subjects('rebased-basealt..rebased-etersoft')

		# now the dependent series is next and prepares on the new master without its dependency
		assert g(robot, 'queue')['queues']['upstream']['next'] == 'use-helper'
		data = g(robot, 'prepare', 'use-helper', '--push', 'fork')
		assert robot.subjects('winehq/master..upstream/use-helper') == ['ntdll: Use the helper.']
		md = tmp / 'UPSTREAM-QUEUE.md'
		g(robot, 'queue', '--format', 'md', '-o', str(md))
		text = md.read_text()
		assert 'Next to send: **use-helper**' in text
		assert '| helper | 2026-11-10 | v1.1 |' in text
		out = tmp / 'patches'
		g(robot, 'series', 'export', '--dir', str(out))
		assert (out / 'use-helper' / 'definition').read_text() == 'Depends: helper\n'
		g(robot, 'series', 'export', '--dir', str(out), '--check')
		no_leftovers(robot)
	finally:
		server.shutdown()
