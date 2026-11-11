import json

import pytest

from gitum import changes as changes_mod
from gitum import packaging
from gitum.cli import main
from gitum.errors import EXIT_NEEDS_HUMAN
from gitum.repo import Repo as GitumRepo

from core_helpers import Repo, make_upstream


def gitum(work, *args):
	return main(['--repo', str(work.path), *args])


def gitum_json(work, capsys, *args):
	capsys.readouterr()
	code = main(['--repo', str(work.path), '--json', *args])
	out = capsys.readouterr().out
	return code, json.loads(out) if out.strip() else None


def journal(work):
	return GitumRepo(work.path).journal()


def make_staging(tmp_path, up):
	"""A wine-staging like repo: patches/<series>/{definition,0001-*.patch}, tags v1.0 and v1.1."""
	# produce real patches against upstream with a scratch clone
	scratch = Repo(tmp_path / 'scratch', init=False)
	up.git.at(tmp_path).run('clone', '-q', str(up.path), str(scratch.path))
	scratch.git = scratch.git.at(scratch.path)

	def patch(base, files, subject):
		scratch.run('checkout', '-q', '--detach', base)
		scratch.commit(subject, files)
		return scratch.git.run('format-patch', '--stdout', '-1', 'HEAD').stdout

	st = Repo(tmp_path / 'wine-staging')
	st.write('staging/upstream-commit', up.rev('v1.0') + '\n')
	st.write('patches/aaa-Base/definition', 'Fixes: [1] base\n')
	st.write('patches/aaa-Base/0001-aaa.patch', patch('v1.0', {'aaa': 'a\n'}, 'aaa: Base.'))
	st.write('patches/bbb-Uses/definition', 'Depends: aaa-Base\n')
	st.write('patches/bbb-Uses/0001-bbb.patch', patch('v1.0', {'bbb': 'b\n'}, 'bbb: Uses base.'))
	st.write('patches/ccc-Broken/definition', '# does not build\nDisabled: true\n')
	st.write('patches/ccc-Broken/0001-ccc.patch', patch('v1.0', {'ccc': 'c\n'}, 'ccc: Broken.'))
	st.write('patches/ddd-Old/0001-ddd.patch', patch('v1.0', {'ddd': 'd\n'}, 'ddd: Accepted upstream later.'))
	st.run('add', '-A')
	st.commit('Release v1.0')
	st.run('tag', 'v1.0')
	# for 1.1 ddd was accepted upstream and removed from staging
	st.run('rm', '-q', '-r', 'patches/ddd-Old')
	st.write('staging/upstream-commit', up.rev('v1.1') + '\n')
	st.run('add', '-A')
	st.commit('Release v1.1')
	st.run('tag', 'v1.1')
	return st


