"""Commands for changes, series, upstream checks and forges."""

import json
import sys
from pathlib import Path

from . import bugs as bugs_mod
from . import changes as changes_mod
from . import forge, series, upstreamcheck
from .errors import EXIT_NEEDS_HUMAN, EXIT_OK, GitumError
from .git import RefTransaction
from .journal import JOURNAL_REF, JournalWriter


def short(sha):
	return sha[:12] if sha else '-'


def parse_values(pairs):
	values = {}
	for pair in pairs:
		key, sep, value = pair.partition('=')
		if not sep:
			raise GitumError('expected KEY=VALUE, got %r' % pair)
		values[key.replace('-', '_')] = value
	return values


def change_rows(journal, layer=None, fate=None, include_gone=False):
	rows = []
	for rec in sorted(changes_mod.load_all(journal).values(), key=lambda c: (c.get('layer', ''), c.get('subject', ''))):
		if rec.get('gone') and not include_gone:
			continue
		if layer and rec.get('layer') != layer:
			continue
		if fate and rec.get('fate') != fate:
			continue
		rows.append({**rec, 'action': changes_mod.action(rec)})
	return rows


def format_change_table(rows):
	lines = []
	for r in rows:
		meta = r.get('meta', {})
		link = meta.get('pr') or meta.get('upstream_commit', '')[:12] or meta.get('reason', '')
		lines.append('%-8s %-16s %-17s %-50.50s %s%s' % (r['id'], r.get('layer', ''), r.get('fate', ''),
							     r.get('subject', ''), r['action'],
							     ('  [%s]' % link) if link else ''))
	return '\n'.join(lines)


# change

def cmd_change(repo, args, out):
	journal = repo.journal()
	if args.action == 'list':
		rows = change_rows(journal, args.layer, args.fate, args.all)
		out.result(rows, format_change_table(rows) or 'No changes.')
		return
	if args.action == 'confirm':
		data = confirm(repo, args.ref, args.values[0] if args.values else None)
		out.result(data, '%s: %s as %s%s' % (data['id'], data['fate'], short(data['meta']['upstream_commit']),
						    ' in ' + data['meta']['release'] if data['meta'].get('release') else ''))
		return
	if args.action == 'show':
		rec = changes_mod.Tracker(journal).get(args.ref)
		rec = {**rec, 'action': changes_mod.action(rec)}
		lines = ['%s  %s' % (rec['id'], rec.get('subject', '')),
			 'layer %s, sha %s, fate %s' % (rec.get('layer'), short(rec.get('sha')), rec.get('fate'))]
		for k, v in sorted(rec.get('meta', {}).items()):
			lines.append('  %s: %s' % (k, ', '.join(v) if isinstance(v, list) else v))
		if rec['action']:
			lines.append('action: ' + rec['action'])
		for e in rec.get('events', []):
			extra = ' '.join('%s=%s' % (k, v) for k, v in e.items() if k not in ('date', 'event'))
			lines.append('  %s %-16s %s' % (e['date'], e['event'], extra))
		out.result(rec, '\n'.join(lines))
		return
	# set
	values = parse_values(args.values)
	fate = values.pop('fate', None)
	if values.get('pr'):
		values['pr'] = series.normalize_link(repo.journal().config, 'pr', values['pr'])
	with repo.lock():
		journal = repo.journal()
		tracker = changes_mod.Tracker(journal)
		recs = [tracker.set(ref, fate, **values) for ref in args.ref.split(',')]
		writer = JournalWriter(repo.git, journal.sha)
		tracker.write(writer)
		new = writer.write('gitum: change %s: %s' % (args.ref, ' '.join(args.values)))
		tr = RefTransaction(repo.git)
		tr.update(JOURNAL_REF, new, journal.sha)
		tr.commit()
	out.result(recs, '\n'.join('%s: %s' % (r['id'], r['fate']) for r in recs))


# series

