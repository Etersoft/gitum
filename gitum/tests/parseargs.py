#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# gitum - Git Upstream Manager.
# Copyright (C) 2016  Konstantin Artyushkin <akv@etersfot.ru>
# This is a test of parse args for gitum 
# At the start test print nums of args
# At the end test print a dictionary of all parsed args

from gitupstream import *
import sys
import argparse

def main():
	print("length of argv is {}".format(len(sys.argv)))
#	print("arg is {}".format(sys.argv[2]))
#	remoteforpush=sys.argv[2]
	parser = argparse.ArgumentParser(description='Git Upstream Manager')
	parser.add_argument('--repo', 
		help='path to the gitum repo (does not take affect in clone command)')
	#parser.add_argument('--remote',
	#	help='path to the gitum repo (does not take affect in clone command)')
	subparsers = parser.add_subparsers(dest='command_name')

	push_p = subparsers.add_parser('push')
	push_p.add_argument('remote', nargs='?', help='remote repo to push to')
	push_p.add_argument('--track', action='store_true',
				help='save the remote to use by default')

	merge_p = subparsers.add_parser('merge')
	gr_merge = merge_p.add_mutually_exclusive_group()
	merge_p.add_argument('--track', action='store_true', help='save the branch to use by default')
	gr_merge.add_argument('--continue', action='store_true', help='continue a merge process')
	gr_merge.add_argument('--skip', action='store_true',
		help='skip the current patch in rebase and continue a merge process')
	gr_merge.add_argument('--abort', action='store_true', help='abort a merge process')
	gr_merge.add_argument('--branch', help='local/remote branch to merge from')

	update_p = subparsers.add_parser('update')
	update_p.add_argument('--message', metavar='text',
			help='specify the current branch commit message')

	create_p = subparsers.add_parser('create')
	create_p.add_argument('--remote', metavar='server/branch',
				help='remote branch to track with')
	create_p.add_argument('--current', metavar='branch',
				help='current development branch')
	create_p.add_argument('--upstream', metavar='branch',
				help='copy of tracked upstream branch')
	create_p.add_argument('--rebased', metavar='branch',
				help='branch with our patches on top')
	create_p.add_argument('--patches', metavar='branch',
				help='branch consists of our patches as files')

	remove_p = subparsers.add_parser('remove')
	remove_p.add_argument('--full', action='store_true',
				help='remove branches and config files (default)')
	remove_p.add_argument('--branches', action='store_true',
				help='remove branches')
	remove_p.add_argument('--configfiles', action='store_true',
				help='remove config files')

	restore_p = subparsers.add_parser('restore')
	restore_p.add_argument('--commit', metavar='commit/branch',
			help='restore rebased branch to a given commit')
	restore_p.add_argument('--full', action='store_true',
				help='restore full repository branches')

	clone_p = subparsers.add_parser('clone')
	clone_p.add_argument('git-repo', help='git repo to clone from')
	clone_p.add_argument('repo-dir', nargs='?', help='directory to clone to')

	pull_p = subparsers.add_parser('pull')
	pull_p.add_argument('remote', nargs='?', help='remote repo to pull from')
	pull_p.add_argument('--track', action='store_true',
				help='save the remote to use by default')
	gr_pull = pull_p.add_mutually_exclusive_group()
	gr_pull.add_argument('--resolved', action='store_true',
				help='continue a pull process')
	gr_pull.add_argument('--skip', action='store_true',
				help='skip the current patch and continue a pull process')
	gr_pull.add_argument('--abort', action='store_true',
				help='abort a pull process')


	status_p = subparsers.add_parser('status')

	args = vars(parser.parse_args(sys.argv[1:]))
	print(args)


if __name__ == "__main__":
	main()
