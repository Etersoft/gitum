"""Packaging: generated layers (wine-staging, a command), patch sets for RPM
spec files, quilt and wine-staging layouts, and HTML run reports."""

import html
import io
import re
import subprocess
import tarfile
from fnmatch import fnmatch
from pathlib import Path

from . import series as series_mod
from .errors import GitumError, NeedsHuman
from .git import RefTransaction

SPEC_BEGIN = '# BEGIN gitum patches'
SPEC_END = '# END gitum patches'


# generated layers

def tag_version(pattern, tag):
	"""The version part of a release tag: wine-* and wine-11.20 give 11.20."""
	if not pattern or not tag or not fnmatch(tag, pattern):
		return ''
	prefix, _, suffix = pattern.partition('*')
	return tag[len(prefix):len(tag) - len(suffix) if suffix else None]


def staging_series(directory, exclude=()):
	"""Enabled series of a wine-staging tree in patchinstall order (dependencies first)."""
	patches = Path(directory) / 'patches'
	defs = {}
	for d in sorted(p for p in patches.iterdir() if p.is_dir()):
		meta = series_mod.parse_definition((d / 'definition').read_text(errors='replace')) \
			if (d / 'definition').exists() else {'depends': [], 'disabled': ''}
		defs[d.name] = meta
	enabled = {n: m for n, m in defs.items() if not m.get('disabled') and n not in exclude}
	for n, m in enabled.items():
		for dep in m.get('depends', []):
			if dep not in enabled:
				raise NeedsHuman('wine-staging: %s depends on %s which is %s'
						 % (n, dep, 'disabled or excluded' if dep in defs else 'missing'))
	order, seen = [], set()

	def visit(name):
		if name in seen:
			return
		seen.add(name)
		for dep in enabled[name].get('depends', []):
			visit(dep)
		order.append(name)

	for name in sorted(enabled):
		visit(name)
	return order, {n: defs[n] for n in defs if defs[n].get('disabled')}


def staging_plan(repo, layer_conf, target_tag, release_pattern, run_dir, layer_name):
	"""Extract wine-staging for the target into the run directory; return (info, plan).

	The plan is the list of patches to apply in patchinstall order. It lives in
	the run directory, so that a stopped sync can continue applying it.
	"""
	staging_repo = Path(layer_conf['staging-repo'])
	if not staging_repo.is_absolute():
		staging_repo = (repo.worktree / staging_repo).resolve()
	sgit = repo.git.at(staging_repo)
	version = tag_version(release_pattern, target_tag)
	template = layer_conf.get('staging-ref', 'v{version}')
	ref = template.format(version=version) if version or '{version}' not in template else ''
	if not ref or not sgit.rev(ref):
		raise NeedsHuman('wine-staging has no %s for %s yet' % (ref or 'release', target_tag or 'this target'),
				 staging_ref=ref)
	exclude = layer_conf.get('staging-exclude', [])
	target = Path(run_dir) / ('staging-%s' % layer_name)
	target.mkdir(parents=True, exist_ok=True)
	archive = sgit.run('archive', ref, 'patches', 'staging').stdout
	with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
		tar.extractall(target, filter='data')
	order, disabled = staging_series(target, exclude)
	upstream_file = target / 'staging' / 'upstream-commit'
	plan = []
	for name in order:
		for patch in sorted((target / 'patches' / name).glob('0*.patch')):
			plan.append({'series': name, 'patch': patch.name, 'path': str(patch)})
	info = {'generator': 'staging', 'staging_ref': ref, 'staging_sha': sgit.rev(ref), 'series': len(order),
		'patches': len(plan), 'disabled': sorted(disabled), 'excluded': sorted(exclude),
		'upstream_commit': upstream_file.read_text().strip() if upstream_file.exists() else ''}
	return info, plan


def patch_subject(path):
	for line in Path(path).read_text(errors='replace').splitlines():
		if line.startswith('Subject: '):
			subject = line[len('Subject: '):]
			return subject[subject.index(']') + 1:].strip() if subject.startswith('[') else subject
	return Path(path).name


def apply_plan_item(wt, item):
	"""Apply one patch of a plan with git am; on failure the am session stays for a person."""
	return wt.run('am', '--keep-cr', '-3', '-C1', '--whitespace=nowarn', item['path'], check=False)


def generate_command(repo, wt, layer_conf, env, run_dir, layer_name):
	log = Path(run_dir) / ('generate-%s.log' % layer_name)
	with log.open('wb') as f:
		proc = subprocess.run(['sh', '-c', layer_conf['command']], cwd=wt.cwd, stdout=f,
				      stderr=subprocess.STDOUT, env=env)
	if proc.returncode != 0:
		raise NeedsHuman('generator of layer %s failed (see %s); fix it and run gitum sync again'
				 % (layer_name, log), log=str(log))
	return {'generator': 'command', 'log': str(log)}


