"""The git wrapper: paths that git would quote, cat-file answers."""

from core_helpers import Repo

NAMES = ['тест.patch', 'with space.txt', 'quote"d.txt']


def test_paths_are_not_quoted(tmp_path):
	r = Repo(tmp_path / 'r')
	r.commit('files', {'d/' + n: n + '\n' for n in NAMES})
	assert sorted(r.git.ls_files('HEAD', 'd')) == sorted('d/' + n for n in NAMES)
	assert sorted(r.git.ls_tree('HEAD', 'd')) == sorted(NAMES)
	blobs = r.git.read_blobs('HEAD:d/' + n for n in NAMES)
	assert all(blobs['HEAD:d/' + n] == (n + '\n').encode() for n in NAMES)


def test_unmerged_files_with_unicode_names(tmp_path):
	r = Repo(tmp_path / 'r')
	r.commit('base', {'тест': 'a\n'})
	r.run('checkout', '-q', '-b', 'other')
	r.commit('other', {'тест': 'b\n'})
	r.run('checkout', '-q', 'master')
	r.commit('ours', {'тест': 'c\n'})
	assert r.git.run('merge', 'other', check=False).returncode != 0
	assert r.git.unmerged_files() == ['тест']


def test_read_blobs_missing_with_spaces(tmp_path):
	r = Repo(tmp_path / 'r')
	r.commit('one', {'a': 'a\n'})
	assert r.git.read_blobs(['HEAD:a b c', 'HEAD:a']) == {'HEAD:a b c': None, 'HEAD:a': b'a\n'}
