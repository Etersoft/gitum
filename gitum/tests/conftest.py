from pathlib import Path

import pytest

from gitupstream import GitUpstream

GIT_ENV_VARS = (
	'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY',
	'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_AUTHOR_DATE',
	'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL', 'GIT_COMMITTER_DATE',
	'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_PARAMETERS',
)


@pytest.fixture(autouse=True)
def git_env(tmp_path_factory, monkeypatch):
	"""Run git with an empty config so the user's settings can't affect tests."""
	home = tmp_path_factory.mktemp('home')
	(home / '.gitconfig').write_text(
		'[init]\n\tdefaultBranch = master\n'
		'[user]\n\tname = tester\n\temail = tester@localhost\n'
		'[advice]\n\tdetachedHead = false\n'
	)
	monkeypatch.setenv('HOME', str(home))
	monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
	monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
	monkeypatch.setenv('GIT_EDITOR', 'true')
	monkeypatch.setenv('LC_ALL', 'C')
	for var in GIT_ENV_VARS:
		monkeypatch.delenv(var, raising=False)
	return home


class Work:
	"""A git repository driven by gitum, with helpers to edit and commit files."""

	def __init__(self, path, user='tester', with_log=False):
		self.path = Path(path)
		self.gitum = GitUpstream(repo_path=str(self.path), with_log=with_log, new_repo=True)
		self.repo = self.gitum.repo()
		self.git = self.repo.git
		self.git.config('user.name', user)
		self.git.config('user.email', '%s@localhost' % user)

	def write(self, content, name='testfile', append=False):
		path = self.path / name
		path.parent.mkdir(parents=True, exist_ok=True)
		data = content.encode() if isinstance(content, str) else content
		if append:
			data = path.read_bytes() + data
		path.write_bytes(data)

	def commit(self, content, message, name='testfile', append=False, **kwargs):
		self.write(content, name, append)
		self.git.add(name)
		self.git.commit('-m', message, **kwargs)

	def read(self, name='testfile'):
		return (self.path / name).read_text()

	def read_bytes(self, name='testfile'):
		return (self.path / name).read_bytes()

	def head(self, branch):
		return self.repo.branches[branch].commit.hexsha

	def diff(self, a, b):
		return self.git.diff(a, b)

	def show(self, rev, path):
		return self.git.show('%s:%s' % (rev, path))

	def ls_tree(self, rev):
		return self.git.ls_tree('--name-only', rev).split('\n')

	def log(self, rev, fmt='%s'):
		return self.git.log('--format=' + fmt, rev).split('\n')


@pytest.fixture
def make_work(tmp_path):
	def factory(name='repo', **kwargs):
		return Work(tmp_path / name, **kwargs)
	return factory


@pytest.fixture
def work(make_work):
	"""A gitum repo with custom branch names: master/rebased/dev/patches, tracking 'merge'."""
	w = make_work()
	w.commit('a', 'initial')
	w.repo.create_head('merge')
	w.gitum.create('merge', 'master', 'rebased', 'dev', 'patches')
	w.git.checkout('rebased')
	return w
