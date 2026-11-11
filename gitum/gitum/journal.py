"""The journal: an append-only branch with one commit per published state.

Tree layout:
    gitum.toml                  project configuration
    upstreams/<name>.json       upstream sync point
    layers/<name>.json          layer base, tip and changes
    layers/<name>.series        the same changes, one line each (readable diffs)
    runs/<id>/report.json       sync reports
    rr-cache/...                shared git rerere cache

Every state commit has the previous state commit as its first parent and the
layer tips of the state as further parents, so all recorded commits stay
reachable. Walk the journal with --first-parent.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass

from . import config as config_mod
from .errors import NotGitumRepo

JOURNAL_BRANCH = 'gitum/journal'
JOURNAL_REF = 'refs/heads/' + JOURNAL_BRANCH
HISTORY_PREFIX = 'gitum/history/'
ROOT_MESSAGE = 'gitum journal'

GITUM_ID_RE = re.compile(r'^Gitum-Id:\s*(\S+)\s*$', re.M)


def history_ref(layer):
	return 'refs/heads/' + HISTORY_PREFIX + layer


def dump_json(data):
	return (json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + '\n').encode()


@dataclass
class Change:
	sha: str
	subject: str
	patch_id: str = ''
	gitum_id: str = ''

	def to_json(self):
		return {k: v for k, v in self.__dict__.items() if v}


class LayerState:
	def __init__(self, name, data):
		self.name = name
		self.base = data['base']
		self.tip = data['tip']
		self.branch = data.get('branch', name)
		self.upstream_ref = data.get('upstream_ref', '')
		self.changes = [Change(**c) for c in data.get('changes', [])]

	def to_json(self):
		return {
			'base': self.base,
			'tip': self.tip,
			'branch': self.branch,
			'upstream_ref': self.upstream_ref,
			'changes': [c.to_json() for c in self.changes],
		}


def describe_changes(git, base, tip):
	"""Changes of a layer: commits base..tip, oldest first."""
	shas = git.rev_list('--reverse', '--no-merges', '%s..%s' % (base, tip))
	pids = git.patch_ids(shas)
	result = []
	for sha in shas:
		info = git.commit_info(sha)
		m = GITUM_ID_RE.findall(info['message'])
		result.append(Change(sha=sha, subject=info['subject'], patch_id=pids.get(sha, ''),
				     gitum_id=m[-1] if m else ''))
	return result


def series_text(changes):
	return ''.join('%s %s %s\n' % (c.sha[:12], c.gitum_id or '-', c.subject) for c in changes).encode()


class Journal:
	"""A journal state (one commit of the journal branch)."""

	def __init__(self, git, sha):
		self.git = git
		self.sha = sha
		text = self.read_text(config_mod.CONFIG_FILE)
		if text is None:
			raise NotGitumRepo('%s has no %s' % (JOURNAL_BRANCH, config_mod.CONFIG_FILE))
		self.config_text = text
		self.config = config_mod.parse(text)

	@classmethod
	def load(cls, git, rev=JOURNAL_REF):
		sha = git.rev(rev)
		if not sha:
			raise NotGitumRepo('no gitum journal (%s): run gitum init' % JOURNAL_BRANCH)
		return cls(git, sha)

	def read(self, path):
		return self.git.show_file(self.sha, path)

	def read_text(self, path):
		data = self.read(path)
		return data.decode('utf-8') if data is not None else None

	def read_json(self, path, default=None):
		text = self.read_text(path)
		return json.loads(text) if text is not None else default

	def layer(self, name):
		data = self.read_json('layers/%s.json' % name)
		return LayerState(name, data) if data else None

	def layers(self):
		return {name: self.layer(name) for name in self.config.layers if self.layer(name)}

	def upstream(self, name):
		return self.read_json('upstreams/%s.json' % name, {})

	def list(self, path):
		return self.git.ls_tree(self.sha, path)

	def history(self, max_count=None):
		"""First-parent commits of the journal, newest first."""
		args = ['--first-parent', self.sha]
		if max_count:
			args.insert(0, '--max-count=%d' % max_count)
		return self.git.rev_list(*args)


class JournalWriter:
	"""Collects file changes and writes a new journal commit (refs are not touched)."""

	def __init__(self, git, parent=None):
		self.git = git
		self.parent = parent
		self.files = {}		# path -> bytes, or None to delete
		self.remove_dirs = []

	def put(self, path, data):
		if isinstance(data, str):
			data = data.encode()
		self.files[path] = data

	def put_json(self, path, data):
		self.put(path, dump_json(data))

	def remove(self, path):
		self.files[path] = None

	def remove_dir(self, path):
		self.remove_dirs.append(path.rstrip('/'))

	def put_layer(self, state):
		self.put_json('layers/%s.json' % state.name, state.to_json())
		self.put('layers/%s.series' % state.name, series_text(state.changes))

	def write(self, message, extra_parents=()):
		with tempfile.TemporaryDirectory() as tmp:
			git = self.git.with_env(GIT_INDEX_FILE=os.path.join(tmp, 'index'))
			if self.parent:
				git.run('read-tree', self.parent)
			for path in self.remove_dirs:
				git.run('rm', '-r', '-q', '--cached', '--ignore-unmatch', '--', path)
			info = []
			for path, data in self.files.items():
				if data is None:
					info.append('0 %s\t%s' % ('0' * 40, path))
				else:
					info.append('100644 %s\t%s' % (git.hash_blob(data), path))
			if info:
				git.run('update-index', '--index-info', input='\n'.join(info) + '\n')
			tree = git.out('write-tree')
		parents = [self.parent] if self.parent else []
		for p in extra_parents:
			if p and p not in parents:
				parents.append(p)
		return self.git.commit_tree(tree, parents, message)


def make_root(git):
	"""An empty parentless commit that starts the journal's first-parent line."""
	empty_tree = git.out('hash-object', '-t', 'tree', '-w', '--stdin', input=b'')
	return git.commit_tree(empty_tree, [], ROOT_MESSAGE)


def history_commit(git, layer, tip, parent, message):
	"""A commit of the linear history branch of a layer: the tree of the layer tip."""
	return git.commit_tree(git.tree(tip), [parent] if parent else [], message)
