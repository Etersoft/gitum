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
from gitupstream import GitUpstream
import os
import shutil

class Chdir:
	def __init__(self, new_path):
		self.saved_path = os.getcwd()
		os.chdir(new_path)

	def __del__(self):
		os.chdir(self.saved_path)

def simple_test(dirname, remove=True):
	if os.path.exists(dirname):
		print('directory exists')
		return

	# create repo
	print('test has started!\ncreating git repo...')
	git.Git().init(dirname)
	ch = Chdir(dirname)
	repo = git.Repo()
	print('OK')

	# create file
	print('creating file...')
	with open('testfile', 'w') as f:
		f.write('a')
	repo.git.add('testfile')
	repo.git.commit('-m', 'initial')
	print('OK')

	# create branch to merge with
	print('creating gitum repo...')
	repo.create_head('merge')

	# crete gitum repo
	repo_um = GitUpstream(with_log=True)
	repo_um.create('merge', 'dev', 'master', 'rebased', 'patches')
	repo.git.checkout('dev')
	print('OK')

	# make local changes
	print('making local changes...')
	with open('testfile', 'a') as f:
		f.write('b')
	repo.git.add('testfile')
	repo.git.commit('-m', 'local: b')
	with open('testfile', 'a') as f:
		f.write('c')
	repo.git.add('testfile')
	repo.git.commit('-m', 'local: c')
	print('OK')

	# update patches branch
	print('updating rebased branch...')
	repo_um.update(2)
	print('OK')

	# make upstream changes
	print('making upstream changes...')
	repo.git.checkout('merge')
	with open('testfile', 'a') as f:
		f.write('\nd')
	repo.git.add('testfile')
	repo.git.commit('-m', 'remote: \nd')
	with open('testfile', 'w') as f:
		f.write('s\nd')
	repo.git.add('testfile')
	repo.git.commit('-m', 'remote: s')
	with open('testfile', 'w') as f:
		f.write('s\nd\n\n\n\n\n\n\nr\n')
	repo.git.add('testfile')
	repo.git.commit('-m', 'remote: r')
	print('OK')

	# gitum pull
	print('doing gitum pull...')
	repo.git.checkout('dev')
	repo_um.pull()

	# 1st fail
	with open('testfile', 'w') as f:
		f.write('ab\nd')
	repo.git.add('testfile')
	repo_um.continue_pull('--continue')

	# 2nd fail
	with open('testfile', 'w') as f:
		f.write('abc\nd')
	repo.git.add('testfile')
	repo_um.continue_pull('--continue')

	# 3rd fail
	with open('testfile', 'w') as f:
		f.write('sb\nd')
	repo.git.add('testfile')
	repo_um.continue_pull('--continue')

	# 4th fail
	with open('testfile', 'w') as f:
		f.write('sbc\nd')
	repo.git.add('testfile')
	repo_um.continue_pull('--continue')
	print('OK')

	if not remove:
		return

	# remove gitum repo
	print('removing gitum repo...')
	repo_um.remove_all()
	print('OK')

	# remove repo
	print('removing git repo...')
	del ch
	shutil.rmtree(dirname)
	print('OK\ntest has finished!')