@pytest.fixture
def staged(tmp_path, capsys):
	up = make_upstream(tmp_path / 'upstream', versions=('1.0',))
	up.commit('upstream: accept ddd', {'ddd': 'd\n'})
	up.commit('Release 1.1', {'VERSION': '1.1\n'})
	up.run('tag', 'v1.1')
	st = make_staging(tmp_path, up)
	work = Repo(tmp_path / 'work', init=False)
	up.git.at(tmp_path).run('clone', '-q', '-o', 'winehq', str(up.path), str(work.path))
	work.git = work.git.at(work.path)
	# the staging layer as patchinstall would build it for 1.0
	work.run('checkout', '-q', '-b', 'rebased', 'v1.0')
	for n in ('aaa-Base', 'bbb-Uses', 'ddd-Old'):
		for f in sorted(st.git.ls_tree('v1.0', 'patches/' + n)):
			if f.endswith('.patch'):
				work.run('am', '-q', input=st.git.show_file('v1.0', 'patches/%s/%s' % (n, f)))
	work.run('checkout', '-q', '-b', 'rebased-etersoft')
	work.commit('etersoft: own', {'eter': 'e\n'})
	work.run('checkout', '-q', '--detach')
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
[layer.etersoft]
base = "layer:staging"
branch = "rebased-etersoft"
''' % st.path)
	assert gitum(work, 'init', '--config', str(cfg)) == 0
	capsys.readouterr()
	return up, st, work


def test_tag_version():
	assert packaging.tag_version('wine-*', 'wine-11.20') == '11.20'
	assert packaging.tag_version('v*', 'v1.1') == '1.1'
	assert packaging.tag_version('wine-*', 'other') == ''


def test_staging_series_order(staged, tmp_path):
	up, st, work = staged
	order, disabled = packaging.staging_series(st.path)
	assert order == ['aaa-Base', 'bbb-Uses']
	assert list(disabled) == ['ccc-Broken']
	with pytest.raises(Exception, match='depends on'):
		packaging.staging_series(st.path, exclude=['aaa-Base'])


def test_staging_generator_sync(staged, capsys):
	up, st, work = staged
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0, data
	layer = data['report']['layers']['staging']
	assert layer['generator']['staging_ref'] == 'v1.1' and layer['generator']['series'] == 2
	assert layer['generator']['disabled'] == ['ccc-Broken']
	res = {r['subject']: r['result'] for r in layer['results']}
	assert res['ddd: Accepted upstream later.'] == 'dropped-by-generator'
	assert res['aaa: Base.'] == 'generated'
	assert work.subjects('v1.1..rebased') == ['aaa: Base.', 'bbb: Uses base.']
	assert work.subjects('rebased..rebased-etersoft') == ['etersoft: own']
	# the change left the layer with an event
	recs = {c['subject']: c for c in changes_mod.load_all(journal(work)).values()}
	assert recs['ddd: Accepted upstream later.']['gone']


def test_staging_generator_waits_for_release(staged, capsys):
	up, st, work = staged
	st.run('tag', '-d', 'v1.1')
	before = work.rev('rebased')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == EXIT_NEEDS_HUMAN and 'no v1.1' in data['error']
	assert work.rev('rebased') == before
	assert GitumRepo(work.path).load_op() is None


def test_command_generator(tmp_path, capsys):
	up = make_upstream(tmp_path / 'upstream', versions=('1.0', '1.1'))
	work = Repo(tmp_path / 'work', init=False)
	up.git.at(tmp_path).run('clone', '-q', '-o', 'winehq', str(up.path), str(work.path))
	work.git = work.git.at(work.path)
	work.run('checkout', '-q', '-b', 'gen', 'v1.0')
	work.commit('generated for v1.0', {'gen': '1.0\n'})
	work.run('checkout', '-q', '--detach')
	script = tmp_path / 'gen.sh'
	script.write_text('#!/bin/sh\necho "$GITUM_TARGET_TAG" > gen\ngit add gen\ngit commit -q -m "generated for $GITUM_TARGET_TAG"\n')
	script.chmod(0o755)
	cfg = tmp_path / 'gitum.toml'
	cfg.write_text('version = 2\n[upstream.u]\nref = "winehq/master"\nrelease-tags = "v*"\n'
		       '[layer.g]\nbase = "upstream:u"\nbranch = "gen"\ngenerator = "command"\ncommand = "%s"\n'
		       'post-command = "echo generated > autogen && git add autogen && git rm -q README"\n'
		       'post-message = "commit autoreconf results"\n' % script)
	assert gitum(work, 'init', '--config', str(cfg)) == 0
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0
	assert work.show('gen', 'gen') == 'v1.1\n'
	assert work.subjects('v1.1..gen') == ['generated for v1.1', 'commit autoreconf results']
	assert work.show('gen', 'README') is None and work.show('gen', 'autogen') == 'generated\n'


# patch sets

@pytest.fixture
def simple(tmp_path, capsys):
	up = make_upstream(tmp_path / 'upstream')
	work = Repo(tmp_path / 'work', init=False)
	up.git.at(tmp_path).run('clone', '-q', '-o', 'winehq', str(up.path), str(work.path))
	work.git = work.git.at(work.path)
	work.run('checkout', '-q', '-b', 'pkg', 'v1.0')
	work.commit('fix one', {'one': '1\n'})
	work.commit('fix two', {'two': '2\r\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'init', '--upstream', 'u=winehq/master', '--layer', 'pkg=pkg') == 0
	capsys.readouterr()
	return up, work


def test_export_quilt_and_spec(simple, tmp_path, capsys):
	up, work = simple
	code, data = gitum_json(work, capsys, 'export', 'layer:pkg', '--format', 'quilt', '--dir', str(tmp_path / 'q'))
	assert (tmp_path / 'q' / 'patches' / 'series').read_text() == '0001-fix-one.patch\n0002-fix-two.patch\n'
	spec = tmp_path / 'pkg.spec'
	spec.write_text('Name: pkg\nSource: %name.tar\n# BEGIN gitum patches\nPatch1: old.patch\n# END gitum patches\n'
			'%prep\n%setup\n%autopatch -p1\n')
	code, data = gitum_json(work, capsys, 'export', 'layer:pkg', '--format', 'spec', '--dir', str(tmp_path / 'g'),
				'--spec', str(spec), '--prefix', 'alt-', '--clean')
	assert data['spec_updated']
	assert spec.read_text() == ('Name: pkg\nSource: %name.tar\n# BEGIN gitum patches\nPatch1: alt-0001-fix-one.patch\n'
				    'Patch2: alt-0002-fix-two.patch\n# END gitum patches\n%prep\n%setup\n%autopatch -p1\n')
	assert sorted(p.name for p in (tmp_path / 'g').iterdir()) == ['alt-0001-fix-one.patch', 'alt-0002-fix-two.patch']
	code, data = gitum_json(work, capsys, 'export', 'layer:pkg', '--format', 'spec', '--dir', str(tmp_path / 'g'),
				'--spec', str(spec), '--prefix', 'alt-', '--clean')
	assert data['spec_updated'] is False
	bad = tmp_path / 'bad.spec'
	bad.write_text('Name: x\n')
	assert gitum(work, 'export', 'layer:pkg', '--dir', str(tmp_path / 'b'), '--spec', str(bad)) == 1


def test_import_spec_and_quilt(simple, tmp_path, capsys):
	up, work = simple
	# patches of a package: one git patch, one plain diff
	patches = tmp_path / 'pkg'
	patches.mkdir()
	work.run('checkout', '-q', '--detach', 'v1.0')
	work.commit('pkg: add feature', {'feature': 'f\n'})
	(patches / 'feature.patch').write_bytes(work.git.run('format-patch', '--stdout', '-1', 'HEAD').stdout)
	work.run('checkout', '-q', '--detach', 'v1.0')
	(patches / 'plain.diff').write_text('--- a/README\n+++ b/README\n@@ -1 +1,2 @@\n upstream\n+packaged\n')
	(patches / 'pkg.spec').write_text('Name: pkg\nPatch2: plain.diff\nPatch1: feature.patch\n')
	work.run('branch', '-f', 'pkg', 'v1.0')
	assert gitum(work, 'save') == 0
	code, data = gitum_json(work, capsys, 'import', str(patches / 'pkg.spec'), '--into', 'pkg')
	assert code == 0 and data['imported'] == ['feature.patch', 'plain.diff']
	assert work.subjects('v1.0..pkg') == ['pkg: add feature', 'Apply plain.diff']
	assert work.show('pkg', 'README') == 'upstream\npackaged\n'
	assert [c.subject for c in journal(work).layer('pkg').changes] == ['pkg: add feature', 'Apply plain.diff']
	# quilt
	code, data = gitum_json(work, capsys, 'export', 'layer:pkg', '--format', 'quilt', '--dir', str(tmp_path / 'q'))
	work.run('branch', '-f', 'pkg', 'v1.0')
	assert gitum(work, 'save') == 0
	code, data = gitum_json(work, capsys, 'import', str(tmp_path / 'q'), '--into', 'pkg')
	assert code == 0 and len(data['imported']) == 2


def test_html_report(simple, tmp_path, capsys):
	up, work = simple
	up.commit('upstream: one', {'one': '1\n'})
	up.commit('Release 1.1', {'VERSION': '1.1\n'})
	up.run('tag', 'v1.1')
	text = journal(work).config_text.replace('ref = "winehq/master"', 'ref = "winehq/master"\nrelease-tags = "v*"')
	(tmp_path / 'c.toml').write_text(text)
	assert gitum(work, 'config', 'load', str(tmp_path / 'c.toml')) == 0
	assert gitum(work, 'sync', '--next-release') == 0
	out = tmp_path / 'report.html'
	assert gitum(work, 'report', '--html', str(out)) == 0
	page = out.read_text()
	assert '<h1>Sync to u v1.1</h1>' in page and 'dropped-upstream' in page and 'fix one' in page
	code, data = gitum_json(work, capsys, 'report')
	assert data['target'] == 'u v1.1'


# vendored upstream

def test_vendored_prefix_check_and_prepare(tmp_path, capsys):
	lib = Repo(tmp_path / 'lib')
	lib.commit('lib: initial', {'src/a.c': 'int a;\n'})
	app = Repo(tmp_path / 'app')
	app.commit('app: initial', {'main.c': 'int main;\n', 'third-party/lib/src/a.c': 'int a;\n'})
	app.run('remote', 'add', 'lib', str(lib.path))
	app.run('fetch', '-q', 'lib')
	app.commit('lib: Fix a.', {'third-party/lib/src/a.c': 'int a = 1;\n'})
	app.commit('app: own change', {'main.c': 'int main = 0;\n'})
	cfg = tmp_path / 'gitum.toml'
	cfg.write_text('version = 2\n[upstream.app]\nref = "master"\n[upstream.rocks]\nref = "lib/master"\n'
		       'prefix = "third-party/lib"\n[layer.main]\nbase = "upstream:app"\nbranch = "master"\n')
	first = app.git.lines('rev-list', '--max-parents=0', 'master')[0]
	app.run('branch', 'series/lib-fix', 'master~1')
	assert main(['--repo', str(app.path), 'init', '--config', str(cfg)]) == 0
	assert main(['--repo', str(app.path), 'series', 'add', 'lib-fix', '--base', first, '--upstream', 'rocks']) == 0
	# upstream lib accepts the fix
	lib.commit('lib: Fix a (upstream).', {'src/a.c': 'int a = 1;\n'})
	app.run('fetch', '-q', 'lib')
	capsys.readouterr()
	code = main(['--repo', str(app.path), '--json', 'check-upstream', 'series:lib-fix', '--ref', 'lib/master',
		     '--since', 'lib/master~1'])
	data = json.loads(capsys.readouterr().out)
	assert code == 0
	assert [e['status'] for e in data['results']] == ['merged']
	# prepare takes the prefix and the base from the series upstream
	capsys.readouterr()
	assert main(['--repo', str(app.path), '--json', 'prepare', 'lib-fix', '--onto', 'lib/master~1']) == 0
	assert app.show('upstream/lib-fix', 'src/a.c') == 'int a = 1;\n'
	assert app.git.is_ancestor('lib/master~1', 'upstream/lib-fix')
