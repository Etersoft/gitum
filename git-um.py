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

upstream_branch = 'upstream'
rebased_branch = 'rebased'
current_branch = 'current'

class GitUpstream(object):
	def __init__(self, repo_path='.'):
		self.repo = Repo(repo_path)

	def pull(self, server, branch):
		self.repo.git.fetch(server)
		commits = self._get_commits(server, branch)
		commits.reverse()
		self._process_commits(commits)

	def _get_commits(self, server, branch):
		return self.repo.log(upstream_branch + '..' + server + '/' + branch)

	def _process_commits(self, commits):
		for q in commits:
			self._process_commit(q.id)

	def _process_commit(self, commit):
		self._stage1(commit)
		diff_str = self._stage2(commit)
		self._stage3(commit, diff_str)

	def _patch_tree(self, diff_str):
		with open('__patch__.patch', 'w') as f:
			f.write(diff_str + '\n')
		with open('__patch__.patch', 'r') as f:
			proc = Popen(['patch', '-p1'], stdin=f)
			status = proc.wait()
		os.unlink('__patch__.patch')

	def _stage1(self, commit):
		git = self.repo.git
		git.checkout(upstream_branch)
		print('merge commit ' + commit)
		git.merge(commit)

	def _stage2(self, commit):
		git = self.repo.git
		git.checkout(rebased_branch)
		git.branch('__' + rebased_branch + '__')
		git.rebase(commit)
		diff_str = self.repo.diff('__' + rebased_branch + '__', rebased_branch)
		git.branch('-D', '__' + rebased_branch + '__')
		return diff_str

	def _stage3(self, commit, diff_str):
		git = self.repo.git
		git.checkout(current_branch)
		self._patch_tree(diff_str)
		git.add('-A')
		git.commit('-m', '"commit upstream ' + str(commit) + '"')

if __name__ == "__main__":
	GitUpstream().pull('origin', 'master')