def confirm(repo, ref, sha=None):
	"""A person confirms that a change was accepted upstream (as the remembered candidate or sha)."""
	git = repo.git
	with repo.lock():
		journal = repo.journal()
		tracker = changes_mod.Tracker(journal)
		rec = tracker.get(ref)
		commit = git.rev(sha) if sha else git.rev(rec['meta'].get('candidate', '') or 'none-given')
		if not commit:
			raise GitumError('change %s: give the upstream commit (no candidate remembered)' % rec['id'])
		cfg = journal.config
		upstream = None
		layer = rec.get('layer', '')
		if layer in cfg.layers:
			upstream = cfg.root_upstream(cfg.layers[layer])
		elif layer.startswith('series:'):
			meta = series.load_all(journal).get(layer.split(':', 1)[1], {})
			up_name = meta.get('upstream') or meta.get('base', '').partition(':')[2]
			upstream = cfg.upstreams.get(up_name)
		rec = tracker.set(rec['id'], 'merged-modified', upstream_commit=commit, candidate='',
				  release=upstreamcheck.first_release(git, upstream, commit))
		writer = JournalWriter(git, journal.sha)
		tracker.write(writer)
		all_series = series.load_all(journal)
		merged, filled = upstreamcheck.merged_series(tracker, all_series, {layer}, git) \
			if layer.startswith('series:') else ({}, [])
		for name in list(merged) + filled:
			writer.put_json(series.meta_path(name), all_series[name])
		rec = {**rec, 'series_merged': sorted(merged), 'release_filled': filled}
		new = writer.write('gitum: change %s confirmed as %s' % (rec['id'], commit[:12]))
		tr = RefTransaction(git)
		tr.update(JOURNAL_REF, new, journal.sha)
		tr.commit()
		for name, previous in merged.items():
			series.state_hook(repo, cfg, all_series[name], previous, 'change confirm')
		return rec


def format_series(all_series, journal):
	lines = []
	for name, meta in sorted(all_series.items()):
		count = len(meta.get('changes') or []) or len([f for f in journal.list('series/' + name) if f.endswith('.patch')])
		deps = (' deps: ' + ','.join(meta['depends'])) if meta.get('depends') else ''
		link = meta.get('pr') or meta.get('internal_mr') or ''
		lines.append('%-32s %-9s %3d patches  base %-22s %s%s%s' % (
			name, meta.get('state', ''), count, meta.get('base', ''), meta.get('branch') or '-', deps,
			('  ' + link) if link else ''))
	return '\n'.join(lines)


def series_tree(repo, journal, with_variants=False, bug=None):
	"""Series as a tree by their dependencies with state, base lag, claims and variants."""
	from . import upstreamwork
	git = repo.git
	cfg = journal.config
	all_series = series.load_all(journal)
	rows = {}
	for name, meta in all_series.items():
		if bug and not bugs_mod.matches(cfg, meta, bug):
			continue
		up_name = meta.get('upstream') or (meta['base'].split(':', 1)[1] if meta['base'].startswith('upstream:') else '')
		up = cfg.upstreams.get(up_name)
		lag = None
		if up and meta.get('base_sha') and git.rev(up.ref):
			lag = git.count('%s..%s' % (meta['base_sha'], up.ref))
		row = {'name': name, 'state': meta.get('state', ''), 'version': meta.get('version', ''),
		       'patches': len(meta.get('changes') or []), 'depends': meta.get('depends', []),
		       'target': meta.get('target') or upstreamwork.DEFAULT_TARGET, 'base_behind': lag,
		       'claimed_by': (series.claim_owner(repo, name) or {}).get('session'), 'updated': meta.get('updated', ''),
		       'variants': sorted(meta.get('variants') or {}), 'pr': meta.get('pr', ''),
		       'upstream_suspects': len(meta.get('upstream_suspects') or []),
		       'warning': series.suspect_warning(meta),
		       'bugs': [i['label'] for i in bugs_mod.of_series(cfg, meta)]}
		if with_variants and meta.get('variants'):
			row['diverged'] = sorted(spec for spec, v in upstreamwork.variants(repo, name).items() if v['diverged'])
		rows[name] = row
	children = {}
	roots = []
	for name in sorted(rows):
		deps = [d for d in rows[name]['depends'] if d in rows]
		if deps:
			children.setdefault(deps[0], []).append(name)
		else:
			roots.append(name)
	ordered = []

	def walk(name, depth):
		ordered.append({**rows[name], 'depth': depth})
		for child in children.get(name, []):
			walk(child, depth + 1)

	for name in roots:
		walk(name, 0)
	return ordered


