"""gitum command line."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import state
from .errors import EXIT_OK, EXIT_UPDATED, GitumError
from .journal import JOURNAL_REF, JournalWriter
from .git import RefTransaction
from .repo import Repo


class Output:
	def __init__(self, as_json):
		self.as_json = as_json

	def result(self, data, text=''):
		if self.as_json:
			json.dump(data, sys.stdout, indent=1, ensure_ascii=False, default=str)
			sys.stdout.write('\n')
		elif text:
			print(text.rstrip('\n'))

	def error(self, err):
		if self.as_json:
			json.dump({'error': err.message, 'exit_code': err.exit_code, **err.details},
				  sys.stdout, indent=1, ensure_ascii=False, default=str)
			sys.stdout.write('\n')
		print('gitum: %s' % err.message, file=sys.stderr)


def parse_pair(value, what):
	if '=' not in value:
		raise argparse.ArgumentTypeError('%s must be NAME=VALUE' % what)
	return value.split('=', 1)


def short(sha):
	return sha[:12] if sha else '-'


# commands

def cmd_init(repo, args, out):
	if args.config:
		data = state.init(repo, config_text=Path(args.config).read_text())
	else:
		upstreams = []
		for spec in args.upstream:
			name, ref = parse_pair(spec, '--upstream')
			up = {'name': name, 'ref': ref}
			if args.release_tags:
				up['release-tags'] = args.release_tags
			upstreams.append(up)
		layers = []
		for spec in args.layer:
			name, rest = parse_pair(spec, '--layer')
			branch, _, base = rest.partition(':')
			layers.append({'name': name, 'branch': branch, 'base': base})
		data = state.init(repo, upstreams, layers)
	lines = ['Created %s: %s' % (JOURNAL_REF, short(data['journal']))]
	for name in data['layer_order']:
		l = data['layers'][name]
		lines.append('  layer %-12s %s..%s  %d changes' % (name, short(l['base']), short(l['tip']), l['changes']))
	out.result(data, '\n'.join(lines))


def cmd_save(repo, args, out):
	data = state.save(repo, args.layer or None, args.message)
	if not data['changed']:
		out.result(data, 'Nothing to save.')
		return EXIT_OK
	lines = []
	for name, l in data['layers'].items():
		lines.append('%s: %d changes (+%d -%d)' % (name, l['changes'], len(l['added']), len(l['removed'])))
	out.result(data, '\n'.join(lines))
	return EXIT_UPDATED if args.detailed_exitcode else EXIT_OK


def format_status(data):
	lines = []
	for name, up in data['upstreams'].items():
		where = up.get('tag') or short(up.get('sha'))
		behind = up.get('behind')
		lines.append('upstream %s (%s) at %s%s' % (name, up['ref'], where,
			', %d new commits' % behind if behind else ', moved (rewritten)' if up.get('moved') else ''))
	for name, l in data['layers'].items():
		notes = []
		if not l['exists']:
			notes.append('branch missing')
		if l.get('unsaved'):
			notes.append('%d unsaved commits (gitum save)' % l['unsaved'])
		if l.get('rewritten'):
			notes.append('branch rewritten outside gitum')
		if l.get('parent_moved'):
			notes.append('parent layer moved')
		if l.get('upstream_behind'):
			notes.append('%d upstream commits behind' % l['upstream_behind'])
		if l.get('upstream_moved'):
			notes.append('upstream moved')
		if l.get('checked_out'):
			notes.append('checked out in %s' % l['checked_out'])
		lines.append('  %-12s %-24s on %-20s %4s changes  %s' % (
			name, l['branch'], l['base'], l.get('changes', '-'), '; '.join(notes) or 'ok'))
	op = data.get('operation')
	if op:
		lines.append('%s in progress (run %s): %s' % (op['kind'], op['run'], op.get('message') or ''))
		if op.get('worktree'):
			lines.append('  work tree: %s' % op['worktree'])
	if data.get('interrupted_commit'):
		lines.append('an interrupted %s will be finished or undone by the next gitum command '
			     '(for example gitum continue)' % data['interrupted_commit'])
	if data.get('candidate'):
		lines.append('candidate waiting for review: run %s (gitum accept)' % data['candidate'].get('run'))
	if data.get('lock'):
		lock = data['lock']
		lines.append('locked by pid %s on %s since %s' % (lock.get('pid'), lock.get('host'), lock.get('since')))
	return '\n'.join(lines)


def cmd_status(repo, args, out):
	from . import changes as changes_mod
	from . import series as series_mod
	from .commands_changes import change_rows, format_change_table
	data = state.status(repo)
	journal = repo.journal()
	fates = {}
	for rec in changes_mod.load_all(journal).values():
		if not rec.get('gone'):
			fates[rec.get('fate', 'local')] = fates.get(rec.get('fate', 'local'), 0) + 1
	data['fates'] = fates
	all_series = series_mod.load_all(journal)
	states = {}
	for meta in all_series.values():
		states[meta.get('state', '')] = states.get(meta.get('state', ''), 0) + 1
	data['series'] = states
	text = format_status(data)
	if fates:
		text += '\nchanges: ' + ', '.join('%s %d' % kv for kv in sorted(fates.items()))
	if states:
		text += '\nseries: ' + ', '.join('%s %d' % kv for kv in sorted(states.items()))
	if args.changes:
		rows = [r for r in change_rows(journal) if args.all or r['action']]
		data['changes'] = rows
		text += '\n\n' + (format_change_table(rows) or 'No changes need attention.')
	if args.series and all_series:
		from .commands_changes import format_tree, series_tree
		rows = series_tree(repo, journal, args.variants)
		data['series_tree'] = rows
		text += '\n\n' + format_tree(rows)
	out.result(data, text)


def cmd_config(repo, args, out):
	journal = repo.journal()
	if args.action == 'show':
		out.result({'config': journal.config_text}, journal.config_text)
		return
	text = Path(args.file).read_text()
	config_mod.parse(text)
	with repo.lock():
		journal = repo.journal()
		writer = JournalWriter(repo.git, journal.sha)
		writer.put(config_mod.CONFIG_FILE, text)
		new = writer.write('gitum: update configuration')
		tr = RefTransaction(repo.git)
		tr.update(JOURNAL_REF, new, journal.sha)
		tr.commit()
	out.result({'journal': new}, 'Configuration updated.')


def cmd_log(repo, args, out):
	if args.target:
		from .commands_changes import object_history
		from . import changes as changes_mod
		entries = object_history(repo, args.target)
		if entries is None:
			rec = changes_mod.Tracker(repo.journal()).get(args.target)
			entries = rec.get('events', [])
			out.result(entries, '\n'.join('%s %-16s %s' % (e['date'], e['event'], ' '.join(
				'%s=%s' % (k, v) for k, v in e.items() if k not in ('date', 'event'))) for e in entries))
			return
		out.result(entries, '\n'.join('%s %s %s' % (short(e['sha']), e['date'], e['subject']) for e in entries))
		return
	journal = repo.journal()
	entries = []
	for sha in journal.history(args.max_count):
		info = repo.git.commit_info(sha)
		entries.append({'sha': sha, 'subject': info['subject']})
	out.result(entries, '\n'.join('%s %s' % (short(e['sha']), e['subject']) for e in entries))


def build_parser():
	from .commands import register
	p = argparse.ArgumentParser(prog='gitum', description='Git Upstream Manager: develop in parallel with upstream')
	p.add_argument('--version', action='version', version='%(prog)s ' + __version__)
	p.add_argument('--repo', default='.', help='repository path (default: current directory)')
	p.add_argument('--json', action='store_true', help='machine-readable output')
	p.add_argument('--detailed-exitcode', action='store_true',
		       help='exit with 10 when something was updated, 0 when nothing changed')
	sub = p.add_subparsers(dest='command', required=True, metavar='command')

	s = sub.add_parser('init', help='create the journal from existing branches')
	s.add_argument('--upstream', action='append', default=[], metavar='NAME=REF',
		       help='upstream to follow, e.g. winehq=winehq/master')
	s.add_argument('--release-tags', metavar='GLOB', help='release tags of the upstream, e.g. wine-*')
	s.add_argument('--layer', action='append', default=[], metavar='NAME=BRANCH[:BASE]',
		       help='layer branch and the upstream or layer it is based on')
	s.add_argument('--config', metavar='FILE', help='use this gitum.toml instead of --upstream/--layer')
	s.set_defaults(func=cmd_init)

	s = sub.add_parser('status', help='where every layer stands')
	s.add_argument('--changes', action='store_true', help='table: change -> fate -> action needed')
	s.add_argument('--series', action='store_true', help='series as a tree by dependencies')
	s.add_argument('--variants', action='store_true', help='with --series: check whether variants diverged')
	s.add_argument('--all', action='store_true', help='with --changes: also changes without an action')
	s.set_defaults(func=cmd_status)

	s = sub.add_parser('save', help='record the current layer branches in the journal')
	s.add_argument('layer', nargs='*')
	s.add_argument('-m', '--message', default='')
	s.set_defaults(func=cmd_save)

	s = sub.add_parser('config', help='show or replace gitum.toml')
	s.add_argument('action', choices=['show', 'load'])
	s.add_argument('file', nargs='?')
	s.set_defaults(func=cmd_config)

	s = sub.add_parser('log', help='journal history, or the history of a change, series:NAME or layer:NAME')
	s.add_argument('target', nargs='?')
	s.add_argument('-n', '--max-count', type=int)
	s.set_defaults(func=cmd_log)

	register(sub)
	return p


def main(argv=None):
	argv = list(sys.argv[1:] if argv is None else argv)
	args = build_parser().parse_args(argv)
	out = Output(args.json)
	try:
		repo = Repo(args.repo)
		code = args.func(repo, args, out)
	except GitumError as e:
		out.error(e)
		return e.exit_code
	except KeyboardInterrupt:
		print('gitum: interrupted', file=sys.stderr)
		return 130
	return code or EXIT_OK


if __name__ == '__main__':
	sys.exit(main())
