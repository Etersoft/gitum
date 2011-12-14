#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# git-um - Git Upstream Manager.
# Copyright (C) 2011  Pavel Shilovsky <piastry@etersoft.ru>
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

START_ST = 0
MERGE_ST = 1
REBASE_ST = 2
COMMIT_ST = 3

PULL_FILE = '.git/um-pull'
CONFIG_FILE = '.git/um-config'

class PatchError(Exception):
	def __init__(self, message):
		self.message = message
	def __str__(self):
		return repr(self.message)

class GitUpstream(object):
	def __init__(self, repo_path='.', with_log=False):
		self._repo = Repo(repo_path)
		self._with_log = with_log

	def pull(self, branch=None):
		self._init_pull()
		if self._repo.is_dirty():
			self._log('Repository is dirty - can not pull!')
			return
		self._load_config(CONFIG_FILE)
		if self._repo.git.diff(self._rebased, self._current) != '':
			self._log('%s and %s work trees are not equal - can not pull!' % (self._rebased, self._current))
			return
		if branch:
			self._remote = branch
		if len(self._remote.split('/')) == 2:
			self._repo.git.fetch(self._remote.split('/')[0])
		self._commits = self._get_commits()
		self._commits.reverse()
		self._all_num = len(self._commits)
		self._save_branches()
		self._process_commits()

	def abort(self):
		self._init_pull()
		self._load_config(CONFIG_FILE)
		if not self._load_state(PULL_FILE):
			return
		try:
			self._repo.git.rebase('--abort')
		except:
			pass
		self._restore_branches()

	def continue_pull(self, rebase_cmd):
		self._init_pull()
		self._load_config(CONFIG_FILE)
		if not self._load_state(PULL_FILE):
			return
		if self._state == REBASE_ST:
			tmp_file = tempfile.TemporaryFile()
			try:
				diff_str = self._stage2(self._commits[self._id], tmp_file, rebase_cmd)
				self._stage3(self._commits[self._id], diff_str)
				self._id += 1
				self._cur_num += 1
			except GitCommandError as e:
				self._save_state(PULL_FILE)
				tmp_file.seek(0)
				self._log(self._fixup_rebase_message(''.join(tmp_file.readlines())))
				self._log(e.stderr)
				return
			except PatchError as e:
				self._save_state(PULL_FILE)
				self._log(e.message)
				return
			except:
				self._save_state(PULL_FILE)
				raise
		elif self._state != MERGE_ST:
			self._log("Don't support continue not from merge or rebase mode")
			return
		self._process_commits()

	def update(self, num):
		self.update_range('HEAD~'+str(num)+':HEAD')

	def update_range(self, commit_range):
		if self._repo.is_dirty():
			self._log('Repository is dirty - can not update!')
			return
		since, to = commit_range.split(':')
		self._load_config(CONFIG_FILE)
		if self._repo.git.diff(self._rebased, self._current) == '':
			self._log('%s and %s work trees are equal - nothing to update!' % (self._rebased, self._current))
			return
		git = self._repo.git
		since = self._repo.commit(since).hexsha
		to = self._repo.commit(to).hexsha
		git.checkout(self._rebased, '-f')
		commits = [q.hexsha for q in self._repo.iter_commits(since + '..' + to)]
		commits.reverse()
		try:
			for i in commits:
				git.cherry_pick(i)
		except GitCommandError as e:
			self._log(e.stderr)
			return
		git.checkout(self._current)

	def edit_patch(self, command=None):
		if command == '--commit':
			return self._update_current()
		if command == '--abort':
			return self.abort()
		self._init_pull()
		if self._repo.is_dirty():
			self._log('Repository is dirty - can not edit patch!')
			return
		self._load_config(CONFIG_FILE)
		if self._repo.git.diff(self._rebased, self._current) != '':
			self._log('%s and %s work trees are not equal - can not edit patch!' % (self._rebased, self._current))
			return
		if not command:
			self._save_branches()
			self._save_state(PULL_FILE)
		elif not self._load_state(PULL_FILE, False):
			return
		tmp_file = tempfile.TemporaryFile()
		try:
			self._stage2(self._upstream, tmp_file, command, True)
		except GitCommandError as e:
			tmp_file.seek(0)
			self._log(self._fixup_rebase_message(''.join(tmp_file.readlines())))
			self._log(e.stderr)
		except:
			self._save_state(PULL_FILE)
			raise
		self._save_state(PULL_FILE)

	def create(self, remote, current, upstream, rebased):
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
			self._repo.delete_head(self._repo.branches[rebased])
		except:
			pass

		git.checkout(current)

		with open(CONFIG_FILE, 'w') as f:
			f.write('remote = %s\n' % remote)
			f.write('current = %s\n' % current)
			f.write('upstream = %s\n' % upstream)
			f.write('rebased = %s\n' % rebased)

		self._repo.create_head(rebased)

	def remove_branches(self):
		self._load_config(CONFIG_FILE)
		self._repo.git.checkout(self._upstream, '-f')
		self._repo.delete_head(self._current, '-D')
		self._repo.delete_head(self._rebased, '-D')

	def remove_config_files(self):
		try:
			os.unlink(CONFIG_FILE)
			os.unlink(PULL_FILE)
		except:
			pass

	def remove_all(self):
		self.remove_branches()
		self.remove_config_files()

	def _fixup_rebase_message(self, mess):
		mess = mess.replace('git rebase --continue', 'gitum pull --continue')
		mess = mess.replace('git rebase --abort', 'gitum pull --abort')
		mess = mess.replace('git rebase --skip', 'gitum pull --skip')
		return mess

	def _load_config(self, filename):
		try:
			self._load_config_raised(filename)
		except IOError:
			self._log('%s missing, using default branch names...' % filename)

	def _load_config_raised(self, filename):
		# set defaults
		self._upstream = 'upstream'
		self._rebased = 'rebased'
		self._current = 'current'
		self._remote = 'origin/master'
		# load config
		with open(filename, 'r') as f:
			_strs = [q.split('\n')[0] for q in f.readlines()]
		num = 0
		for i in _strs:
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

	def _restore_branches(self):
		git = self._repo.git
		git.checkout(self._upstream, '-f')
		git.reset(self._saved_branches[self._upstream], '--hard')
		git.checkout(self._rebased, '-f')
		git.reset(self._saved_branches[self._rebased], '--hard')
		git.checkout(self._current, '-f')
		git.reset(self._saved_branches[self._current], '--hard')

	def _save_branches(self):
		git = self._repo.git
		self._saved_branches[self._upstream] = self._repo.branches[self._upstream].commit.hexsha
		self._saved_branches[self._rebased] = self._repo.branches[self._rebased].commit.hexsha
		self._saved_branches[self._current] = self._repo.branches[self._current].commit.hexsha
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
			self._save_state(PULL_FILE)
			tmp_file.seek(0)
			self._log(self._fixup_rebase_message(''.join(tmp_file.readlines())))
			self._log(e.stderr)
		except PatchError as e:
			self._save_state(PULL_FILE)
			self._log(e.message)
		except:
			self._save_state(PULL_FILE)
			raise

	def _process_commit(self, commit, output):
		self._log("[%d/%d] commit %s" % \
			  (self._cur_num + 1, self._all_num,
			   self._repo.commit(commit).summary))
		self._stage1(commit)
		diff_str = self._stage2(commit, output)
		self._stage3(commit, diff_str)

	def _patch_tree(self, diff_str):
		status = 0
		if self._with_log:
			out = sys.stdout
		else:
			out = open('/dev/null', 'w')
		with open('__patch__.patch', 'w') as f:
			f.write(diff_str + '\n')
		with open('__patch__.patch', 'r') as f:
			proc = Popen(['patch', '-p1'], stdin=f, stdout=out)
			status = proc.wait()
		os.unlink('__patch__.patch')
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
				res = call(['git', 'rebase', rebase_cmd])
				if res != 0:
					raise GitCommandError('git rebase %s' % rebase_cmd, res, '')
			else:
				git.rebase(rebase_cmd, output_stream=output)
		else:
			git.checkout(self._rebased)
			self._saved_branches['prev_head'] = self._repo.branches[self._rebased].commit.hexsha
			if interactive:
				res = call(['git', 'rebase', '-i', commit])
				if res != 0:
					raise GitCommandError('git rebase', res, '')
			else:
				git.rebase(commit, output_stream=output)
		diff_str = self._repo.git.diff(self._saved_branches['prev_head'], self._rebased)
		return diff_str

	def _stage3(self, commit, diff_str, interactive=False):
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
		git.add('-A')
		if interactive:
			res = call(['git', 'commit', '-e', '-m',
				    'remove this line and place your comments to commit into %s branch' % self._current])
			if res != 0:
				raise GitCommandError('git commit', res, '')
		else:
			mess = self._repo.commit(commit).message
			author = self._repo.commit(commit).author
			git.commit('-m', mess, '--author="%s <%s>"' % (author.name, author.email))

	def _update_current(self):
		self._init_pull()
		self._load_config(CONFIG_FILE)
		if not self._load_state(PULL_FILE):
			return
		try:
			diff_str = self._repo.git.diff(self._saved_branches['prev_head'], self._rebased)
			self._stage3('editpatch result', diff_str, True)
		except PatchError as e:
			self._save_state(PULL_FILE)
			self._log(e.message)
		except:
			self._save_state(PULL_FILE)
			raise

	def _save_state(self, filename):
		with open(filename, 'w') as f:
			f.write(self._saved_branches[self._upstream] + '\n')
			f.write(self._saved_branches[self._rebased] + '\n')
			f.write(self._saved_branches[self._current] + '\n')
			f.write(self._saved_branches['prev_head'] + '\n')
			f.write(str(self._state) + '\n')
			f.write(str(self._all_num) + '\n')
			f.write(str(self._cur_num) + '\n')
			for i in xrange(self._id, len(self._commits)):
				f.write(str(self._commits[i]) + '\n')

	def _load_state(self, filename, remove=True):
		ret = True
		try:
			self._load_state_raised(filename, remove)
		except IOError:
			self._log('state file is missed or corrupted: nothing to continue.')
			ret = False
		return ret

	def _load_state_raised(self, filename, remove):
		with open(filename, 'r') as f:
			strs = [q.split()[0] for q in f.readlines() if len(q.split()) > 0]
		if len(strs) < 5:
			raise IOError
		self._saved_branches[self._upstream] = strs[0]
		self._saved_branches[self._rebased] = strs[1]
		self._saved_branches[self._current] = strs[2]
		self._saved_branches['prev_head'] = strs[3]
		self._state = int(strs[4])
		self._all_num = int(strs[5])
		self._cur_num = int(strs[6])
		for i in xrange(7, len(strs)):
			self._commits.append(strs[i])
		if remove:
			os.unlink(filename)

	def _log(self, mess):
		if self._with_log:
			print(mess)

	def _init_pull(self):
		self._state = START_ST
		self._id = 0
		self._cur_num = 0
		self._all_num = 0
		self._commits = []
		self._saved_branches = {}