def format_tree(rows):
	lines = []
	for r in rows:
		notes = []
		if r.get('upstream_suspects'):
			notes.append('%d commits look like upstream ones: fetch the upstream?' % r['upstream_suspects'])
		elif r['base_behind']:
			notes.append('base %d behind' % r['base_behind'])
		elif r['base_behind'] == 0:
			notes.append('base up to date')
		if r.get('diverged'):
			notes.append('diverged: ' + ', '.join(r['diverged']))
		elif r['variants']:
			notes.append('variants: ' + ', '.join(r['variants']))
		if r.get('bugs'):
			notes.append('bugs ' + ', '.join(r['bugs']))
		if len(r['depends']) > 1:
			notes.append('also depends on ' + ', '.join(r['depends'][1:]))
		if r['claimed_by']:
			notes.append('claimed by ' + r['claimed_by'])
		state = r['state'] + ((' ' + r['version']) if r['version'] else '')
		lines.append('%-40s %-12s %3d patches  %s' % ('  ' * r['depth'] + r['name'], state, r['patches'],
							   '; '.join(notes)))
	return '\n'.join(lines)


def with_warnings(out, data, text):
	"""Print a result with its warnings; exit 2 when there are any."""
	warnings = data.get('warnings') or []
	out.result(data, '\n'.join([text] + ['warning: ' + w for w in warnings]))
	return EXIT_NEEDS_HUMAN if warnings else EXIT_OK


def cmd_series(repo, args, out):
	a = args.action
	if a == 'list':
		journal = repo.journal()
		data = series.load_all(journal)
		if args.bug:
			data = {n: m for n, m in data.items() if bugs_mod.matches(journal.config, m, args.bug)}
		out.result(data, format_series(data, journal) or 'No series.')
	elif a == 'show':
		journal = repo.journal()
		meta = series.load(journal, args.name)
		lines = [format_series({args.name: meta}, journal)]
		for c in meta.get('changes') or []:
			lines.append('  %s %s %s' % (c.get('gitum_id', '-'), short(c['sha']), c['subject']))
		out.result(meta, '\n'.join(lines))
	elif a == 'add':
		meta = series.add(repo, args.name, args.branch or 'series/' + args.name, args.base,
				  [d for d in (args.depends or '').split(',') if d], args.state, upstream=args.upstream)
		return with_warnings(out, meta, 'Series %s: %d patches on %s' % (
			args.name, len(meta['changes']), short(meta['base_sha'])))
	elif a == 'save':
		data = series.save(repo, args.names or None)
		return with_warnings(out, data, ('Saved: ' + ', '.join(data['saved'])) if data['saved'] else 'Nothing to save.')
	elif a == 'set':
		meta = series.set_(repo, args.name, parse_values(args.values))
		out.result(meta, 'Series %s updated.' % args.name)
	elif a == 'remove':
		series.remove(repo, args.name)
		out.result({'removed': args.name}, 'Series %s removed.' % args.name)
	elif a == 'export':
		data = series.export(repo, args.names, args.dir, args.check)
		if args.check:
			out.result(data, ('Stale: ' + ', '.join(data['stale'])) if data['stale'] else 'Up to date.')
			return EXIT_NEEDS_HUMAN if data['stale'] else EXIT_OK
		out.result(data, 'Written: %s' % (', '.join(data['written']) or 'nothing (up to date)'))
	elif a == 'import-staging':
		data = series.import_staging(repo, args.dir, args.base, args.names or None)
		out.result(data, 'Imported %d series.' % len(data['imported']))
	elif a == 'claim':
		data = series.claim(repo, args.name, args.reason or '', args.force)
		out.result(data, 'Series %s claimed by %s.' % (args.name, data['session']))
	elif a == 'release':
		data = series.release(repo, args.name, args.force)
		out.result(data or {}, 'Series %s released.' % args.name if data else 'Series %s was not claimed.' % args.name)
	elif a == 'import-json':
		text = sys.stdin.read() if args.file in (None, '-') else Path(args.file).read_text()
		data = series.import_json(repo, json.loads(text), args.force)
		lines = ['Created: %s; updated: %s' % (', '.join(data['created']) or '-', ', '.join(data['updated']) or '-')]
		for sk in data['skipped']:
			lines.append('kept %s state %s instead of %s: %s; if it is right anyway: %s (or import with --force)' % (
				sk['series'], sk['kept'], sk['state'], sk['reason'], sk['command']))
		return with_warnings(out, data, '\n'.join(lines))
	elif a == 'checkout':
		data = series.checkout(repo, args.name, args.onto, args.branch, not args.no_depends, args.force)
		out.result(data, '%s: %d patches on %s' % (data['branch'], len(data['applied']), short(data['base'])))


# upstream check

