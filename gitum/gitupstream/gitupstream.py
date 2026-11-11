# git-um - Git Upstream Manager.
# Copyright (C) 2012  Pavel Shilovsky <piastry@etersoft.ru>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from git import BadName, GitCommandError, InvalidGitRepositoryError, NoSuchPathError, Repo

from .constants import MAINLINE_BRANCH, PATCHES_BRANCH, REBASED_BRANCH, UPSTREAM_BRANCH
from .errors import (
	BranchExists,
	BrokenRepo,
	NoGitumRemote,
	NoMergeBranch,
	NoStateFile,
	NotGitRepo,
	NotGitumRepo,
	NotSupported,
	NotUptodate,
	PatchError,
	PatchFailed,
	RebaseFailed,
	RepoIsDirty,
)

START_ST = 0
MERGE_ST = 1
REBASE_ST = 2
COMMIT_ST = 3

CONFIG_FILE = '.gitum-config'
CONFIG_BRANCH = 'gitum-config'
# per-repository state files, stored in the git directory
STATE_FILE = '.gitum-state'
REMOTE_REPO = '.gitum-remote'
MERGE_BRANCH = '.gitum-mbranch'
CURRENT_REBASED = '.curent_rebased'
CURRENT_MAINLINE = '.curent_mainline'
STATE_FILES = (STATE_FILE, REMOTE_REPO, MERGE_BRANCH, CURRENT_REBASED, CURRENT_MAINLINE)
# files in the patches branch
UPSTREAM_COMMIT_FILE = '_upstream_commit_'
LAST_PATCH_FILE = '_current_patch_'
TMP_LAST_PATCH_FILE = '_current.patch'
PATCHES_ROOT_MESSAGE = 'gitum-patches: begin'

# never start an editor from a non-interactive git call
NO_EDITOR_ENV = {'GIT_EDITOR': 'true'}


def _decode(data):
	if isinstance(data, bytes):
		return data.decode('utf-8', errors='replace')
	return data


