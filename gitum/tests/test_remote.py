import git
import pytest

from gitupstream import GitUmException, GitUpstream, NoGitumRemote
from gitupstream.gitupstream import CONFIG_BRANCH, REMOTE_REPO

from conftest import Work


@pytest.fixture
def origin(make_work):
	w = make_work('origin', user='tester1')
	w.commit('a', 'a')
	w.gitum.create('merge', 'master', 'rebased', 'dev', 'patches')
	w.git.checkout('rebased')
	return w


def clone(origin, tmp_path, name, user):
	w = Work(tmp_path / name, user=user)
	w.gitum.clone(str(origin.path))
	return w


def test_clone(origin, tmp_path):
	origin.commit('b', 'local: b', append=True)
	origin.gitum.update()
	w = clone(origin, tmp_path, 'clone', 'tester2')
	assert {h.name for h in w.repo.heads} == {'master', 'dev', 'patches', 'rebased', CONFIG_BRANCH}
	assert w.repo.active_branch.name == 'rebased'
	assert w.read() == 'ab'
	assert w.diff('dev', 'rebased') == ''
	assert (w.path / '.git' / REMOTE_REPO).read_text() == 'origin'
	for branch in ('master', 'dev', 'patches'):
		assert w.head(branch) == origin.head(branch)


def test_clone_relative_path(origin, tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	w = Work(tmp_path / 'clone')
	w.gitum.clone('origin')
	assert w.repo.remotes.origin.url == str(tmp_path / 'origin')


def test_pull_and_push_without_remote(origin):
	with pytest.raises(NoGitumRemote):
		origin.gitum.push()


def test_remote_work(origin, tmp_path):
	"""Port of the original RemoteWorkTest.test_remote_work scenario."""
	remote2 = clone(origin, tmp_path, 'remote2', 'tester2')
	local = clone(origin, tmp_path, 'local', 'tester3')

	origin.commit('ab', 'ab')
	origin.gitum.update()
	remote2.commit('af', 'af')
	remote2.gitum.update()
	local.commit('ac', 'ac')
	local.gitum.update()

	with pytest.raises(GitUmException):
		local.gitum.pull('origin')
	local.write('abc')
	local.git.add('testfile')
	local.gitum.continue_pull('--resolved')
	assert local.log('dev')[:2] == ['ac', 'ab']

	local.git.remote('add', 'origin2', str(remote2.path))
	with pytest.raises(GitUmException):
		local.gitum.pull('origin2')
	local.write('abf')
	local.git.add('testfile')
	with pytest.raises(GitUmException):
		local.gitum.continue_pull('--resolved')
	local.write('abcf')
	local.git.add('testfile')
	local.gitum.continue_pull('--resolved')
	assert local.read() == 'abcf'
	assert local.diff('dev', 'rebased') == ''

	bare_path = tmp_path / 'bare.git'
	git.Repo.init(bare_path, bare=True)
	local.git.remote('add', 'new', str(bare_path))
	local.gitum.push('new', track_with=True)
	bare = git.Repo(bare_path)
	for branch in ('master', 'dev', 'patches', CONFIG_BRANCH):
		assert bare.heads[branch].commit.hexsha == local.head(branch)
	assert 'rebased' not in [h.name for h in bare.heads]
	assert (local.path / '.git' / REMOTE_REPO).read_text() == 'new'

	# a fresh clone of the pushed repo has the same content
	fresh = Work(tmp_path / 'fresh')
	fresh.gitum.clone(str(bare_path))
	assert fresh.read() == 'abcf'

	for w in (origin, remote2, local):
		w.gitum.remove_all()


def test_pull_fast_forward(origin, tmp_path):
	other = clone(origin, tmp_path, 'other', 'tester2')
	origin.commit('b', 'local: b', append=True)
	origin.gitum.update()
	other.gitum.pull()
	assert other.read() == 'ab'
	assert other.head('dev') == origin.head('dev')
	assert other.diff('dev', 'rebased') == ''


def test_pull_abort(origin, tmp_path):
	other = clone(origin, tmp_path, 'other', 'tester2')
	origin.commit('ab', 'ab')
	origin.gitum.update()
	other.commit('ax', 'ax')
	other.gitum.update()
	before = {b: other.head(b) for b in ('master', 'dev', 'patches', 'rebased')}
	with pytest.raises(GitUmException):
		other.gitum.pull()
	other.gitum.abort(am=True)
	assert {b: other.head(b) for b in before} == before
	assert other.read() == 'ax'


def test_clone_default_branch_names(make_work, tmp_path):
	w = make_work('origin')
	w.commit('a', 'a')
	w.gitum.create('origin/master', 'upstream', 'rebased', 'mainline', 'patches')
	w.commit('b', 'b', append=True)
	w.gitum.update()
	c = GitUpstream(str(tmp_path / 'clone'), new_repo=True)
	c.clone(str(w.path))
	assert {h.name for h in c.repo().heads} == {'upstream', 'mainline', 'patches', 'rebased'}
	assert (tmp_path / 'clone' / 'testfile').read_text() == 'ab'
