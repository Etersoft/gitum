"""The quick start of README.md works as written."""

import re
import shlex
from pathlib import Path

import pytest

from gitum.cli import main

from core_helpers import Repo, make_upstream

README = Path(__file__).resolve().parents[2] / 'README.md'


def quick_start_commands():
	text = README.read_text()
	part = text[text.index('## Quick start'):text.index('## Documentation')]
	blocks = re.findall(r'```sh\n(.*?)```', part, re.S)
	return [[shlex.split(line.split('#')[0]) for line in b.splitlines() if line.startswith('gitum ')] for b in blocks]


@pytest.mark.skipif(not README.exists(), reason='README.md is not in the source tarball of the package')
def test_readme_quick_start(tmp_path, capsys):
	sync_block, series_block = quick_start_commands()
	up = make_upstream(tmp_path / 'up', versions=('1.0', '1.1'))
	work = Repo(tmp_path / 'myproject', init=False)
	up.git.at(tmp_path).run('clone', '-q', '-o', 'upstream', str(up.path), str(work.path))
	work.git = work.git.at(work.path)
	work.run('checkout', '-q', '-b', 'my-branch', 'v1.0')
	work.commit('mine: Change.', {'mine': '1\n'})
	origin = Repo(tmp_path / 'origin.git', bare=True)
	work.run('remote', 'add', 'origin', str(origin.path))
	work.run('checkout', '-q', '--detach')
	for argv in sync_block:
		if argv[1] == 'publish':
			cfg = tmp_path / 'c.toml'
			from gitum.repo import Repo as GitumRepo
			cfg.write_text(GitumRepo(work.path).journal().config_text + '\n[publish]\nremote = "origin"\n')
			assert main(['--repo', str(work.path), 'config', 'load', str(cfg)]) == 0
		code = main(['--repo', str(work.path), *argv[1:]])
		# continue is "after resolving a conflict, if there was one": there was none here
		assert code == (1 if argv[1] == 'continue' else 0), argv
	assert work.git.is_ancestor(up.rev('v1.1'), 'my-branch')
	assert origin.rev('my-branch') == work.rev('my-branch')
	work.run('checkout', '-q', '-b', 'series/my-fix', 'upstream/master')
	work.commit('fix: Something.', {'fix': '1\n'})
	work.run('checkout', '-q', '--detach')
	fork = Repo(tmp_path / 'fork.git', bare=True)
	work.run('remote', 'add', 'fork', str(fork.path))
	for argv in series_block:
		assert main(['--repo', str(work.path), *argv[1:]]) == 0, argv
	assert fork.rev('my-fix')
