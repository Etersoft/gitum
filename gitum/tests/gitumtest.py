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

import git
from gitupstream import *
import os
import sys
import shutil
import unittest
import tempfile

# set to True for debugging
_WITH_LOG = False
_NO_REMOVE = False

def _log(string):
	if _WITH_LOG:
		sys.stderr.write(string + '\n')

class LocalWorkTest(unittest.TestCase):
	def setUp(self):
		self.dirname = tempfile.mkdtemp()

	def tearDown(self):
		if not _NO_REMOVE:
			shutil.rmtree(self.dirname)
		else:
			_log('dirname: %s' % self.dirname)

	def test_restore(self):
		_log('Resore test has started!')

		_log('creating git repo...')
		gitum_repo = GitUpstream(repo_path=self.dirname, with_log=_WITH_LOG, new_repo=True)
		_log('OK')

		_log('creating file...')
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('a')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'initial')
		_log('OK')

		# create branch to merge with
		_log('creating gitum repo...')
		gitum_repo.repo().create_head('merge')

		gitum_repo.create('merge', 'master' , 'rebased', 'dev', 'patches')
		gitum_repo.repo().git.checkout('rebased')
		_log('OK')

		_log('making local changes...')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('b')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'local: b')
		_log('OK')

		_log('restore rebased branch...')
		gitum_repo.restore(rebased_only=True)
		with open(self.dirname + '/testfile', 'r') as f:
			data = f.read(2)
		self.assertEqual(data, 'a')
		self.assertEqual(gitum_repo.repo().git.diff('dev', 'rebased'), '')
		_log('OK')

		_log('making local changes...')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('c')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'local: c')
		_log('OK')

		_log('updating current branch...')
		gitum_repo.update()
		_log('OK')

		_log('restore rebased branch...')
		gitum_repo.restore(rebased_only=True)
		with open(self.dirname + '/testfile', 'r') as f:
			data = f.read(2)
		self.assertEqual(data, 'ac')
		self.assertEqual(gitum_repo.repo().git.diff('dev', 'rebased'), '')
		_log('OK')

		_log('making local changes...')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('d')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'local: d')
		_log('OK')

		_log('making local changes...')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('e')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'local: e')
		_log('OK')

		_log('updating current branch...')
		gitum_repo.update()
		_log('OK')

		_log('restore gitum repo to the state 2 changes before...')
		gitum_repo.restore(commit='patches^')
		with open(self.dirname + '/testfile', 'r') as f:
			data = f.read(3)
		self.assertEqual(data, 'acd')
		self.assertEqual(gitum_repo.repo().git.diff('dev', 'rebased'), '')
		_log('OK')

		_log('restore gitum repo to the initial state...')
		gitum_repo.restore(commit='patches^^^')
		with open(self.dirname + '/testfile', 'r') as f:
			data = f.read(2)
		self.assertEqual(data, 'a')
		self.assertEqual(gitum_repo.repo().git.diff('dev', 'rebased'), '')
		_log('OK')

		_log('Restore test has finished!')

	def test_local_work(self):
		_log('LocalWork test has started!')

		_log('creating git repo...')
		gitum_repo = GitUpstream(repo_path=self.dirname, with_log=_WITH_LOG, new_repo=True)
		_log('OK')

		_log('creating file...')
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('a')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'initial')
		_log('OK')

		# create branch to merge with
		_log('creating gitum repo...')
		gitum_repo.repo().create_head('merge')

		gitum_repo.create('merge', 'master' , 'rebased', 'dev', 'patches')
		gitum_repo.repo().git.checkout('rebased')
		_log('OK')

		_log('making local changes...')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('b')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'local: b')
		_log('OK')

		_log('updating current branch...')
		gitum_repo.update()
		_log('OK')

		_log('making local changes...')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('c')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'local: c')
		_log('OK')

		_log('updating current branch...')
		gitum_repo.update()
		_log('OK')

		_log('making upstream changes...')
		gitum_repo.repo().git.checkout('merge')
		with open(self.dirname + '/testfile', 'a') as f:
			f.write('\nd')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'remote: \nd')
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('s\nd')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'remote: s')
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('s\nd\n\n\n\n\n\n\nr\n')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.repo().git.commit('-m', 'remote: r')
		_log('OK')

		_log('doing gitum merge...')
		gitum_repo.repo().git.checkout('rebased')
		self.assertRaises(GitUmException, gitum_repo.merge, ())

		# 1st fail
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('ab\nd')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		self.assertRaises(GitUmException, gitum_repo.continue_merge, ('--continue'))

		# 2nd fail
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('abc\nd')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		self.assertRaises(GitUmException, gitum_repo.continue_merge, ('--continue'))

		# 3rd fail
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('sb\nd')
		gitum_repo.repo().git.add('testfile')
		self.assertRaises(GitUmException, gitum_repo.continue_merge, ('--continue'))

		# 4th fail
		with open(self.dirname + '/testfile', 'w') as f:
			f.write('sbc\nd')
		gitum_repo.repo().git.add(self.dirname + '/testfile')
		gitum_repo.continue_merge('--continue')
		_log('OK')

		_log('removing gitum repo...')
		gitum_repo.remove_all()
		_log('OK')

		_log('LocalWork test has finished!')

