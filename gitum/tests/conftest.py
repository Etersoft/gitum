import pytest

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