def post_command(repo, wt, layer_conf, env, run_dir, layer_name):
	"""Run the post-command of a generated layer (autoreconf, removing files, ...).

	The command may commit itself; whatever it leaves uncommitted is committed
	with post-message.
	"""
	cmd = layer_conf.get('post-command')
	if not cmd:
		return None
	log = Path(run_dir) / ('post-%s.log' % layer_name)
	with log.open('wb') as f:
		proc = subprocess.run(['sh', '-c', cmd], cwd=wt.cwd, stdout=f, stderr=subprocess.STDOUT, env=env)
	if proc.returncode != 0:
		raise NeedsHuman('post-command of layer %s failed (see %s)' % (layer_name, log), log=str(log))
	if wt.out('status', '--porcelain', '--untracked-files=no'):
		wt.run('commit', '-q', '-a', '--no-verify', '-m', layer_conf.get('post-message', 'commit generated files'))
	return str(log)


# patch sets

def layer_range(repo, what):
	"""(base, tip, name) of 'layer:NAME', 'series:NAME' or a 'BASE..TIP' range."""
	journal = repo.journal()
	kind, _, name = what.partition(':')
	if kind == 'layer':
		st = journal.layer(name)
		if not st:
			raise GitumError('unknown layer %s' % name)
		return st.base, repo.git.rev('refs/heads/' + st.branch) or st.tip, name
	if kind == 'series':
		meta = series_mod.load(journal, name)
		return meta['base_sha'], meta['tip'], name
	if '..' in what:
		base, tip = what.split('..', 1)
		return repo.git.rev(base), repo.git.rev(tip), 'patches'
	raise GitumError('expected layer:NAME, series:NAME or BASE..TIP, not %s' % what)


def format_patches(git, base, tip, directory, prefix='', start_number=1):
	directory.mkdir(parents=True, exist_ok=True)
	names = git.lines('format-patch', '--no-signature', '--zero-commit', '--start-number', str(start_number),
			  '-o', str(directory), '%s..%s' % (base, tip))
	files = [Path(n).name for n in names]
	if prefix:
		renamed = []
		for f in files:
			new = prefix + f
			(directory / f).rename(directory / new)
			renamed.append(new)
		files = renamed
	return files


def spec_lines(files, first=1):
	return ['Patch%d: %s' % (i, f) for i, f in enumerate(files, first)]


def update_spec(spec_path, files, first=1):
	"""Replace the Patch lines between the gitum markers of a spec file."""
	text = Path(spec_path).read_text()
	if SPEC_BEGIN not in text or SPEC_END not in text:
		raise GitumError('%s has no "%s" ... "%s" block for the patches' % (spec_path, SPEC_BEGIN, SPEC_END))
	head, rest = text.split(SPEC_BEGIN, 1)
	_, tail = rest.split(SPEC_END, 1)
	block = ''.join(line + '\n' for line in spec_lines(files, first))
	new = head + SPEC_BEGIN + '\n' + block + SPEC_END + tail
	if new != text:
		Path(spec_path).write_text(new)
	return new != text


def export(repo, what, fmt, directory, spec=None, prefix='', first=1, clean=False):
	git = repo.git
	base, tip, name = layer_range(repo, what)
	directory = Path(directory)
	if fmt == 'staging':
		if not what.startswith('series:'):
			raise GitumError('the staging format is for series (gitum series export)')
		return series_mod.export(repo, [name], directory)
	target = directory / 'patches' if fmt == 'quilt' else directory
	if clean and target.is_dir():
		pattern = (prefix or '') + '[0-9][0-9][0-9][0-9]-*.patch'
		for old in target.glob(pattern):
			old.unlink()
	files = format_patches(git, base, tip, target, prefix, 1)
	result = {'format': fmt, 'directory': str(target), 'files': files, 'base': base, 'tip': tip}
	if fmt == 'quilt':
		(target / 'series').write_text(''.join(f + '\n' for f in files))
	elif fmt == 'spec':
		result['spec_lines'] = spec_lines(files, first)
		if spec:
			result['spec_updated'] = update_spec(spec, files, first)
	return result


def _patch_message(path):
	text = path.read_bytes()
	return text.startswith(b'From ') and b'\nSubject: ' in text


