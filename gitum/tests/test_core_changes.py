import http.server
import json
import threading

import pytest

from gitum import changes as changes_mod
from gitum import forge
from gitum.cli import main
from gitum.errors import EXIT_NEEDS_HUMAN, GitumError
from gitum.repo import Repo as GitumRepo

from core_helpers import INIT_ARGS, Repo, make_stack


def gitum(work, *args):
	return main(['--repo', str(work.path), *args])


def gitum_json(work, capsys, *args):
	capsys.readouterr()
	code = main(['--repo', str(work.path), '--json', *args])
	out = capsys.readouterr().out
	return code, json.loads(out) if out.strip() else None


@pytest.fixture
def stack(tmp_path, capsys):
	up, work = make_stack(tmp_path)
	assert gitum(work, *INIT_ARGS) == 0
	capsys.readouterr()
	return up, work


def release(up, version, files=None, message=None, author=None):
	up.commit(message or 'upstream change for %s' % version, files or {'up-%s' % version: version + '\n'},
		  author=author)
	up.commit('Release %s' % version, {'VERSION': version + '\n'})
	up.run('tag', 'v' + version)


def journal(work):
	return GitumRepo(work.path).journal()


def by_subject(work):
	return {c['subject']: c for c in changes_mod.load_all(journal(work)).values()}


# identity

def test_init_assigns_ids(stack):
	up, work = stack
	recs = by_subject(work)
	assert set(recs) == {'staging: patch one', 'staging: patch two', 'basealt: tweak', 'etersoft: feature',
			     'etersoft: change base line'}
	feature = recs['etersoft: feature']
	assert feature['layer'] == 'etersoft' and feature['fate'] == 'local'
	assert feature['events'][0]['event'] == 'added'
	layer = journal(work).layer('etersoft')
	assert layer.changes[0].gitum_id == feature['id']


def test_ids_survive_sync_and_dropped_change_gets_fate(stack, capsys):
	up, work = stack
	ids = {s: c['id'] for s, c in by_subject(work).items()}
	up.commit('etersoft: feature', {'eter': 'eter\n'})
	release(up, '1.1')
	assert gitum(work, 'sync', '--next-release') == 0
	recs = by_subject(work)
	assert {s: c['id'] for s, c in recs.items()} == ids
	assert recs['etersoft: change base line']['sha'] == work.rev('rebased-etersoft')
	feature = recs['etersoft: feature']
	assert feature['gone'] and feature['fate'] == 'merged'
	assert feature['events'][-1]['event'] == 'dropped-upstream'
	# not in the default list any more, but in --all
	code, rows = gitum_json(work, capsys, 'change', 'list')
	assert 'etersoft: feature' not in [r['subject'] for r in rows]
	code, rows = gitum_json(work, capsys, 'change', 'list', '--all')
	assert 'etersoft: feature' in [r['subject'] for r in rows]


