import subprocess
import sys

import pytest

from gitupstream import __version__
from gitupstream.cli import default_clone_dir, main


@pytest.mark.parametrize('url, expected', [
	('/srv/git/project.git', 'project'),
	('/srv/git/project/', 'project'),
	('https://github.com/Etersoft/gitum.git', 'gitum'),
	('git.alt:packages/gitum', 'gitum'),
	('host:repo.git', 'repo'),
	('project.github', 'project.github'),
	('relative/dir', 'dir'),
])
def test_default_clone_dir(url, expected):
	assert default_clone_dir(url) == expected


def test_version(capsys):
	with pytest.raises(SystemExit) as e:
		main(['--version'])
	assert e.value.code == 0
	assert __version__ in capsys.readouterr().out


def test_command_required(capsys):
	with pytest.raises(SystemExit) as e:
		main([])
	assert e.value.code == 2


def test_not_a_git_repo(tmp_path, capsys):
	assert main(['--repo', str(tmp_path), 'status']) == 1


def test_not_a_gitum_repo(make_work, capsys):
	w = make_work()
	w.commit('a', 'a')
	assert main(['--repo', str(w.path), 'status']) == 1
	assert 'Not a gitum repository' in capsys.readouterr().out


def test_workflow(make_work, capsys, monkeypatch):
	w = make_work()
	w.commit('a', 'a')
	w.repo.create_head('vendor')
	repo = ['--repo', str(w.path)]
	assert main(repo + ['create', '--remote', 'vendor']) == 0
	assert 'Successfully created work branches.' in capsys.readouterr().out

	w.commit('b', 'local: b', append=True)
	assert main(repo + ['status']) == 0
	assert 'local: b' in capsys.readouterr().out
	assert main(repo + ['update']) == 0
	assert w.log('mainline')[0] == 'local: b'

	w.git.checkout('vendor')
	w.commit('x', 'upstream: x', name='other')
	w.git.checkout('rebased')
	assert main(repo + ['merge']) == 0
	assert w.log('mainline')[0] == 'upstream: x'

	# update with uncommitted changes fails with exit code 1
	w.write('dirty')
	assert main(repo + ['update']) == 1
	w.git.checkout('--', 'testfile')

	# from a subdirectory, without --repo
	(w.path / 'sub').mkdir()
	monkeypatch.chdir(w.path / 'sub')
	assert main(['status']) == 0

	assert main(repo + ['remove']) == 0
	assert {h.name for h in w.repo.heads} == {'upstream', 'vendor'}


def test_merge_conflict_exit_code(make_work, capsys):
	w = make_work()
	w.commit('a', 'a')
	w.repo.create_head('vendor')
	repo = ['--repo', str(w.path)]
	main(repo + ['create', '--remote', 'vendor'])
	w.commit('b', 'local: b', append=True)
	main(repo + ['update'])
	w.git.checkout('vendor')
	w.commit('x', 'upstream: x')
	w.git.checkout('rebased')
	capsys.readouterr()
	assert main(repo + ['merge']) == 1
	out = capsys.readouterr().out
	# git hints are rewritten to gitum commands
	assert 'gitum merge --continue' in out or 'gitum merge --abort' in out or 'CONFLICT' in out
	assert main(repo + ['merge', '--abort']) == 0
	assert w.read() == 'ab'


def test_module_entry_point():
	res = subprocess.run([sys.executable, '-m', 'gitupstream', '--help'],
			     capture_output=True, text=True, check=True)
	assert 'Git Upstream Manager' in res.stdout
