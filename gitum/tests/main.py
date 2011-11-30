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

import gitumtest
import sys

if __name__ == "__main__":
	if len(sys.argv) < 2:
		print('specify a repo dir')
		exit()

	dirname = sys.argv[1]

	if len(sys.argv) == 3 and sys.argv[2] == '--noremove':
		remove = False
	else:
		remove = True

	gitumtest.simple_test(dirname, remove)