def import_patches(repo, into, source, fmt):
	"""Apply a quilt series or the Patch lines of a spec file onto a layer branch."""
	git = repo.git
	journal = repo.journal()
	cfg = journal.config
	if into not in cfg.layers:
		raise GitumError('unknown layer %s' % into)
	source = Path(source)
	if fmt == 'quilt':
		directory = source if (source / 'series').exists() else source / 'patches'
		names = [l.split()[0] for l in (directory / 'series').read_text().splitlines()
			 if l.strip() and not l.startswith('#')]
	else:
		directory = source.parent
		numbered = []
		for line in source.read_text().splitlines():
			m = re.match(r'^Patch(\d*)\s*:\s*(\S+)', line)
			if m:
				numbered.append((int(m.group(1) or 0), m.group(2)))
		names = [n for _, n in sorted(numbered, key=lambda t: t[0])]
	if not names:
		raise GitumError('no patches in %s' % source)
	branch = cfg.layers[into].branch
	tip = git.rev('refs/heads/' + branch)
	wt = repo.add_worktree(tip, 'import-' + into)
	imported = []
	try:
		for n in names:
			path = directory / n
			if not path.exists():
				raise GitumError('patch %s is missing' % path)
			if _patch_message(path):
				res = wt.run('am', '--keep-cr', '-3', str(path), check=False)
			else:
				res = wt.run('apply', '--index', '-p1', str(path), check=False)
				if res.returncode == 0:
					res = wt.run('commit', '-q', '--no-verify', '-m', 'Apply %s' % n, check=False)
			if res.returncode != 0:
				wt.run('am', '--abort', check=False)
				raise NeedsHuman('%s does not apply: %s' % (n, res.stderr.decode(errors='replace').strip()))
			imported.append(n)
		new_tip = wt.rev('HEAD')
	finally:
		repo.remove_worktree(wt.cwd)
	found = repo.check_checkouts([branch])
	tr = RefTransaction(git)
	tr.update('refs/heads/' + branch, new_tip, tip)
	tr.commit()
	repo.follow_checkouts([(branch, tip, new_tip)], found)
	from . import state
	state.save(repo, [into], 'import %d patches from %s' % (len(imported), source.name))
	return {'layer': into, 'imported': imported, 'tip': new_tip}


# HTML report

RESULT_CLASS = {'picked': 'ok', 'resolved': 'warn', 'emptied': 'info', 'skipped': 'bad',
		'dropped-upstream': 'info', 'dropped-backport': 'info', 'dropped-marked': 'info', 'annihilated': 'info',
		'generated': 'ok'}


def html_report(report):
	e = html.escape
	rows = []
	for layer, data in report['layers'].items():
		counts = ', '.join('%s %d' % kv for kv in sorted(data.get('counts', {}).items())) or 'unchanged'
		rows.append('<h2>%s</h2><p>%s → %s · %s</p>' % (e(layer), e(data['old_tip'][:12]), e(data['new_tip'][:12]),
								    e(counts)))
		if data.get('generator'):
			g = data['generator']
			rows.append('<p>generated by %s %s</p>' % (e(g.get('generator', '')), e(g.get('staging_ref', ''))))
		items = [r for r in data.get('results', []) if r['result'] != 'picked']
		if items:
			rows.append('<table><tr><th>result</th><th>change</th><th>resolver</th><th>files</th></tr>')
			for r in items:
				rows.append('<tr class="%s"><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
					RESULT_CLASS.get(r['result'], ''), e(r['result']), e(r['subject']),
					e(r.get('resolver', '')), e(', '.join(r.get('files', [])))))
			rows.append('</table>')
	return '''<!doctype html>
<html><head><meta charset="utf-8"><title>gitum sync %(run)s</title>
<style>
body{font:14px/1.4 sans-serif;margin:16px;color:#222;background:#fff}
table{border-collapse:collapse;width:100%%}td,th{border:1px solid #ccc;padding:3px 6px;text-align:left}
tr.warn{background:#fff4d6}tr.bad{background:#fde2e2}tr.info{background:#e8f1ff}
@media (prefers-color-scheme: dark){body{color:#ddd;background:#1b1b1b}td,th{border-color:#444}
tr.warn{background:#4a3d14}tr.bad{background:#4a1f1f}tr.info{background:#1d2e4a}}
</style></head><body>
<h1>Sync to %(target)s</h1><p>run %(run)s, %(started)s – %(finished)s</p>
%(rows)s
</body></html>
''' % {'run': e(report['run']), 'target': e(report.get('target', '')), 'started': e(report.get('started', '')),
	'finished': e(report.get('finished', '')), 'rows': '\n'.join(rows)}


def load_report(repo, run=None):
	journal = repo.journal()
	runs = sorted(journal.list('runs'))
	if not runs:
		raise GitumError('no sync runs in the journal')
	if run:
		match = [r for r in runs if r.startswith(run) or run in r]
		if len(match) != 1:
			raise GitumError('run %s: %s' % (run, 'not found' if not match else 'ambiguous'))
		run = match[0]
	else:
		run = runs[-1]
	return journal.read_json('runs/%s/report.json' % run)