class GitUpstream:
	def __init__(self, repo_path='.', with_log=False, new_repo=False):
		try:
			if new_repo:
				self._repo = Repo.init(repo_path)
			else:
				self._repo = Repo(repo_path, search_parent_directories=True)
		except (InvalidGitRepositoryError, NoSuchPathError) as e:
			raise NotGitRepo('%s is not a git repository' % e) from e
		self._with_log = with_log

	def repo(self):
		return self._repo

	@property
	def _work_dir(self):
		return Path(self._repo.working_tree_dir)

	def merge(self, mbranch=None, track_with=None):
		self._init_merge()
		if self._repo.is_dirty():
			self._log_error('You have local changes. Run git commit and gitum update to save them, please.')
			raise RepoIsDirty
		self._load_config()
		self._check_mainline()
		if self._diff(self._rebased, self._mainline):
			self._log_error('You have local commited changes. Run gitum update to save them, please.')
			raise NotUptodate
		if not mbranch:
			mbranch = self._load_mbranch()
		if track_with:
			self._save_mbranch(mbranch)
		if len(mbranch.split('/')) >= 2:
			self._repo.git.fetch(mbranch.split('/')[0])
		try:
			self._repo.commit(mbranch)
		except (BadName, GitCommandError, ValueError):
			self._log_error('Can not merge from %s - not exists.' % mbranch)
			raise NoMergeBranch
		self._commits = self._get_commits(mbranch)
		if not self._commits:
			self._log('Repository is up to date - nothing to merge.')
			return
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._save_branches()
		self._process_commits()
		self._finish('Successfully updated work branches.')

	def abort(self, am=False):
		self._init_merge()
		self._load_config()
		if not self._load_state():
			raise NoStateFile
		try:
			if am:
				self._repo.git.am('--abort')
			else:
				self._repo.git.rebase('--abort')
		except GitCommandError:
			pass
		self._restore_branches()
		self._finish('Restored work branches.')

	def continue_merge(self, rebase_cmd):
		self._init_merge()
		self._load_config()
		if not self._load_state():
			raise NoStateFile
		if self._state == REBASE_ST:
			with tempfile.TemporaryFile() as tmp_file:
				try:
					diff_str = self._stage2(self._commits[self._id], tmp_file, rebase_cmd)
					self._stage3(self._commits[self._id], diff_str)
					self._save_repo_state(self._head(self._mainline) if diff_str else '')
					self._id += 1
					self._cur_num += 1
				except GitCommandError as e:
					self._save_state()
					self._log(self._fixup_merge_message(self._read_output(tmp_file)))
					self._log(e.stderr)
					raise RebaseFailed from e
				except PatchError as e:
					self._save_state()
					self._log_error(e.message)
					raise PatchFailed from e
				except BaseException:
					self._save_state()
					raise
		elif self._state != MERGE_ST:
			self._log_error("Don't support continue not from merge or rebase mode.")
			raise NotSupported
		self._process_commits()
		self._finish('Successfully updated work branches.')

	def status(self):
		self._load_config()
		current_rebased = self._load_current_rebased()
		self._check_mainline()
		diff = self._diff('--full-index', self._mainline, self._rebased)
		ca = self._find_ca(current_rebased, self._rebased)
		if self._repo.is_dirty():
			self._log_error('You have local changes. Run git commit and gitum update to save them, please.')
			return
		if current_rebased == self._head(self._rebased):
			self._log('Nothing to update.')
			return
		if ca == current_rebased:
			new_commits = list(self._repo.iter_commits(ca + '..' + self._rebased))
			new_commits.reverse()
			self._log('Have new commits, run gitum update to save them:')
			for c_id in new_commits:
				self._log('\t%s' % c_id.summary)
		else:
			self._log('Existing patches were modified.')
			self._log('Run gitum update to save the result diff:\n%s' % _decode(diff))

	def update(self, message=''):
		if self._repo.is_dirty():
			self._log_error('You have local changes. Commit them and try again, please.')
			raise RepoIsDirty
		self._init_merge()
		self._load_config()
		self._check_mainline()
		current_rebased = self._load_current_rebased()
		if current_rebased == self._head(self._rebased):
			self._log('Nothing to update.')
			return
		diff = self._diff('--full-index', self._mainline, self._rebased)
		ca = self._find_ca(current_rebased, self._rebased)
		if ca == current_rebased:
			new_commits = list(self._repo.iter_commits(ca + '..' + self._rebased))
			if not new_commits and diff:
				self._log_error('You have equal commits between mainline and rebased branches.\n'
						'But git diff between these branches is not empty.')
				return
			new_commits.reverse()
			for c_id in new_commits:
				self._log('Applying commit: %s' % c_id.summary)
				self._repo.git.checkout(self._mainline)
				self._repo.git.cherry_pick(c_id.hexsha)
				self._save_repo_state(self._mainline if diff else '', message, c_id.hexsha)
		else:
			self._diffapply(diff, message)
		self._finish('Successfully updated work branches.')

	def create(self, remote, upstream, rebased, mainline, patches):
		config = (upstream, rebased, mainline, patches) != \
			(UPSTREAM_BRANCH, REBASED_BRANCH, MAINLINE_BRANCH, PATCHES_BRANCH)
		for branch in (mainline, rebased, patches):
			if self._has_branch(branch):
				self._log_error('%s branch exists.' % branch)
				raise BranchExists
		if config and self._has_branch(CONFIG_BRANCH):
			self._log_error('%s branch exists.' % CONFIG_BRANCH)
			raise BranchExists
		if not self._has_branch(upstream):
			self._repo.git.branch('-m', upstream)
		self._repo.git.checkout(upstream)
		self._repo.create_head(mainline)
		self._repo.create_head(rebased)
		self._save_patches(patches, upstream)
		if config:
			self._save_config(mainline, upstream, rebased, patches)
		self._save_mbranch(remote)
		self._repo.git.checkout(rebased)
		self._save_current_rebased(rebased)
		self._save_current_mainline(mainline)
		self._log('Successfully created work branches.')

	def remove_branches(self):
		self._load_config()
		if self._has_branch(self._upstream):
			self._repo.git.checkout(self._upstream, '-f')
		for branch in (self._mainline, self._rebased, self._patches, CONFIG_BRANCH):
			if self._has_branch(branch):
				self._repo.delete_head(branch, force=True)
		self._log('Successfully removed work branches.')

	def remove_config_files(self):
		for name in STATE_FILES:
			self._state_path(name).unlink(missing_ok=True)
		self._log('Successfully removed gitum config files.')

	def remove_all(self):
		self.remove_branches()
		self.remove_config_files()

	def restore(self, commit=None, rebased_only=False):
		self._load_config()
		if not commit:
			commit = self._patches
		if rebased_only:
			self._gen_rebased(commit)
			self._save_current_rebased(self._rebased)
			return
		commits = []
		for i in self._repo.iter_commits(commit):
			commits.append(i.hexsha)
			if i.message.startswith(PATCHES_ROOT_MESSAGE):
				break
		else:
			self._log_error('Broken %s commit.' % commit)
			raise BrokenRepo
		commits.reverse()
		git = self._repo.git
		start = commits[0]
		commits = commits[1:]
		git.checkout(start)
		upstream_commit = self._read_upstream_commit()
		git.checkout(upstream_commit)
		with tempfile.TemporaryDirectory() as tmp:
			tmp_dir = Path(tmp)
			saved_commit_id = self._repo.head.commit.hexsha
			for i in commits:
				git.checkout(i)
				self._copy_patches(self._work_dir, tmp_dir)
				shutil.copy(self._work_dir / LAST_PATCH_FILE, tmp_dir / LAST_PATCH_FILE)
				upstream_commit = self._read_upstream_commit()
				git.checkout(saved_commit_id)
				if (tmp_dir / LAST_PATCH_FILE).stat().st_size:
					self._apply_patch_am(tmp_dir, LAST_PATCH_FILE)
				(tmp_dir / LAST_PATCH_FILE).unlink()
				saved_commit_id = self._repo.head.commit.hexsha
			self._recreate_head(self._mainline)
			git.checkout(upstream_commit)
			self._recreate_head(self._upstream)
			self._recreate_head(self._rebased)
			git.checkout(self._rebased)
			for i in self._list_patches(tmp_dir):
				self._apply_patch_am(tmp_dir, i)
		git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully restored work branches to %s commit from %s branch.' % (commit, self._patches))

	def clone(self, remote_repo):
		if not remote_repo:
			self._log_error('Specify remote repo, please.')
			raise NoGitumRemote
		if not os.path.isabs(remote_repo) and not self._has_hostname(remote_repo):
			remote_repo = os.path.join(os.getcwd(), remote_repo)
		self._repo.git.remote('add', 'origin', remote_repo)
		self._repo.git.fetch('origin')
		if any(ref.remote_head == CONFIG_BRANCH for ref in self._repo.remotes.origin.refs):
			self._repo.git.checkout('-b', CONFIG_BRANCH, 'origin/' + CONFIG_BRANCH)
		self._load_config()
		self._repo.git.checkout('-b', self._upstream, 'origin/' + self._upstream)
		self._repo.git.checkout('-b', self._patches, 'origin/' + self._patches)
		self._repo.git.checkout('-b', self._mainline, 'origin/' + self._mainline)
		self._save_remote('origin')
		self._gen_rebased()
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Repository from %s was cloned into %s.' % (remote_repo, self._repo.working_dir))

	def pull(self, remote=None, track_with=None):
		self._load_config()
		self._check_mainline()
		self._init_merge()
		if not remote:
			remote = self._load_remote()
		if track_with:
			self._save_remote(remote)
		self._save_branches()
		cur = self._head(self._patches)
		git = self._repo.git
		git.fetch(remote)
		for branch in (self._upstream, self._patches, self._mainline):
			git.checkout(branch, '-f')
			git.reset(remote + '/' + branch, '--hard')
		self._gen_rebased()
		self._log('Reset work branches to the remote state, applying our commits on top...')
		git.checkout(self._mainline)
		previd = self._find_ca(remote + '/' + self._patches, cur)
		self._commits = [q.hexsha for q in self._repo.iter_commits(previd + '..' + cur)]
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._pull_commits()
		self._finish('Successfully updated work branches.')

	def continue_pull(self, command):
		self._load_config()
		self._init_merge()
		if not self._load_state():
			raise NoStateFile
		with tempfile.TemporaryFile() as tmp_file:
			try:
				self._repo.git.am(command, output_stream=tmp_file)
				if command == '--resolved':
					self._repo.git.checkout(self._rebased)
					self._repo.git.cherry_pick(self._mainline)
					self._save_repo_state(self._mainline)
				self._repo.git.checkout(self._upstream, '-f')
				self._repo.git.merge(self._show(self._commits[self._id], UPSTREAM_COMMIT_FILE))
				self._repo.git.checkout(self._mainline)
				self._id += 1
				self._cur_num += 1
			except GitCommandError as e:
				self._save_state()
				self._log(self._fixup_pull_message(self._read_output(tmp_file)))
				self._log(e.stderr)
				raise RebaseFailed from e
			except BaseException:
				self._save_state()
				raise
		self._pull_commits()
		self._finish('Successfully updated work branches.')

	def push(self, remote=None, track_with=None):
		self._load_config()
		self._check_mainline()
		if not remote:
			remote = self._load_remote()
		if track_with:
			self._save_remote(remote)
		self._repo.git.push(remote, self._upstream, self._mainline, self._patches)
		if self._has_branch(CONFIG_BRANCH):
			self._repo.git.push(remote, CONFIG_BRANCH)
		self._log('Successfully pushed work branches.')

	# helpers

	def _finish(self, message):
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log(message)

	def _head(self, branch):
		return self._repo.branches[branch].commit.hexsha

	def _has_branch(self, head):
		return any(h.name == head for h in self._repo.heads)

	def _recreate_head(self, head):
		if self._has_branch(head):
			self._repo.delete_head(head, force=True)
		self._repo.create_head(head)

	@staticmethod
	def _has_hostname(repo_path):
		return ':' in repo_path

	def _diff(self, *args):
		"""Return git diff output as bytes to keep binary data and line endings intact."""
		return self._repo.git.diff(*args, stdout_as_string=False)

	def _show(self, commit, path):
		return self._repo.git.show('%s:%s' % (commit, path))

	def _run_git(self, *args, stdin=None):
		"""Run git with data on stdin (not supported by GitPython) and return stdout bytes."""
		return subprocess.run(['git', *args], cwd=self._work_dir, input=stdin,
				      stdout=subprocess.PIPE, check=True).stdout

	def _read_upstream_commit(self):
		lines = (self._work_dir / UPSTREAM_COMMIT_FILE).read_text().splitlines()
		if len(lines) != 1:
			self._log_error('Broken upstream commit file.')
			raise BrokenRepo
		return lines[0].strip()

	@staticmethod
	def _list_patches(directory):
		return sorted(p.name for p in Path(directory).iterdir() if p.name.endswith('.patch'))

	@classmethod
	def _copy_patches(cls, src, dst):
		for name in cls._list_patches(src):
			shutil.copy(Path(src) / name, Path(dst) / name)

	@staticmethod
	def _read_output(tmp_file):
		tmp_file.seek(0)
		return _decode(tmp_file.read())

	def _gen_rebased(self, commit=''):
		if not commit:
			commit = self._patches
		self._repo.git.checkout(commit)
		with tempfile.TemporaryDirectory() as tmp_dir:
			self._copy_patches(self._work_dir, tmp_dir)
			if self._has_branch(self._rebased):
				self._repo.delete_head(self._rebased, force=True)
			self._repo.git.checkout('-b', self._rebased, self._show(commit, UPSTREAM_COMMIT_FILE))
			for i in self._list_patches(tmp_dir):
				self._apply_patch_am(tmp_dir, i)

	def _find_ca(self, c1, c2):
		return self._repo.git.merge_base(c1, c2)

	def _state_path(self, name):
		return Path(self._repo.git_dir) / name

	def _save_parm(self, filename, parm):
		self._state_path(filename).write_text(parm)

	def _load_parm(self, filename):
		with self._state_path(filename).open() as f:
			return f.readline().strip()

	def _save_remote(self, remote):
		self._save_parm(REMOTE_REPO, remote)

	def _load_remote(self):
		try:
			return self._load_parm(REMOTE_REPO)
		except OSError:
			self._log_error('Specify a remote gitum repository, please.')
			raise NoGitumRemote

	def _save_mbranch(self, mbranch):
		self._save_parm(MERGE_BRANCH, mbranch)

	def _load_mbranch(self):
		try:
			return self._load_parm(MERGE_BRANCH)
		except OSError:
			self._log_error('Specify a merge branch, please.')
			raise NoMergeBranch

	def _save_current_rebased(self, rebased):
		self._save_parm(CURRENT_REBASED, self._head(rebased))

	def _load_current_parm(self, filename):
		try:
			return self._load_parm(filename)
		except OSError:
			self._log_error('Not a gitum repository (no %s): run gitum create or gitum clone first.' %
					self._state_path(filename))
			raise NotGitumRepo

	def _load_current_rebased(self):
		return self._load_current_parm(CURRENT_REBASED)

	def _save_current_mainline(self, mainline):
		self._save_parm(CURRENT_MAINLINE, self._head(mainline))

	def _load_current_mainline(self):
		return self._load_current_parm(CURRENT_MAINLINE)

	def _check_mainline(self):
		current_mainline = self._load_current_mainline()
		if current_mainline != self._head(self._mainline):
			self._log_unexpected_head(self._mainline, self._head(self._mainline), current_mainline)
			raise RepoIsDirty

	@staticmethod
	def _get_commit_name_from_patch(lines):
		for line in _decode(lines).split('\n'):
			parts = line.split('Subject: [PATCH] ')
			if len(parts) == 2:
				return parts[1]
		return ''

	def _pull_commits(self):
		tmp_file = tempfile.TemporaryFile()
		try:
			for q in range(self._id, len(self._commits)):
				lines = self._repo.git.show(self._commits[q] + ':' + LAST_PATCH_FILE,
							    stdout_as_string=False)
				if lines:
					with tempfile.TemporaryDirectory() as tmp_dir:
						patch = Path(tmp_dir) / TMP_LAST_PATCH_FILE
						patch.write_bytes(lines)
						self._log('Applying commit: %s' % self._get_commit_name_from_patch(lines))
						self._repo.git.am('-3', '--keep-cr', str(patch), output_stream=tmp_file)
					self._repo.git.checkout(self._rebased)
					self._repo.git.cherry_pick(self._mainline)
					self._save_repo_state(self._mainline)
				self._repo.git.checkout(self._upstream)
				self._repo.git.merge(self._show(self._commits[q], UPSTREAM_COMMIT_FILE))
				self._repo.git.checkout(self._mainline)
				tmp_file.close()
				tmp_file = tempfile.TemporaryFile()
				self._id += 1
				self._cur_num += 1
		except GitCommandError as e:
			self._save_state()
			self._log(self._fixup_pull_message(self._read_output(tmp_file)))
			self._log(e.stderr)
			raise RebaseFailed from e
		except BaseException:
			self._save_state()
			raise
		finally:
			tmp_file.close()

	def _make_orphan_branch(self, branch, filename, content, message):
		"""Create a branch with a single parentless commit holding one file."""
		blob = self._run_git('hash-object', '-w', '--stdin', stdin=content.encode()).decode().strip()
		tree = self._run_git('mktree', stdin=('100644 blob %s\t%s\n' % (blob, filename)).encode()).decode().strip()
		commit = self._repo.git.commit_tree(tree, '-m', message)
		self._repo.git.branch(branch, commit)

	def _save_patches(self, patches, upstream):
		self._make_orphan_branch(patches, UPSTREAM_COMMIT_FILE, self._head(upstream),
					 PATCHES_ROOT_MESSAGE)

	def _save_config(self, mainline, upstream, rebased, patches):
		content = ('current = %s\nupstream = %s\nrebased = %s\npatches = %s\n' %
			   (mainline, upstream, rebased, patches))
		self._make_orphan_branch(CONFIG_BRANCH, CONFIG_FILE, content, 'Save config file')

	def _save_repo_state(self, commit, message='', cur_rebased=None):
		mainline_c = commit if commit else self._mainline
		rebased_c = cur_rebased if cur_rebased else self._rebased
		if self._diff(rebased_c, mainline_c):
			self._log_error("%s and %s work trees are not equal - can't save state!" %
					(rebased_c, mainline_c))
			raise NotUptodate
		git = self._repo.git
		work_dir = self._work_dir
		with tempfile.TemporaryDirectory() as tmp:
			tmp_dir = Path(tmp)
			# generate new patches
			git.format_patch('-o', str(tmp_dir), '%s..%s' % (self._upstream, rebased_c))
			# get mainline branch commit
			if commit:
				out = Path(git.format_patch('-o', str(tmp_dir / 'last'), '%s^..%s' % (commit, commit)))
				shutil.move(out, tmp_dir / LAST_PATCH_FILE)
			else:
				(tmp_dir / LAST_PATCH_FILE).touch()
			git.checkout(self._patches, '-f')
			# replace old patches in the patches branch with new ones
			git.rm(str(work_dir / '*.patch'), '--ignore-unmatch')
			for name in self._list_patches(tmp_dir):
				shutil.move(tmp_dir / name, work_dir / name)
			shutil.move(tmp_dir / LAST_PATCH_FILE, work_dir / LAST_PATCH_FILE)
		# update upstream head
		(work_dir / UPSTREAM_COMMIT_FILE).write_text(self._head(self._upstream))
		# commit the result
		git.add(str(work_dir))
		mess = message
		if not mess and commit:
			mess = self._repo.commit(commit).message
		if not mess:
			mess = '%s branch updated without code changes' % self._rebased
		if commit:
			author = self._repo.commit(commit).author
			git.commit('-m', mess, '--author=%s <%s>' % (author.name, author.email))
		else:
			git.commit('-m', mess)

	@staticmethod
	def _fixup_merge_message(mess):
		for cmd in ('continue', 'abort', 'skip'):
			mess = mess.replace('git rebase --' + cmd, 'gitum merge --' + cmd)
		return mess

	@staticmethod
	def _fixup_pull_message(mess):
		for cmd in ('resolved', 'abort', 'skip'):
			mess = mess.replace('git am --' + cmd, 'gitum pull --' + cmd)
		return mess

	def _load_config(self):
		# set defaults
		self._upstream = UPSTREAM_BRANCH
		self._rebased = REBASED_BRANCH
		self._mainline = MAINLINE_BRANCH
		self._patches = PATCHES_BRANCH
		# load config
		try:
			lines = self._show(CONFIG_BRANCH, CONFIG_FILE).split('\n')
		except GitCommandError:
			return
		for num, line in enumerate(lines, 1):
			parts = line.split('#')[0].strip().split(' ')
			if parts == ['']:
				continue
			if len(parts) != 3 or parts[1] != '=':
				self._log('error in config file on line %d :' % num)
				self._log('    %s' % line)
				continue
			key, value = parts[0], parts[2]
			if key == 'upstream':
				self._upstream = value
			elif key == 'rebased':
				self._rebased = value
			elif key == 'current':
				self._mainline = value
			elif key == 'patches':
				self._patches = value

	def _restore_branches(self):
		git = self._repo.git
		for branch in (self._upstream, self._rebased, self._mainline, self._patches):
			git.checkout(branch, '-f')
			git.reset(self._saved_branches[branch], '--hard')

	def _save_branches(self):
		for branch in (self._upstream, self._rebased, self._mainline, self._patches):
			self._saved_branches[branch] = self._head(branch)
		self._saved_branches['prev_head'] = self._head(self._rebased)

	def _get_commits(self, upstream_repo):
		return [q.hexsha for q in self._repo.iter_commits(self._upstream + '..' + upstream_repo)]

	def _process_commits(self):
		tmp_file = tempfile.TemporaryFile()
		try:
			for i in range(self._id, len(self._commits)):
				self._process_commit(self._commits[i], tmp_file)
				self._id += 1
				self._cur_num += 1
				tmp_file.close()
				tmp_file = tempfile.TemporaryFile()
		except GitCommandError as e:
			self._save_state()
			self._log(self._fixup_merge_message(self._read_output(tmp_file)))
			self._log(e.stderr)
			raise RebaseFailed from e
		except PatchError as e:
			self._save_state()
			self._log_error(e.message)
			raise PatchFailed from e
		except BaseException:
			self._save_state()
			raise
		finally:
			tmp_file.close()

	def _process_commit(self, commit, output):
		self._log('[%d/%d] Applying commit: %s' %
			  (self._cur_num + 1, self._all_num, self._repo.commit(commit).summary))
		self._stage1(commit)
		diff_str = self._stage2(commit, output)
		self._stage3(commit, diff_str)
		self._save_repo_state(self._head(self._mainline) if diff_str else '')

	def _patch_tree(self, diff_str):
		with tempfile.TemporaryDirectory() as tmp_dir:
			patch = Path(tmp_dir) / '__patch__.patch'
			patch.write_bytes(diff_str + b'\n')
			self._repo.git.apply(str(patch))

	def _stage1(self, commit):
		git = self._repo.git
		self._state = MERGE_ST
		git.checkout(self._upstream)
		git.merge(commit)

	def _stage2(self, commit, output, rebase_cmd=None):
		git = self._repo.git
		self._state = REBASE_ST
		if rebase_cmd:
			git.rebase(rebase_cmd, output_stream=output, env=NO_EDITOR_ENV)
		else:
			git.checkout(self._rebased)
			self._saved_branches['prev_head'] = self._head(self._rebased)
			git.rebase(commit, output_stream=output, env=NO_EDITOR_ENV)
		return self._diff('--full-index', self._saved_branches['prev_head'], self._rebased)

	def _stage3(self, commit, diff_str, interactive=False, message=''):
		git = self._repo.git
		self._state = COMMIT_ST
		git.checkout(self._mainline)
		if not diff_str:
			self._log('Nothing to commit in branch current, skipping %s commit.' % commit)
			return
		git.clean('-d', '-f')
		try:
			self._patch_tree(diff_str)
		except GitCommandError:
			self._id += 1
			self._state = MERGE_ST
			raise PatchError('Error occurs during applying %s.\n'
					 'Fix error, commit and continue the process, please.' % commit)
		git.add('-A', str(self._work_dir))
		if interactive:
			res = subprocess.run(['git', 'commit', '-e', '-m',
					      'place your comments for %s branch commit' % self._mainline],
					     cwd=self._work_dir)
			if res.returncode != 0:
				raise GitCommandError('git commit', res.returncode, '')
		elif message:
			git.commit('-m', message)
		else:
			orig = self._repo.commit(commit)
			git.commit('-m', orig.message, '--author=%s <%s>' % (orig.author.name, orig.author.email))

	def _save_state(self):
		lines = [
			self._saved_branches[self._upstream],
			self._saved_branches[self._rebased],
			self._saved_branches[self._mainline],
			self._saved_branches[self._patches],
			self._saved_branches['prev_head'],
			str(self._state),
			str(self._all_num),
			str(self._cur_num),
		] + [str(c) for c in self._commits[self._id:]]
		self._state_path(STATE_FILE).write_text(''.join(line + '\n' for line in lines))

	def _load_state(self, remove=True):
		try:
			self._load_state_raised(remove)
		except (OSError, ValueError):
			self._log_error('State file is missed or corrupted: nothing to continue.')
			return False
		return True

	def _load_state_raised(self, remove):
		path = self._state_path(STATE_FILE)
		strs = [q.split()[0] for q in path.read_text().splitlines() if q.split()]
		if len(strs) < 8:
			raise ValueError('state file is too short')
		self._saved_branches[self._upstream] = strs[0]
		self._saved_branches[self._rebased] = strs[1]
		self._saved_branches[self._mainline] = strs[2]
		self._saved_branches[self._patches] = strs[3]
		self._saved_branches['prev_head'] = strs[4]
		self._state = int(strs[5])
		self._all_num = int(strs[6])
		self._cur_num = int(strs[7])
		self._commits.extend(strs[8:])
		if remove:
			path.unlink()

	def _log_error(self, mess):
		if self._with_log and mess:
			print('error: %s' % mess)

	def _log(self, mess):
		if self._with_log and mess:
			print(mess)

	def _log_unexpected_head(self, mainline, wrong, right):
		self._log_error('You have an unexpected HEAD of %s branch (%s instead of %s).' %
				(mainline, wrong, right))

	def _init_merge(self):
		self._state = START_ST
		self._id = 0
		self._cur_num = 0
		self._all_num = 0
		self._commits = []
		self._saved_branches = {}

	def _apply_patch_am(self, directory, patch, **kwargs):
		self._log('Applying patch: ' + patch)
		self._repo.git.am('--keep-cr', str(Path(directory) / patch), **kwargs)

	def _diffapply(self, diff, message):
		try:
			if diff:
				self._log('Applying result diff between %s and %s' % (self._mainline, self._rebased))
				self._stage3('update current', diff, interactive=not message, message=message)
		except PatchError as e:
			self._save_state()
			self._log_error(e.message)
			raise PatchFailed from e
		except BaseException:
			self._save_state()
			raise
		self._save_repo_state(self._mainline if diff else '', message)
