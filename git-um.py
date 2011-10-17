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
from subprocess import Popen
import os
import sys

upstream_branch = 'upstream'
rebased_branch = 'rebased'
current_branch = 'current'

START_ST = 0
MERGE_ST = 1
REBASE_ST = 2
COMMIT_ST = 3

PULL_FILE = '.git-um-pull'
CONFIG_FILE = '.git-um-config'

class PatchError(Exception):
	def __init__(self, message):
		self.message = message
	def __str__(self):
		return repr(self.message)

class GitUpstream(object):
	def __init__(self, repo_path='.'):
		self._repo = Repo(repo_path)
		self._state = START_ST
		self._id = 0
		self._commits = []
		self._saved_branches = {}
		self._load_config(CONFIG_FILE)

	def pull(self, server, branch):
		self._repo.git.fetch(server)
		self._commits = self._get_commits(server, branch)
		self._commits.reverse()
		self._save_branches()
		self._process_commits()

	def abort(self):
		self._load_state()
		try:
			self._repo.git.rebase('--abort')
		except:
			pass
		self._restore_branches()

	def continue_pull(self):
		self._load_state()
		if self._state == REBASE_ST:
			try:
				diff_str = self._stage2(self._commits[self._id], True)
				self._stage3(self._commits[self._id], diff_str)
				self._id += 1
			except GitCommandError as e:
				self._save_state()
				print(e.stdout)
				return
			except PatchError as e:
				self._save_state()
				print(e.message)
				return
			except:
				self._save_state()
				raise
		else:
			print("Don't support continue not from rebase mode")
			return
		if self._process_commits() == -1:
			return

	def _load_config(self, filename):
		global upstream_branch, rebased_branch, current_branch
		with open(filename, 'r') as f:
			num = 0
			_strs = [q.split('\n')[0] for q in f.readlines()]
			for i in _strs:
				num = 1
				i = i.split('#')[0].strip()
				parts = i.split(' ')
				if len(parts) != 3 or parts[1] != '=':
					print('error in config file on line %d :' % num)
					print('    %s' % i)
					return -1
				if parts[0] == 'upstream':
					upstream_branch = parts[2]
				elif parts[0] == 'rebased':
					rebased_branch = parts[2]
				elif parts[0] == 'current':
					current_branch = parts[2]
		return 0

	def _restore_branches(self):
		git = self._repo.git
		git.checkout(upstream_branch, '-f')
		git.reset(self._saved_branches[upstream_branch], '--hard')
		git.checkout(rebased_branch, '-f')
		git.reset(self._saved_branches[rebased_branch], '--hard')
		git.checkout(current_branch, '-f')
		git.reset(self._saved_branches[current_branch], '--hard')

	def _save_branches(self):
		git = self._repo.git
		self._saved_branches[upstream_branch] = self._repo.commit(upstream_branch).id
		self._saved_branches[rebased_branch] = self._repo.commit(rebased_branch).id
		self._saved_branches[current_branch] = self._repo.commit(current_branch).id

	def _get_commits(self, server, branch):
		return [q.id for q in self._repo.log(upstream_branch + '..' + server + '/' + branch)]

	def _process_commits(self):
		try:
			for i in xrange(self._id, len(self._commits)):
				self._process_commit(self._commits[i])
				self._id += 1
		except GitCommandError as e:
			self._save_state()
			print(e.stdout)
		except PatchError as e:
			self._save_state()
			print(e.message)
		except:
			self._save_state()
			raise

	def _process_commit(self, commit):
		self._stage1(commit)
		diff_str = self._stage2(commit)
		self._stage3(commit, diff_str)

	def _patch_tree(self, diff_str):
		status = 0
		with open('__patch__.patch', 'w') as f:
			f.write(diff_str + '\n')
		with open('__patch__.patch', 'r') as f:
			proc = Popen(['patch', '-p1'], stdin=f)
			status = proc.wait()
		os.unlink('__patch__.patch')
		return status

	def _stage1(self, commit):
		git = self._repo.git
		self._state = MERGE_ST
		git.checkout(upstream_branch)
		print('merge commit ' + commit)
		git.merge(commit)

	def _stage2(self, commit, continue_rebase=False):
		git = self._repo.git
		self._state = REBASE_ST
		if continue_rebase:
			git.rebase('--continue')
		else:
			git.checkout(rebased_branch)
			self._saved_branches['prev_head'] = self._repo.commit(rebased_branch).id
			git.rebase(commit)
		diff_str = self._repo.diff(self._saved_branches['prev_head'], rebased_branch)
		return diff_str

	def _stage3(self, commit, diff_str):
		git = self._repo.git
		self._state = COMMIT_ST
		git.checkout(current_branch)
		if diff_str == "":
			print('nothing to commit in branch current, skipping %s commit' % commit)
			return
		if self._patch_tree(diff_str) != 0:
			self._id += 1
			self._state = MERGE_ST
			raise PatchError('error occurs during applying the commit %s\n'
					 'fix error, commit and continue the process, please!' % commit)
		git.add('-A')
		mess = self._fix_commit_message(self._repo.commit(commit).message)
		git.commit('-m', mess)

	def _fix_commit_message(self, mess):
		return '\n\n'.join(mess.split('\n'))

	def _save_state(self):
		with open(PULL_FILE, 'w') as f:
			f.write(self._saved_branches[upstream_branch] + '\n')
			f.write(self._saved_branches[rebased_branch] + '\n')
			f.write(self._saved_branches[current_branch] + '\n')
			f.write(self._saved_branches['prev_head'] + '\n')
			f.write(str(self._state) + '\n')
			for i in xrange(self._id, len(self._commits)):
				f.write(str(self._commits[i]) + '\n')

	def _load_state(self):
		with open(PULL_FILE, 'r') as f:
			self._saved_branches[upstream_branch] = f.readline().split()[0]
			self._saved_branches[rebased_branch] = f.readline().split()[0]
			self._saved_branches[current_branch] = f.readline().split()[0]
			self._saved_branches['prev_head'] = f.readline().split()[0]
			self._state = int(f.readline())
			for i in f.readlines():
				self._commits.append(i.split()[0])
		os.unlink(PULL_FILE)

if __name__ == "__main__":
	if len(sys.argv) < 2:
		GitUpstream().pull('origin', 'master')
	elif len(sys.argv) < 3 and sys.argv[1] == '--continue':
		GitUpstream().continue_pull()
	elif len(sys.argv) < 3 and sys.argv[1] == '--abort':
		GitUpstream().abort()
	else:
		print("Usage git-um.py [--continue | --abort]")
