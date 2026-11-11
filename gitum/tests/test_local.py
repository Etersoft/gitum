import pytest

from gitupstream import (
	BranchExists,
	GitUmException,
	NoMergeBranch,
	NoStateFile,
	NotGitRepo,
	NotUptodate,
	RepoIsDirty,
	GitUpstream,
)
from gitupstream.gitupstream import (
	CONFIG_BRANCH,
	CURRENT_MAINLINE,
	CURRENT_REBASED,
	LAST_PATCH_FILE,
	MERGE_BRANCH,
	STATE_FILE,
	UPSTREAM_COMMIT_FILE,
)


def patch_files(work, rev='patches'):
	return sorted(f for f in work.ls_tree(rev) if f.endswith('.patch'))


def assert_in_sync(work):
	assert work.diff('dev', 'rebased') == ''
	assert work.show('patches', UPSTREAM_COMMIT_FILE) == work.head('master')


# create

def test_create_custom_names(work):
	assert {h.name for h in work.repo.heads} == {'master', 'merge', 'rebased', 'dev', 'patches', CONFIG_BRANCH}
	assert work.repo.active_branch.name == 'rebased'
	assert work.show(CONFIG_BRANCH, '.gitum-config').split('\n') == [
		'current = dev', 'upstream = master', 'rebased = rebased', 'patches = patches']
	# config and patches are orphan branches with a single file
	assert work.ls_tree(CONFIG_BRANCH) == ['.gitum-config']
	assert work.ls_tree('patches') == [UPSTREAM_COMMIT_FILE]
	assert work.repo.commit('patches').parents == ()
	assert work.repo.commit('patches').message.startswith('gitum-patches: begin')
	assert work.show('patches', UPSTREAM_COMMIT_FILE) == work.head('master')
	git_dir = work.path / '.git'
	assert (git_dir / MERGE_BRANCH).read_text() == 'merge'
	assert (git_dir / CURRENT_REBASED).read_text() == work.head('rebased')
	assert (git_dir / CURRENT_MAINLINE).read_text() == work.head('dev')


def test_create_default_names_has_no_config_branch(make_work):
	w = make_work()
	w.commit('a', 'initial')
	w.gitum.create('origin/master', 'upstream', 'rebased', 'mainline', 'patches')
	# the current branch is renamed to upstream
	assert {h.name for h in w.repo.heads} == {'upstream', 'rebased', 'mainline', 'patches'}
	assert w.repo.active_branch.name == 'rebased'


@pytest.mark.parametrize('branch', ['dev', 'rebased', 'patches'])
def test_create_refuses_existing_branch(make_work, branch):
	w = make_work()
	w.commit('a', 'initial')
	w.repo.create_head(branch)
	with pytest.raises(BranchExists):
		w.gitum.create('merge', 'master', 'rebased', 'dev', 'patches')


def test_config_parsing_ignores_comments_and_bad_lines(work):
	work.git.checkout(CONFIG_BRANCH)
	work.write('# comment\nupstream = master  # trailing\nbroken line\n\ncurrent = dev\n'
		   'rebased = rebased\npatches = patches\n', name='.gitum-config')
	work.git.commit('-am', 'edit config')
	work.git.checkout('rebased')
	work.gitum._load_config()
	assert (work.gitum._upstream, work.gitum._mainline, work.gitum._rebased, work.gitum._patches) == \
		('master', 'dev', 'rebased', 'patches')


# update / restore

def test_restore(work):
	work.commit('b', 'local: b', append=True)
	work.gitum.restore(rebased_only=True)
	assert work.read() == 'a'
	assert work.diff('dev', 'rebased') == ''

	work.commit('c', 'local: c', append=True)
	work.gitum.update()
	work.gitum.restore(rebased_only=True)
	assert work.read() == 'ac'
	assert work.diff('dev', 'rebased') == ''

	work.commit('d', 'local: d', append=True)
	work.commit('e', 'local: e', append=True)
	work.gitum.update()

	work.gitum.restore(commit='patches^')
	assert work.read() == 'acd'
	assert work.diff('dev', 'rebased') == ''

	work.gitum.restore(commit='patches^^^')
	assert work.read() == 'a'
	assert work.diff('dev', 'rebased') == ''


def test_update_records_each_commit(work):
	work.commit('b', 'local: b', append=True)
	work.commit('c', 'local: c', append=True)
	work.gitum.update()
	assert work.log('dev') == ['local: c', 'local: b', 'initial']
	assert work.log('patches') == ['local: c', 'local: b', 'gitum-patches: begin']
	assert patch_files(work) == ['0001-local-b.patch', '0002-local-c.patch']
	assert 'local: c' in work.show('patches', LAST_PATCH_FILE)
	assert_in_sync(work)


