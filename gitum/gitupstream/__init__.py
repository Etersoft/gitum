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

from .constants import (
	MAINLINE_BRANCH,
	PATCHES_BRANCH,
	REBASED_BRANCH,
	REMOTE_BRANCH,
	UPSTREAM_BRANCH,
)
from .errors import (
	BranchExists,
	BrokenRepo,
	CherryPickFailed,
	GitUmException,
	NoConfigFile,
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
from .gitupstream import GitUpstream

__version__ = '0.9.0'

__all__ = [
	'GitUpstream',
	'MAINLINE_BRANCH', 'PATCHES_BRANCH', 'REBASED_BRANCH', 'REMOTE_BRANCH', 'UPSTREAM_BRANCH',
	'GitUmException', 'BranchExists', 'BrokenRepo', 'CherryPickFailed', 'NoConfigFile',
	'NoGitumRemote', 'NoMergeBranch', 'NoStateFile', 'NotGitRepo', 'NotGitumRepo',
	'NotSupported', 'NotUptodate', 'PatchError', 'PatchFailed', 'RebaseFailed', 'RepoIsDirty',
]