class RemoteWorkTest(unittest.TestCase):
	def setUp(self):
		self.dirname1 = tempfile.mkdtemp()
		self.dirname2 = tempfile.mkdtemp()
		self.dirname3 = tempfile.mkdtemp()
		self.baredir = tempfile.mkdtemp()

	def tearDown(self):
		if not _NO_REMOVE:
			shutil.rmtree(self.dirname1)
			shutil.rmtree(self.dirname2)
			shutil.rmtree(self.dirname3)
			shutil.rmtree(self.baredir)
		else:
			_log('dirname1: %s' % self.dirname1)
			_log('dirname2: %s' % self.dirname2)
			_log('dirname3: %s' % self.dirname3)
			_log('baredir: %s' % self.baredir)

	def test_remote_work(self):
		_log('RemoteWork test has started!')

		_log('creating git repo...')
		gitum_repo = GitUpstream(repo_path=self.dirname1, with_log=_WITH_LOG, new_repo=True)
		_log('OK')

		_log('creating a file...')
		with open(self.dirname1 + '/testfile', 'w') as f:
			f.write('a')
		gitum_repo.repo().git.add(self.dirname1 + '/testfile')
		gitum_repo.repo().git.commit('-m', 'a')
		gitum_repo.create('merge', 'master', 'rebased', 'dev', 'patches')
		gitum_repo.repo().git.checkout('rebased')
		_log('OK')

		_log('cloning the repo...')
		gitum_repo2 = GitUpstream(repo_path=self.dirname2, with_log=_WITH_LOG, new_repo=True)
		gitum_repo2.clone(self.dirname1)
		_log('OK')

		_log('cloning the repo...')
		gitum_local_repo = GitUpstream(repo_path=self.dirname3, with_log=_WITH_LOG, new_repo=True)
		gitum_local_repo.clone(self.dirname1)
		_log('OK')

		_log('updating the file on the remote side...')
		with open(self.dirname1 + '/testfile', 'w') as f:
			f.write('ab')
		gitum_repo.repo().git.add(self.dirname1 + '/testfile')
		gitum_repo.repo().git.commit('-m', 'ab')
		gitum_repo.update()
		_log('OK')

		_log('updating the file on the remote2 side...')
		with open(self.dirname2 + '/testfile', 'w') as f:
			f.write('af')
		gitum_repo2.repo().git.add(self.dirname2 + '/testfile')
		gitum_repo2.repo().git.commit('-m', 'af')
		gitum_repo2.update()
		_log('OK')

		_log('updating the file on the local side...')
		with open(self.dirname3 + '/testfile', 'w') as f:
			f.write('ac')
		gitum_local_repo.repo().git.add(self.dirname3 + '/testfile')
		gitum_local_repo.repo().git.commit('-m', 'ac')
		gitum_local_repo.update()
		_log('OK')

		_log('pulling the remote side from the local one...')
		self.assertRaises(GitUmException, gitum_local_repo.pull, 'origin')

		with open(self.dirname3 + '/testfile', 'w') as f:
			f.write('abc')
		gitum_local_repo.repo().git.add(self.dirname3 + '/testfile')
		gitum_local_repo.continue_pull('--resolved')
		_log('OK')

		_log('pulling the remote2 side from the local one...')
		gitum_local_repo.repo().git.remote('add', 'origin2', self.dirname2)
		self.assertRaises(GitUmException, gitum_local_repo.pull, 'origin2')

		with open(self.dirname3 + '/testfile', 'w') as f:
			f.write('abf')
		gitum_local_repo.repo().git.add(self.dirname3 + '/testfile')
		self.assertRaises(GitUmException, gitum_local_repo.continue_pull, '--resolved')

		with open(self.dirname3 + '/testfile', 'w') as f:
			f.write('abcf')
		gitum_local_repo.repo().git.add(self.dirname3 + '/testfile')
		gitum_local_repo.continue_pull('--resolved')
		_log('OK')

		_log('pushing to the remote side...')
		bare = git.Repo.init(self.baredir, bare=True)
		gitum_local_repo.repo().git.remote('add', 'new', self.baredir)
		gitum_local_repo.push('new')
		_log('OK')

		_log('removing gitum repos...')
		gitum_repo.remove_all()
		gitum_repo2.remove_all()
		gitum_local_repo.remove_all()
		_log('OK')

		_log('RemoteWork test has finished!')

if __name__ == "__main__":
	unittest.main()