def cmd_check_upstream(repo, args, out):
	data = upstreamcheck.check(repo, args.what, args.ref, args.apply, args.since)
	lines = []
	for e in data['results']:
		if e['status'] == 'not-found' and not args.verbose:
			continue
		where = ''
		if e.get('upstream_commit'):
			where = ' as %s (%.0f%%)%s' % (short(e['upstream_commit']), e['similarity'] * 100,
						     ' in ' + e['release'] if e.get('release') else '')
		same = [w for w, flag in (('same subject', e.get('same_subject')), ('same author', e.get('same_author'))) if flag]
		if same:
			where += ' [%s]' % ', '.join(same)
		lines.append('%-15s %-8s %s%s' % (e['status'], e.get('id') or '-', e['subject'], where))
		if e.get('note'):
			lines.append('%24s%s' % ('', e['note']))
	if data['applied']:
		lines.append('Recorded as merged: %s' % ', '.join(data['applied']))
	if data.get('series_merged'):
		lines.append('Series merged: %s' % ', '.join(data['series_merged']))
	if data['suspect'] and args.apply:
		lines.append('Confirm a suspect with: gitum change confirm ID [UPSTREAM_SHA]')
	out.result(data, '\n'.join(lines) or 'Nothing found upstream.')
	return EXIT_NEEDS_HUMAN if data['suspect'] else EXIT_OK


# forge

def cmd_forge(repo, args, out):
	if args.action == 'export-json':
		data = forge.export_json(repo)
		out.result(data, json.dumps(data, indent=1, ensure_ascii=False))
		return
	if args.action == 'import-json':
		text = sys.stdin.read() if args.file in (None, '-') else Path(args.file).read_text()
		data = forge.import_json(repo, json.loads(text))
	else:
		data = forge.refresh(repo)
	text = 'Updated: %s' % (', '.join(data['updated']) or 'nothing')
	for o in data.get('outdated', []):
		text += '\n%s: the merge request head %s is not the prepared %s' % (o['series'], short(o['head']),
										 short(o['prepared']))
	for err in data.get('errors', []):
		text += '\nerror: ' + err
	out.result(data, text)


# journal diff and object history

def cmd_diff(repo, args, out):
	git = repo.git
	a = repo.journal(args.state1)
	b = repo.journal(args.state2 or JOURNAL_REF)
	result = {}
	text = []
	for name in sorted(set(a.layers()) | set(b.layers())):
		la, lb = a.layer(name), b.layer(name)
		if not la or not lb or (la.base, la.tip) == (lb.base, lb.tip):
			continue
		rd = git.out('range-diff', '--no-color', '%s..%s' % (la.base, la.tip), '%s..%s' % (lb.base, lb.tip), check=False)
		result[name] = rd
		text.append('== layer %s\n%s' % (name, rd))
	for name in sorted(set(series.load_all(a)) | set(series.load_all(b))):
		ma, mb = a.read_json(series.meta_path(name)), b.read_json(series.meta_path(name))
		if not ma or not mb or ma.get('tip') == mb.get('tip') or not ma.get('tip') or not mb.get('tip'):
			continue
		rd = git.out('range-diff', '--no-color', '%s..%s' % (ma['base_sha'], ma['tip']),
			     '%s..%s' % (mb['base_sha'], mb['tip']), check=False)
		result['series:' + name] = rd
		text.append('== series %s\n%s' % (name, rd))
	out.result(result, '\n'.join(text) or 'No differences.')


def object_history(repo, target):
	"""Journal states that touched a series or a layer."""
	kind, _, name = target.partition(':')
	path = {'series': 'series/%s' % name, 'layer': 'layers/%s.json' % name}.get(kind)
	if not path:
		return None
	entries = []
	for line in repo.git.lines('log', '--first-parent', '--format=%H%x00%cI%x00%s', JOURNAL_REF, '--', path):
		sha, date, subject = line.split('\x00')
		entries.append({'sha': sha, 'date': date, 'subject': subject})
	return entries


def cmd_bugs(repo, args, out):
	journal = repo.journal()
	md = journal.config.queue.get('markdown', {})
	rows = bugs_mod.index(journal.config, series.load_all(journal), args.bug)
	if args.format == 'md':
		text = bugs_mod.markdown(rows, md.get('titles', {}).get('bugs', 'Bugs'), md.get('bug-titles'), md.get('timezone'))
	else:
		text = bugs_mod.text(rows)
	if args.output:
		Path(args.output).write_text(text if text.endswith('\n') else text + '\n')
		text = 'Written %s' % args.output
	out.result(rows, text)


