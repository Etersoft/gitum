"""Repository access: git, journal, lock, run logs, private work trees."""

import contextlib
import datetime
import fcntl
import json
import os
import secrets
import socket
from pathlib import Path

from .errors import Busy, NotGitumRepo
from .git import Git
from .journal import JOURNAL_REF, Journal

LOCK_FILE = 'lock'
OP_FILE = 'operation.json'


def now_id():
	return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(2)


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

	# journal

	def has_journal(self):
		return self.git.rev(JOURNAL_REF) is not None

	def journal(self, rev=JOURNAL_REF):
		return Journal.load(self.git, rev)

	@property
	def config(self):
		return self.journal().config

	# lock

	@contextlib.contextmanager
	def lock(self, wait=False):
		"""Exclusive lock for operations that change the gitum state of the repository."""
		if self._lock_fd is not None:
			yield
			return
		self.gitum_dir.mkdir(parents=True, exist_ok=True)
		path = self.gitum_dir / LOCK_FILE
		fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
		try:
			fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
		except BlockingIOError:
			os.close(fd)
			raise Busy('the repository is locked by another gitum: %s' % self.lock_owner(),
				   owner=self.lock_owner())
		os.ftruncate(fd, 0)
		os.write(fd, json.dumps(owner_info()).encode())
		self._lock_fd = fd
		try:
			yield
		finally:
			os.ftruncate(fd, 0)
			fcntl.flock(fd, fcntl.LOCK_UN)
			os.close(fd)
			self._lock_fd = None

	def lock_owner(self):
		"""Owner of the repository lock if it is held, else None."""
		path = self.gitum_dir / LOCK_FILE
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

	# operation in progress

	def op_path(self):
		return self.gitum_dir / OP_FILE

	def load_op(self):
		path = self.op_path()
		return json.loads(path.read_text()) if path.exists() else None

	def save_op(self, op):
		self.gitum_dir.mkdir(parents=True, exist_ok=True)
		tmp = self.op_path().with_suffix('.tmp')
		tmp.write_text(json.dumps(op, indent=1, ensure_ascii=False))
		tmp.replace(self.op_path())

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

	def checked_out(self, branch):
		"""Work tree path where a branch is checked out (ignoring our own), or None."""
		path = self.git.branch_checkouts().get('refs/heads/' + branch)
		if path and Path(path).resolve().is_relative_to((self.gitum_dir / 'wt').resolve()):
			return None
		return path

