"""Commands for packaging: patch sets out and in (quilt, spec, wine-staging) and run reports."""

from pathlib import Path

from . import packaging
from . import series as series_mod
from .cmdline import UsageError, done, write_output
from .errors import EXIT_NEEDS_HUMAN, GitumError


def cmd_export(repo, args, out):
	if args.format == 'staging':
		bad = [o for o, v in (('--spec', args.spec), ('--prefix', args.prefix), ('--clean', args.clean),
				      ('--dep3', args.dep3), ('--verify', args.verify)) if v]
		if bad:
			raise UsageError('%s do not apply to --format staging' % ', '.join(bad))
		names = []
		for what in args.what:
			kind, _, name = what.partition(':')
			if kind != 'series':
				raise UsageError('--format staging writes series: series:NAME or series (all), not %s' % what)
			names += [name] if name else []
		if 'series' in args.what:
			names = []
		data = series_mod.export(repo, names, args.dir, args.check)
		if args.check:
			out.result(data, ('Stale: ' + ', '.join(data['stale'])) if data['stale'] else 'Up to date.')
			return EXIT_NEEDS_HUMAN if data['stale'] else None
		out.result(data, 'Written: %s' % (', '.join(data['written']) or 'nothing (up to date)'))
		return done(args, bool(data['written']))
	if args.check:
		raise UsageError('--check compares a wine-staging copy: use it with --format staging')
	if len(args.what) != 1:
		raise UsageError('--format %s writes one layer, series or range' % args.format)
	if args.format == 'quilt' and (args.spec or args.first != 1):
		raise UsageError('--spec and --first are for --format spec')
	data = packaging.export(repo, args.what[0], args.format, args.dir, args.spec, args.prefix or '', args.first,
				args.clean, args.dep3, args.verify)
	text = '%d patches in %s' % (len(data['files']), data['directory'])
	if data.get('removed'):
		text += ' (removed %d old ones)' % len(data['removed'])
	if data.get('verified'):
		text += '; they rebuild the tip exactly'
	if args.format == 'spec' and not args.spec:
		text += '\n' + '\n'.join(data['spec_lines'])
	elif data.get('spec_updated') is not None:
		text += '; %s %s' % (args.spec, 'updated' if data['spec_updated'] else 'unchanged')
	out.result(data, text)
	return done(args, bool(data['files'] or data.get('removed')))


def cmd_import(repo, args, out):
	fmt = guess_format(args)
	if fmt == 'staging':
		if args.into or not args.base:
			raise UsageError('--format staging creates series: give --base (and no --into)')
		data = series_mod.import_staging(repo, args.source, args.base, args.series or None)
		out.result(data, 'Imported %d series.' % len(data['imported']))
		return done(args, True)
	if args.base or args.series or not args.into:
		raise UsageError('--format %s applies patches onto a layer: give --into (and no --base, --series)' % fmt)
	data = packaging.import_patches(repo, args.into, args.source, fmt)
	out.result(data, 'Imported %d patches into %s.' % (len(data['imported']), args.into))
	return done(args, True)


def cmd_report(repo, args, out):
	report = packaging.load_report(repo, args.run)
	if args.format == 'html':
		text = packaging.html_report(report)
	else:
		from .commands import format_report
		text = format_report(report)
	if args.output:
		out.result({'run': report['run'], 'format': args.format, 'output': args.output},
			   write_output(args.output, text))
		return
	out.result(report, text)


def register(sub):
	s = sub.add_parser('export', help='write patches: a layer or series (quilt, spec) or series (wine-staging)')
	s.add_argument('what', nargs='+', metavar='layer:NAME|series:NAME|series|BASE..TIP')
	s.add_argument('--format', required=True, choices=['quilt', 'spec', 'staging'])
	s.add_argument('--dir', required=True)
	s.add_argument('--check', action='store_true', help='staging: only report stale copies (exit 2 if any)')
	s.add_argument('--spec', help='spec: update the Patch lines between the gitum markers of this spec file')
	s.add_argument('--prefix', help='quilt, spec: prefix of the patch file names')
	s.add_argument('--first', type=int, default=1, help='spec: number of the first PatchN')
	s.add_argument('--clean', action='store_true', help='quilt, spec: remove the old patches gitum wrote there')
	s.add_argument('--dep3', action='store_true', help='quilt, spec: write the fate of each change as DEP-3 fields')
	s.add_argument('--verify', action='store_true',
		       help='quilt, spec: apply the written patches onto the base and require the tree of the tip')
	s.set_defaults(func=cmd_export)

	s = sub.add_parser('import', help='read patches: onto a layer (quilt, spec) or as series (wine-staging)')
	s.add_argument('source', help='quilt directory, spec file, or wine-staging patches directory')
	s.add_argument('--format', choices=['quilt', 'spec', 'staging'], help='default: guessed from the source')
	s.add_argument('--into', metavar='LAYER', help='quilt, spec: the layer to apply the patches to')
	s.add_argument('--base', help='staging: base of the imported series, e.g. upstream:winehq')
	s.add_argument('--series', action='append', metavar='NAME', help='staging: only these series')
	s.set_defaults(func=cmd_import)

	s = sub.add_parser('report', help='a sync report from the journal')
	s.add_argument('run', nargs='?', help='run id or its prefix (default: the last one)')
	s.add_argument('--format', choices=['text', 'html'], default='text')
	s.add_argument('-o', '--output', metavar='FILE')
	s.set_defaults(func=cmd_report)


def guess_format(args):
	if args.format:
		return args.format
	source = Path(args.source)
	if str(source).endswith('.spec'):
		return 'spec'
	if (source / 'series').exists() or (source / 'patches' / 'series').exists():
		return 'quilt'
	if source.is_dir() and any(source.glob('*/definition')) or any(source.glob('*/*/definition')):
		return 'staging'
	raise GitumError('cannot guess the format of %s: use --format' % args.source)
