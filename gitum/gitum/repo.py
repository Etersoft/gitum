"""Repository access: git, journal, lock, run logs, private work trees."""

import contextlib
import datetime
import fcntl
import json
import os
import secrets
import socket
import time
from pathlib import Path

from .errors import Busy, NeedsHuman, NotGitumRepo
from .git import Git
from .journal import JOURNAL_REF, Journal

LOCK_FILE = 'lock'
OP_FILE = 'operation.json'
COMMIT_FILE = 'committing.json'
CANDIDATE_FILE = 'candidate.json'


def now_id():
	"""A run id that sorts by time (microseconds) and stays unique across machines."""
	return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S%f-') + secrets.token_hex(2)


def owner_info():
	return {
		'pid': os.getpid(),
		'host': socket.gethostname(),
		'user': os.environ.get('USER', ''),
		'session': os.environ.get('GITUM_SESSION', ''),
		'since': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
	}


class Repo:
	def __init__(self, path='.'):
		git = Git(path)
		if not Path(path).is_dir() or not git.ok('rev-parse', '--git-dir'):
			raise NotGitumRepo('%s is not a git repository' % Path(path).resolve())
		res = git.run('rev-parse', '--show-toplevel', check=False)
		self.worktree = Path(res.stdout.decode().strip()) if res.returncode == 0 else Path(path).resolve()
		self.git = Git(self.worktree)
		self.common_dir = self.git.common_dir()
		self.gitum_dir = self.common_dir / 'gitum'
		self._lock_fd = None
		self.recovered = []

	# journal

	def has_journal(self):
		return self.git.rev(JOURNAL_REF) is not None

	def journal(self, rev=JOURNAL_REF):
		return Journal.load(self.git, rev)

	@property
	def config(self):
		return self.journal().config

	# lock

	def lock_path(self):
		return self.gitum_dir / LOCK_FILE

	@contextlib.contextmanager
	def lock(self, wait=False):
		"""Exclusive lock for operations that change the gitum state of the repository.

		The lock is a flock on <git common dir>/gitum/lock; the file holds the
		owner as JSON. A command run by gitum while it holds the lock gets
		GITUM_LOCK_HELD=<lock file>, and a gitum started from it does not lock again.
		"""
		if self._lock_fd is not None:
			yield
			return
		path = self.lock_path()
		held = os.environ.get('GITUM_LOCK_HELD')
		if held and Path(held).resolve() == path.resolve():
			yield
			return
		self.gitum_dir.mkdir(parents=True, exist_ok=True)
		fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
		if wait:
			fcntl.flock(fd, fcntl.LOCK_EX)
		else:
			# a short retry: `gitum status` holds a shared lock for a moment to read the owner
			for attempt in range(20):
				try:
					fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
					break
				except BlockingIOError:
					owner = self.lock_owner()
					if owner is not None and attempt >= 2:
						os.close(fd)
						raise Busy('the repository is locked by another gitum: %s'
							   % json.dumps(owner, ensure_ascii=False), owner=owner) from None
					time.sleep(0.05)
			else:
				os.close(fd)
				raise Busy('the repository is locked by another gitum', owner={})
		os.ftruncate(fd, 0)
		os.write(fd, json.dumps(owner_info()).encode())
		self._lock_fd = fd
		try:
			self.recovered = self.recover()
			yield
		finally:
			os.ftruncate(fd, 0)
			fcntl.flock(fd, fcntl.LOCK_UN)
			os.close(fd)
			self._lock_fd = None

	def lock_owner(self):
		"""Owner of the repository lock if it is held, else None."""
		path = self.lock_path()
		if not path.exists():
			return None
		fd = os.open(path, os.O_RDONLY)
		try:
			try:
				fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
			except BlockingIOError:
				try:
					return json.loads(path.read_text() or '{}')
				except ValueError:
					return {}
			fcntl.flock(fd, fcntl.LOCK_UN)
			return None
		finally:
			os.close(fd)

	# crash safety

	def write_json(self, name, data):
		"""Write a state file atomically (a crash leaves the old or the new content, never a mix)."""
		path = self.gitum_dir / name
		path.parent.mkdir(parents=True, exist_ok=True)
		tmp = path.with_name(path.name + '.tmp')
		with open(tmp, 'w') as f:
			json.dump(data, f, indent=1, ensure_ascii=False)
			f.flush()
			os.fsync(f.fileno())
		tmp.replace(path)

	def read_json(self, name):
		path = self.gitum_dir / name
		return json.loads(path.read_text()) if path.exists() else None

	@contextlib.contextmanager
	def committing(self, tr, moves=(), **info):
		"""Commit a ref transaction so that a crash at any point can be recovered.

		moves: (branch, old, new) of the branches the transaction moves; their
		clean user work trees are updated first and put back if the transaction
		fails. The marker file stays until the body of the with-block (cleanup,
		hooks) is done; recover() finishes or undoes an interrupted commit.
		"""
		found = self.check_checkouts([m[0] for m in moves])
		follow = [(found[b], old, new) for b, old, new in moves if b in found and old and new and old != new]
		refs = {ref: new for op, ref, new, _old in tr.updates if op == 'update'}
		self.write_json(COMMIT_FILE, {'refs': refs, 'checkouts': follow, **info})
		done = []
		try:
			for path, old, new in follow:
				self.git.at(path).run('read-tree', '-m', '-u', old, new)
				done.append((path, old, new))
			tr.commit()
		except BaseException:
			for path, old, new in reversed(done):
				self.git.at(path).run('read-tree', '-m', '-u', new, old, check=False)
			(self.gitum_dir / COMMIT_FILE).unlink(missing_ok=True)
			raise
		yield
		(self.gitum_dir / COMMIT_FILE).unlink(missing_ok=True)

	def recover(self):
		"""Finish or undo what an interrupted gitum left; called with the lock held."""
		done = []
		marker = self.read_json(COMMIT_FILE)
		if marker:
			committed = all(self.git.ref(ref) == new for ref, new in marker['refs'].items())
			if committed:
				done.append('finished the interrupted %s' % marker.get('kind', 'commit'))
				self._finish_commit(marker)
			else:
				for path, old, new in marker.get('checkouts', []):
					if Path(path).is_dir():
						self.git.at(path).run('read-tree', '-m', '-u', new, old, check=False)
				done.append('undid the interrupted %s (nothing was changed)' % marker.get('kind', 'commit'))
			(self.gitum_dir / COMMIT_FILE).unlink(missing_ok=True)
		op = self.load_op()
		keep = Path(op['worktree']).resolve() if op and op.get('worktree') else None
		wt_dir = self.gitum_dir / 'wt'
		if wt_dir.is_dir():
			for path in wt_dir.iterdir():
				if path.resolve() != keep:
					self.remove_worktree(path)
					done.append('removed the leftover work tree %s' % path.name)
		return done

	def _finish_commit(self, marker):
		kind = marker.get('kind')
		if kind in ('sync', 'accept'):
			op = self.load_op()
			if op and op.get('run') == marker.get('run'):
				if op.get('worktree'):
					self.remove_worktree(op['worktree'])
				self.clear_op()
			if kind == 'accept':
				if marker.get('candidate_on_remote'):
					self.write_json('candidate-on-remote.json', marker['candidate_on_remote'])
				(self.gitum_dir / CANDIDATE_FILE).unlink(missing_ok=True)
				for ref in marker.get('delete_refs', []):
					if self.git.ref(ref):
						self.git.run('update-ref', '-d', ref, check=False)
			if marker.get('hooks_report'):
				from . import hooks
				report = self.journal().read_json(marker['hooks_report'])
				if report:
					hooks.run_hooks(self, report)

	# operation in progress

	def op_path(self):
		return self.gitum_dir / OP_FILE

	def load_op(self):
		path = self.op_path()
		return json.loads(path.read_text()) if path.exists() else None

	def save_op(self, op):
		self.write_json(OP_FILE, op)

	def clear_op(self):
		self.op_path().unlink(missing_ok=True)

	# run logs

	def new_run_dir(self, kind):
		run_id = now_id()
		path = self.gitum_dir / 'runs' / ('%s-%s' % (run_id, kind))
		path.mkdir(parents=True)
		return run_id, path

	# private work trees

	def add_worktree(self, rev, name=None):
		"""A detached work tree of our own, never one of the user's."""
		path = self.gitum_dir / 'wt' / (name or now_id())
		path.parent.mkdir(parents=True, exist_ok=True)
		self.git.run('worktree', 'add', '--detach', '--force', str(path), rev)
		return self.git.at(path)

	def remove_worktree(self, path):
		path = Path(path)
		if path.exists():
			self.git.run('worktree', 'remove', '--force', str(path), check=False)
		self.git.run('worktree', 'prune', check=False)

	def hook_env(self, **values):
		"""Environment for commands gitum runs (hooks, resolvers, checks, generators).

		Every value becomes GITUM_<NAME>; None values are left out.
		"""
		env = {
			'GITUM_REPO': str(self.worktree),
			'GITUM_COMMON_DIR': str(self.common_dir),
			'GITUM_LOCK_FILE': str(self.lock_path()),
		}
		if self._lock_fd is not None or os.environ.get('GITUM_LOCK_HELD'):
			env['GITUM_LOCK_HELD'] = str(self.lock_path())
		for key, value in values.items():
			if value is not None:
				env['GITUM_' + key.upper()] = str(value)
		return {**os.environ, **env}

	def check_checkouts(self, branches):
		"""Branches checked out in user work trees: {branch: path}.

		Such a branch can move only if its work tree is clean: the work tree then
		follows it (see committing). Local changes stop the operation.
		"""
		found = {}
		checkouts = self.git.branch_checkouts() if branches else {}
		for branch in branches:
			path = self._user_checkout(checkouts.get('refs/heads/' + branch))
			if not path:
				continue
			if not self.git.at(path).is_clean():
				raise NeedsHuman('%s is checked out in %s with local changes; commit or stash them first'
						 % (branch, path), checked_out={branch: path})
			found[branch] = path
		return found

	def checked_out(self, branch):
		"""Work tree path where a branch is checked out (ignoring our own), or None."""
		return self._user_checkout(self.git.branch_checkouts().get('refs/heads/' + branch))

	def _user_checkout(self, path):
		if path and Path(path).resolve().is_relative_to((self.gitum_dir / 'wt').resolve()):
			return None
		return path

