#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# gitum - Git Upstream Manager.
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

from git import *
from subprocess import Popen, call
import os
import tempfile
import sys
import shutil
from errors import *
from constants import *

START_ST = 0
MERGE_ST = 1
REBASE_ST = 2
COMMIT_ST = 3

CONFIG_FILE = '.gitum-config'
CONFIG_BRANCH = 'gitum-config'
STATE_FILE = '.git/.gitum-state'
REMOTE_REPO = '.git/.gitum-remote'
MERGE_BRANCH = '.git/.gitum-mbranch'
CURRENT_REBASED = '.git/.curent_rebased'
CURRENT_MAINLINE = '.git/.curent_mainline'
UPSTREAM_COMMIT_FILE = '_upstream_commit_'
LAST_PATCH_FILE = '_current_patch_'
TMP_LAST_PATCH_FILE = '_current.patch'

class GitUpstream(object):
	def __init__(self, repo_path='.', with_log=False, new_repo=False):
		if new_repo:
			self._repo = Repo.init(repo_path)
		else:
			self._repo = Repo(repo_path)
		self._with_log = with_log

	def repo(self):
		return self._repo

	def merge(self, mbranch=None, track_with=None):
		self._init_merge()
		if self._repo.is_dirty():
			self._log_error('You have local changes. Run git commit and gitum update to save them, please.')
			raise RepoIsDirty
		self._load_config()
		self._check_mainline()
		if self._repo.git.diff(self._rebased, self._mainline, stdout_as_string=False) != '':
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
		except:
			self._log_error('Can not merge from %s - not exists.' % mbranch)
			raise NoMergeBranch
		self._commits = self._get_commits(mbranch)
		if len(self._commits) == 0:
			self._log('Repository is up to date - nothing to merge.')
			return
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._save_branches()
		self._process_commits()
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully updated work branches.')

	def abort(self, am=False):
		self._init_merge()
		self._load_config()
		if not self._load_state():
			raise NoStateFile
		try:
			if not am:
				self._repo.git.rebase('--abort')
			else:
				self._repo.git.am('--abort')
		except:
			pass
		self._restore_branches()
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Restored work branches.')

	def continue_merge(self, rebase_cmd):
		self._init_merge()
		self._load_config()
		if not self._load_state():
			raise NoStateFile
		if self._state == REBASE_ST:
			tmp_file = tempfile.TemporaryFile()
			try:
				diff_str = self._stage2(self._commits[self._id], tmp_file, rebase_cmd)
				self._stage3(self._commits[self._id], diff_str)
				self._save_repo_state(
					self._repo.branches[self._mainline].commit.hexsha if diff_str else ''
				)
				self._id += 1
				self._cur_num += 1
			except GitCommandError as e:
				self._save_state()
				tmp_file.seek(0)
				self._log(self._fixup_merge_message(''.join(tmp_file.readlines())))
				self._log(e.stderr)
				raise RebaseFailed
			except PatchError as e:
				self._save_state()
				self._log_error(e.message)
				raise PatchFailed
			except:
				self._save_state()
				raise
		elif self._state != MERGE_ST:
			self._log_error("Don't support continue not from merge or rebase mode.")
			raise NotSupported
		self._process_commits()
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully updated work branches.')

	def status(self):
		self._load_config()
		diff = self._repo.git.diff('--full-index', self._mainline, self._rebased, stdout_as_string=False)
		ca = self._find_ca(self._load_current_rebased(), self._rebased)
		self._check_mainline()
		if self._load_current_rebased() == self._repo.branches[self._rebased].commit.hexsha:
			self._log('Nothing to update.')
			return
		if ca == self._load_current_rebased():
			new_commits = [i for i in self._repo.iter_commits(ca + '..' + self._rebased)]
			new_commits.reverse()
			self._log('Have new commits, run gitum update to save them:')
			for c_id in new_commits:
				self._log('\t%s' % c_id.summary)
		else:
			self._log('Existing patches were modified.')
			self._log('Run gitum update to save the result diff:\n%s' % diff)

	def update(self, message=''):
		if self._repo.is_dirty():
			self._log_error('You have local changes. Commit them and try again, please.')
			raise RepoIsDirty
		self._init_merge()
		self._load_config()
		self._check_mainline()
		current_rebased = self._load_current_rebased()
		if current_rebased == self._repo.branches[self._rebased].commit.hexsha:
			self._log('Nothing to update.')
			return
		diff = self._repo.git.diff('--full-index', self._mainline, self._rebased, stdout_as_string=False)
		ca = self._find_ca(current_rebased, self._rebased)
		if ca == current_rebased:
			new_commits = [i for i in self._repo.iter_commits(ca + '..' + self._rebased)]
			new_commits.reverse()
			for c_id in new_commits:
				self._log('Applying commit: %s' % c_id.summary)
				self._repo.git.checkout(self._mainline)
				self._repo.git.cherry_pick(c_id.hexsha)
				self._save_repo_state(self._mainline if diff else '', message, c_id.hexsha)
		else:
			try:
				if diff:
					self._log('Applying result diff between %s and %s' % (self._mainline, self._rebased))
					interactive = False if message else True
					self._stage3('update current', diff, interactive, message)
			except PatchError as e:
				self._save_state()
				self._log_error(e.message)
				raise PatchFailed
			except:
				self._save_state()
				raise
			self._save_repo_state(self._mainline if diff else '', message)
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully updated work branches.')

	def create(self, remote, upstream, rebased, mainline, patches):
		config = True
		if upstream == UPSTREAM_BRANCH and rebased == REBASED_BRANCH \
		   and mainline == MAINLINE_BRANCH and patches == PATCHES_BRANCH:
			config = False
		if self._has_branch(mainline):
			self._log_error("%s branch exists." % mainline)
			raise BranchExists
		if self._has_branch(rebased):
			self._log_error("%s branch exists." % rebased)
			raise BranchExists
		if self._has_branch(patches):
			self._log_error("%s branch exists." % patches)
			raise BranchExists
		if config and self._has_branch(CONFIG_BRANCH):
			self._log_error("%s branch exists." % CONFIG_BRANCH)
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
		if self._has_branch(self._mainline):
			self._repo.delete_head(self._mainline, '-D')
		if self._has_branch(self._rebased):
			self._repo.delete_head(self._rebased, '-D')
		if self._has_branch(self._patches):
			self._repo.delete_head(self._patches, '-D')
		try:
			self._repo.delete_head(CONFIG_BRANCH, '-D')
		except:
			pass
		self._log('Successfully removed work branches.')

	def remove_config_files(self):
		for name in [STATE_FILE, REMOTE_REPO, MERGE_BRANCH, CURRENT_REBASED, CURRENT_MAINLINE]:
			if os.path.exists(self._repo.working_dir + '/' + name):
				os.unlink(self._repo.working_dir + '/' + name)
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
		ok = False
		for i in self._repo.iter_commits(commit):
			commits.append(i.hexsha)
			if i.message.startswith('gitum-patches: begin'):
				ok = True
				break
		if not ok:
			self._log_error('Broken %s commit.' % commit)
			raise BrokenRepo
		commits.reverse()
		git = self._repo.git
		start = commits[0]
		commits = commits[1:]
		git.checkout(start)
		with open(self._repo.working_tree_dir + '/' + UPSTREAM_COMMIT_FILE) as f:
			tmp_list = f.readlines()
			if len(tmp_list) > 1:
				self._log_error('Broken upstream commit file.')
				raise BrokenRepo
			upstream_commit = tmp_list[0]
		git.checkout(upstream_commit)
		tmp_dir = tempfile.mkdtemp()
		saved_commit_id = self._repo.head.commit.hexsha
		for i in commits:
			git.checkout(i)
			for j in os.listdir(self._repo.working_tree_dir):
				if j.endswith('.patch'):
					shutil.copy(self._repo.working_tree_dir + '/' + j, tmp_dir + '/' + j)
			shutil.copy(self._repo.working_tree_dir + '/' + LAST_PATCH_FILE, tmp_dir + '/' + LAST_PATCH_FILE)
			with open(self._repo.working_tree_dir + '/' + UPSTREAM_COMMIT_FILE) as f:
				tmp_list = f.readlines()
				if len(tmp_list) > 1:
					self._log_error('Broken upstream commit file.')
					raise BrokenRepo
				upstream_commit = tmp_list[0]
			git.checkout(saved_commit_id)
			patch_exists = False
			with open(tmp_dir + '/' + LAST_PATCH_FILE) as f:
				if f.readlines():
					patch_exists = True
			if patch_exists:
				git.am(tmp_dir + '/' + LAST_PATCH_FILE)
			os.unlink(tmp_dir + '/' + LAST_PATCH_FILE)
			saved_commit_id = self._repo.head.commit.hexsha
		if self._has_branch(self._mainline):
			self._repo.delete_head(self._mainline, '-D')
		self._repo.create_head(self._mainline)
		git.checkout(upstream_commit)
		if self._has_branch(self._upstream):
			self._repo.delete_head(self._upstream, '-D')
		self._repo.create_head(self._upstream)
		if self._has_branch(self._rebased):
			self._repo.delete_head(self._rebased, '-D')
		self._repo.create_head(self._rebased)
		git.checkout(self._rebased)
		patches_to_apply = [i for i in os.listdir(tmp_dir) if i.endswith('.patch')]
		patches_to_apply.sort()
		for i in patches_to_apply:
			git.am(tmp_dir + '/' + i)
		shutil.rmtree(tmp_dir)
		git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully restored work branches to %s commit from %s branch.' % (commit, self._patches))

	def clone(self, remote_repo):
		if not remote_repo:
			self._log_error('Specify remote repo, please.')
			raise NoGitumRemote
		if remote_repo[0] != '/' and not self._has_hostname(remote_repo):
			remote_repo = os.getcwd() + '/' + remote_repo
		self._repo.git.remote('add', 'origin', remote_repo)
		self._repo.git.fetch('origin')
		try:
			self._repo.remotes['origin'].refs[CONFIG_BRANCH]
			self._repo.git.checkout('-b', CONFIG_BRANCH, 'origin/' + CONFIG_BRANCH)
		except:
			pass
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
		cur = self._repo.branches[self._patches].commit.hexsha
		self._repo.git.fetch(remote)
		self._repo.git.checkout(self._upstream, '-f')
		self._repo.git.reset(remote + '/' + self._upstream, '--hard')
		self._repo.git.checkout(self._patches, '-f')
		self._repo.git.reset(remote + '/' + self._patches, '--hard')
		self._repo.git.checkout(self._mainline, '-f')
		self._repo.git.reset(remote + '/' + self._mainline, '--hard')
		self._gen_rebased()
		self._log('Reset work branches to the remote state, applying our commits on top...')
		self._repo.git.checkout(self._mainline)
		previd = self._find_ca(remote + '/' + self._patches, cur)
		self._commits = [q.hexsha for q in self._repo.iter_commits(previd + '..' + cur)]
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._pull_commits()
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully updated work branches.')

	def continue_pull(self, command):
		self._load_config()
		self._init_merge()
		if not self._load_state():
			raise NoStateFile
		try:
			tmp_file = tempfile.TemporaryFile()
			self._repo.git.am(command, output_stream=tmp_file)
			if command == '--resolved':
				self._repo.git.checkout(self._rebased)
				self._repo.git.cherry_pick(self._mainline)
				self._save_repo_state(self._mainline)
			self._repo.git.checkout(self._upstream, '-f')
			self._repo.git.merge(
				self._repo.git.show(
					self._commits[self._id] + ':' + UPSTREAM_COMMIT_FILE,
					stdout_as_string=False
				)
			)
			self._repo.git.checkout(self._mainline)
			self._id += 1
			self._cur_num += 1
		except GitCommandError as e:
			self._save_state()
			tmp_file.seek(0)
			self._log(self._fixup_pull_message(''.join(tmp_file.readlines())))
			self._log(e.stderr)
			raise RebaseFailed
		except:
			self._save_state()
			raise
		self._pull_commits()
		self._repo.git.checkout(self._rebased)
		self._save_current_rebased(self._rebased)
		self._save_current_mainline(self._mainline)
		self._log('Successfully updated work branches.')

	def push(self, remote=None, track_with=None):
		self._load_config()
		self._check_mainline()
		if not remote:
			remote = self._load_remote()
		if track_with:
			self._save_remote(remote)
		self._repo.git.push(remote, self._upstream, self._mainline, self._patches)
		exist = False
		if self._has_branch(CONFIG_BRANCH):
			exist = True
		if exist:
			self._repo.git.push(remote, CONFIG_BRANCH)
		self._log('Successfully pushed work branches.')

	def _has_branch(self, head):
		return self._repo.branches.count(Head(head, "refs/heads/" + head, True)) == 1

	def _has_hostname(self, repo_path):
		if repo_path.find(':') == -1:
			return False
		return True

	def _gen_rebased(self, commit=''):
		if not commit:
			commit = self._patches
		self._repo.git.checkout(commit)
		tmp_dir = tempfile.mkdtemp()
		for j in os.listdir(self._repo.working_tree_dir):
			if j.endswith('.patch'):
				shutil.copy(self._repo.working_tree_dir + '/' + j, tmp_dir + '/' + j)
		if self._has_branch(self._rebased):
			self._repo.delete_head(self._rebased, '-D')
		self._repo.git.checkout('-b', self._rebased,
			self._repo.git.show(
				commit + ':' + UPSTREAM_COMMIT_FILE,
				stdout_as_string=False
			)
		)
		patches_to_apply = [i for i in os.listdir(tmp_dir) if i.endswith('.patch')]
		patches_to_apply.sort()
		for i in patches_to_apply:
			self._repo.git.am(tmp_dir + '/' + i)
		shutil.rmtree(tmp_dir)

	def _find_ca(self, c1, c2):
		return self._repo.git.merge_base(c1, c2)

	def _save_parm(self, filename, parm):
		with open(self._repo.working_dir + '/' + filename, 'w') as f:
			f.write(parm)

	def _load_parm(self, filename):
		with open(self._repo.working_dir + '/' + filename) as f:
			parm = f.readline().strip()
		return parm

	def _save_remote(self, remote):
		self._save_parm(REMOTE_REPO, remote)

	def _load_remote(self):
		try:
			return self._load_parm(REMOTE_REPO)
		except:
			self._log_error('Specify a remote gitum repository, please.')
			raise NoGitumRemote

	def _save_mbranch(self, mbranch):
		self._save_parm(MERGE_BRANCH, mbranch)

	def _load_mbranch(self):
		try:
			return self._load_parm(MERGE_BRANCH)
		except:
			self._log_error('Specify a merge branch, please.')
			raise NoMergeBranch

	def _save_current_rebased(self, rebased):
		self._save_parm(CURRENT_REBASED, self._repo.branches[rebased].commit.hexsha)

	def _load_current_rebased(self):
		return self._load_parm(CURRENT_REBASED)

	def _save_current_mainline(self, mainline):
		self._save_parm(CURRENT_MAINLINE, self._repo.branches[mainline].commit.hexsha)

	def _load_current_mainline(self):
		return self._load_parm(CURRENT_MAINLINE)

	def _check_mainline(self):
		current_mainline = self._load_current_mainline()
		if current_mainline != self._repo.branches[self._mainline].commit.hexsha:
			self._log_unexpected_head(self._mainline,
						  self._repo.branches[self._mainline].commit.hexsha,
						  current_mainline)
			raise RepoIsDirty

	def _get_commit_name_from_patch(self, lines):
		for i in lines.split('\n'):
			parts = i.split('Subject: [PATCH] ')
			if len(parts) == 2:
				return parts[1]
		return ''

	def _pull_commits(self):
		tmp_file = tempfile.TemporaryFile()
		try:
			for q in xrange(self._id, len(self._commits)):
				lines = self._repo.git.show(
						self._commits[q] + ':' + LAST_PATCH_FILE,
						stdout_as_string=False
					)
				if len(lines) > 0:
					tmp_dir = tempfile.mkdtemp()
					with open(tmp_dir + '/' + TMP_LAST_PATCH_FILE, 'w') as f:
						f.write(lines)
					self._log('Applying commit: %s' % self._get_commit_name_from_patch(lines))
					self._repo.git.am('-3', tmp_dir + '/' + TMP_LAST_PATCH_FILE,
							  output_stream=tmp_file)
					self._repo.git.checkout(self._rebased)
					self._repo.git.cherry_pick(self._mainline)
					self._save_repo_state(self._mainline)
					shutil.rmtree(tmp_dir)
				self._repo.git.checkout(self._upstream)
				self._repo.git.merge(
					self._repo.git.show(
						self._commits[q] + ':' + UPSTREAM_COMMIT_FILE,
						stdout_as_string=False
					)
				)
				self._repo.git.checkout(self._mainline)
				tmp_file.close()
				tmp_file = tempfile.TemporaryFile()
				self._id += 1
				self._cur_num += 1
		except GitCommandError as e:
			self._save_state()
			tmp_file.seek(0)
			self._log(self._fixup_pull_message(''.join(tmp_file.readlines())))
			self._log(e.stderr)
			raise RebaseFailed
		except:
			self._save_state()
			raise

	def _save_patches(self, patches, upstream):
		# create blob
		tmp_dir = tempfile.mkdtemp()
		with open(tmp_dir + '/' + UPSTREAM_COMMIT_FILE, 'w') as f:
			f.write(self._repo.branches[upstream].commit.hexsha)
		blob = self._repo.git.hash_object('-w', tmp_dir + '/' + UPSTREAM_COMMIT_FILE)
		# create tree
		in_file = tempfile.TemporaryFile()
		out_file = tempfile.TemporaryFile()
		in_file.write(('100644 blob %s\t' + UPSTREAM_COMMIT_FILE) % blob)
		in_file.seek(0)
		proc = Popen(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
			      '--work-tree=' + self._repo.working_tree_dir, 'mktree'],
			      stdin=in_file, stdout=out_file)
		status = proc.wait()
		if status != 0:
			self._log(status)
		out_file.seek(0)
		tree=out_file.readline().strip()
		in_file.close()
		out_file.close()
		# create commit
		in_file = tempfile.TemporaryFile()
		in_file.write('gitum-patches: begin')
		in_file.seek(0)
		out_file = tempfile.TemporaryFile()
		proc = Popen(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
			      '--work-tree=' + self._repo.working_tree_dir, 'commit-tree', tree],
			      stdin=in_file, stdout=out_file)
		status = proc.wait()
		if status != 0:
			self._log(status)
		out_file.seek(0)
		commit=out_file.readline().strip()
		self._repo.git.branch(patches, commit)
		shutil.rmtree(tmp_dir)

	def _save_config(self, mainline, upstream, rebased, patches):
		# create blob
		tmp_dir = tempfile.mkdtemp()
		with open(tmp_dir + '/' + CONFIG_FILE, 'w') as f:
			f.write('current = %s\n' % mainline)
			f.write('upstream = %s\n' % upstream)
			f.write('rebased = %s\n' % rebased)
			f.write('patches = %s\n' % patches)
		blob = self._repo.git.hash_object('-w', tmp_dir + '/' + CONFIG_FILE)
		# create tree
		in_file = tempfile.TemporaryFile()
		out_file = tempfile.TemporaryFile()
		in_file.write(('100644 blob %s\t' + CONFIG_FILE) % blob)
		in_file.seek(0)
		proc = Popen(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
			      '--work-tree=' + self._repo.working_tree_dir, 'mktree'],
			      stdin=in_file, stdout=out_file)
		status = proc.wait()
		if status != 0:
			self._log(status)
		out_file.seek(0)
		tree=out_file.readline().strip()
		in_file.close()
		out_file.close()
		# create commit
		in_file = tempfile.TemporaryFile()
		in_file.write('Save config file')
		in_file.seek(0)
		out_file = tempfile.TemporaryFile()
		proc = Popen(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
			      '--work-tree=' + self._repo.working_tree_dir, 'commit-tree', tree],
			      stdin=in_file, stdout=out_file)
		status = proc.wait()
		if status != 0:
			self._log(status)
		out_file.seek(0)
		commit=out_file.readline().strip()
		self._repo.git.branch(CONFIG_BRANCH, commit)
		shutil.rmtree(tmp_dir)

	def _save_repo_state(self, commit, message='', cur_rebased=None):
		mainline_c = commit if commit else self._mainline
		rebased_c = cur_rebased if cur_rebased else self._rebased
		if self._repo.git.diff(rebased_c, mainline_c, stdout_as_string=False) != '':
			self._log_error('%s and %s work trees are not equal - can\'t save state!' %
					(rebased_c, mainline_c))
			raise NotUptodate
		# create tmp dir
		tmp_dir = tempfile.mkdtemp()
		git = self._repo.git
		# generate new patches
		for i in os.listdir(self._repo.working_tree_dir):
			if i.endswith('.patch'):
				os.unlink(self._repo.working_tree_dir + '/' + i)
		git.format_patch('%s..%s' % (self._upstream, rebased_c))
		# move patches to tmp dir
		for i in os.listdir(self._repo.working_tree_dir):
			if i.endswith('.patch'):
				shutil.move(self._repo.working_tree_dir + '/' + i,
					    tmp_dir + '/' + i)
		# get mainline branch commit
		if commit:
			git.format_patch('%s^..%s' % (commit, commit))
		else:
			with open(self._repo.working_tree_dir + '/' + TMP_LAST_PATCH_FILE, 'w') as f:
				pass
		# move it to tmp dir
		for i in os.listdir(self._repo.working_tree_dir):
			if i.endswith('.patch'):
				shutil.move(self._repo.working_tree_dir + '/' + i,
					    tmp_dir + '/' + LAST_PATCH_FILE)
		git.checkout(self._patches, '-f')
		# remove old patches from patches branch
		git.rm(self._repo.working_tree_dir + '/*.patch', '--ignore-unmatch')
		# move new patches from tmp dir to patches branch
		for i in os.listdir(tmp_dir):
			if i.endswith('.patch'):
				shutil.move(tmp_dir + '/' + i, self._repo.working_tree_dir + '/' + i)
		shutil.move(tmp_dir + '/' + LAST_PATCH_FILE,
			    self._repo.working_tree_dir + '/' + LAST_PATCH_FILE)
		# update upstream head
		with open(self._repo.working_tree_dir + '/' + UPSTREAM_COMMIT_FILE, 'w') as f:
			f.write(self._repo.branches[self._upstream].commit.hexsha)
		# commit the result
		git.add(self._repo.working_tree_dir)
		mess = message
		if not mess and commit:
			mess = self._repo.commit(commit).message.encode('utf-8')
		if not mess:
			mess = '%s branch updated without code changes' % self._rebased
		if commit:
			author = self._repo.commit(commit).author
			git.commit('-m', mess, '--author="%s <%s>"' % (author.name.encode('utf-8'), author.email))
		else:
			git.commit('-m', mess)
		shutil.rmtree(tmp_dir)

	def _fixup_merge_message(self, mess):
		mess = mess.replace('git rebase --continue', 'gitum merge --continue')
		mess = mess.replace('git rebase --abort', 'gitum merge --abort')
		mess = mess.replace('git rebase --skip', 'gitum merge --skip')
		return mess

	def _fixup_pull_message(self, mess):
		mess = mess.replace('git am --resolved', 'gitum pull --resolved')
		mess = mess.replace('git am --abort', 'gitum pull --abort')
		mess = mess.replace('git am --skip', 'gitum pull --skip')
		return mess

	def _load_config(self):
		# set defaults
		self._upstream = UPSTREAM_BRANCH
		self._rebased = REBASED_BRANCH
		self._mainline = MAINLINE_BRANCH
		self._patches = PATCHES_BRANCH
		# load config
		try:
			lines = self._repo.git.show(
				CONFIG_BRANCH + ':' + CONFIG_FILE,
				stdout_as_string=False
			).split('\n')
		except:
			return
		num = 0
		for i in lines:
			num += 1
			parts = i.split('#')[0].strip().split(' ')
			if len(parts) != 3 or parts[1] != '=':
				self._log('error in config file on line %d :' % num)
				self._log('    %s' % i)
			if parts[0] == 'upstream':
				self._upstream = parts[2]
			elif parts[0] == 'rebased':
				self._rebased = parts[2]
			elif parts[0] == 'current':
				self._mainline = parts[2]
			elif parts[0] == 'patches':
				self._patches = parts[2]

	def _restore_branches(self):
		git = self._repo.git
		git.checkout(self._upstream, '-f')
		git.reset(self._saved_branches[self._upstream], '--hard')
		git.checkout(self._rebased, '-f')
		git.reset(self._saved_branches[self._rebased], '--hard')
		git.checkout(self._mainline, '-f')
		git.reset(self._saved_branches[self._mainline], '--hard')
		git.checkout(self._patches, '-f')
		git.reset(self._saved_branches[self._patches], '--hard')

	def _save_branches(self):
		git = self._repo.git
		self._saved_branches[self._upstream] = self._repo.branches[self._upstream].commit.hexsha
		self._saved_branches[self._rebased] = self._repo.branches[self._rebased].commit.hexsha
		self._saved_branches[self._mainline] = self._repo.branches[self._mainline].commit.hexsha
		self._saved_branches[self._patches] = self._repo.branches[self._patches].commit.hexsha
		self._saved_branches['prev_head'] = self._repo.branches[self._rebased].commit.hexsha

	def _get_commits(self, upstream_repo):
		return [q.hexsha for q in self._repo.iter_commits(self._upstream + '..' + upstream_repo)]

	def _process_commits(self):
		tmp_file = tempfile.TemporaryFile()
		try:
			for i in xrange(self._id, len(self._commits)):
				self._process_commit(self._commits[i], tmp_file)
				self._id += 1
				self._cur_num += 1
				tmp_file.close()
				tmp_file = tempfile.TemporaryFile()
		except GitCommandError as e:
			self._save_state()
			tmp_file.seek(0)
			self._log(self._fixup_merge_message(''.join(tmp_file.readlines())))
			self._log(e.stderr)
			raise RebaseFailed
		except PatchError as e:
			self._save_state()
			self._log_error(e.message)
			raise PatchFailed
		except:
			self._save_state()
			raise

	def _process_commit(self, commit, output):
		self._log("[%d/%d] Applying commit: %s" % \
			  (self._cur_num + 1, self._all_num,
			   self._repo.commit(commit).summary))
		self._stage1(commit)
		diff_str = self._stage2(commit, output)
		self._stage3(commit, diff_str)
		self._save_repo_state(self._repo.branches[self._mainline].commit.hexsha if diff_str else '')

	def _patch_tree(self, diff_str):
		status = 0
		tmp_dir = tempfile.mkdtemp()
		with open(tmp_dir + '/__patch__.patch', 'w') as f:
			f.write(diff_str + '\n')
		self._repo.git.apply(tmp_dir + '/__patch__.patch')
		shutil.rmtree(tmp_dir)

	def _stage1(self, commit):
		git = self._repo.git
		self._state = MERGE_ST
		git.checkout(self._upstream)
		git.merge(commit)

	def _stage2(self, commit, output, rebase_cmd=None, interactive=False):
		git = self._repo.git
		self._state = REBASE_ST
		if rebase_cmd:
			if interactive:
				res = call(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
					    '--work-tree=' + self._repo.working_tree_dir, 'rebase', rebase_cmd], stderr=output)
				if res != 0:
					raise GitCommandError('git rebase %s' % rebase_cmd, res, '')
			else:
				git.rebase(rebase_cmd, output_stream=output)
		else:
			git.checkout(self._rebased)
			self._saved_branches['prev_head'] = self._repo.branches[self._rebased].commit.hexsha
			if interactive:
				res = call(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
					    '--work-tree=' + self._repo.working_tree_dir, 'rebase', '-i', commit], stderr=output)
				if res != 0:
					raise GitCommandError('git rebase', res, '')
			else:
				git.rebase(commit, output_stream=output)
		diff_str = self._repo.git.diff('--full-index', self._saved_branches['prev_head'], self._rebased, stdout_as_string=False)
		return diff_str

	def _stage3(self, commit, diff_str, interactive=False, message=''):
		git = self._repo.git
		self._state = COMMIT_ST
		git.checkout(self._mainline)
		if diff_str == "":
			self._log('Nothing to commit in branch current, skipping %s commit.' % commit)
			return
		git.clean('-d', '-f')
		try:
			self._patch_tree(diff_str)
		except:
			self._id += 1
			self._state = MERGE_ST
			raise PatchError('Error occurs during applying %s.\n'
					 'Fix error, commit and continue the process, please.' % commit)
		git.add('-A', self._repo.working_tree_dir)
		if interactive:
			res = call(['git', '--git-dir=' + self._repo.working_dir + '/.git/',
				    '--work-tree=' + self._repo.working_tree_dir, 'commit', '-e', '-m',
				    'place your comments for %s branch commit' % self._mainline])
			if res != 0:
				raise GitCommandError('git commit', res, '')
		else:
			if not message:
				mess = self._repo.commit(commit).message.encode('utf-8')
				author = self._repo.commit(commit).author
				git.commit('-m', mess, '--author="%s <%s>"' % (author.name.encode('utf-8'), author.email))
			else:
				git.commit('-m', message)

	def _save_state(self):
		with open(self._repo.working_dir + '/' + STATE_FILE, 'w') as f:
			f.write(self._saved_branches[self._upstream] + '\n')
			f.write(self._saved_branches[self._rebased] + '\n')
			f.write(self._saved_branches[self._mainline] + '\n')
			f.write(self._saved_branches[self._patches] + '\n')
			f.write(self._saved_branches['prev_head'] + '\n')
			f.write(str(self._state) + '\n')
			f.write(str(self._all_num) + '\n')
			f.write(str(self._cur_num) + '\n')
			for i in xrange(self._id, len(self._commits)):
				f.write(str(self._commits[i]) + '\n')

	def _load_state(self, remove=True):
		ret = True
		try:
			self._load_state_raised(remove)
		except IOError:
			self._log_error('State file is missed or corrupted: nothing to continue.')
			ret = False
		return ret

	def _load_state_raised(self, remove):
		with open(self._repo.working_dir + '/' + STATE_FILE, 'r') as f:
			strs = [q.split()[0] for q in f.readlines() if len(q.split()) > 0]
		if len(strs) < 6:
			raise IOError
		self._saved_branches[self._upstream] = strs[0]
		self._saved_branches[self._rebased] = strs[1]
		self._saved_branches[self._mainline] = strs[2]
		self._saved_branches[self._patches] = strs[3]
		self._saved_branches['prev_head'] = strs[4]
		self._state = int(strs[5])
		self._all_num = int(strs[6])
		self._cur_num = int(strs[7])
		for i in xrange(8, len(strs)):
			self._commits.append(strs[i])
		if remove:
			os.unlink(self._repo.working_dir + '/' + STATE_FILE)

	def _log_error(self, mess):
		if self._with_log and mess:
			print('error: %s' % mess)

	def _log(self, mess):
		if self._with_log and mess:
			print(mess)

	def _log_unexpected_head(self, mainline, wrong, right):
		self._log_error('You have an unexpected HEAD of %s branch (%s instead of %s).' % \
				(mainline, wrong, right))

	def _init_merge(self):
		self._state = START_ST
		self._id = 0
		self._cur_num = 0
		self._all_num = 0
		self._commits = []
		self._saved_branches = {}
