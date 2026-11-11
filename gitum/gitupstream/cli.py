# gitum - Git Upstream Manager.
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

import argparse
import re
import sys

from . import __version__
from .constants import MAINLINE_BRANCH, PATCHES_BRANCH, REBASED_BRANCH, REMOTE_BRANCH, UPSTREAM_BRANCH
from .errors import GitUmException
from .gitupstream import GitUpstream


def build_parser():
	parser = argparse.ArgumentParser(prog='gitum', description='Git Upstream Manager')
	parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
	parser.add_argument('--repo', default='.',
		help='path to the gitum repo (does not take affect in clone command)')
	subparsers = parser.add_subparsers(dest='command_name', required=True)

	merge_p = subparsers.add_parser('merge', help='merge upstream changes into work branches')
	gr_merge = merge_p.add_mutually_exclusive_group()
	merge_p.add_argument('--track', action='store_true', help='save the branch to use by default')
	gr_merge.add_argument('--continue', action='store_true', help='continue a merge process')
	gr_merge.add_argument('--skip', action='store_true',
		help='skip the current patch in rebase and continue a merge process')
	gr_merge.add_argument('--abort', action='store_true', help='abort a merge process')
	gr_merge.add_argument('--branch', help='local/remote branch to merge from')

	update_p = subparsers.add_parser('update', help='save changes of the rebased branch')
	update_p.add_argument('--message', metavar='text', default='',
			help='specify the current branch commit message')

	create_p = subparsers.add_parser('create', help='create work branches')
	create_p.add_argument('--remote', metavar='server/branch', default=REMOTE_BRANCH,
				help='remote branch to track with')
	create_p.add_argument('--current', metavar='branch', default=MAINLINE_BRANCH,
				help='current development branch')
	create_p.add_argument('--upstream', metavar='branch', default=UPSTREAM_BRANCH,
				help='copy of tracked upstream branch')
	create_p.add_argument('--rebased', metavar='branch', default=REBASED_BRANCH,
				help='branch with our patches on top')
	create_p.add_argument('--patches', metavar='branch', default=PATCHES_BRANCH,
				help='branch consists of our patches as files')

	remove_p = subparsers.add_parser('remove', help='remove work branches and config files')
	remove_p.add_argument('--full', action='store_true',
				help='remove branches and config files (default)')
	remove_p.add_argument('--branches', action='store_true',
				help='remove branches')
	remove_p.add_argument('--configfiles', action='store_true',
				help='remove config files')

	restore_p = subparsers.add_parser('restore', help='restore work branches from the patches branch')
	restore_p.add_argument('--commit', metavar='commit/branch',
			help='restore rebased branch to a given commit')
	restore_p.add_argument('--full', action='store_true',
				help='restore full repository branches')

	clone_p = subparsers.add_parser('clone', help='clone a gitum repo')
	clone_p.add_argument('git_repo', metavar='git-repo', help='git repo to clone from')
	clone_p.add_argument('repo_dir', metavar='repo-dir', nargs='?', help='directory to clone to')

	pull_p = subparsers.add_parser('pull', help='pull work branches from a gitum repo')
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

	push_p = subparsers.add_parser('push', help='push work branches to a gitum repo')
	push_p.add_argument('remote', nargs='?', help='remote repo to push to')
	push_p.add_argument('--track', action='store_true',
				help='save the remote to use by default')

	subparsers.add_parser('status', help='show changes not saved by gitum update')
	return parser


def default_clone_dir(git_repo):
	"""Directory name for clone: the last path component without .git suffix."""
	name = re.split(r'[/:]', git_repo.rstrip('/'))[-1]
	return name.removesuffix('.git') or name


def run_command(args):
	if args.command_name == 'clone':
		repo_dir = args.repo_dir or default_clone_dir(args.git_repo)
		GitUpstream(repo_path=repo_dir, with_log=True, new_repo=True).clone(args.git_repo)
		return

	repo = GitUpstream(args.repo, with_log=True)
	cmd = args.command_name
	if cmd == 'merge':
		if args.__dict__['continue']:
			repo.continue_merge('--continue')
		elif args.skip:
			repo.continue_merge('--skip')
		elif args.abort:
			repo.abort()
		else:
			repo.merge(args.branch, track_with=args.track)
	elif cmd == 'update':
		repo.update(args.message)
	elif cmd == 'create':
		repo.create(args.remote, args.upstream, args.rebased, args.current, args.patches)
	elif cmd == 'restore':
		repo.restore(args.commit, rebased_only=not args.full)
	elif cmd == 'remove':
		if args.branches:
			repo.remove_branches()
		elif args.configfiles:
			repo.remove_config_files()
		else:
			repo.remove_all()
	elif cmd == 'pull':
		if args.resolved:
			repo.continue_pull('--resolved')
		elif args.skip:
			repo.continue_pull('--skip')
		elif args.abort:
			repo.abort(am=True)
		else:
			repo.pull(args.remote, track_with=args.track)
	elif cmd == 'push':
		repo.push(args.remote, track_with=args.track)
	elif cmd == 'status':
		repo.status()


def main(argv=None):
	args = build_parser().parse_args(argv)
	try:
		run_command(args)
	except GitUmException as e:
		# the details are already logged by GitUpstream
		if str(e):
			print('error: %s' % e, file=sys.stderr)
		return 1
	return 0


if __name__ == '__main__':
	sys.exit(main())
