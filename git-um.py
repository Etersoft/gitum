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
import os, sys

upstream_branch = 'upstream'
rebased_branch = 'rebased'
current_branch = 'current'

START_ST = 0
MERGE_ST = 1
REBASE_ST = 2
COMMIT_ST = 3

CONFIG_FILE = '.git-um-config'

class GitUpstream(object):
	def __init__(self, repo_path='.'):
		self.repo = Repo(repo_path)
		self.state = START_ST
		self.id = 0
		self.commits = []

	def pull(self, server, branch):
		self.repo.git.fetch(server)
		self.commits = self._get_commits(server, branch)
		self.commits.reverse()
		self._save_branches()
		if self._process_commits() == -1:
			return
		self._clear_branches()

	def abort(self):
		try:
			self.repo.git.rebase('--abort')
		except:
			pass
		try:
			self.repo.git.branch('-D', '__' + rebased_branch + '__')
		except:
			pass
		self._restore_branches()
		self._clear_branches()

	def continue_pull(self):
		self._load_state()
		if self.state == REBASE_ST:
			try:
				diff_str = self._stage2(self.commits[self.id], True)
				self._stage3(self.commits[self.id], diff_str)
				self.id += 1
			except GitCommandError as e:
				self._save_state()
				print e.stdout
				return
			except:
				self._save_state()
				raise
		else:
			print("Don't support continue not from rebase mode")
			return
		if self._process_commits() == -1:
			return
		self._clear_branches()

	def _restore_branches(self):
		git = self.repo.git
		git.checkout(upstream_branch, '-f')
		git.reset('__saved_' + upstream_branch + '__', '--hard')
		git.checkout(rebased_branch, '-f')
		git.reset('__saved_' + rebased_branch + '__', '--hard')
		git.checkout(current_branch, '-f')
		git.reset('__saved_' + current_branch + '__', '--hard')

	def _clear_branches(self):
		git = self.repo.git
		git.branch('-D', '__saved_' + rebased_branch + '__')
		git.branch('-D', '__saved_' + upstream_branch + '__')
		git.branch('-D', '__saved_' + current_branch + '__')

	def _save_branches(self):
		git = self.repo.git
		cur_id = self.repo.commits('HEAD')[0].id
		git.stash()
		git.checkout(upstream_branch)
		git.branch('__saved_' + upstream_branch + '__')
		git.checkout(rebased_branch)
		git.branch('__saved_' + rebased_branch + '__')
		git.checkout(current_branch)
		git.branch('__saved_' + current_branch + '__')
		git.checkout(cur_id)
		try:
			git.stash('pop')
		except:
			pass

	def _get_commits(self, server, branch):
		return [q.id for q in self.repo.log(upstream_branch + '..' + server + '/' + branch)]

	def _process_commits(self):
		try:
			for i in xrange(self.id, len(self.commits)):
				self._process_commit(self.commits[i])
				self.id += 1
		except GitCommandError as e:
			self._save_state()
			print e.stdout
			return -1
		except:
			self._save_state()
			raise
		return 0

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
		git = self.repo.git
		self.state = MERGE_ST
		git.checkout(upstream_branch)
		print('merge commit ' + commit)
		git.merge(commit)

	def _stage2(self, commit, continue_rebase=False):
		git = self.repo.git
		self.state = REBASE_ST
		if continue_rebase:
			git.rebase('--continue')
		else:
			git.checkout(rebased_branch)
			git.branch('__' + rebased_branch + '__')
			git.rebase(commit)
		diff_str = self.repo.diff('__' + rebased_branch + '__', rebased_branch)
		git.branch('-D', '__' + rebased_branch + '__')
		return diff_str

	def _stage3(self, commit, diff_str):
		git = self.repo.git
		self.state = COMMIT_ST
		git.checkout(current_branch)
		if self._patch_tree(diff_str) != 0:
			print 'error occurs during applying the patch'
		git.add('-A')
		git.commit('-m', '"commit upstream ' + str(commit) + '"')

	def _save_state(self):
		with open(CONFIG_FILE, 'w') as f:
			f.write(str(self.state) + '\n')
			for i in xrange(self.id, len(self.commits)):
				f.write(str(self.commits[i]) + '\n')

	def _load_state(self):
		with open(CONFIG_FILE, 'r') as f:
			_str = f.readline()
			self.state = int(_str)
			_strs = f.readlines()
			for i in _strs:
				self.commits.append(i.split()[0])

if __name__ == "__main__":
	if len(sys.argv) < 2:
		GitUpstream().pull('origin', 'master')
	elif len(sys.argv) < 3 and sys.argv[1] == '--continue':
		GitUpstream().continue_pull()
	elif len(sys.argv) < 3 and sys.argv[1] == '--abort':
		GitUpstream().abort()
	else:
		print("Usage git-um.py [--continue | --abort]")
