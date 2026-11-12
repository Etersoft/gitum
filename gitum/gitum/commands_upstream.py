"""Commands for working with upstream: variants, queue, prepare, backport, branches, mirrors."""

from . import mrtext
from . import upstreamwork as uw
from .cmdline import done, pairs, write_output
from .errors import EXIT_NEEDS_HUMAN, EXIT_OK
from .util import short


def cmd_port(repo, args, out):
	data = uw.port(repo, args.name, args.onto, args.branch)
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
	action = args.action or 'list'
	target = args.target or uw.DEFAULT_TARGET
	if action == 'add':
		meta = uw.queue_add(repo, args.name, args.position)
		out.result(meta, '%s queued at %d' % (args.name, meta['position']))
		return done(args, True)
	if action in ('pause', 'resume'):
		data = uw.queue_pause(repo, target, action == 'pause', getattr(args, 'reason', None) or '')
		out.result(data, 'Queue %s %s.' % (target, 'paused' if data['paused'] else 'resumed'))
		return
	data = uw.queue(repo, args.target)
	if getattr(args, 'format', 'text') == 'md':
		labels = repo.journal().config.queue.get('markdown', {})
		text = uw.queue_markdown(data, labels)
		out.result(data, write_output(args.output, text) if args.output else text)
		return
	lines = []
	for t, q in data['queues'].items():
		if action == 'next':
			lines.append('%s: %s' % (t, q['next'] or 'nothing to send (%s)' % q['reason']))
			continue
		lines.append('== %s%s' % (t, ' (paused: %s)' % q['pause_reason'] if q['paused'] else ''))
		for i, r in enumerate(q['rows'], 1):
			blocked = (' blocked by ' + ', '.join(r['blocked_by'])) if r['blocked_by'] else ''
			version = (' ' + r['version']) if r['version'] else ''
			lines.append('%2d %-32s %-9s %3d patches%s%s' % (i, r['name'], r['state'] + version, r['patches'],
								      blocked, ('  ' + r['pr']) if r['pr'] else ''))
		lines.append('next: %s' % (q['next'] or 'none (%s)' % q['reason']))
	text = '\n'.join(lines)
	out.result(data, write_output(args.output, text) if getattr(args, 'output', None) else text)


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
	return done(args, bool(data['deleted']))


def cmd_mirror(repo, args, out):
	data = uw.mirror(repo, args.name)
	out.result(data, '%s: mirror %s, remote %s' % (data['upstream'], data['mirror'], data['remote']))


def cmd_mr(repo, args, out):
	if args.action == 'record':
		data = mrtext.record_uploads(repo, args.name, args.forge, args.project, pairs(args.pairs, 'FILE=URL'))
		out.result(data, 'Remembered %s for %s' % (', '.join(data['recorded']), data['forge']))
		return done(args, True)
	if args.action == 'render':
		data = mrtext.render(repo, args.name, args.forge, args.project, args.internal, args.assets_dir)
		text = ('# %s\n\n' % data['title'] if data['title'] else '') + data['description']
		if args.output:
			text = write_output(args.output, text)
		notes = ['upload %s and replace %s, then: gitum mr record %s %s=URL --forge %s'
			 % (p['path'], p['placeholder'], args.name, p['file'], args.forge or 'F') for p in data['pending']]
		out.result(data, text.rstrip('\n') + ('\n\n' + '\n'.join(notes) if notes else ''))
		return
	if args.action == 'set':
		data = mrtext.set_from_file(repo, args.name, args.file)
	else:
		data = mrtext.import_mr(repo, args.name, args.mr)
	out.result(data, '%s: merge request text%s, %d images' % (
		args.name, (' "%s"' % data['title']) if data['title'] else '', len(data['assets'])))
	return done(args, True)


def register(sub):
	s = sub.add_parser('mr', help='the merge request text of a series, with images')
	ms = s.add_subparsers(dest='action', required=True, metavar='action')
	p = ms.add_parser('set', help='take the text from a markdown file; local images it shows become assets')
	p.add_argument('name', metavar='SERIES')
	p.add_argument('file')
	p = ms.add_parser('import', help='take the title, text and images of a public merge request')
	p.add_argument('name', metavar='SERIES')
	p.add_argument('mr', metavar='[FORGE:]GROUP/PROJECT!N|URL')
	p = ms.add_parser('render', help='the text for a forge: known image URLs, placeholders for the rest')
	p.add_argument('name', metavar='SERIES')
	p.add_argument('--forge', help='the [forge.NAME] the text is for (its recorded image URLs are used)')
	p.add_argument('--project', help='the project there (default: from the series links or the upstream remote)')
	p.add_argument('--internal', action='store_true', help='keep the internal parts')
	p.add_argument('-o', '--output', metavar='FILE')
	p.add_argument('--assets-dir', metavar='DIR', help='write images to upload to DIR/SERIES/ '
		       '(default: [series] mr-assets-dir, or <common dir>/gitum/mr)')
	p = ms.add_parser('record', help='remember the URLs of images uploaded outside gitum')
	p.add_argument('name', metavar='SERIES')
	p.add_argument('pairs', nargs='+', metavar='FILE=URL')
	p.add_argument('--forge', required=True)
	p.add_argument('--project')
	s.set_defaults(func=cmd_mr)

	s = sub.add_parser('port', help='put a series onto another base as a variant branch')
	s.add_argument('name')
	s.add_argument('--onto', required=True, help='upstream:NAME, layer:NAME or a ref')
	s.add_argument('--branch')
	s.set_defaults(func=cmd_port)

	s = sub.add_parser('variants', help='compare a series with its variants on other bases')
	s.add_argument('name')
	s.add_argument('--range-diff', action='store_true')
	s.add_argument('--check', action='store_true', help='exit 2 if a variant diverged')
	s.set_defaults(func=cmd_variants)

	s = sub.add_parser('queue', help='the upstream submission queue (default: list)')
	qs = s.add_subparsers(dest='action', metavar='action')
	p = qs.add_parser('list', help='the queues in order, with what to send next')
	p.add_argument('--target', help='queue of this target (series field target; default: all)')
	p.add_argument('--format', choices=['text', 'md'], default='text')
	p.add_argument('-o', '--output', metavar='FILE')
	p = qs.add_parser('next', help='the series to send next')
	p.add_argument('--target')
	p = qs.add_parser('add', help='queue a series')
	p.add_argument('name', metavar='SERIES')
	p.add_argument('--position', type=int)
	p = qs.add_parser('pause', help='stop sending to a target')
	p.add_argument('--target')
	p.add_argument('--reason')
	p = qs.add_parser('resume', help='send to a target again')
	p.add_argument('--target')
	s.set_defaults(func=cmd_queue, target=None)

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
