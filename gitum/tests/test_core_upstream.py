import json

import pytest

from gitum import changes as changes_mod
from gitum.cli import main
from gitum.errors import EXIT_NEEDS_HUMAN
from gitum.repo import Repo as GitumRepo
from gitum.upstreamwork import clean_message, queue_markdown

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

def queue_of(work, capsys, *args):
	code, data = gitum_json(work, capsys, 'queue', *args)
	return data['queues']['upstream']


def test_queue_one_at_a_time_and_dependencies(ser, tmp_path, capsys):
	up, work = ser
	work.run('checkout', '-q', '-b', 'series/kernel32', 'v1.0')
	work.commit('kernel32: Something.', {'k32': 'k\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'kernel32', '--base', 'upstream:winehq') == 0
	assert gitum(work, 'queue', 'add', 'ntdll-use') == 0
	assert gitum(work, 'queue', 'add', 'ntdll-base') == 0
	assert gitum(work, 'queue', 'add', 'kernel32') == 0
	q = queue_of(work, capsys)
	assert [r['name'] for r in q['rows']] == ['ntdll-use', 'ntdll-base', 'kernel32']
	assert q['rows'][0]['blocked_by'] == ['ntdll-base']
	assert q['next'] == 'ntdll-base'
	# one at a time: while a series is sent and not approved, nothing else goes
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=sent', 'version=v2',
		     'pr=https://gitlab.winehq.org/wine/wine/-/merge_requests/1') == 0
	q = queue_of(work, capsys, 'next')
	assert q['next'] is None and 'one at a time' in q['reason']
	# an approved series does not block the next one
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=approved') == 0
	assert queue_of(work, capsys)['next'] == 'kernel32'
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=merged') == 0
	assert queue_of(work, capsys)['next'] == 'ntdll-use'
	# pause and resume
	assert gitum(work, 'queue', 'pause', '--reason', 'CI is broken') == 0
	q = queue_of(work, capsys)
	assert q['next'] is None and q['reason'] == 'paused: CI is broken'
	assert gitum(work, 'queue', 'resume') == 0
	assert queue_of(work, capsys)['next'] == 'ntdll-use'
	md = tmp_path / 'UPSTREAM-QUEUE.md'
	assert gitum(work, 'queue', '--format', 'md', '-o', str(md)) == 0
	text = md.read_text()
	assert 'Next to send: **ntdll-use**' in text
	assert '### Queue' in text and '### Done' in text and '| 1 | ntdll-use | queued |' in text
	assert '~~ntdll-base~~' in text


def test_queue_targets_and_labels(ser, tmp_path, capsys):
	up, work = ser
	assert gitum(work, 'series', 'set', 'ntdll-base', 'target=staging', 'state=sent', 'note=waits for CI') == 0
	assert gitum(work, 'queue', 'add', 'ntdll-use') == 0
	code, data = gitum_json(work, capsys, 'queue')
	assert set(data['queues']) == {'staging', 'upstream'}
	# each target has its own one-at-a-time rule; ntdll-use still waits for its dependency
	assert data['queues']['staging']['rows'][0]['note'] == 'waits for CI'
	assert data['queues']['upstream']['reason'] == 'all queued series wait for their dependencies'
	load_config(work, tmp_path, '[queue.markdown]\ntitle = "Очередь в апстрим"\n'
		    'fields = ["position", "name", "state", "reviewer"]\n'
		    '[queue.markdown.titles]\nqueued = "Очередь"\nname = "Серия"\nreviewer = "Ревьюер"\n')
	code, data = gitum_json(work, capsys, 'queue', '--target', 'upstream', '--format', 'md')
	text = queue_markdown(data, journal(work).config.queue['markdown'])
	assert text.startswith('# Очередь в апстрим') and '### Очередь' in text
	assert '| # | Серия | State | Ревьюер |' in text


# prepare

def test_prepare_for_upstream(ser, tmp_path, capsys):
	up, work = ser
	fork = Repo(tmp_path / 'fork.git', bare=True)
	work.run('remote', 'add', 'fork', str(fork.path))
	up.commit('upstream: newer master', {'newer': 'n\n'})
	work.run('fetch', '-q', 'winehq')
	load_config(work, tmp_path, '[series]\nsubject-lint = \'^[a-z0-9_./-]+: [A-Z].*\\.$\'\n')
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use', '--push', 'fork')
	assert code == EXIT_NEEDS_HUMAN and data['depends'] == ['ntdll-base']
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use', '--push', 'fork', '--force')
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
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use', '--force')
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


def test_prepare_dates_and_range_diff(ser, capsys):
	up, work = ser
	up.commit('ntdll: Add a helper.', {'ntdll': 'helper\n'})
	work.run('fetch', '-q', 'winehq')
	gitum(work, 'series', 'set', 'ntdll-base', 'state=merged')
	work.run('checkout', '-q', 'series/ntdll-use')
	work.commit('ntdll: Second change.', {'ntdll2': 'x\n'})
	work.run('checkout', '-q', '--detach')
	gitum(work, 'series', 'save')
	code, data = gitum_json(work, capsys, 'prepare', 'ntdll-use', '--no-depends')
	assert code == 0, data
	dates = work.git.lines('log', '--reverse', '--format=%at', 'winehq/master..upstream/ntdll-use')
	assert [int(d) - int(dates[0]) for d in dates] == [0, 1]
	assert 'ntdll: Use the helper.' in data['range_diff']


def test_series_claims(ser, capsys, monkeypatch):
	up, work = ser
	monkeypatch.setenv('GITUM_SESSION', 'session-a')
	assert gitum(work, 'series', 'claim', 'ntdll-use', '--reason', 'rebasing') == 0
	assert gitum(work, 'series', 'set', 'ntdll-use', 'note=mine') == 0
	monkeypatch.setenv('GITUM_SESSION', 'session-b')
	code, data = gitum_json(work, capsys, 'series', 'set', 'ntdll-use', 'note=theirs')
	assert code == 75 and data['owner']['session'] == 'session-a'
	assert gitum(work, 'prepare', 'ntdll-use', '--force') == 75
	code, data = gitum_json(work, capsys, 'status', '--series')
	rows = {r['name']: r for r in data['series_tree']}
	assert rows['ntdll-use']['claimed_by'] == 'session-a' and rows['ntdll-use']['depth'] == 1
	assert gitum(work, 'series', 'release', 'ntdll-use') == 75
	assert gitum(work, 'series', 'release', 'ntdll-use', '--force') == 0
	assert gitum(work, 'series', 'set', 'ntdll-use', 'note=theirs') == 0


def test_series_import_json(stack, tmp_path, capsys):
	up, work = stack
	work.run('checkout', '-q', '-b', 'series/fix', 'v1.0')
	work.commit('fix: Something.', {'fix': 'f\n'})
	work.run('checkout', '-q', '--detach')
	items = [
		{'name': 'on-top', 'base': 'series:fix', 'depends': ['fix'], 'state': 'queued', 'position': 2},
		{'name': 'fix', 'base': 'upstream:winehq', 'branch': 'series/fix', 'state': 'sent', 'version': 'v3',
		 'pr': 'https://gitlab.example/wine/-/merge_requests/9', 'bugs': ['123'], 'note': 'ping reviewer'},
	]
	(tmp_path / 's.json').write_text(json.dumps(items))
	code, data = gitum_json(work, capsys, 'series', 'import-json', str(tmp_path / 's.json'))
	assert code == 0 and sorted(data['created']) == ['fix', 'on-top']
	fix = journal(work).read_json('series/fix/meta.json')
	assert fix['state'] == 'sent' and fix['version'] == 'v3' and len(fix['changes']) == 1
	assert fix['history'][0]['state'] == 'sent'
	on_top = journal(work).read_json('series/on-top/meta.json')
	assert on_top['changes'] is None and on_top['position'] == 2
	(tmp_path / 's.json').write_text(json.dumps([{'name': 'on-top', 'updated': '2026-10-01T10:00:00+03:00',
						       'history': [{'date': '2026-09-01T10:00:00+03:00', 'state': 'queued'}]}]))
	gitum_json(work, capsys, 'series', 'import-json', str(tmp_path / 's.json'))
	on_top = journal(work).read_json('series/on-top/meta.json')
	assert on_top['updated'] == '2026-10-01T10:00:00+03:00' and on_top['history'][0]['date'].startswith('2026-09-01')
	(tmp_path / 's.json').write_text(json.dumps([{'name': 'fix', 'state': 'merged'}]))
	# moving forward is fine without a date
	code, data = gitum_json(work, capsys, 'series', 'import-json', str(tmp_path / 's.json'))
	assert data['updated'] == ['fix'] and journal(work).read_json('series/fix/meta.json')['state'] == 'merged'
	(tmp_path / 'bad.json').write_text(json.dumps([{'name': 'x', 'base': 'upstream:winehq', 'colour': 1}]))
	assert gitum(work, 'series', 'import-json', str(tmp_path / 'bad.json')) == 1


def test_queue_markdown_sections_links_and_done(ser, tmp_path, capsys):
	up, work = ser
	load_config(work, tmp_path, '[forge.internal]\ntype = "gitlab"\nurl = "https://gitlab.example.org"\n'
		    '[forge.upstream]\ntype = "forgejo"\nurl = "https://code.example.org"\n'
		    '[series]\ninternal-mr-forge = "internal"\n')
	work.run('checkout', '-q', '-b', 'series/later', 'v1.0')
	work.commit('later: Draft.', {'later': '1\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'later', '--base', 'upstream:winehq') == 0
	gitum(work, 'series', 'set', 'later', 'position=5', 'internal_mr=wine/wine!7')
	meta = journal(work).read_json('series/later/meta.json')
	assert meta['internal_mr'] == 'https://gitlab.example.org/wine/wine/-/merge_requests/7'
	gitum(work, 'series', 'set', 'later', 'pr=upstream:wine/wine!12501')
	assert journal(work).read_json('series/later/meta.json')['pr'] == 'https://code.example.org/wine/wine/pulls/12501'
	assert gitum(work, 'series', 'set', 'later', 'pr=wine/wine!3') == 1		# no default forge for pr
	gitum(work, 'series', 'set', 'ntdll-base', 'state=merged', 'release=v1.1', 'upstream_commit=' + 'a' * 40,
	      'merged_at=2026-11-05T10:00:00+00:00')
	code, data = gitum_json(work, capsys, 'queue', '--format', 'md')
	text = queue_markdown(data, {})
	assert '### Waiting to be queued' in text
	assert '[wine/wine!7](https://gitlab.example.org/wine/wine/-/merge_requests/7)' in text
	done = text[text.index('### Done'):]
	assert '| ntdll-base | 2026-11-05 | v1.1 | aaaaaaaaaaaa | 1 |' in done
	# sections can be switched off
	assert '### Waiting' not in queue_markdown(data, {'sections': ['sent', 'queued']})


def test_global_flags_anywhere(ser, capsys):
	up, work = ser
	capsys.readouterr()
	assert main(['series', 'list', '--json', '--repo', str(work.path)]) == 0
	assert 'ntdll-base' in json.loads(capsys.readouterr().out)


def test_mr_url_template(ser, tmp_path, capsys):
	"""An older GitLab without /-/ in its paths: the forge sets its own merge request URL form."""
	up, work = ser
	load_config(work, tmp_path, '[forge.old]\ntype = "gitlab"\nurl = "https://gitlab.old.example"\n'
		    'mr-url = "{url}/{project}/merge_requests/{n}"\n[series]\ninternal-mr-forge = "old"\n')
	gitum(work, 'series', 'set', 'ntdll-base', 'internal_mr=wine/wine!1')
	url = journal(work).read_json('series/ntdll-base/meta.json')['internal_mr']
	assert url == 'https://gitlab.old.example/wine/wine/merge_requests/1'
	from gitum import forge
	assert forge.parse_url(url, journal(work).config.forges)[:4] == (
		'gitlab', 'https://gitlab.old.example/api/v4', 'wine/wine', 1)
	from gitum.upstreamwork import md_link
	assert md_link(url) == '[wine/wine!1](%s)' % url
