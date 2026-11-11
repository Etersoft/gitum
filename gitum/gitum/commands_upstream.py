"""Commands for working with upstream: variants, queue, prepare, backport, branches, mirrors."""

from pathlib import Path

from . import upstreamwork as uw
from .errors import EXIT_NEEDS_HUMAN, EXIT_OK


def short(sha):
	return sha[:12] if sha else '-'


def cmd_port(repo, args, out):
	data = uw.port(repo, args.name, args.to, args.branch)
	out.result(data, '%s: %d changes ported, %d already in the base' % (
		data['branch'], data['ported'], len(data['already_in_base'])))


def cmd_variants(repo, args, out):
	data = uw.variants(repo, args.name)
	lines = []
	for spec, v in data.items():
		lines.append('%s (%s): %s' % (spec, v['branch'], 'DIVERGED' if v['diverged'] else 'same changes'))
		for key, title in (('only_main', 'only in the main branch'), ('only_variant', 'only in the variant'),
				   ('differ', 'different content')):
			for s in v[key]:
				lines.append('  %s: %s' % (title, s))
		if args.range_diff:
			lines.append(v['range_diff'])
	out.result(data, '\n'.join(lines) or 'No variants.')
	return EXIT_NEEDS_HUMAN if any(v['diverged'] for v in data.values()) and args.check else EXIT_OK


def cmd_queue(repo, args, out):
	target = args.target or uw.DEFAULT_TARGET
	if args.action == 'add':
		meta = uw.queue_add(repo, args.name, args.position)
		out.result(meta, '%s queued at %d' % (args.name, meta['position']))
		return
	if args.action in ('pause', 'resume'):
		data = uw.queue_pause(repo, target, args.action == 'pause', args.reason or '')
		out.result(data, 'Queue %s %s.' % (target, 'paused' if data['paused'] else 'resumed'))
		return
	data = uw.queue(repo, args.target)
	if args.format == 'md':
		labels = repo.journal().config.queue.get('markdown', {})
		text = uw.queue_markdown(data, labels)
		if args.output:
			Path(args.output).write_text(text)
			out.result(data, 'Written %s' % args.output)
		else:
			out.result(data, text)
		return
	lines = []
	for t, q in data['queues'].items():
		if args.action == 'next':
			lines.append('%s: %s' % (t, q['next'] or 'nothing to send (%s)' % q['reason']))
			continue
		lines.append('== %s%s' % (t, ' (paused: %s)' % q['pause_reason'] if q['paused'] else ''))
		for i, r in enumerate(q['rows'], 1):
			blocked = (' blocked by ' + ', '.join(r['blocked_by'])) if r['blocked_by'] else ''
			version = (' ' + r['version']) if r['version'] else ''
			lines.append('%2d %-32s %-9s %3d patches%s%s' % (i, r['name'], r['state'] + version, r['patches'],
								      blocked, ('  ' + r['pr']) if r['pr'] else ''))
		lines.append('next: %s' % (q['next'] or 'none (%s)' % q['reason']))
	out.result(data, '\n'.join(lines))


def cmd_prepare(repo, args, out):
	strip = args.strip.split(',') if args.strip is not None else None
	data = uw.prepare(repo, args.name, args.onto, args.branch, args.push, args.directory, strip,
			  not args.no_lint, not args.no_depends, force=args.force)
	text = '%s: %d commits on %s' % (data['branch'], data['commits'], short(data['onto']))
	if data.get('pushed'):
		text += ', pushed to %s (open the merge request by hand)' % data['pushed']
	if data.get('range_diff'):
		text += '\n\nrange-diff of the saved series and the prepared branch:\n' + data['range_diff']
	out.result(data, text)


def cmd_backport(repo, args, out):
	data = uw.backport(repo, args.commits, args.into, args.series, args.drop_on)
	out.result(data, '\n'.join('%s %s <- %s %s' % (p['id'], short(p['sha']), short(p['origin']), p['subject'])
				  for p in data['picked']))


def cmd_branches(repo, args, out):
	data = uw.branches(repo, args.remote, args.prune)
	lines = []
	for r in data['branches']:
		flag = 'STALE' if r['stale'] else ('unknown' if r['unknown'] else '')
		lines.append('%-7s %-40s %-24s %-9s %s' % (r['where'], r['branch'], r['series'] or '-', r['state'] or '-', flag))
	if data['deleted']:
		lines.append('deleted: ' + ', '.join(data['deleted']))
	out.result(data, '\n'.join(lines) or 'No series branches.')


def cmd_mirror(repo, args, out):
	data = uw.mirror(repo, args.name)
	out.result(data, '%s: mirror %s, remote %s' % (data['upstream'], data['mirror'], data['remote']))


def register(sub):
	s = sub.add_parser('port', help='put a series onto another base as a variant branch')
	s.add_argument('name')
	s.add_argument('--to', required=True, help='upstream:NAME, layer:NAME or a ref')
	s.add_argument('--branch')
	s.set_defaults(func=cmd_port)

	s = sub.add_parser('variants', help='compare a series with its variants on other bases')
	s.add_argument('name')
	s.add_argument('--range-diff', action='store_true')
	s.add_argument('--check', action='store_true', help='exit 2 if a variant diverged')
	s.set_defaults(func=cmd_variants)

	s = sub.add_parser('queue', help='the upstream submission queue')
	s.add_argument('action', nargs='?', default='list', choices=['list', 'next', 'add', 'pause', 'resume'])
	s.add_argument('name', nargs='?')
	s.add_argument('--position', type=int)
	s.add_argument('--target', help='queue of this target (series field target; default: all)')
	s.add_argument('--reason', help='why the queue is paused')
	s.add_argument('--format', choices=['text', 'md'], default='text')
	s.add_argument('-o', '--output', help='write the markdown queue to a file')
	s.set_defaults(func=cmd_queue)

	s = sub.add_parser('prepare', help='build the upstream form of a series and push it to a fork')
	s.add_argument('name')
	s.add_argument('--onto', help='base for upstream (default: the upstream of the series)')
	s.add_argument('--branch', help='branch name in the fork (default: the series name)')
	s.add_argument('--push', metavar='REMOTE', help='push the result to this remote (a fork)')
	s.add_argument('--directory', metavar='PREFIX', help='the series changes a vendored tree under PREFIX')
	s.add_argument('--strip', metavar='TRAILERS', help='comma separated trailers to remove')
	s.add_argument('--no-lint', action='store_true')
	s.add_argument('--no-depends', action='store_true')
	s.add_argument('--force', action='store_true', help='include commits of dependencies not merged yet')
	s.set_defaults(func=cmd_prepare)

	s = sub.add_parser('backport', help='take upstream commits into a layer with an Origin: trailer')
	s.add_argument('commits', nargs='+', metavar='SHA|URL')
	s.add_argument('--into', required=True, metavar='LAYER')
	s.add_argument('--series')
	s.add_argument('--drop-on', metavar='REF', help='drop the backport when the base contains REF')
	s.set_defaults(func=cmd_backport)

	s = sub.add_parser('branches', help='series and upstream branches; stale ones after merge')
	s.add_argument('--remote', help='also look at branches pushed to this remote (a fork)')
	s.add_argument('--prune', action='store_true', help='delete stale branches')
	s.set_defaults(func=cmd_branches)

	s = sub.add_parser('mirror', help='keep a shared mirror of an upstream and fetch it')
	s.add_argument('name')
	s.set_defaults(func=cmd_mirror)
