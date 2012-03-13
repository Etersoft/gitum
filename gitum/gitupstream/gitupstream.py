#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
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

from git import *
from subprocess import Popen, call
import os
import tempfile
import sys
import shutil
from errors import *

START_ST = 0
MERGE_ST = 1
REBASE_ST = 2
COMMIT_ST = 3

CONFIG_FILE = '.gitum-config'
CONFIG_BRANCH = 'gitum-config'
STATE_FILE = '.git/.gitum-state'
REMOTE_REPO = '.git/.gitum-remote'

GITUM_TMP_DIR = '/tmp/gitum'
GITUM_PATCHES_DIR = 'gitum-patches'

class GitUpstream(object):
	def __init__(self, repo_path='.', with_log=False, new_repo=False):
		self._repo_path = repo_path
		if new_repo:
			self._repo = Repo.init(repo_path)
		else:
			self._repo = Repo(repo_path)
		self._with_log = with_log

	def repo(self):
		return self._repo

	def merge(self, branch=None):
		self._init_merge()
		if self._repo.is_dirty():
			self._log('Repository is dirty - can not merge!')
			raise RepoIsDirty
		self._load_config()
		if self._repo.git.diff(self._rebased, self._current) != '':
			self._log('%s and %s work trees are not equal - can not merge!' % \
					(self._rebased, self._current))
			raise NotUptodate
		if branch:
			self._remote = branch
		if len(self._remote.split('/')) == 2:
			self._repo.git.fetch(self._remote.split('/')[0])
		self._commits = self._get_commits()
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._save_branches()
		self._process_commits()
		self._repo.git.checkout(self._rebased)

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
					self._repo.branches[self._current].commit.hexsha if diff_str else ''
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
				self._log(e.message)
				raise PatchFailed
			except:
				self._save_state()
				raise
		elif self._state != MERGE_ST:
			self._log("Don't support continue not from merge or rebase mode!")
			raise NotSupported
		self._process_commits()
		self._repo.git.checkout(self._rebased)

	def update(self, message=''):
		if self._repo.is_dirty():
			self._log('Repository is dirty - can not update!')
			raise RepoIsDirty
		self._load_config()
		diff = self._repo.git.diff(self._current, self._rebased)
		if diff == '':
			self._log('%s and %s work trees are equal - nothing to update!' % \
				  (self._rebased, self._current))
			raise NotUptodate
		git = self._repo.git
		git.stash()
		interactive = False if message else True
		self._stage3('update result', diff, interactive, message)
		self._save_repo_state(self._current)
		git.checkout(self._rebased)
		try:
			git.stash('pop')
		except:
			pass

	def edit_patch(self, command=None):
		if command == '--commit':
			return self._update_current()
		if command == '--abort':
			return self.abort()
		self._init_merge()
		if not command and self._repo.is_dirty():
			self._log('Repository is dirty - can not edit patch!')
			raise RepoIsDirty
		self._load_config()
		if self._repo.git.diff(self._rebased, self._current) != '':
			self._log('%s and %s work trees are not equal - can not edit patch!' % \
					(self._rebased, self._current))
			raise NotUptodate
		if not command:
			self._save_branches()
			self._save_state()
		elif not self._load_state(False):
			raise NoStateFile
		tmp_file = tempfile.TemporaryFile()
		try:
			self._stage2(self._upstream, tmp_file, command, True)
		except GitCommandError as e:
			self._log(e.stderr)
		except:
			self._save_state()
			raise
		tmp_file.seek(0)
		self._log(self._fixup_editpatch_message(''.join(tmp_file.readlines())))
		self._save_state()

	def create(self, remote, current, upstream, rebased, patches):
		git = self._repo.git
		try:
			self._repo.branches[upstream]
		except:
			self._repo.create_head(upstream)
		try:
			self._repo.branches[current]
		except:
			self._repo.create_head(current)
		try:
			self._repo.delete_head(self._repo.branches[rebased], '-D')
		except:
			pass
		git.checkout(current)
		self._repo.create_head(rebased)
		try:
			self._repo.branches[patches]
		except:
			self._repo.create_head(patches)
			git.checkout(patches)
			patches_dir = self._repo_path + '/' + GITUM_PATCHES_DIR
			shutil.rmtree(patches_dir, ignore_errors=True)
			os.mkdir(patches_dir)
			with open(patches_dir + '/_upstream_commit_', 'w') as f:
				f.write(self._repo.branches[upstream].commit.hexsha)
			git.add(patches_dir)
			git.commit('-m', 'gitum-patches: begin')
		try:
			self._repo.branches[CONFIG_BRANCH]
		except:
			self._repo.create_head(CONFIG_BRANCH)
		self._save_config(remote, current, upstream, rebased, patches)
		git.checkout(rebased)

	def remove_branches(self):
		self._load_config()
		self._repo.git.checkout(self._upstream, '-f')
		self._repo.delete_head(self._current, '-D')
		self._repo.delete_head(self._rebased, '-D')
		self._repo.delete_head(self._patches, '-D')
		self._repo.delete_head(CONFIG_BRANCH, '-D')

	def remove_config_files(self):
		try:
			os.unlink(STATE_FILE)
		except:
			pass

	def remove_all(self):
		self.remove_branches()
		self.remove_config_files()

	def restore(self, commit, rebased_only=False):
		self._load_config()
		if rebased_only:
			return self._gen_rebased(commit)
		commits = []
		ok = False
		for i in self._repo.iter_commits(commit):
			commits.append(i.hexsha)
			if i.message.startswith('gitum-patches: begin'):
				ok = True
				break
		if not ok:
			self._log('broken %s commit' % commit)
			raise BrokenRepo
		commits.reverse()
		git = self._repo.git
		start = commits[0]
		commits = commits[1:]
		git.checkout(start)
		patches_dir = self._repo_path + '/' + GITUM_PATCHES_DIR
		with open(patches_dir + '/_upstream_commit_') as f:
			tmp_list = f.readlines()
			if len(tmp_list) > 1:
				self._log('broken upstream commit file')
				raise BrokenRepo
			upstream_commit = tmp_list[0]
		git.checkout(upstream_commit)
		self._repo.create_head(self._current)
		for i in commits:
			git.checkout(i)
			shutil.rmtree(GITUM_TMP_DIR, ignore_errors=True)
			os.mkdir(GITUM_TMP_DIR)
			for j in os.listdir(patches_dir):
				if j.endswith('.patch'):
					shutil.copy(patches_dir + '/' + j, GITUM_TMP_DIR + '/' + j)
			shutil.copy(patches_dir + '/_current_patch_', GITUM_TMP_DIR + '/_current_patch_')
			with open(patches_dir + '/_upstream_commit_') as f:
				tmp_list = f.readlines()
				if len(tmp_list) > 1:
					self._log('broken upstream commit file')
					raise BrokenRepo
				upstream_commit = tmp_list[0]
			git.checkout(self._current)
			patch_exists = False
			with open(GITUM_TMP_DIR + '/_current_patch_') as f:
				if f.readlines():
					patch_exists = True
			if patch_exists:
				git.am(GITUM_TMP_DIR + '/_current_patch_')
			os.unlink(GITUM_TMP_DIR + '/_current_patch_')
		git.checkout(upstream_commit)
		try:
			self._repo.delete_head(upstream, '-D')
		except:
			pass
		try:
			self._repo.delete_head(rebased, '-D')
		except:
			pass
		self._repo.create_head(self._upstream)
		self._repo.create_head(self._rebased)
		git.checkout(self._rebased)
		patches_to_apply = [i for i in os.listdir(GITUM_TMP_DIR) if i.endswith('.patch')]
		patches_to_apply.sort()
		for i in patches_to_apply:
			git.am(GITUM_TMP_DIR + '/' + i)
		git.checkout(self._rebased)

	def clone(self, remote_repo):
		self._repo.git.remote('add', 'origin', remote_repo)
		self._repo.git.fetch('origin')
		self._repo.git.checkout('-b', 'gitum-config', 'origin/gitum-config')
		self._load_config()
		self._repo.git.checkout('-b', self._upstream, 'origin/' + self._upstream)
		self._repo.git.checkout('-b', self._patches, 'origin/' + self._patches)
		self._repo.git.checkout('-b', self._current, 'origin/' + self._current)
		self._gen_rebased()
		self._update_remote('origin')

	def pull(self, remote=None):
		self._load_config()
		self._init_merge()
		self._load_remote()
		if remote:
			self._remote_repo = remote
		self._save_branches()
		cur = self._repo.branches[self._patches].commit.hexsha
		self._repo.git.fetch(self._remote_repo)
		self._repo.git.checkout(self._upstream, '-f')
		self._repo.git.reset(self._remote_repo + '/' + self._upstream, '--hard')
		self._repo.git.checkout(self._patches, '-f')
		self._repo.git.reset(self._remote_repo + '/' + self._patches, '--hard')
		self._repo.git.checkout(self._current, '-f')
		self._repo.git.reset(self._remote_repo + '/' + self._current, '--hard')
		self._gen_rebased()
		self._repo.git.checkout(self._current)
		self._commits = [q.hexsha for q in self._repo.iter_commits(self._previd + '..' + cur)]
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._pull_commits()
		self._repo.git.checkout(self._rebased)

	def continue_pull(self, command):
		self._load_config()
		self._init_merge()
		if not self._load_state():
			raise NoStateFile
		try:
			tmp_file = tempfile.TemporaryFile()
			self._repo.git.am(command, command, output_stream=tmp_file)
			if command == '--skip':
				self._repo.git.checkout('-f')
			self._repo.git.checkout(self._rebased)
			self._repo.git.cherry_pick(self._current)
			self._save_repo_state(self._current)
			self._repo.git.checkout(self._upstream)
			self._repo.git.merge(
				self._repo.git.show(
					self._commits[self._id] + ':' + GITUM_PATCHES_DIR + '/_upstream_commit_'
				)
			)
			self._repo.git.checkout(self._current)
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
		self._load_remote()
		self._pull_commits()
		self._repo.git.checkout(self._rebased)

	def push(self, remote=None):
		self._load_config()
		if not remote:
			self._load_remote()
			remote = self._remote_repo
		self._repo.git.push(remote, self._upstream, self._current, self._patches)

	def _gen_rebased(self, commit=''):
		if not commit:
			commit = self._patches
		self._repo.git.checkout(commit)
		patches_dir = self._repo_path + '/' + GITUM_PATCHES_DIR
		shutil.rmtree(GITUM_TMP_DIR, ignore_errors=True)
		os.mkdir(GITUM_TMP_DIR)
		for j in os.listdir(patches_dir):
			if j.endswith('.patch'):
				shutil.copy(patches_dir + '/' + j, GITUM_TMP_DIR + '/' + j)
		try:
			self._repo.git.branch('-D', self._rebased)
		except:
			pass
		self._repo.git.checkout('-b', self._rebased,
			self._repo.git.show(
				commit + ':' + GITUM_PATCHES_DIR + '/_upstream_commit_'
			)
		)
		patches_to_apply = [i for i in os.listdir(GITUM_TMP_DIR) if i.endswith('.patch')]
		patches_to_apply.sort()
		for i in patches_to_apply:
			self._repo.git.am(GITUM_TMP_DIR + '/' + i)

	def _update_remote(self, remote):
		with open(self._repo_path + '/' + REMOTE_REPO, 'w') as f:
			f.write('%s\n%s' % (remote, self._repo.remote(remote).refs[self._patches].object.hexsha))

	def _load_remote(self):
		try:
			with open(self._repo_path + '/' + REMOTE_REPO) as f:
				self._remote_repo, self._previd = f.readlines()
				self._remote_repo = self._remote_repo.split('\n')[0]
		except IOError:
			self._log('remote was not specified and no one to track with')
			raise

	def _pull_commits(self):
		tmp_file = tempfile.TemporaryFile()
		try:
			for q in xrange(self._id, len(self._commits)):
				lines = self._repo.git.show(
						self._commits[q] + ':' + GITUM_PATCHES_DIR + '/_current_patch_'
					)
				if len(lines) > 0:
					with open(GITUM_TMP_DIR + '/_current.patch', 'w') as f:
						f.write(lines)
					self._repo.git.am('-3', GITUM_TMP_DIR + '/_current.patch', output_stream=tmp_file)
					self._repo.git.checkout(self._rebased)
					self._repo.git.cherry_pick(self._current)
					self._save_repo_state(self._current)
				self._repo.git.checkout(self._upstream)
				self._repo.git.merge(
					self._repo.git.show(
						self._commits[q] + ':' + GITUM_PATCHES_DIR + '/_upstream_commit_'
					)
				)
				self._repo.git.checkout(self._current)
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
		self._update_remote(self._remote_repo)

	def _save_config(self, remote, current, upstream, rebased, patches):
		self._repo.git.checkout(CONFIG_BRANCH)
		with open(self._repo_path + '/' + CONFIG_FILE, 'w') as f:
			f.write('remote = %s\n' % remote)
			f.write('current = %s\n' % current)
			f.write('upstream = %s\n' % upstream)
			f.write('rebased = %s\n' % rebased)
			f.write('patches = %s\n' % patches)
		self._repo.git.add(self._repo_path + '/' + CONFIG_FILE)
		self._repo.git.commit('-m', 'Save config file')

	def _save_repo_state(self, commit):
		cur = commit if commit else self._current
		if self._repo.git.diff(self._rebased, cur) != '':
			self._log('%s and %s work trees are not equal - can\'t save state!' % (self._rebased, cur))
			raise NotUptodate
		# create tmp dir
		shutil.rmtree(GITUM_TMP_DIR, ignore_errors=True)
		os.mkdir(GITUM_TMP_DIR)
		git = self._repo.git
		# generate new patches
		for i in os.listdir(self._repo_path):
			if i.endswith('.patch'):
				os.unlink(self._repo_path + '/' + i)
		git.format_patch('%s..%s' % (self._upstream, self._rebased))
		# move patches to tmp dir
		for i in os.listdir(self._repo_path):
			if i.endswith('.patch'):
				shutil.move(self._repo_path + '/' + i, GITUM_TMP_DIR + '/' + i)
		# get current branch commit
		if commit:
			git.format_patch('%s^..%s' % (commit, commit))
		else:
			with open(self._repo_path + '/_current.patch', 'w') as f:
				pass
		# move it to tmp dir
		for i in os.listdir(self._repo_path):
			if i.endswith('.patch'):
				shutil.move(self._repo_path + '/' + i, GITUM_TMP_DIR + '/_current_patch_')
		git.checkout(self._patches, '-f')
		patches_dir = self._repo_path + '/' + GITUM_PATCHES_DIR
		# remove old patches from patches branch
		git.rm(patches_dir + '/*.patch', '--ignore-unmatch')
		# move new patches from tmp dir to patches branch
		for i in os.listdir(GITUM_TMP_DIR):
			if i.endswith('.patch'):
				shutil.move(GITUM_TMP_DIR + '/' + i, patches_dir + '/' + i)
		shutil.move(GITUM_TMP_DIR + '/_current_patch_', patches_dir + '/_current_patch_')
		# update upstream head
		with open(patches_dir + '/_upstream_commit_', 'w') as f:
			f.write(self._repo.branches[self._upstream].commit.hexsha)
		# commit the result
		git.add(patches_dir)
		if commit:
			mess = self._repo.commit(commit).message
			author = self._repo.commit(commit).author
			git.commit('-m', mess, '--author="%s <%s>"' % (author.name, author.email))
		else:
			git.commit('-m', '%s branch updated without code changes' % self._rebased)
		git.checkout(self._rebased)

	def _fixup_editpatch_message(self, mess):
		mess = mess.replace('git rebase --continue', 'gitum editpatch --continue')
		mess = mess.replace('git rebase --abort', 'gitum editpatch --abort')
		mess = mess.replace('git rebase --skip', 'gitum editpatch --skip')
		return mess

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
		try:
			self._load_config_raised()
		except IOError:
			self._log('config file is missed!')
			raise NoConfigFile

	def _load_config_raised(self):
		# set defaults
		self._upstream = 'upstream'
		self._rebased = 'rebased'
		self._current = 'current'
		self._patches = 'patches'
		self._remote = 'origin/master'
		# load config
		lines = self._repo.git.show(CONFIG_BRANCH + ':' + CONFIG_FILE).split('\n')
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
				self._current = parts[2]
			elif parts[0] == 'remote':
				self._remote = parts[2]
			elif parts[0] == 'patches':
				self._patches = parts[2]

	def _restore_branches(self):
		git = self._repo.git
		git.checkout(self._upstream, '-f')
		git.reset(self._saved_branches[self._upstream], '--hard')
		git.checkout(self._rebased, '-f')
		git.reset(self._saved_branches[self._rebased], '--hard')
		git.checkout(self._current, '-f')
		git.reset(self._saved_branches[self._current], '--hard')
		git.checkout(self._patches, '-f')
		git.reset(self._saved_branches[self._patches], '--hard')

	def _save_branches(self):
		git = self._repo.git
		self._saved_branches[self._upstream] = self._repo.branches[self._upstream].commit.hexsha
		self._saved_branches[self._rebased] = self._repo.branches[self._rebased].commit.hexsha
		self._saved_branches[self._current] = self._repo.branches[self._current].commit.hexsha
		self._saved_branches[self._patches] = self._repo.branches[self._patches].commit.hexsha
		self._saved_branches['prev_head'] = self._repo.branches[self._rebased].commit.hexsha

	def _get_commits(self):
		return [q.hexsha for q in self._repo.iter_commits(self._upstream + '..' + self._remote)]

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
			self._log(e.message)
			raise PatchFailed
		except:
			self._save_state()
			raise

	def _process_commit(self, commit, output):
		self._log("[%d/%d] commit %s" % \
			  (self._cur_num + 1, self._all_num,
			   self._repo.commit(commit).summary))
		self._stage1(commit)
		diff_str = self._stage2(commit, output)
		self._stage3(commit, diff_str)
		self._save_repo_state(self._repo.branches[self._current].commit.hexsha if diff_str else '')

	def _patch_tree(self, diff_str):
		status = 0
		if self._with_log:
			out = sys.stdout
		else:
			out = open('/dev/null', 'w')
		with open(self._repo_path + '/__patch__.patch', 'w') as f:
			f.write(diff_str + '\n')
		with open(self._repo_path + '/__patch__.patch', 'r') as f:
			proc = Popen(['patch', '-d', self._repo_path, '-p1'], stdin=f, stdout=out)
			status = proc.wait()
		os.unlink(self._repo_path + '/__patch__.patch')
		return status

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
				res = call(['git', '--git-dir=' + self._repo_path + '/.git/',
					    '--work-tree=' + self._repo_path, 'rebase', rebase_cmd], stderr=output)
				if res != 0:
					raise GitCommandError('git rebase %s' % rebase_cmd, res, '')
			else:
				git.rebase(rebase_cmd, output_stream=output)
		else:
			git.checkout(self._rebased)
			self._saved_branches['prev_head'] = self._repo.branches[self._rebased].commit.hexsha
			if interactive:
				res = call(['git', '--git-dir=' + self._repo_path + '/.git/',
					    '--work-tree=' + self._repo_path, 'rebase', '-i', commit], stderr=output)
				if res != 0:
					raise GitCommandError('git rebase', res, '')
			else:
				git.rebase(commit, output_stream=output)
		diff_str = self._repo.git.diff(self._saved_branches['prev_head'], self._rebased)
		return diff_str

	def _stage3(self, commit, diff_str, interactive=False, message=''):
		git = self._repo.git
		self._state = COMMIT_ST
		git.checkout(self._current)
		if diff_str == "":
			self._log('nothing to commit in branch current, skipping %s commit' % commit)
			return
		git.clean('-d', '-f')
		if self._patch_tree(diff_str) != 0:
			self._id += 1
			self._state = MERGE_ST
			raise PatchError('error occurs during applying %s\n'
					 'fix error, commit and continue the process, please!' % commit)
		git.add('-A', self._repo_path)
		if interactive:
			res = call(['git', '--git-dir=' + self._repo_path + '/.git/',
				    '--work-tree=' + self._repo_path, 'commit', '-e', '-m',
				    'place your comments for %s branch commit' % self._current])
			if res != 0:
				raise GitCommandError('git commit', res, '')
		else:
			if not message:
				mess = self._repo.commit(commit).message
				author = self._repo.commit(commit).author
				git.commit('-m', mess, '--author="%s <%s>"' % (author.name, author.email))
			else:
				git.commit('-m', message)

	def _update_current(self):
		self._init_merge()
		self._load_config()
		if not self._load_state():
			return
		try:
			diff_str = self._repo.git.diff(self._saved_branches['prev_head'], self._rebased)
			self._stage3('editpatch result', diff_str, True)
			self._save_repo_state(self._repo.branches[self._current].commit.hexsha if diff_str else '')
		except PatchError as e:
			self._save_state()
			self._log(e.message)
			raise PatchFailed
		except:
			self._save_state()
			raise

	def _save_state(self):
		with open(self._repo_path + '/' + STATE_FILE, 'w') as f:
			f.write(self._saved_branches[self._upstream] + '\n')
			f.write(self._saved_branches[self._rebased] + '\n')
			f.write(self._saved_branches[self._current] + '\n')
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
			self._log('state file is missed or corrupted: nothing to continue!')
			ret = False
		return ret

	def _load_state_raised(self, remove):
		with open(self._repo_path + '/' + STATE_FILE, 'r') as f:
			strs = [q.split()[0] for q in f.readlines() if len(q.split()) > 0]
		if len(strs) < 6:
			raise IOError
		self._saved_branches[self._upstream] = strs[0]
		self._saved_branches[self._rebased] = strs[1]
		self._saved_branches[self._current] = strs[2]
		self._saved_branches[self._patches] = strs[3]
		self._saved_branches['prev_head'] = strs[4]
		self._state = int(strs[5])
		self._all_num = int(strs[6])
		self._cur_num = int(strs[7])
		for i in xrange(8, len(strs)):
			self._commits.append(strs[i])
		if remove:
			os.unlink(self._repo_path + '/' + STATE_FILE)

	def _log(self, mess):
		if self._with_log and mess:
			print(mess)

	def _init_merge(self):
		self._state = START_ST
		self._id = 0
		self._cur_num = 0
		self._all_num = 0
		self._commits = []
		self._saved_branches = {}