def test_ids_survive_amend_and_trailer(stack):
	up, work = stack
	ids = {s: c['id'] for s, c in by_subject(work).items()}
	work.run('checkout', '-q', 'rebased-etersoft')
	work.run('commit', '-q', '--amend', '-m', 'etersoft: change base line, reworded')
	work.commit('etersoft: with trailer\n\nGitum-Id: fixedid1', {'t': 't\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'save') == 0
	recs = by_subject(work)
	assert recs['etersoft: change base line, reworded']['id'] == ids['etersoft: change base line']
	assert recs['etersoft: with trailer']['id'] == 'fixedid1'


def test_bugs_from_subject(stack):
	up, work = stack
	work.run('checkout', '-q', 'rebased-etersoft')
	work.commit('kerberos: fix (eterbug #16552)', {'k': 'k\n'})
	work.run('checkout', '-q', '--detach')
	gitum(work, 'save')
	assert by_subject(work)['kerberos: fix (eterbug #16552)']['meta']['bugs'] == ['16552']


# fates

def test_change_set_status_table_and_log(stack, capsys):
	up, work = stack
	cid = by_subject(work)['basealt: tweak']['id']
	assert gitum(work, 'change', 'set', cid, 'fate=distro-only', 'reason=ALT specific') == 0
	code, data = gitum_json(work, capsys, 'status', '--changes')
	rows = {r['subject']: r for r in data['changes']}
	assert 'basealt: tweak' not in rows		# no action needed
	assert rows['etersoft: feature']['action'].startswith('decide')
	assert data['fates'] == {'local': 4, 'distro-only': 1}
	code, data = gitum_json(work, capsys, 'change', 'show', cid[:5])
	assert data['meta']['reason'] == 'ALT specific'
	code, events = gitum_json(work, capsys, 'log', cid)
	assert [e['event'] for e in events] == ['added', 'fate']
	assert gitum(work, 'change', 'set', cid, 'fate=nonsense') == 1
	assert gitum(work, 'change', 'set', cid, 'colour=red') == 1


def test_marked_changes_are_dropped_by_sync(stack, capsys):
	up, work = stack
	recs = by_subject(work)
	# accepted upstream as a rewritten commit, recorded by hand
	accepted = up.commit('feature, the upstream way', {'eter': 'eter upstream\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	assert gitum(work, 'change', 'set', recs['etersoft: feature']['id'], 'fate=merged',
		     'upstream_commit=' + accepted) == 0
	assert gitum(work, 'change', 'set', recs['basealt: tweak']['id'], 'fate=fixed-differently',
		     'link=https://example.org/x') == 0
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0
	res = {r['subject']: r for l in data['report']['layers'].values() for r in l['results']}
	assert res['etersoft: feature']['result'] == 'dropped-marked'
	assert res['basealt: tweak']['result'] == 'dropped-marked'
	assert work.subjects('rebased..rebased-etersoft') == ['etersoft: change base line']
	recs = by_subject(work)
	assert recs['etersoft: feature']['fate'] == 'merged' and recs['etersoft: feature']['gone']


# check-upstream

def test_check_upstream_exact_and_modified(stack, capsys):
	up, work = stack
	# exact copy of one patch, an edited version of another (other subject, extra line)
	up.commit('etersoft: feature', {'eter': 'eter\n'})
	up.commit('base: change line 2 like Etersoft does.', {'base': 'line1\nETER\nline3\nline4\nline5\nmore\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft', '-v')
	res = {e['subject']: e for e in data['results']}
	assert res['etersoft: feature']['status'] == 'merged'
	assert res['etersoft: feature']['release'] == 'v1.1'
	assert res['etersoft: change base line']['status'] == 'merged-modified'
	assert res['etersoft: change base line']['content_in_upstream'] is True
	assert res['etersoft: change base line']['upstream_subject'] == 'base: change line 2 like Etersoft does.'
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:basealt', '-v')
	assert data['results'][0]['status'] == 'not-found'
	# record the findings: the next sync drops both
	gitum_json(work, capsys, 'change', 'set', res['etersoft: change base line']['id'], 'fate=merged-modified',
		   'upstream_commit=' + res['etersoft: change base line']['upstream_commit'])
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft', '--apply')
	assert data['applied'] == [res['etersoft: feature']['id']]
	assert by_subject(work)['etersoft: feature']['meta']['release'] == 'v1.1'
	assert gitum(work, 'sync', '--next-release') == 0
	assert work.subjects('rebased-basealt..rebased-etersoft') == []


def test_check_upstream_unknown_target(stack):
	up, work = stack
	assert gitum(work, 'check-upstream', 'layer:nope') == 1


# series

@pytest.fixture
def with_series(stack):
	up, work = stack
	work.run('checkout', '-q', '-b', 'series/ntdll-base', 'v1.0')
	work.commit('ntdll: Add a helper.', {'ntdll': 'helper\n'})
	work.run('checkout', '-q', '-b', 'series/ntdll-use')
	work.commit('ntdll: Use the helper (bug 123).', {'ntdll': 'helper\nuse\n'})
	work.commit('ntdll/tests: Test the helper.', {'ntdll-tests': 't\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'ntdll-base', '--base', 'upstream:winehq') == 0
	assert gitum(work, 'series', 'add', 'ntdll-use', '--base', 'series:ntdll-base', '--depends', 'ntdll-base') == 0
	return up, work


def test_series_add_list_set(with_series, capsys):
	up, work = with_series
	code, data = gitum_json(work, capsys, 'series', 'list')
	assert set(data) == {'ntdll-base', 'ntdll-use'}
	use = data['ntdll-use']
	assert [c['subject'] for c in use['changes']] == ['ntdll: Use the helper (bug 123).', 'ntdll/tests: Test the helper.']
	assert use['depends'] == ['ntdll-base'] and use['state'] == 'draft'
	j = journal(work)
	assert sorted(j.list('series/ntdll-use')) == ['0001-ntdll-Use-the-helper-bug-123.patch',
						      '0002-ntdll-tests-Test-the-helper.patch', 'definition', 'meta.json']
	assert j.read_text('series/ntdll-use/definition') == 'Depends: ntdll-base\n'
	# changes of a series have ids and know their series
	recs = by_subject(work)
	assert recs['ntdll: Use the helper (bug 123).']['meta'] == {'bugs': ['123'], 'series': 'ntdll-use'}
	assert gitum(work, 'series', 'set', 'ntdll-use', 'state=sent', 'pr=https://gitlab.example/wine/-/merge_requests/7',
		     'fixes=[123] Something') == 0
	meta = journal(work).read_json('series/ntdll-use/meta.json')
	assert meta['state'] == 'sent' and meta['pr'].endswith('/7')
	assert journal(work).read_text('series/ntdll-use/definition') == 'Fixes: [123] Something\nDepends: ntdll-base\n'
	assert gitum(work, 'series', 'set', 'ntdll-use', 'state=flying') == 1
	assert gitum(work, 'series', 'set', 'ntdll-base', 'depends=ntdll-use') == 1	# cycle
	assert gitum(work, 'series', 'remove', 'ntdll-base') == 1			# has dependents


def test_series_save_and_log(with_series, capsys):
	up, work = with_series
	work.run('checkout', '-q', 'series/ntdll-use')
	work.commit('ntdll: Fix the helper use.', {'ntdll': 'helper\nuse fixed\n'})
	work.run('checkout', '-q', '--detach')
	code, data = gitum_json(work, capsys, 'series', 'save')
	assert data['saved'] == ['ntdll-use']
	assert len(journal(work).read_json('series/ntdll-use/meta.json')['changes']) == 3
	code, entries = gitum_json(work, capsys, 'log', 'series:ntdll-use')
	assert [e['subject'] for e in entries] == ['gitum: series save ntdll-use', 'gitum: series add ntdll-use']
	code, data = gitum_json(work, capsys, 'diff', 'gitum/journal~1')
	assert 'series:ntdll-use' in data


def test_series_export_import_checkout(with_series, tmp_path, capsys):
	up, work = with_series
	out = tmp_path / 'patches'
	code, data = gitum_json(work, capsys, 'series', 'export', '--dir', str(out))
	assert set(data['written']) == {'ntdll-base', 'ntdll-use'}
	assert (out / 'ntdll-use' / 'definition').read_text() == 'Depends: ntdll-base\n'
	assert gitum(work, 'series', 'export', '--dir', str(out), '--check') == 0
	(out / 'ntdll-use' / 'definition').write_text('stale\n')
	assert gitum(work, 'series', 'export', '--dir', str(out), '--check') == EXIT_NEEDS_HUMAN
	# a wine-staging style directory with a disabled series
	staging = tmp_path / 'staging'
	(staging / 'foo-Bar').mkdir(parents=True)
	(staging / 'foo-Bar' / 'definition').write_text(
		'Fixes: [999] Make foo work\nDepends: ntdll-use\n# broken since 1.1\nDisabled: true\n')
	(staging / 'foo-Bar' / '0001-foo.patch').write_bytes(
		journal(work).read('series/ntdll-base/0001-ntdll-Add-a-helper.patch'))
	assert gitum(work, 'series', 'import-staging', str(staging), '--base', 'upstream:winehq') == 0
	meta = journal(work).read_json('series/foo-Bar/meta.json')
	assert meta['fixes'] == ['[999] Make foo work'] and meta['depends'] == ['ntdll-use']
	assert meta['disabled'] == 'broken since 1.1' and meta['state'] == 'disabled'
	# build branches from the journal patches, dependencies first
	work.run('branch', '-D', 'series/ntdll-use')
	code, data = gitum_json(work, capsys, 'series', 'checkout', 'ntdll-use', '--onto', 'v1.0', '--branch', 'try-use')
	assert code == 0 and len(data['applied']) == 3
	assert work.subjects('v1.0..try-use') == ['ntdll: Add a helper.', 'ntdll: Use the helper (bug 123).',
						  'ntdll/tests: Test the helper.']


# forges

def test_parse_request_urls():
	assert forge.parse_url('https://github.com/vitlav/ton/pull/12')[:4] == \
		('github', 'https://api.github.com', 'vitlav/ton', 12)
	assert forge.parse_url('https://gitlab.winehq.org/wine/wine/-/merge_requests/8123')[:4] == \
		('gitlab', 'https://gitlab.winehq.org/api/v4', 'wine/wine', 8123)
	assert forge.parse_url('https://codeberg.org/a/b/pulls/5')[:4] == \
		('forgejo', 'https://codeberg.org/api/v1', 'a/b', 5)
	with pytest.raises(GitumError):
		forge.parse_url('https://example.org/nothing')


class FakeGitLab(http.server.BaseHTTPRequestHandler):
	states = {}

	approvals = {}

	def do_GET(self):
		parts = self.path.rstrip('/').split('/')
		if parts[-1] == 'approvals':
			data = {'approved': self.approvals.get(int(parts[-2]), False)}
		else:
			data = self.states.get(int(parts[-1]))
		self.send_response(200 if data else 404)
		self.send_header('Content-Type', 'application/json')
		self.end_headers()
		self.wfile.write(json.dumps(data or {}).encode())

	def log_message(self, *args):
		pass


def test_forge_refresh_with_gitlab_api(with_series, tmp_path, capsys):
	up, work = with_series
	server = http.server.HTTPServer(('127.0.0.1', 0), FakeGitLab)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	base = 'http://127.0.0.1:%d' % server.server_port
	try:
		text = journal(work).config_text + '\n[forge.test]\ntype = "gitlab"\nurl = "%s"\n' % base
		(tmp_path / 'c.toml').write_text(text)
		assert gitum(work, 'config', 'load', str(tmp_path / 'c.toml')) == 0
		recs = by_subject(work)
		fid = recs['etersoft: feature']['id']
		gitum(work, 'change', 'set', fid, 'fate=sent', 'pr=%s/wine/wine/-/merge_requests/1' % base)
		gitum(work, 'series', 'set', 'ntdll-use', 'state=sent', 'pr=%s/wine/wine/-/merge_requests/2' % base)
		FakeGitLab.states = {1: {'state': 'merged', 'merge_commit_sha': 'a' * 40}, 2: {'state': 'opened'}}
		code, data = gitum_json(work, capsys, 'forge', 'refresh')
		assert code == 0 and data['checked'] == 2 and data['updated'] == ['change ' + fid]
		rec = changes_mod.Tracker(journal(work)).get(fid)
		assert rec['fate'] == 'merged' and rec['meta']['upstream_commit'] == 'a' * 40
		FakeGitLab.states[2] = {'state': 'closed'}
		code, data = gitum_json(work, capsys, 'forge', 'refresh')
		assert data['updated'] == ['series ntdll-use']
		assert journal(work).read_json('series/ntdll-use/meta.json')['state'] == 'rejected'
	finally:
		server.shutdown()


def test_forge_json_import_export(with_series, tmp_path, capsys):
	up, work = with_series
	fid = by_subject(work)['basealt: tweak']['id']
	data = {'changes': [{'id': fid, 'fate': 'distro-only', 'reason': 'ALT only'}],
		'series': [{'series': 'ntdll-base', 'state': 'merged', 'upstream_commit': 'b' * 40}]}
	(tmp_path / 'in.json').write_text(json.dumps(data))
	code, res = gitum_json(work, capsys, 'forge', 'import-json', str(tmp_path / 'in.json'))
	assert sorted(res['updated']) == ['change ' + fid, 'series ntdll-base']
	code, exported = gitum_json(work, capsys, 'forge', 'export-json')
	assert {c['id']: c['fate'] for c in exported['changes']}[fid] == 'distro-only'
	assert {s['name']: s['state'] for s in exported['series']}['ntdll-base'] == 'merged'
	(tmp_path / 'bad.json').write_text(json.dumps([{'id': 'nope', 'fate': 'merged'}]))
	assert gitum(work, 'forge', 'import-json', str(tmp_path / 'bad.json')) == 1


# parallel sessions

def test_journal_merge_between_clones(with_series, tmp_path, capsys):
	up, work = with_series
	bare = Repo(tmp_path / 'origin.git', bare=True)
	work.run('remote', 'add', 'origin', str(bare.path))
	text = journal(work).config_text + '\n[publish]\nremote = "origin"\n'
	(tmp_path / 'c.toml').write_text(text)
	assert gitum(work, 'config', 'load', str(tmp_path / 'c.toml')) == 0
	assert gitum(work, 'publish') == 0
	other = Repo(tmp_path / 'other', init=False)
	other.git.at(tmp_path).run('clone', '-q', '--no-checkout', str(bare.path), str(other.path))
	other.git = other.git.at(other.path)
	assert main(['--repo', str(other.path), 'pull']) in (0, 10)
	# both sessions change different series
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=review') == 0
	assert main(['--repo', str(other.path), 'series', 'set', 'ntdll-use', 'state=queued']) == 0
	assert main(['--repo', str(other.path), 'publish']) == 0
	assert gitum(work, 'publish') == EXIT_NEEDS_HUMAN
	code, data = gitum_json(work, capsys, 'pull')
	assert code == 0 and data['merged']
	assert gitum(work, 'publish') == 0
	j = journal(work)
	assert j.read_json('series/ntdll-base/meta.json')['state'] == 'review'
	assert j.read_json('series/ntdll-use/meta.json')['state'] == 'queued'
	# the same series changed on both sides cannot merge
	assert gitum(work, 'series', 'set', 'ntdll-use', 'state=sent') == 0
	assert main(['--repo', str(other.path), 'pull']) in (0, 10)
	assert main(['--repo', str(other.path), 'series', 'set', 'ntdll-use', 'state=review']) == 0
	assert gitum(work, 'publish') == 0
	assert main(['--repo', str(other.path), 'pull']) == EXIT_NEEDS_HUMAN


def test_check_upstream_since(stack, capsys):
	up, work = stack
	up.commit('etersoft: feature', {'eter': 'eter\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	assert gitum(work, 'sync', '--next-release') == 0
	# after the sync the default range (sync point..ref) is empty; --since searches the past
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft')
	assert [e['status'] for e in data['results']] == ['not-found']
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft', '--since', 'v1.0', '--ref', 'v1.1')
	assert code == 0


def test_check_upstream_similar_but_not_contained_is_suspect(stack, capsys):
	"""A similar upstream commit whose content differs never drops our change by itself."""
	up, work = stack
	up.commit('etersoft: change base line', {'base': 'line1\nUPSTREAM WAY\nline3\nline4\nline5\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft', '--apply')
	res = {e['subject']: e for e in data['results']}
	assert code == EXIT_NEEDS_HUMAN
	assert res['etersoft: change base line']['status'] == 'suspect'
	assert res['etersoft: change base line']['content_in_upstream'] is False
	assert data['applied'] == []
	assert by_subject(work)['etersoft: change base line']['fate'] == 'local'


def test_series_export_keeps_foreign_files(with_series, tmp_path, capsys):
	up, work = with_series
	out = tmp_path / 'patches'
	work_dir = out / 'ntdll-use'
	(work_dir / 'src').mkdir(parents=True)
	(work_dir / 'src' / 'probe.c').write_text('int main;\n')
	(work_dir / 'README.md').write_text('notes\n')
	(work_dir / 'run-tests.sh').write_text('#!/bin/sh\n')
	(work_dir / '0009-stale.patch').write_text('old\n')
	assert gitum(work, 'series', 'export', 'ntdll-use', '--dir', str(out)) == 0
	# a patch the series never wrote is not ours: it stays
	assert sorted(p.name for p in work_dir.iterdir()) == [
		'0001-ntdll-Use-the-helper-bug-123.patch', '0002-ntdll-tests-Test-the-helper.patch', '0009-stale.patch',
		'README.md', 'definition', 'run-tests.sh', 'src']
	assert (work_dir / 'src' / 'probe.c').read_text() == 'int main;\n'
	# foreign files do not make the copy stale
	(work_dir / 'notes.tsv').write_text('a\tb\n')
	assert gitum(work, 'series', 'export', 'ntdll-use', '--dir', str(out), '--check') == 0


def test_series_nested_directories_and_reimport(with_series, tmp_path, capsys):
	"""Series at any depth: font/bold and font/regular are two series with their own export_dir."""
	up, work = with_series
	patch = journal(work).read('series/ntdll-base/0001-ntdll-Add-a-helper.patch')
	src = tmp_path / 'incoming'
	for sub in ('font/bold', 'font/regular'):
		(src / sub).mkdir(parents=True)
		(src / sub / '0001-a.patch').write_bytes(patch)
	(src / 'font' / 'README.md').write_text('two merge requests\n')
	code, data = gitum_json(work, capsys, 'series', 'import-staging', str(src), '--base', 'upstream:winehq')
	assert sorted(data['imported']) == ['font-bold', 'font-regular']
	assert journal(work).read_json('series/font-bold/meta.json')['export_dir'] == 'font/bold'
	assert gitum(work, 'series', 'set', 'font-bold', 'state=sent', 'pr=https://example.org/mr/21') == 0
	# re-import keeps the metadata, updates definition and patches
	(src / 'font' / 'bold' / 'definition').write_text('Fixes: [5] Bold strikes\n')
	assert gitum(work, 'series', 'import-staging', str(src), '--base', 'upstream:winehq', 'font/bold') == 0
	meta = journal(work).read_json('series/font-bold/meta.json')
	assert meta['state'] == 'sent' and meta['pr'] == 'https://example.org/mr/21'
	assert meta['fixes'] == ['[5] Bold strikes']
	out = tmp_path / 'out'
	assert gitum(work, 'series', 'export', 'font-bold', '--dir', str(out)) == 0
	assert sorted(p.name for p in (out / 'font' / 'bold').iterdir()) == ['0001-a.patch', 'definition']
	assert gitum(work, 'series', 'set', 'font-bold', 'export_dir=../escape') == 0
	assert gitum(work, 'series', 'export', 'font-bold', '--dir', str(out)) == 1


def test_forge_approved_and_outdated_head(with_series, tmp_path, capsys):
	up, work = with_series
	server = http.server.HTTPServer(('127.0.0.1', 0), FakeGitLab)
	threading.Thread(target=server.serve_forever, daemon=True).start()
	base = 'http://127.0.0.1:%d' % server.server_port
	try:
		(tmp_path / 'c.toml').write_text(journal(work).config_text + '\n[forge.test]\ntype = "gitlab"\nurl = "%s"\n' % base)
		assert gitum(work, 'config', 'load', str(tmp_path / 'c.toml')) == 0
		assert gitum(work, 'prepare', 'ntdll-base', '--no-lint') == 0
		gitum(work, 'series', 'set', 'ntdll-base', 'state=sent', 'pr=%s/wine/wine/-/merge_requests/3' % base)
		prepared = journal(work).read_json('series/ntdll-base/meta.json')['prepared']['tip']
		FakeGitLab.states = {3: {'state': 'opened', 'sha': prepared}}
		FakeGitLab.approvals = {3: True}
		code, data = gitum_json(work, capsys, 'forge', 'refresh')
		meta = journal(work).read_json('series/ntdll-base/meta.json')
		assert meta['state'] == 'approved' and data['outdated'] == []
		assert meta['history'][-1] == {'date': meta['updated'], 'state': 'approved', 'previous': 'sent',
					       'source': 'forge'}
		FakeGitLab.states = {3: {'state': 'opened', 'sha': 'f' * 40}}
		FakeGitLab.approvals = {}
		code, data = gitum_json(work, capsys, 'forge', 'refresh')
		assert data['outdated'] == [{'series': 'ntdll-base', 'head': 'f' * 40, 'prepared': prepared}]
		assert journal(work).read_json('series/ntdll-base/meta.json')['state'] == 'sent'
	finally:
		server.shutdown()


def test_check_upstream_one_to_one_and_aliases(stack, tmp_path, capsys):
	"""Two of our changes squashed into one upstream commit; the author uses another address."""
	up, work = stack
	from gitum import upstreamcheck
	cfg = journal(work).config
	assert upstreamcheck.identities(cfg, 'Tester', 'a@x') == {'tester', 'a@x'}
	load_config(work, tmp_path, '[identity]\naliases = [["tester@localhost", "tester@upstream.org"]]\n')
	assert 'tester@upstream.org' in upstreamcheck.identities(journal(work).config, 'T', 'tester@localhost')
	up.commit('etersoft: feature and base line together',
		  {'eter': 'eter\n', 'base': 'line1\nETER\nline3\nline4\nline5\n'}, author='Tester <tester@upstream.org>')
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft', '-v')
	res = {e['subject']: e for e in data['results']}
	a, b = res['etersoft: feature'], res['etersoft: change base line']
	assert a['upstream_commit'] == b['upstream_commit']
	assert {a['status'], b['status']} == {'merged-modified'}
	assert any('also matches' in (e.get('note') or '') for e in (a, b))


def load_config(work, tmp_path, extra):
	path = tmp_path / 'cfg-extra.toml'
	path.write_text(journal(work).config_text + '\n' + extra)
	assert gitum(work, 'config', 'load', str(path)) == 0


def test_dependent_series_and_shared_export_dir(stack, tmp_path, capsys):
	"""A series branch grown on top of its dependency: own changes only, numbering continues in a shared dir."""
	up, work = stack
	work.run('checkout', '-q', '-b', 'series/italic', 'v1.0')
	work.commit('win32u: Fake italic 1.', {'i1': '1\n'})
	work.commit('win32u: Fake italic 2.', {'i2': '2\n'})
	work.run('checkout', '-q', '-b', 'series/gasp')
	work.commit('win32u: Use gasp.', {'gasp': 'g\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'italic', '--base', 'upstream:winehq') == 0
	# base given as upstream: the dependency tip becomes the base anyway
	assert gitum(work, 'series', 'add', 'gasp', '--base', 'upstream:winehq', '--depends', 'italic') == 0
	gitum(work, 'series', 'set', 'gasp', 'export_dir=italic')
	gasp = journal(work).read_json('series/gasp/meta.json')
	assert [c['subject'] for c in gasp['changes']] == ['win32u: Use gasp.']
	assert gasp['base_sha'] == work.rev('series/italic')
	out = tmp_path / 'patches'
	assert gitum(work, 'series', 'export', '--dir', str(out)) == 0
	assert sorted(p.name for p in (out / 'italic').iterdir() if p.name.endswith('.patch')) == [
		'0001-win32u-Fake-italic-1.patch', '0002-win32u-Fake-italic-2.patch', '0003-win32u-Use-gasp.patch']
	# the dependency changes: its own old patch goes, the gasp patch stays
	work.run('checkout', '-q', 'series/italic')
	work.run('commit', '-q', '--amend', '-m', 'win32u: Fake italic 2, reworded.')
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'save', 'italic') == 0
	assert gitum(work, 'series', 'export', 'italic', '--dir', str(out)) == 0
	names = sorted(p.name for p in (out / 'italic').iterdir() if p.name.endswith('.patch'))
	assert names == ['0001-win32u-Fake-italic-1.patch', '0002-win32u-Fake-italic-2-reworded.patch',
			 '0003-win32u-Use-gasp.patch']
	code, data = gitum_json(work, capsys, 'queue')
	code, st = gitum_json(work, capsys, 'status', '--series')
	assert {r['name']: r['patches'] for r in st['series_tree']} == {'italic': 2, 'gasp': 1}


def test_series_merged_when_all_changes_are_upstream(with_series, capsys):
	up, work = with_series
	gitum(work, 'series', 'set', 'ntdll-base', 'state=sent')
	up.commit('ntdll: Add a helper.', {'ntdll': 'helper\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	code, data = gitum_json(work, capsys, 'check-upstream', 'series:ntdll-base', '--apply')
	assert code == 0 and data['series_merged'] == ['ntdll-base']
	meta = journal(work).read_json('series/ntdll-base/meta.json')
	assert meta['state'] == 'merged' and meta['release'] == 'v1.1' and meta['upstream_commit']
	assert meta['history'][-1]['source'] == 'check-upstream'


def test_change_confirm_suspect(stack, capsys):
	up, work = stack
	accepted = up.commit('etersoft: change base line', {'base': 'line1\nUPSTREAM WAY\nline3\nline4\nline5\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	code, data = gitum_json(work, capsys, 'check-upstream', 'layer:etersoft', '--apply')
	suspect = {e['subject']: e for e in data['results']}['etersoft: change base line']
	assert suspect['status'] == 'suspect' and suspect['same_subject'] and suspect['same_author']
	cid = suspect['id']
	assert changes_mod.Tracker(journal(work)).get(cid)['meta']['candidate'] == accepted
	code, rec = gitum_json(work, capsys, 'change', 'confirm', cid)
	assert code == 0 and rec['fate'] == 'merged-modified'
	assert rec['meta']['upstream_commit'] == accepted and rec['meta']['release'] == 'v1.1'
	assert 'candidate' not in rec['meta']
	# the next sync drops it
	assert gitum(work, 'sync', '--next-release') == 0
	assert work.subjects('rebased-basealt..rebased-etersoft') == ['etersoft: feature']


def test_init_with_series_only(tmp_path, capsys):
	up = __import__('core_helpers').make_upstream(tmp_path / 'up')
	w = Repo(tmp_path / 'w', init=False)
	up.git.at(tmp_path).run('clone', '-q', '-o', 'winehq', str(up.path), str(w.path))
	w.git = w.git.at(w.path)
	assert main(['--repo', str(w.path), 'init', '--upstream', 'winehq=winehq/master']) == 0
	w.run('checkout', '-q', '-b', 'series/x', 'v1.0')
	w.commit('x: Change.', {'x': '1\n'})
	assert main(['--repo', str(w.path), 'series', 'add', 'x', '--base', 'upstream:winehq']) == 0
	capsys.readouterr()
	assert main(['--repo', str(w.path), '--json', 'status', '--series']) == 0
	rows = json.loads(capsys.readouterr().out)['series_tree']
	assert rows[0]['name'] == 'x' and rows[0]['base_behind'] == 0


def test_explicit_save_recounts_and_shared_definition(stack, tmp_path, capsys):
	up, work = stack
	work.run('checkout', '-q', '-b', 'series/italic', 'v1.0')
	work.commit('win32u: Fake italic.', {'i1': '1\n'})
	work.run('checkout', '-q', '-b', 'series/gasp')
	work.commit('win32u: Use gasp.', {'gasp': 'g\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'italic', '--base', 'upstream:winehq') == 0
	assert gitum(work, 'series', 'add', 'gasp', '--base', 'upstream:winehq', '--depends', 'italic') == 0
	# a journal written by older rules: gasp claims the commit of italic too
	from gitum.git import RefTransaction
	from gitum.journal import JOURNAL_REF, JournalWriter
	g = GitumRepo(work.path)
	j = g.journal()
	meta = j.read_json('series/gasp/meta.json')
	meta['changes'] = j.read_json('series/italic/meta.json')['changes'] + meta['changes']
	w = JournalWriter(g.git, j.sha)
	w.put_json('series/gasp/meta.json', meta)
	tr = RefTransaction(g.git)
	tr.update(JOURNAL_REF, w.write('old rules'), j.sha)
	tr.commit()
	assert gitum(work, 'series', 'save') == 0		# nothing moved: not recounted
	assert len(journal(work).read_json('series/gasp/meta.json')['changes']) == 2
	assert gitum(work, 'series', 'save', 'gasp') == 0	# named: recounted
	assert len(journal(work).read_json('series/gasp/meta.json')['changes']) == 1
	# one definition for a shared directory: fixes of both, depends only outside
	work.run('checkout', '-q', '-b', 'series/other', 'v1.0')
	work.commit('other: X.', {'o': 'o\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'series', 'add', 'other', '--base', 'upstream:winehq') == 0
	gitum(work, 'series', 'set', 'italic', 'fixes=[1] Italic', 'depends=other')
	gitum(work, 'series', 'set', 'gasp', 'fixes=[2] Gasp', 'export_dir=italic')
	out = tmp_path / 'p'
	assert gitum(work, 'series', 'export', '--dir', str(out)) == 0
	assert (out / 'italic' / 'definition').read_text() == 'Fixes: [1] Italic\nFixes: [2] Gasp\nDepends: other\n'
	assert gitum(work, 'series', 'export', '--dir', str(out), '--check') == 0


def test_import_json_does_not_roll_back(with_series, tmp_path, capsys):
	up, work = with_series
	assert gitum(work, 'series', 'set', 'ntdll-base', 'state=merged') == 0
	(tmp_path / 'q.json').write_text(json.dumps([{'name': 'ntdll-base', 'state': 'sent', 'note': 'from the table'}]))
	code, data = gitum_json(work, capsys, 'series', 'import-json', str(tmp_path / 'q.json'))
	assert code == 0 and data['skipped'][0]['kept'] == 'merged'
	meta = journal(work).read_json('series/ntdll-base/meta.json')
	assert meta['state'] == 'merged' and meta['note'] == 'from the table'
	code, data = gitum_json(work, capsys, 'series', 'import-json', str(tmp_path / 'q.json'), '--force')
	assert journal(work).read_json('series/ntdll-base/meta.json')['state'] == 'sent'


def test_change_confirm_merges_the_series(with_series, capsys):
	up, work = with_series
	up.commit('ntdll: helper, upstream style', {'ntdll': 'helper\n', 'extra': 'x\n'})
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	cid = journal(work).read_json('series/ntdll-base/meta.json')['changes'][0]['gitum_id']
	code, rec = gitum_json(work, capsys, 'change', 'confirm', cid, 'v1.1~1')
	assert code == 0 and rec['series_merged'] == ['ntdll-base']
	meta = journal(work).read_json('series/ntdll-base/meta.json')
	assert meta['state'] == 'merged' and meta['merged_at'] and meta['release'] == 'v1.1'
