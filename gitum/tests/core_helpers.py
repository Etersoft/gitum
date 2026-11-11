"""Helpers for gitum 2 tests: plain git repositories driven through gitum.git.Git."""

from pathlib import Path

from gitum.git import Git


class Repo:
	def __init__(self, path, init=True, bare=False):
		self.path = Path(path)
		if init:
			self.path.mkdir(parents=True, exist_ok=True)
			Git(self.path).run('init', '-q', *(['--bare'] if bare else []))
		self.git = Git(self.path)

	def run(self, *args, **kwargs):
		return self.git.out(*args, **kwargs)

	def write(self, name, content):
		path = self.path / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content.encode() if isinstance(content, str) else content)

	def read(self, name):
		return (self.path / name).read_text()

	def commit(self, message, files=None, author=None, **more):
		"""Write files ({name: content}, content None deletes) and commit."""
		for name, content in (files or {}).items():
			if content is None:
				self.run('rm', '-q', name)
			else:
				self.write(name, content)
				self.run('add', name)
		args = ['commit', '-q', '--allow-empty', '-m', message]
		if author:
			args.append('--author=' + author)
		self.run(*args)
		return self.rev('HEAD')

	def rev(self, rev):
		return self.git.rev(rev)

	def log(self, rev, fmt='%s'):
		return self.git.lines('log', '--format=' + fmt, rev)

	def subjects(self, range_):
		return self.git.lines('log', '--reverse', '--format=%s', range_)

	def show(self, rev, path):
		data = self.git.show_file(rev, path)
		return data.decode() if data is not None else None


def make_upstream(path, versions=('1.0',)):
	"""An upstream repo with file 'base' and a release tag per version."""
	up = Repo(path)
	up.commit('initial', {'base': 'line1\nline2\nline3\nline4\nline5\n', 'README': 'upstream\n'})
	for v in versions:
		up.commit('Release %s' % v, {'VERSION': v + '\n'})
		up.run('tag', 'v' + v)
	return up


def make_stack(tmp_path, versions=('1.0',)):
	"""Upstream plus a work clone with layers rebased -> basealt -> etersoft based on v1.0."""
	up = make_upstream(tmp_path / 'upstream', versions)
	work = Repo(tmp_path / 'work', init=False)
	Git(tmp_path).run('clone', '-q', '-o', 'winehq', str(up.path), str(work.path))
	work.git = Git(work.path)
	work.run('checkout', '-q', '-b', 'rebased', 'v1.0')
	work.commit('staging: patch one', {'staging1': 'one\n'})
	work.commit('staging: patch two', {'staging2': 'two\n'})
	work.run('checkout', '-q', '-b', 'rebased-basealt')
	work.run('tag', 'rebased-basealt-START')
	work.commit('basealt: tweak', {'basealt': 'alt\n'})
	work.run('checkout', '-q', '-b', 'rebased-etersoft')
	work.run('tag', 'rebased-etersoft-START')
	work.commit('etersoft: feature', {'eter': 'eter\n'})
	work.commit('etersoft: change base line', {'base': 'line1\nETER\nline3\nline4\nline5\n'})
	work.run('checkout', '-q', '--detach')
	return up, work


INIT_ARGS = ['init', '--upstream', 'winehq=winehq/master', '--release-tags', 'v*',
	     '--layer', 'rebased=rebased:winehq',
	     '--layer', 'basealt=rebased-basealt:rebased',
	     '--layer', 'etersoft=rebased-etersoft:basealt']
