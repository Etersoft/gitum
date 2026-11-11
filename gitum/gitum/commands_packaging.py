"""Commands for packaging: patch set export/import and run reports."""

from pathlib import Path

from . import packaging
from .errors import GitumError


def cmd_export(repo, args, out):
	data = packaging.export(repo, args.what, args.format, args.dir, args.spec, args.prefix or '', args.first,
				args.clean)
	text = '%d patches in %s' % (len(data.get('files', data.get('written', []))), data.get('directory', args.dir))
	if args.format == 'spec' and not args.spec:
		text += '\n' + '\n'.join(data['spec_lines'])
	elif data.get('spec_updated') is not None:
		text += '; %s %s' % (args.spec, 'updated' if data['spec_updated'] else 'unchanged')
	out.result(data, text)


def cmd_import(repo, args, out):
	data = packaging.import_patches(repo, args.into, args.source, guess_format(args))
	out.result(data, 'Imported %d patches into %s.' % (len(data['imported']), args.into))


def cmd_report(repo, args, out):
	report = packaging.load_report(repo, args.run)
	if args.html:
		Path(args.html).write_text(packaging.html_report(report))
		out.result({'run': report['run'], 'html': args.html}, 'Written %s' % args.html)
		return
	from .commands import format_report
	out.result(report, format_report(report))


def register(sub):
	s = sub.add_parser('export', help='write the patches of a layer or series (quilt, spec, staging)')
	s.add_argument('what', metavar='layer:NAME|series:NAME|BASE..TIP')
	s.add_argument('--format', choices=['quilt', 'spec', 'staging'], default='spec')
	s.add_argument('--dir', required=True)
	s.add_argument('--spec', help='update the Patch lines between the gitum markers of this spec file')
	s.add_argument('--prefix', help='prefix of the patch file names')
	s.add_argument('--first', type=int, default=1, help='number of the first PatchN')
	s.add_argument('--clean', action='store_true', help='remove old numbered patches first')
	s.set_defaults(func=cmd_export)

	s = sub.add_parser('import', help='apply a quilt series or the patches of a spec onto a layer')
	s.add_argument('source', help='quilt directory or spec file')
	s.add_argument('--into', required=True, metavar='LAYER')
	s.add_argument('--format', choices=['quilt', 'spec'])
	s.set_defaults(func=cmd_import)

	s = sub.add_parser('report', help='a sync report from the journal (text or HTML)')
	s.add_argument('run', nargs='?', help='run id (default: the last one)')
	s.add_argument('--html', metavar='FILE')
	s.set_defaults(func=cmd_report)


def guess_format(args):
	if args.format:
		return args.format
	if str(args.source).endswith('.spec'):
		return 'spec'
	if Path(args.source).is_dir():
		return 'quilt'
	raise GitumError('cannot guess the format of %s: use --format' % args.source)
