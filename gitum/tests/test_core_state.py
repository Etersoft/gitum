import json

import pytest

from gitum import config as config_mod
from gitum.cli import main
from gitum.errors import Busy, ConfigError, EXIT_BUSY, EXIT_ERROR, EXIT_UPDATED
from gitum.git import Git, RefTransaction
from gitum.journal import JOURNAL_REF, history_ref
from gitum.repo import Repo as GitumRepo

from core_helpers import INIT_ARGS, Repo, make_stack


def run(work, *args, capsys=None):
	code = main(['--repo', str(work.path), *args])
	return code


def run_json(work, capsys, *args):
	capsys.readouterr()
	code = main(['--repo', str(work.path), '--json', *args])
	out = capsys.readouterr().out
	return code, json.loads(out) if out.strip() else None


@pytest.fixture
def stack(tmp_path):
	return make_stack(tmp_path)


@pytest.fixture
def inited(stack, capsys):
	up, work = stack
	assert run(work, *INIT_ARGS) == 0
	capsys.readouterr()
	return up, work


# config

def test_config_parse_and_order():
	cfg = config_mod.parse('''
version = 2
[upstream.w]
ref = "w/master"
release-tags = "wine-*"
[layer.c]
base = "layer:b"
[layer.b]
base = "layer:a"
branch = "bb"
[layer.a]
base = "upstream:w"
annihilate-reverts = true
[resolve]
chain = ["rerere", "rules"]
rules = "tools/rules"
''')
	assert [l.name for l in cfg.ordered_layers()] == ['a', 'b', 'c']
	assert cfg.layers['b'].branch == 'bb' and cfg.layers['c'].branch == 'c'
	assert cfg.root_upstream(cfg.layers['c']).name == 'w'
	assert cfg.layers['a'].annihilate_reverts
	assert cfg.resolve_chain == ['rerere', 'rules']


@pytest.mark.parametrize('text, error', [
	('version = 3', 'unsupported version'),
	('[upstream.w]\nremote = "x"', 'ref is required'),
	('[upstream.w]\nref = "m"\n[layer.a]\nbase = "upstream:nope"', 'base must be'),
	('[upstream.w]\nref = "m"\n[layer.a]\nbase = "layer:b"\n[layer.b]\nbase = "layer:a"', 'cycle'),
	('[upstream.w]\nref = "m"\n[resolve]\nchain = ["magic"]', 'unknown resolvers'),
	('[upstream.w]\nref = 1', 'must be str'),
	('not toml [', 'gitum.toml'),
])
def test_config_errors(text, error):
	with pytest.raises(ConfigError, match=error):
		config_mod.parse(text)


# git wrapper

def test_ref_transaction_is_atomic(tmp_path):
	r = Repo(tmp_path / 'r')
	a = r.commit('a')
	b = r.commit('b')
	git = Git(r.path)
	tr = RefTransaction(git)
	tr.update('refs/heads/x', a, None)
	tr.commit()
	tr = RefTransaction(git)
	tr.update('refs/heads/y', b, None)
	tr.update('refs/heads/x', b, b)		# wrong old value: nothing must change
	with pytest.raises(Exception):
		tr.commit()
	assert git.rev('refs/heads/x') == a
	assert git.rev('refs/heads/y') is None


def test_patch_ids_and_commit_info(tmp_path):
	r = Repo(tmp_path / 'r')
	r.commit('one', {'f': 'a\n'})
	c = r.commit('Тема', {'f': 'b\n'}, author='Иван <i@x>')
	git = Git(r.path)
	assert git.commit_info(c)['author'] == 'Иван'
	assert git.commit_info(c)['subject'] == 'Тема'
	assert set(git.patch_ids([c])) == {c}


# init

def test_init_from_branches_and_start_tags(inited):
	up, work = inited
	g = GitumRepo(work.path)
	journal = g.journal()
	cfg = journal.config
	assert [l.name for l in cfg.ordered_layers()] == ['rebased', 'basealt', 'etersoft']
	assert cfg.layers['basealt'].compat_start_tag == 'rebased-basealt-START'
	layers = journal.layers()
	assert layers['rebased'].base == up.rev('v1.0')
	assert layers['basealt'].base == work.rev('rebased-basealt-START') == work.rev('rebased')
	assert [c.subject for c in layers['etersoft'].changes] == ['etersoft: feature', 'etersoft: change base line']
	assert all(c.patch_id for c in layers['etersoft'].changes)
	assert journal.upstream('winehq') == {'sha': up.rev('v1.0'), 'ref': 'winehq/master', 'tag': 'v1.0'}
	assert cfg.upstreams['winehq'].remote == 'winehq'
	# readable series file
	assert work.show(JOURNAL_REF, 'layers/etersoft.series').splitlines()[0].endswith('etersoft: feature')
	# journal: root commit + state commit with the layer tips as extra parents
	assert len(journal.history()) == 2
	parents = work.git.lines('log', '-1', '--format=%P', JOURNAL_REF)[0].split()
	assert parents[1:] == [work.rev(b) for b in ('rebased', 'rebased-basealt', 'rebased-etersoft')]
	# linear history branch per layer with the tree of the tip
	for name, branch in (('rebased', 'rebased'), ('etersoft', 'rebased-etersoft')):
		assert work.git.tree(history_ref(name)) == work.git.tree(branch)