def test_update_nothing(work, capsys):
	work.gitum._with_log = True
	work.gitum.update()
	assert 'Nothing to update.' in capsys.readouterr().out
	assert work.log('patches') == ['gitum-patches: begin']


def test_update_dirty_tree(work):
	work.write('dirty')
	with pytest.raises(RepoIsDirty):
		work.gitum.update()


def test_update_unexpected_mainline_head(work):
	work.git.checkout('dev')
	work.commit('x', 'commit to dev directly')
	work.git.checkout('rebased')
	with pytest.raises(RepoIsDirty):
		work.gitum.update()


def test_update_rewritten_history_with_message(work):
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	work.write('B', append=True)
	work.git.commit('-a', '--amend', '-m', 'local: bB')
	work.gitum.update('fix b')
	assert work.log('dev') == ['fix b', 'local: b', 'initial']
	assert work.read() == 'abB'
	assert patch_files(work) == ['0001-local-bB.patch']
	assert work.show('patches', LAST_PATCH_FILE) != ''
	assert_in_sync(work)


def test_update_rewritten_history_interactive(work):
	"""Without --message the editor is started (GIT_EDITOR=true keeps the default text)."""
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	work.git.commit('--amend', '-m', 'local: b renamed')
	work.write('c', append=True)
	work.git.commit('-a', '--amend', '--no-edit')
	work.gitum.update()
	assert work.log('dev')[0] == 'place your comments for dev branch commit'
	assert_in_sync(work)


def test_update_dropped_commit(work):
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	work.git.reset('--hard', 'rebased^')
	work.gitum.update('drop b')
	assert work.read() == 'a'
	assert patch_files(work) == []
	assert_in_sync(work)


def test_unicode_and_author_preserved(work):
	work.commit('ж', 'Исправление: ё', append=True, author='Иван Тестов <ivan@example.org>')
	work.gitum.update()
	commit = work.repo.commit('dev')
	assert commit.message.strip() == 'Исправление: ё'
	assert (commit.author.name, commit.author.email) == ('Иван Тестов', 'ivan@example.org')
	assert work.repo.commit('patches').author.name == 'Иван Тестов'
	work.gitum.restore(rebased_only=True)
	assert work.read() == 'aж'


def test_binary_crlf_and_non_utf8_roundtrip(work):
	files = {
		'crlf.txt': b'line1\r\nline2\r\n',
		'blob.bin': bytes(range(256)) * 4,
		'koi8.txt': 'Привет\n'.encode('koi8-r'),
	}
	for name, data in files.items():
		work.write(data, name=name)
		work.git.add(name)
	work.git.commit('-m', 'add special files')
	work.write(b'line3\r\n', name='crlf.txt', append=True)
	work.git.commit('-am', 'change crlf')
	work.gitum.update()
	assert_in_sync(work)

	files['crlf.txt'] += b'line3\r\n'
	work.gitum.restore(rebased_only=True)
	for name, data in files.items():
		assert work.read_bytes(name) == data, name

	work.gitum.restore()
	for name, data in files.items():
		assert work.read_bytes(name) == data, name
	assert work.diff('dev', 'rebased') == ''


def test_restore_broken_patches_branch(work):
	from gitupstream import BrokenRepo
	work.git.checkout('--orphan', 'other')
	work.commit('x', 'unrelated root')
	work.git.checkout('rebased')
	with pytest.raises(BrokenRepo):
		work.gitum.restore(commit='other')


# merge

def make_upstream_commits(work, commits, author=None):
	work.git.checkout('merge')
	for content, message in commits:
		kwargs = {'author': author} if author else {}
		work.commit(content, message, **kwargs)
	work.git.checkout('rebased')


def test_merge_on_separate_files(work):
	work.commit('b', 'local: b', name='local.txt')
	work.gitum.update()
	work.git.checkout('merge')
	work.commit('a\nup\n', 'upstream: change testfile')
	work.git.checkout('rebased')
	work.gitum.merge()
	assert work.read() == 'a\nup\n'
	assert work.read('local.txt') == 'b'
	assert work.log('dev') == ['upstream: change testfile', 'local: b', 'initial']
	assert work.repo.commit('dev').author.name == 'tester'
	assert_in_sync(work)


def test_merge_up_to_date(work, capsys):
	work.gitum._with_log = True
	work.gitum.merge()
	assert 'nothing to merge' in capsys.readouterr().out


def test_merge_unknown_branch(work):
	with pytest.raises(NoMergeBranch):
		work.gitum.merge('no-such-branch')


