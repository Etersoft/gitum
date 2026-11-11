"""Thin wrapper over the git command line."""

import os
import subprocess
import tempfile
from pathlib import Path

from .errors import GitError

# never wait for an editor or a password prompt
BASE_ENV = {
	'GIT_EDITOR': 'true',
	'GIT_TERMINAL_PROMPT': '0',
	'GIT_MERGE_AUTOEDIT': 'no',
}

ZERO_SHA = '0' * 40


class Git:
	"""Runs git in a work tree (or a git dir) with a non-interactive environment."""

	def __init__(self, cwd, env=None):
		self.cwd = Path(cwd)
		self.env = dict(env or {})

	def with_env(self, **env):
		return Git(self.cwd, {**self.env, **env})

	def at(self, cwd):
		return Git(cwd, self.env)

	def run(self, *args, input=None, check=True, env=None):
		"""Run git, return CompletedProcess with bytes stdout/stderr."""
		full_env = {**os.environ, **BASE_ENV, **self.env, **(env or {})}
		if isinstance(input, str):
			input = input.encode()
		res = subprocess.run(['git', *args], cwd=self.cwd, input=input, env=full_env,
				     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		if check and res.returncode != 0:
			raise GitError(args, res.returncode, res.stdout, res.stderr)
		return res

	def out(self, *args, **kwargs):
		"""Run git and return stripped stdout as text."""
		return self.run(*args, **kwargs).stdout.decode('utf-8', errors='replace').strip()

	def lines(self, *args, **kwargs):
		text = self.out(*args, **kwargs)
		return text.split('\n') if text else []

	def ok(self, *args, **kwargs):
		return self.run(*args, check=False, **kwargs).returncode == 0

	# repository

	def git_dir(self):
		return Path(self.out('rev-parse', '--absolute-git-dir'))

	def common_dir(self):
		path = Path(self.out('rev-parse', '--git-common-dir'))
		return path if path.is_absolute() else (self.cwd / path).resolve()

	def toplevel(self):
		return Path(self.out('rev-parse', '--show-toplevel'))

	# objects and refs

	def rev(self, rev):
		"""Full sha of a commit, or None."""
		res = self.run('rev-parse', '--verify', '--quiet', rev + '^{commit}', check=False)
		return res.stdout.decode().strip() if res.returncode == 0 else None

	def ref(self, ref):
		"""Value of a ref (any object), or None."""
		res = self.run('rev-parse', '--verify', '--quiet', ref, check=False)
		return res.stdout.decode().strip() if res.returncode == 0 else None

	def is_ancestor(self, a, b):
		return self.ok('merge-base', '--is-ancestor', a, b)

	def merge_base(self, a, b):
		res = self.run('merge-base', a, b, check=False)
		return res.stdout.decode().strip() if res.returncode == 0 else None

	def rev_list(self, *args):
		return self.lines('rev-list', *args)

	def count(self, range_):
		return int(self.out('rev-list', '--count', range_))

	def tree(self, rev):
		return self.out('rev-parse', rev + '^{tree}')

	def show_file(self, rev, path):
		"""Contents of a file at a revision as bytes, or None."""
		res = self.run('cat-file', 'blob', '%s:%s' % (rev, path), check=False)
		return res.stdout if res.returncode == 0 else None

	def read_blobs(self, specs):
		"""Contents of many objects ('rev:path' or sha) in one git process: {spec: bytes or None}."""
		specs = list(specs)
		if not specs:
			return {}
		out = self.run('cat-file', '--batch', input='\n'.join(specs) + '\n').stdout
		result, pos = {}, 0
		for spec in specs:
			end = out.index(b'\n', pos)
			header = out[pos:end].split()
			pos = end + 1
			if len(header) < 3 or header[1] == b'missing':
				result[spec] = None
				continue
			size = int(header[2])
			result[spec] = out[pos:pos + size]
			pos += size + 1
		return result

	def ls_files(self, rev, path):
		"""Paths of all files under a directory of a revision (recursive)."""
		return self.lines('ls-tree', '-r', '--name-only', rev, '--', path.rstrip('/') + '/')

	def commit_infos(self, revs):
		"""commit_info() of many commits in one git process: {sha: info}."""
		revs = list(revs)
		if not revs:
			return {}
		raw = self.run('log', '--no-walk=unsorted', '--stdin', '--format=%x01%H%x00%s%x00%an%x00%ae%x00%B',
			       input='\n'.join(revs) + '\n').stdout
		result = {}
		for block in raw.decode('utf-8', errors='replace').split('\x01')[1:]:
			sha, subject, name, email, body = block.split('\x00', 4)
			result[sha] = {'sha': sha, 'subject': subject, 'author': name, 'email': email,
				       'message': body.rstrip('\n')}
		return result

	def diffs(self, revs, *options):
		"""Patch text of many commits in one git process: {sha: bytes}."""
		revs = list(revs)
		if not revs:
			return {}
		raw = self.run('log', '--no-walk=unsorted', '--stdin', '--no-color', '--format=%x01%x01%H', '--patch',
			       *options, input='\n'.join(revs) + '\n').stdout
		result = {}
		for block in raw.split(b'\x01\x01')[1:]:
			sha, _, patch = block.partition(b'\n')
			result[sha.decode()] = patch
		return result

	def ls_tree(self, rev, path=''):
		"""Names directly under a directory of a revision."""
		args = ['ls-tree', '--name-only', rev]
		if path:
			args.append(path.rstrip('/') + '/')
		return [p.rsplit('/', 1)[-1] for p in self.lines(*args)]

	def hash_blob(self, data):
		return self.out('hash-object', '-w', '--stdin', input=data)

	def hash_blobs(self, datas):
		"""Write many blobs in one git process; return their shas in order."""
		datas = list(datas)
		if not datas:
			return []
		with tempfile.TemporaryDirectory() as tmp:
			paths = []
			for i, data in enumerate(datas):
				path = os.path.join(tmp, str(i))
				with open(path, 'wb') as f:
					f.write(data)
				paths.append(path)
			return self.lines('hash-object', '-w', '--no-filters', '--stdin-paths', input='\n'.join(paths) + '\n')

	def commit_tree(self, tree, parents=(), message='', author=None):
		args = ['commit-tree', tree]
		for p in parents:
			args += ['-p', p]
		env = {}
		if author:
			env['GIT_AUTHOR_NAME'], env['GIT_AUTHOR_EMAIL'] = author
		return self.out(*args, input=message, env=env)

	def commit_info(self, rev):
		"""Subject, author, author email and full message of a commit."""
		raw = self.run('show', '-s', '--format=%H%x00%s%x00%an%x00%ae%x00%B', rev).stdout
		sha, subject, name, email, body = raw.decode('utf-8', errors='replace').split('\x00', 4)
		return {'sha': sha, 'subject': subject, 'author': name, 'email': email, 'message': body.rstrip('\n')}

	def patch_ids(self, revs):
		"""Map commit sha -> stable patch-id for the given commits."""
		if not revs:
			return {}
		diff = self.run('show', '--no-color', '--format=commit %H', '--patch', *revs).stdout
		out = self.run('patch-id', '--stable', input=diff).stdout.decode()
		result = {}
		for line in out.splitlines():
			pid, sha = line.split()
			result[sha] = pid
		return result

	def branch_checkouts(self):
		"""Map branch ref -> work tree path for all work trees."""
		result = {}
		path = None
		for line in self.lines('worktree', 'list', '--porcelain'):
			if line.startswith('worktree '):
				path = line[len('worktree '):]
			elif line.startswith('branch '):
				result[line[len('branch '):]] = path
		return result

	def is_clean(self):
		return not self.out('status', '--porcelain', '--untracked-files=no')

	def unmerged_files(self):
		return self.lines('diff', '--name-only', '--diff-filter=U')

	def config(self, key, default=None):
		res = self.run('config', '--get', key, check=False)
		return res.stdout.decode().strip() if res.returncode == 0 else default


class RefTransaction:
	"""Atomic update of several refs, each checked against its expected old value."""

	def __init__(self, git):
		self.git = git
		self.updates = []

	def update(self, ref, new, old=None):
		"""Set ref to new; old=None means 'must not exist', old='' means 'any'."""
		if old is None:
			old = ZERO_SHA
		self.updates.append(('update', ref, new, old))

	def delete(self, ref, old=''):
		self.updates.append(('delete', ref, None, old))

	def commit(self):
		if not self.updates:
			return
		lines = []
		for op, ref, new, old in self.updates:
			if op == 'update':
				lines.append('update %s %s %s' % (ref, new, old) if old else 'update %s %s' % (ref, new))
			else:
				lines.append('delete %s %s' % (ref, old) if old else 'delete %s' % ref)
		self.git.run('update-ref', '--stdin', input='start\n' + '\n'.join(lines) + '\nprepare\ncommit\n')
		self.updates = []