def register(sub):
	s = sub.add_parser('bugs', help='bugs of the series: bug -> series, state, merge requests, release')
	s.add_argument('bug', nargs='?', metavar='[TRACKER:]N', help='only this bug')
	s.add_argument('--format', choices=['text', 'md'], default='text')
	s.add_argument('-o', '--output', metavar='FILE')
	s.set_defaults(func=cmd_bugs)

	s = sub.add_parser('change', help='changes and their fate')
	s.add_argument('action', choices=['list', 'show', 'set', 'confirm'])
	s.add_argument('ref', nargs='?', help='change id (or prefix, or commit sha); several with commas for set')
	s.add_argument('values', nargs='*', metavar='KEY=VALUE',
		       help='fate=..., pr=URL, bugs=1,2, upstream_commit=SHA, release=, reason=, link=, drop_on=REF')
	s.add_argument('--layer')
	s.add_argument('--fate')
	s.add_argument('--all', action='store_true', help='also changes that left their layer')
	s.set_defaults(func=cmd_change)

	s = sub.add_parser('series', help='series of changes')
	ss = s.add_subparsers(dest='action', required=True)
	p = ss.add_parser('list')
	p.add_argument('--bug', metavar='[TRACKER:]N', help='only the series of this bug')
	p = ss.add_parser('show')
	p.add_argument('name')
	p = ss.add_parser('add', help='register a branch as a series')
	p.add_argument('name')
	p.add_argument('--branch', help='default: series/NAME')
	p.add_argument('--base', required=True, help='upstream:NAME, layer:NAME, series:NAME or a ref')
	p.add_argument('--depends', help='comma separated series names')
	p.add_argument('--state', default='draft', choices=series.STATES)
	p.add_argument('--upstream', help='the upstream the series is for, if not its base (vendored trees)')
	p = ss.add_parser('save', help='record series branches in the journal')
	p.add_argument('names', nargs='*')
	p = ss.add_parser('set')
	p.add_argument('name')
	p.add_argument('values', nargs='+', metavar='KEY=VALUE',
		       help='state=, pr=URL, internal_mr=URL, bugs=, depends=, disabled=REASON, fixes=, title=')
	p = ss.add_parser('remove')
	p.add_argument('name')
	p = ss.add_parser('export', help='write series in the wine-staging layout')
	p.add_argument('names', nargs='*')
	p.add_argument('--dir', required=True)
	p.add_argument('--check', action='store_true', help='only report stale copies (exit 2 if any)')
	p = ss.add_parser('import-staging', help='import <dir>/<series>/{definition,*.patch}')
	p.add_argument('dir')
	p.add_argument('--base', required=True, help='base of the imported series, e.g. upstream:winehq')
	p.add_argument('names', nargs='*')
	p = ss.add_parser('claim', help='mark a series as being worked on by this session (GITUM_SESSION)')
	p.add_argument('name')
	p.add_argument('--reason')
	p.add_argument('--force', action='store_true', help='take it from another session')
	p = ss.add_parser('release', help='release a claimed series')
	p.add_argument('name')
	p.add_argument('--force', action='store_true')
	p = ss.add_parser('import-json', help='create or update series from a JSON list')
	p.add_argument('file', nargs='?')
	p.add_argument('--force', action='store_true', help='also roll back states changed after the record')
	p = ss.add_parser('checkout', help='build a branch from the patches of a series')
	p.add_argument('name')
	p.add_argument('--onto', help='base commit (default: the series base)')
	p.add_argument('--branch')
	p.add_argument('--no-depends', action='store_true')
	p.add_argument('--force', action='store_true', help='replace a branch with unsaved commits')
	s.set_defaults(func=cmd_series)

	s = sub.add_parser('check-upstream', help='find our changes in upstream, also edited ones')
	s.add_argument('what', nargs='*', metavar='layer:NAME|series:NAME')
	s.add_argument('--ref', help='upstream ref to search (default: the upstream of each layer or series)')
	s.add_argument('--since', metavar='REF', help='search upstream commits after REF '
		       '(default: the upstream sync point, or the base of a series)')
	s.add_argument('--apply', action='store_true', help='record found changes as merged')
	s.add_argument('-v', '--verbose', action='store_true', help='also list changes not found')
	s.set_defaults(func=cmd_check_upstream)

	s = sub.add_parser('forge', help='states of pull/merge requests')
	s.add_argument('action', choices=['refresh', 'import-json', 'export-json'])
	s.add_argument('file', nargs='?')
	s.set_defaults(func=cmd_forge)

	s = sub.add_parser('diff', help='range-diff of layers and series between two journal states')
	s.add_argument('state1')
	s.add_argument('state2', nargs='?')
	s.set_defaults(func=cmd_diff)