def test_merge_track_saves_branch(work):
	work.repo.create_head('other')
	work.gitum.merge('other', track_with=True)
	assert (work.path / '.git' / MERGE_BRANCH).read_text() == 'other'


def test_merge_refuses_unsaved_commits(work):
	work.commit('b', 'local: b', append=True)
	with pytest.raises(NotUptodate):
		work.gitum.merge()


def test_merge_conflicts_continue(work):
	"""Port of the original LocalWorkTest.test_local_work scenario."""
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	work.commit('c', 'local: c', append=True)
	work.gitum.update()

	# the last upstream change is far from the local ones and merges cleanly
	make_upstream_commits(work, [('a\nd\n', 'remote: \\nd'), ('s\nd\n', 'remote: s'),
				     ('s\nd\n\n\n\n\n\n\nr\n', 'remote: r')])
	with pytest.raises(GitUmException):
		work.gitum.merge()

	resolutions = ['ab\nd\n', 'abc\nd\n', 'sb\nd\n', 'sbc\nd\n']
	for i, content in enumerate(resolutions):
		work.write(content)
		work.git.add('testfile')
		if i < len(resolutions) - 1:
			with pytest.raises(GitUmException):
				work.gitum.continue_merge('--continue')
		else:
			work.gitum.continue_merge('--continue')

	assert work.read() == 'sbc\nd\n\n\n\n\n\n\nr\n'
	assert work.head('master') == work.head('merge')
	assert work.log('dev')[:3] == ['remote: r', 'remote: s', 'remote: \\nd']
	assert patch_files(work) == ['0001-local-b.patch', '0002-local-c.patch']
	assert_in_sync(work)
	assert not (work.path / '.git' / STATE_FILE).exists()

	work.gitum.remove_all()
	assert {h.name for h in work.repo.heads} == {'master', 'merge'}


def test_merge_abort_restores_branches(work):
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	before = {b: work.head(b) for b in ('master', 'rebased', 'dev', 'patches')}
	make_upstream_commits(work, [('x', 'upstream: conflicting')])
	with pytest.raises(GitUmException):
		work.gitum.merge()
	assert (work.path / '.git' / STATE_FILE).exists()
	work.gitum.abort()
	assert {b: work.head(b) for b in before} == before
	assert work.repo.active_branch.name == 'rebased'
	assert not (work.path / '.git' / STATE_FILE).exists()


def test_merge_skip_drops_local_patch(work):
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	make_upstream_commits(work, [('x', 'upstream: conflicting')])
	with pytest.raises(GitUmException):
		work.gitum.merge()
	work.gitum.continue_merge('--skip')
	assert work.read() == 'x'
	assert patch_files(work) == []
	assert_in_sync(work)


def test_merge_drops_patch_accepted_upstream(work):
	work.commit('b', 'local: b', append=True)
	work.gitum.update()
	make_upstream_commits(work, [('ab', 'upstream: same change as local b')])
	work.gitum.merge()
	assert work.read() == 'ab'
	assert patch_files(work) == []
	assert_in_sync(work)


def test_continue_without_state(work):
	with pytest.raises(NoStateFile):
		work.gitum.continue_merge('--continue')


# misc

def test_status(work, capsys):
	work.gitum._with_log = True
	work.gitum.status()
	assert 'Nothing to update.' in capsys.readouterr().out
	work.commit('b', 'local: b', append=True)
	work.gitum.status()
	out = capsys.readouterr().out
	assert 'Have new commits' in out and 'local: b' in out
	work.gitum.update()
	work.git.commit('--amend', '-m', 'local: b reworded')
	work.gitum.status()
	assert 'Existing patches were modified.' in capsys.readouterr().out
	work.write('dirty')
	work.gitum.status()
	assert 'error: You have local changes' in capsys.readouterr().out


def test_state_files_and_remove(work):
	git_dir = work.path / '.git'
	assert (git_dir / CURRENT_REBASED).exists()
	work.gitum.remove_config_files()
	assert not any((git_dir / f).exists() for f in (CURRENT_REBASED, CURRENT_MAINLINE, MERGE_BRANCH))
	# nothing in the work tree
	assert work.git.status('--porcelain') == ''


def test_works_from_subdirectory(work):
	work.commit('b', 'local: b', name='sub/dir/file')
	sub = GitUpstream(str(work.path / 'sub' / 'dir'))
	sub.update()
	assert work.log('dev')[0] == 'local: b'


def test_not_a_git_repo(tmp_path):
	with pytest.raises(NotGitRepo):
		GitUpstream(str(tmp_path))
