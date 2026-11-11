import json

import pytest

from gitum import changes as changes_mod
from gitum.cli import main
from gitum.errors import EXIT_NEEDS_HUMAN
from gitum.repo import Repo as GitumRepo
from gitum.upstreamwork import clean_message

from core_helpers import INIT_ARGS, Repo, make_stack


def gitum(work, *args):
	return main(['--repo', str(work.path), *args])


def gitum_json(work, capsys, *args):
	capsys.readouterr()
	code = main(['--repo', str(work.path), '--json', *args])
	out = capsys.readouterr().out
	return code, json.loads(out) if out.strip() else None


def journal(work):
	return GitumRepo(work.path).journal()


def load_config(work, tmp_path, extra):
	path = tmp_path / 'cfg.toml'
	path.write_text(journal(work).config_text + '\n' + extra)
	assert gitum(work, 'config', 'load', str(path)) == 0


@pytest.fixture
def stack(tmp_path, capsys):
	up, work = make_stack(tmp_path)
	assert gitum(work, *INIT_ARGS) == 0
	capsys.readouterr()
	return up, work


@pytest.fixture
def ser(stack):
	"""Two series on upstream: ntdll-base and ntdll-use depending on it."""
	up, work = stack
	work.run('checkout', '-q', '-b', 'series/ntdll-base', 'v1.0')
	work.commit('ntdll: Add a helper.\n\nCo-Authored-By: Bot <bot@x>', {'ntdll': 'helper\n'})
	work.run('checkout', '-q', '-b', 'series/ntdll-use')
	work.commit('ntdll: Use the helper.', {'ntdll': 'helper\nuse\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'ntdll-base', '--base', 'upstream:winehq') == 0
	assert gitum(work, 'series', 'add', 'ntdll-use', '--base', 'series:ntdll-base', '--depends', 'ntdll-base') == 0
	return up, work


def test_clean_message():
	msg = 'mod: Subject.\n\nBody.\n\nSigned-off-by: A <a@x>\nCo-Authored-By: Claude <c@x>\nGitum-Id: abc\n'
	assert clean_message(msg, ['Co-Authored-By', 'Gitum-Id']) == 'mod: Subject.\n\nBody.\n\nSigned-off-by: A <a@x>\n'


# variants

def test_port_and_variants(ser, capsys):
	up, work = ser
	code, data = gitum_json(work, capsys, 'port', 'ntdll-base', '--to', 'layer:etersoft')
	assert code == 0 and data['branch'] == 'series/ntdll-base@etersoft'
	assert work.git.is_ancestor('rebased-etersoft', 'series/ntdll-base@etersoft')
	assert work.subjects('rebased-etersoft..series/ntdll-base@etersoft') == ['ntdll: Add a helper.']
	code, data = gitum_json(work, capsys, 'variants', 'ntdll-base', '--check')
	assert code == 0 and not data['layer:etersoft']['diverged']
	# the variant gets a fix that the main branch does not have
	work.run('checkout', '-q', 'series/ntdll-base@etersoft')
	work.commit('ntdll: Fix the helper for etersoft.', {'ntdll': 'helper fixed\n'})
	work.run('checkout', '-q', '--detach')
	code, data = gitum_json(work, capsys, 'variants', 'ntdll-base', '--check')
	assert code == EXIT_NEEDS_HUMAN
	assert data['layer:etersoft']['only_variant'] == ['ntdll: Fix the helper for etersoft.']


def test_port_skips_changes_already_in_base(ser, capsys):
	up, work = ser
	work.run('checkout', '-q', 'rebased-etersoft')
	work.run('cherry-pick', 'series/ntdll-base')
	work.run('checkout', '-q', '--detach')
	code, data = gitum_json(work, capsys, 'port', 'ntdll-base', '--to', 'refs/heads/rebased-etersoft',
				'--branch', 'v-eter')
	assert data['ported'] == 0 and len(data['already_in_base']) == 1


# queue

def test_queue_one_at_a_time_and_dependencies(ser, tmp_path, capsys):
	up, work = ser
	work.run('checkout', '-q', '-b', 'series/kernel32', 'v1.0')
	work.commit('kernel32: Something.', {'k32': 'k\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'kernel32', '--base', 'upstream:winehq') == 0
	assert gitum(work, 'queue', 'add', 'ntdll-use') == 0
	assert gitum(work, 'queue', 'add', 'ntdll-base') == 0
	assert gitum(work, 'queue', 'add', 'kernel32') == 0
	code, data = gitum_json(work, capsys, 'queue')
	assert [r['name'] for r in data['rows']] == ['ntdll-use', 'ntdll-base', 'kernel32']
	assert data['rows'][0]['blocked_by'] == ['ntdll-base']
	assert data['next'] == 'ntdll-base'
	# one at a time: while a series is sent, nothing else goes
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=sent', 'pr=https://gitlab.winehq.org/wine/wine/-/merge_requests/1') == 0
	code, data = gitum_json(work, capsys, 'queue', 'next')
	assert data['next'] is None and 'one at a time' in data['reason']
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=merged') == 0
	code, data = gitum_json(work, capsys, 'queue', 'next')
	assert data['next'] == 'ntdll-use'
	md = tmp_path / 'UPSTREAM-QUEUE.md'
	assert gitum(work, 'queue', '--format', 'md', '-o', str(md)) == 0
	text = md.read_text()
	assert 'Next to send: **ntdll-use**' in text and '| 1 | ntdll-use | queued |' in text
	assert '~~ntdll-base~~' in text


# prepare

def test_prepare_for_upstream(ser, tmp_path, capsys):
	up, work = ser
	fork = Repo(tmp_path / 'fork.git', bare=True)
	work.run('remote', 'add', 'fork', str(fork.path))
	up.commit('upstream: newer master', {'newer': 'n\n'})
	work.run('fetch', '-q', 'winehq')
	load_config(work, tmp_path, '[series]\nsubject-lint = \'^[a-z0-9_./-]+: [A-Z].*\\.$\'\n')
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use', '--push', 'fork')
	assert code == 0, data
	assert data['branch'] == 'upstream/ntdll-use' and data['commits'] == 2
	assert work.git.is_ancestor('winehq/master', 'upstream/ntdll-use')
	assert work.subjects('winehq/master..upstream/ntdll-use') == ['ntdll: Add a helper.', 'ntdll: Use the helper.']
	msg = work.git.out('log', '-1', '--format=%B', 'upstream/ntdll-use~1')
	assert 'Co-Authored-By' not in msg
	assert fork.rev('ntdll-use') == work.rev('upstream/ntdll-use')
	meta = journal(work).read_json('series/ntdll-use/meta.json')
	assert meta['prepared']['pushed'] == 'fork/ntdll-use' and meta['upstream_branch'] == 'ntdll-use'


def test_prepare_lint_and_merged_dependency(ser, tmp_path, capsys):
	up, work = ser
	load_config(work, tmp_path, '[series]\nsubject-lint = \'^[a-z0-9_./-]+: [A-Z].*\\.$\'\n')
	work.run('checkout', '-q', 'series/ntdll-use')
	work.commit('bad subject', {'bad': 'b\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'save') == 0
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use')
	assert code == EXIT_NEEDS_HUMAN and 'bad subject' in data['error']
	# a merged dependency is already upstream: only the own changes are prepared
	up.commit('ntdll: Add a helper.', {'ntdll': 'helper\n'})
	work.run('fetch', '-q', 'winehq')
	gitum(work, 'series', 'set', 'ntdll-base', 'state=merged')
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use', '--no-lint')
	assert code == 0 and data['commits'] == 2
	assert work.subjects('winehq/master..upstream/ntdll-use') == ['ntdll: Use the helper.', 'bad subject']


def test_prepare_vendored_directory(tmp_path, capsys):
	"""A change of a vendored tree (third-party/lib) goes to the lib upstream without the prefix."""
	lib = Repo(tmp_path / 'lib')
	lib.commit('lib: initial', {'src/a.c': 'int a;\n'})
	app = Repo(tmp_path / 'app')
	app.commit('app: initial', {'main.c': 'int main;\n', 'third-party/lib/src/a.c': 'int a;\n'})
	app.run('remote', 'add', 'lib', str(lib.path))
	app.run('fetch', '-q', 'lib')
	app.run('branch', 'series/lib-fix')
	app.run('checkout', '-q', 'series/lib-fix')
	app.commit('lib: Fix a.', {'third-party/lib/src/a.c': 'int a = 1;\n'})
	app.run('checkout', '-q', '--detach')
	assert main(['--repo', str(app.path), 'init', '--upstream', 'app=master', '--layer', 'main=master']) == 0
	assert main(['--repo', str(app.path), 'series', 'add', 'lib-fix', '--base', 'master']) == 0
	capsys.readouterr()
	code = main(['--repo', str(app.path), '--json', 'prepare', 'lib-fix', '--onto', 'lib/master',
		     '--directory', 'third-party/lib'])
	assert code == 0
	assert app.show('upstream/lib-fix', 'src/a.c') == 'int a = 1;\n'
	assert app.git.is_ancestor('lib/master', 'upstream/lib-fix')


# backport

def test_backport_and_automatic_drop(stack, capsys):
	up, work = stack
	fix = up.commit('upstream: important fix', {'fix': 'fixed\n'})
	work.run('fetch', '-q', 'winehq')
	url = 'https://gitlab.winehq.org/wine/wine/-/commit/%s' % fix
	code, data = gitum_json(work, capsys, 'backport', url, '--into', 'etersoft')
	assert code == 0
	p = data['picked'][0]
	msg = work.git.out('log', '-1', '--format=%B', 'rebased-etersoft')
	assert msg.endswith('Origin: %s' % url)
	rec = changes_mod.Tracker(journal(work)).get(p['id'])
	assert rec['fate'] == 'backport' and rec['meta']['origin'] == fix
	assert gitum(work, 'backport', fix, '--into', 'etersoft') == 1	# already there
	up.commit('Release 1.1', {'VERSION': '1.1\n'})
	up.run('tag', 'v1.1')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	res = {r['subject']: r['result'] for r in data['report']['layers']['etersoft']['results']}
	assert res['upstream: important fix'] == 'dropped-backport'
	assert changes_mod.Tracker(journal(work)).get(p['id'])['gone']


# branches

def test_branches_stale_and_prune(ser, tmp_path, capsys):
	up, work = ser
	fork = Repo(tmp_path / 'fork.git', bare=True)
	work.run('remote', 'add', 'fork', str(fork.path))
	assert gitum(work, 'prepare', 'ntdll-base', '--push', 'fork') == 0
	work.run('branch', 'series/forgotten', 'v1.0')
	code, data = gitum_json(work, capsys, 'branches', '--remote', 'fork')
	rows = {(r['where'], r['branch']): r for r in data['branches']}
	assert rows[('local', 'series/forgotten')]['unknown']
	assert not rows[('fork', 'ntdll-base')]['stale']
	gitum(work, 'series', 'set', 'ntdll-base', 'state=merged')
	code, data = gitum_json(work, capsys, 'branches', '--remote', 'fork', '--prune')
	assert set(data['deleted']) == {'local:series/ntdll-base', 'local:upstream/ntdll-base', 'fork:ntdll-base'}
	assert not work.rev('series/ntdll-base') and not fork.rev('ntdll-base')


# mirrors

def test_mirror(stack, tmp_path, capsys, monkeypatch):
	up, work = stack
	monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
	load_config(work, tmp_path, '[upstream.lib]\nref = "libup/master"\nremote = "libup"\nurl = "%s"\n' % up.path)
	code, data = gitum_json(work, capsys, 'mirror', 'lib')
	assert code == 0 and data['remote'] == 'libup'
	assert (tmp_path / 'cache' / 'gitum' / 'mirrors').is_dir()
	assert work.rev('libup/master') == up.rev('master')
	up.commit('more', {'more': 'm\n'})
	gitum_json(work, capsys, 'mirror', 'lib')
	assert work.rev('libup/master') == up.rev('master')