def test_init_from_config_file(stack, tmp_path, capsys):
	up, work = stack
	cfg = tmp_path / 'gitum.toml'
	cfg.write_text('version = 2\n[upstream.wine]\nref = "winehq/master"\n'
		       '[layer.all]\nbase = "upstream:wine"\nbranch = "rebased-etersoft"\n')
	assert run(work, 'init', '--config', str(cfg)) == 0
	layer = GitumRepo(work.path).journal().layer('all')
	assert layer.base == up.rev('v1.0') and len(layer.changes) == 5


def test_init_twice_and_bad_args(inited, capsys):
	up, work = inited
	assert run(work, *INIT_ARGS) == EXIT_ERROR
	assert 'already exists' in capsys.readouterr().err


def test_init_unknown_base(stack, capsys):
	up, work = stack
	code = run(work, 'init', '--upstream', 'w=winehq/master', '--layer', 'a=rebased:nope')
	assert code == EXIT_ERROR and 'unknown base' in capsys.readouterr().err


def test_init_missing_branch(stack, capsys):
	up, work = stack
	assert run(work, 'init', '--upstream', 'w=winehq/master', '--layer', 'a=nope') == EXIT_ERROR


# status and save

def test_status(inited, capsys):
	up, work = inited
	code, data = run_json(work, capsys, 'status')
	assert code == 0
	assert data['upstreams']['winehq']['tag'] == 'v1.0'
	assert data['layers']['etersoft']['changes'] == 2
	assert data['operation'] is None and data['lock'] is None
	up.commit('upstream: new', {'new': 'x\n'})
	work.run('fetch', '-q', 'winehq')
	code, data = run_json(work, capsys, 'status')
	assert data['upstreams']['winehq']['behind'] == 1
	assert data['layers']['rebased']['upstream_behind'] == 1
	assert run(work, 'status') == 0
	assert '1 new commits' in capsys.readouterr().out


def test_save_records_new_commits(inited, capsys):
	up, work = inited
	work.run('checkout', '-q', 'rebased-etersoft')
	work.commit('etersoft: more', {'more': 'm\n'})
	code, data = run_json(work, capsys, 'status')
	assert data['layers']['etersoft']['unsaved'] == 1
	code, data = run_json(work, capsys, '--detailed-exitcode', 'save', '-m', 'add more')
	assert code == EXIT_UPDATED
	assert data['layers']['etersoft']['added'] == ['etersoft: more']
	journal = GitumRepo(work.path).journal()
	assert journal.layer('etersoft').tip == work.rev('rebased-etersoft')
	assert work.git.tree(history_ref('etersoft')) == work.git.tree('rebased-etersoft')
	assert work.log(history_ref('etersoft'))[0] == 'etersoft: add more'
	# nothing more to save
	code, data = run_json(work, capsys, '--detailed-exitcode', 'save')
	assert code == 0 and data['changed'] is False


def test_save_after_rewrite_on_same_base(inited, capsys):
	up, work = inited
	work.run('checkout', '-q', 'rebased-etersoft')
	work.run('commit', '-q', '--amend', '-m', 'etersoft: reworded')
	code, data = run_json(work, capsys, 'save')
	assert code == 0
	assert data['layers']['etersoft']['added'] == [] and data['layers']['etersoft']['removed'] == []
	assert GitumRepo(work.path).journal().layer('etersoft').changes[-1].subject == 'etersoft: reworded'


def test_save_refuses_unknown_base(inited, capsys):
	up, work = inited
	work.run('checkout', '-q', 'rebased-etersoft')
	work.run('rebase', '-q', '--onto', 'v1.0', 'rebased-etersoft-START')
	assert run(work, 'save') == 2
	assert 'unknown base' in capsys.readouterr().err


# lock

def test_lock_busy(inited, capsys):
	up, work = inited
	g = GitumRepo(work.path)
	with g.lock():
		assert g.lock_owner()['pid']
		with pytest.raises(Busy):
			with GitumRepo(work.path).lock():
				pass
		assert run(work, 'save') == EXIT_BUSY
	assert g.lock_owner() is None


# config command and log

def test_config_show_load_and_log(inited, capsys, tmp_path):
	up, work = inited
	code, data = run_json(work, capsys, 'config', 'show')
	assert '[layer.etersoft]' in data['config']
	new = tmp_path / 'new.toml'
	new.write_text(data['config'] + '\n[publish]\nremote = "origin"\n')
	assert run(work, 'config', 'load', str(new)) == 0
	assert GitumRepo(work.path).config.publish_remote == 'origin'
	bad = tmp_path / 'bad.toml'
	bad.write_text('version = 7')
	assert run(work, 'config', 'load', str(bad)) == EXIT_ERROR
	code, data = run_json(work, capsys, 'log')
	assert [e['subject'] for e in data] == ['gitum: update configuration', 'gitum: init', 'gitum journal']


def test_not_a_gitum_repo(tmp_path, capsys):
	r = Repo(tmp_path / 'r')
	r.commit('a')
	assert main(['--repo', str(r.path), 'status']) == EXIT_ERROR
	assert 'gitum init' in capsys.readouterr().err
	assert main(['--repo', str(tmp_path / 'nothing'), 'log']) == EXIT_ERROR

