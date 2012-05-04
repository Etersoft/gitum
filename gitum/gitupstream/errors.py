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

class PatchError(Exception):
	def __init__(self, message):
		self.message = message
	def __str__(self):
		return repr(self.message)

class GitUmException(Exception):
	pass

class NotSupported(GitUmException):
	pass

class RepoIsDirty(GitUmException):
	pass

class NotUptodate(GitUmException):
	pass

class PatchFailed(GitUmException):
	pass

class RebaseFailed(GitUmException):
	pass

class CherryPickFailed(GitUmException):
	pass

class NoStateFile(GitUmException):
	pass

class NoConfigFile(GitUmException):
	pass

class BrokenRepo(GitUmException):
	pass

class NoGitumRemote(GitUmException):
	pass
