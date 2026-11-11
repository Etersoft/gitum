"""Commands of gitum 2 beyond the basic ones in cli.py."""

from . import publish, sync
from .errors import EXIT_OK, EXIT_UPDATED


def short(sha):
	return sha[:12] if sha else '-'


def format_report(report):
	lines = ['Synced to %s (run %s)' % (report['target'], report['run'])]
	for name, l in report['layers'].items():
		if l.get('unchanged'):
			lines.append('  %-12s unchanged' % name)
			continue
		counts = ', '.join('%s %d' % (k, v) for k, v in sorted(l['counts'].items())) or 'no changes'
		lines.append('  %-12s %s..%s -> %s..%s  %s' % (name, short(l['old_base']), short(l['old_tip']),
							       short(l['new_base']), short(l['new_tip']), counts))
		for r in l['results']:
			if r['result'] != sync.PICKED:
				extra = ' [%s]' % r['resolver'] if r.get('resolver') else ''
				culprit = ' (broken by %s %s)' % (short(r['culprit']['sha']), r['culprit']['subject']) \
					if r.get('culprit') else ''
				lines.append('      %-17s %s%s%s' % (r['result'], r['subject'], extra, culprit))
	return '\n'.join(lines)


def _result(args, out, data):
	if not data.get('changed'):
		out.result(data, data.get('message', 'Nothing to do.'))
		return EXIT_OK
	if data.get('steps'):
		text = '\n'.join(format_report(r) for r in data['steps'])
	else:
		text = format_report(data['report']) if data.get('report') else ''
	if data.get('published'):
		text += '\nPublished to %s.' % data['published']['remote']
	out.result(data, text)
	return EXIT_UPDATED if args.detailed_exitcode else EXIT_OK


def cmd_sync(repo, args, out):
	to = {}
	for spec in args.to or []:
		name, sep, ref = spec.partition('=')
		if sep:
			to[name] = ref
		else:
			to[None] = spec
	data = sync.sync(repo, to=to or None, next_release_=args.next_release, candidate=args.candidate,
			 publish=args.publish, fetch=not args.no_fetch, find_culprit=args.find_culprit,
			 update_checked_out=args.update_checked_out, wait=args.wait, step=args.step)
	return _result(args, out, data)


def cmd_resume(repo, args, out):
	data = sync.resume(repo, args.command)
	if data.get('aborted'):
		out.result(data, data['message'])
		return EXIT_OK
	return _result(args, out, data)


def cmd_accept(repo, args, out):
	return _result(args, out, sync.accept(repo, args.publish))


def cmd_drop_candidate(repo, args, out):
	data = sync.drop_candidate(repo)
	out.result(data, 'Candidate of run %s dropped.' % data['dropped'])


def cmd_publish(repo, args, out):
	data = publish.publish(repo, args.remote)
	text = 'Published to %s.' % data['remote']
	for name, subjects in data['integrated'].items():
		text += '\n%s: put on top: %s' % (name, '; '.join(subjects))
	out.result(data, text)


def cmd_pull(repo, args, out):
	data = publish.pull(repo, args.remote, args.update_checked_out)
	if not data['changed']:
		out.result(data, 'Already up to date.')
		return EXIT_OK
	out.result(data, 'Pulled %s from %s; moved: %s' % (short(data['journal']), data['remote'],
							     ', '.join(data['moved']) or 'nothing'))
	return EXIT_UPDATED if args.detailed_exitcode else EXIT_OK


def register(sub):
	s = sub.add_parser('sync', help='move the layer stack onto a new upstream in one pass')
	g = s.add_mutually_exclusive_group()
	g.add_argument('--to', action='append', metavar='[NAME=]REF', help='target commit or tag of an upstream')
	g.add_argument('--next-release', action='store_true', help='the next release tag after the current base')
	s.add_argument('--candidate', choices=['auto', 'always', 'never'], default='auto',
		       help='put the result into candidate branches for a review (auto: when a resolver '
			    'not listed in resolve.review-unless was used)')
	s.add_argument('--publish', action='store_true', help='publish the result')
	s.add_argument('--no-fetch', action='store_true', help='do not fetch the upstream remotes')
	s.add_argument('--step', action='store_true',
		       help='one upstream commit at a time, recording a state after each (for bisect)')
	s.add_argument('--find-culprit', action='store_true',
		       help='on a conflict, find the upstream commit that broke the patch')
	s.add_argument('--update-checked-out', action='store_true',
		       help='also move layer branches checked out in clean work trees')
	s.add_argument('--wait', action='store_true', help='wait for the repository lock')
	s.set_defaults(func=cmd_sync)
	for name, help_ in (('continue', 'continue a stopped sync after resolving the conflict'),
			    ('skip', 'drop the conflicting change and continue'),
			    ('abort', 'stop the sync; branches stay as they were')):
		s = sub.add_parser(name, help=help_)
		s.set_defaults(func=cmd_resume)
	s = sub.add_parser('accept', help='publish the reviewed candidate')
	s.add_argument('--publish', action='store_true')
	s.set_defaults(func=cmd_accept)
	s = sub.add_parser('drop-candidate', help='throw the candidate away')
	s.set_defaults(func=cmd_drop_candidate)
	s = sub.add_parser('publish', help='push layers, history and journal')
	s.add_argument('remote', nargs='?')
	s.set_defaults(func=cmd_publish)
	s = sub.add_parser('pull', help='take the published state')
	s.add_argument('remote', nargs='?')
	s.add_argument('--update-checked-out', action='store_true')
	s.set_defaults(func=cmd_pull)
