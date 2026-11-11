import json
import shutil
import sys
from pathlib import Path

import pytest

import gitum as gitum_pkg

from gitum import rrcache
from gitum.cli import main
from gitum.errors import EXIT_BUSY, EXIT_NEEDS_HUMAN, EXIT_UPDATED
from gitum.journal import JOURNAL_REF, Journal, history_ref
from gitum.repo import Repo as GitumRepo

from core_helpers import INIT_ARGS, Repo, make_stack

BRANCHES = ('rebased', 'rebased-basealt', 'rebased-etersoft')


def gitum(work, *args):
	return main(['--repo', str(work.path), *args])


def gitum_json(work, capsys, *args):
	capsys.readouterr()
	code = main(['--repo', str(work.path), '--json', *args])
	out = capsys.readouterr().out
	return code, json.loads(out) if out.strip() else None


@pytest.fixture
def stack(tmp_path, capsys):
	up, work = make_stack(tmp_path)
	assert gitum(work, *INIT_ARGS) == 0
	capsys.readouterr()
	return up, work


def release(up, version, files=None, message=None):
	up.commit(message or 'upstream change for %s' % version, files or {'up-%s' % version: version + '\n'})
	up.commit('Release %s' % version, {'VERSION': version + '\n'})
	up.run('tag', 'v' + version)
	return up.rev('HEAD')


def tips(work):
	return {b: work.rev(b) for b in BRANCHES}


def results(report, layer):
	return {r['subject']: r['result'] for r in report['layers'][layer]['results']}


def test_up_to_date(stack, capsys):
	up, work = stack
	code, data = gitum_json(work, capsys, '--detailed-exitcode', 'sync', '--next-release')
	assert code == 0 and data['changed'] is False


def test_sync_next_release_cascade(stack, capsys):
	up, work = stack
	v11 = release(up, '1.1')
	release(up, '1.2')
	code, data = gitum_json(work, capsys, '--detailed-exitcode', 'sync', '--next-release')
	assert code == EXIT_UPDATED
	report = data['report']
	assert report['target'] == 'winehq v1.1'
	# one release per run, cascade through all layers
	assert work.git.is_ancestor(v11, 'rebased') and not work.git.is_ancestor(up.rev('v1.2'), 'rebased')
	assert work.git.merge_base('rebased', 'rebased-basealt') == work.rev('rebased')
	assert work.subjects('rebased..rebased-basealt') == ['basealt: tweak']
	assert work.subjects('rebased-basealt..rebased-etersoft') == ['etersoft: feature', 'etersoft: change base line']
	assert all(v == 'picked' for v in results(report, 'etersoft').values())
	# START tags follow the bases
	assert work.rev('rebased-basealt-START') == work.rev('rebased')
	assert work.rev('rebased-etersoft-START') == work.rev('rebased-basealt')
	# journal, history and report
	journal = GitumRepo(work.path).journal()
	assert journal.upstream('winehq')['tag'] == 'v1.1'
	assert journal.layer('etersoft').tip == work.rev('rebased-etersoft')
	assert json.loads(journal.read_text('runs/%s/report.json' % report['run']))['target'] == 'winehq v1.1'
	assert journal.read_text('runs/%s/range-diff-etersoft.txt' % report['run'])
	assert work.git.tree(history_ref('etersoft')) == work.git.tree('rebased-etersoft')
	assert work.log(history_ref('etersoft'))[0] == 'etersoft: sync to winehq v1.1'
	assert work.git.count(history_ref('etersoft')) == 2
	# old tips stay reachable from the journal
	old_tip = journal.layer('etersoft').tip
	assert old_tip in work.git.lines('log', '-1', '--format=%P', JOURNAL_REF)[0]
	# the private work tree is gone, no operation left
	g = GitumRepo(work.path)
	assert g.load_op() is None
	assert not list((g.gitum_dir / 'wt').glob('*'))
	# next run goes to 1.2
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert data['report']['target'] == 'winehq v1.2'


def test_sync_to_explicit_ref(stack, capsys):
	up, work = stack
	release(up, '1.1')
	extra = up.commit('upstream: after release', {'after': 'x\n'})
	work.run('fetch', '-q', 'winehq')
	code, data = gitum_json(work, capsys, 'sync', '--no-fetch', '--to', extra)
	assert code == 0
	assert work.git.is_ancestor(extra, 'rebased')
	code, data = gitum_json(work, capsys, 'sync', '--to', 'v1.1')
	assert code == 1 and 'not newer' in data['error']


def test_dropped_upstream_and_emptied(stack, capsys):
	up, work = stack
	# exact same patch accepted, and a modified version of another one
	up.commit('etersoft: feature', {'eter': 'eter\n'})
	up.commit('basealt: tweak, rewritten', {'basealt': 'alt\n', 'other': 'o\n'})
	release(up, '1.1')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0
	assert results(data['report'], 'etersoft')['etersoft: feature'] == 'dropped-upstream'
	assert results(data['report'], 'basealt')['basealt: tweak'] == 'emptied'
	assert work.subjects('rebased-basealt..rebased-etersoft') == ['etersoft: change base line']
	assert work.subjects('rebased..rebased-basealt') == []


def conflicting_release(up):
	return release(up, '1.1', {'base': 'line1\nUPSTREAM\nline3\nline4\nline5\n'}, 'upstream: change line 2')


def test_conflict_continue(stack, capsys):
	up, work = stack
	conflicting_release(up)
	before = tips(work)
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == EXIT_NEEDS_HUMAN
	assert data['files'] == ['base'] and 'gitum continue' in data['error']
	wt = Repo(data['worktree'], init=False)
	# nothing moved yet, status shows the operation
	assert tips(work) == before
	code, st = gitum_json(work, capsys, 'status')
	assert st['operation']['kind'] == 'sync' and 'conflict' in st['operation']['message']
	assert st['operation']['layer'] == 'etersoft'
	# continue without resolving
	code, d2 = gitum_json(work, capsys, 'continue')
	assert code == EXIT_NEEDS_HUMAN and d2['files'] == ['base']
	wt.write('base', 'line1\nETER+UPSTREAM\nline3\nline4\nline5\n')
	wt.run('add', 'base')
	code, data = gitum_json(work, capsys, 'continue')
	assert code == 0
	r = {x['subject']: x for x in data['report']['layers']['etersoft']['results']}
	assert r['etersoft: change base line']['result'] == 'resolved'
	assert r['etersoft: change base line']['resolver'] == 'manual'
	assert work.show('rebased-etersoft', 'base') == 'line1\nETER+UPSTREAM\nline3\nline4\nline5\n'
	run_dir = GitumRepo(work.path).gitum_dir / 'runs'
	assert list(run_dir.glob('*/conflicts/001-*/resolution.diff'))
	# the resolution went to the shared rr-cache of the journal
	journal = GitumRepo(work.path).journal()
	assert journal.list('rr-cache')


def test_conflict_skip(stack, capsys):
	up, work = stack
	conflicting_release(up)
	assert gitum(work, 'sync', '--next-release') == EXIT_NEEDS_HUMAN
	code, data = gitum_json(work, capsys, 'skip')
	assert code == 0
	assert results(data['report'], 'etersoft')['etersoft: change base line'] == 'skipped'
	assert work.subjects('rebased-basealt..rebased-etersoft') == ['etersoft: feature']


def test_conflict_abort(stack, capsys):
	up, work = stack
	conflicting_release(up)
	before = tips(work)
	journal_before = work.rev(JOURNAL_REF)
	assert gitum(work, 'sync', '--next-release') == EXIT_NEEDS_HUMAN
	code, data = gitum_json(work, capsys, 'abort')
	assert code == 0 and data['aborted']
	assert tips(work) == before and work.rev(JOURNAL_REF) == journal_before
	g = GitumRepo(work.path)
	assert g.load_op() is None
	assert work.git.lines('for-each-ref', '--format=%(refname)', 'refs/gitum/failed/')
	assert gitum(work, 'continue') == 1


def test_find_culprit(stack, capsys):
	up, work = stack
	up.commit('upstream: harmless', {'h': '1\n'})
	culprit = up.commit('upstream: change line 2', {'base': 'line1\nUPSTREAM\nline3\nline4\nline5\n'})
	up.commit('upstream: harmless 2', {'h2': '2\n'})
	release(up, '1.1')
	code, data = gitum_json(work, capsys, 'sync', '--next-release', '--find-culprit')
	assert code == EXIT_NEEDS_HUMAN
	code, data = gitum_json(work, capsys, 'skip')
	report = data['report']
	r = {x['subject']: x for x in report['layers']['etersoft']['results']}
	assert r['etersoft: change base line']['culprit']['sha'] == culprit


def test_shared_rerere_resolves_in_another_clone(stack, tmp_path, capsys):
	up, work = stack
	conflicting_release(up)
	other = Repo(tmp_path / 'other', init=False)
	shutil.copytree(work.path, other.path, symlinks=True)
	other.git = other.git.at(other.path)
	# resolve by hand in the first clone
	assert gitum(work, 'sync', '--next-release') == EXIT_NEEDS_HUMAN
	op = GitumRepo(work.path).load_op()
	wt = Repo(op['worktree'], init=False)
	wt.write('base', 'line1\nRESOLVED\nline3\nline4\nline5\n')
	wt.run('add', 'base')
	assert gitum(work, 'continue') == 0
	# the other clone gets only the journal: its rerere cache is empty
	other.run('fetch', '-q', str(work.path), '+%s:refs/heads/gitum/journal-from-work' % JOURNAL_REF)
	g = GitumRepo(other.path)
	assert not (g.common_dir / 'rr-cache').exists()
	assert rrcache.import_from(g, Journal.load(g.git, 'refs/heads/gitum/journal-from-work')) > 0
	code, data = gitum_json(other, capsys, 'sync', '--next-release')
	assert code == 0
	r = {x['subject']: x for x in data['report']['layers']['etersoft']['results']}
	assert r['etersoft: change base line']['resolver'] == 'rerere'
	assert other.show('rebased-etersoft', 'base') == 'line1\nRESOLVED\nline3\nline4\nline5\n'


def load_config(work, tmp_path, extra):
	text = GitumRepo(work.path).journal().config_text + '\n' + extra
	path = tmp_path / 'cfg.toml'
	path.write_text(text)
	assert gitum(work, 'config', 'load', str(path)) == 0


def test_rules_resolver_goes_to_review(stack, tmp_path, capsys):
	up, work = stack
	script = tmp_path / 'rules.sh'
	script.write_text('#!/bin/sh\necho "$GITUM_SUBJECT" > "$GITUM_CONFLICT_DIR/seen"\n'
			  'git checkout --theirs -- $GITUM_FILES && git add -- $GITUM_FILES\n')
	script.chmod(0o755)
	load_config(work, tmp_path, '[resolve]\nchain = ["rerere", "rules"]\nrules = "%s"\n' % script)
	conflicting_release(up)
	before = tips(work)
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == EXIT_NEEDS_HUMAN and 'review' in data['error']
	assert tips(work) == before
	assert work.show('candidate/rebased-etersoft', 'base') == 'line1\nETER\nline3\nline4\nline5\n'
	code, st = gitum_json(work, capsys, 'status')
	assert st['candidate']['run']
	# a sync is refused while the candidate waits
	assert gitum(work, 'sync', '--next-release') == EXIT_NEEDS_HUMAN
	# the reviewer fixes the candidate before accepting
	work.run('checkout', '-q', 'candidate/rebased-etersoft')
	work.commit('etersoft: review fix', {'review': 'ok\n'})
	work.run('checkout', '-q', '--detach')
	code, data = gitum_json(work, capsys, 'accept')
	assert code == 0
	assert work.subjects('rebased-basealt..rebased-etersoft')[-1] == 'etersoft: review fix'
	assert not work.rev('candidate/rebased-etersoft')
	r = {x['subject']: x for x in data['report']['layers']['etersoft']['results']}
	assert r['etersoft: change base line']['resolver'] == 'rules'
	journal = GitumRepo(work.path).journal()
	assert [c.subject for c in journal.layer('etersoft').changes][-1] == 'etersoft: review fix'


def test_candidate_always_and_drop(stack, capsys):
	up, work = stack
	release(up, '1.1')
	before = tips(work)
	assert gitum(work, 'sync', '--next-release', '--candidate', 'always') == EXIT_NEEDS_HUMAN
	assert work.rev('candidate/rebased')
	assert gitum(work, 'drop-candidate') == 0
	assert not work.rev('candidate/rebased') and tips(work) == before
	assert gitum(work, 'sync', '--next-release') == 0


def test_annihilate_reverts_and_backports(stack, tmp_path, capsys):
	up, work = stack
	load_config(work, tmp_path, '')
	text = GitumRepo(work.path).journal().config_text.replace(
		'[layer.etersoft]\n', '[layer.etersoft]\nannihilate-reverts = true\n')
	(tmp_path / 'c.toml').write_text(text)
	assert gitum(work, 'config', 'load', str(tmp_path / 'c.toml')) == 0
	fix = up.commit('upstream: fix', {'fix': 'fixed\n'})
	work.run('fetch', '-q', 'winehq')
	work.run('checkout', '-q', 'rebased-etersoft')
	work.commit('etersoft: experiment', {'exp': '1\n'})
	work.run('revert', '--no-edit', 'HEAD')
	work.run('cherry-pick', '-x', fix)
	work.run('checkout', '-q', '--detach')
	release(up, '1.1')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0
	res = results(data['report'], 'etersoft')
	assert res['etersoft: experiment'] == 'annihilated'
	assert res['Revert "etersoft: experiment"'] == 'annihilated'
	assert res['upstream: fix'] == 'dropped-backport'
	assert work.subjects('rebased-basealt..rebased-etersoft') == ['etersoft: feature', 'etersoft: change base line']


def test_new_commits_on_parent_cascade(stack, capsys):
	up, work = stack
	work.run('checkout', '-q', 'rebased-basealt')
	work.commit('basealt: new', {'basealt2': 'x\n'})
	work.run('checkout', '-q', '--detach')
	code, data = gitum_json(work, capsys, 'sync')
	assert code == 0
	assert data['report']['layers']['rebased']['unchanged']
	assert work.git.is_ancestor('rebased-basealt', 'rebased-etersoft')
	assert work.rev('rebased-etersoft-START') == work.rev('rebased-basealt')


def test_checked_out_branch(stack, capsys):
	"""A layer branch checked out in a clean work tree follows the sync; local changes stop it."""
	up, work = stack
	release(up, '1.1')
	work.run('checkout', '-q', 'rebased-etersoft')
	work.write('eter', 'local edit\n')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == EXIT_NEEDS_HUMAN and 'local changes' in data['error']
	work.run('checkout', '-q', '--', 'eter')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0
	assert work.git.is_clean() and work.read('VERSION') == '1.1\n'
	assert work.git.out('symbolic-ref', 'HEAD') == 'refs/heads/rebased-etersoft'


def test_check_failure_keeps_branches(stack, tmp_path, capsys):
	up, work = stack
	load_config(work, tmp_path, '[publish]\ncheck = "test -f eter && false"\n')
	release(up, '1.1')
	before = tips(work)
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == EXIT_NEEDS_HUMAN and 'check failed' in data['error']
	assert tips(work) == before
	assert GitumRepo(work.path).load_op() is None
	assert work.rev('refs/gitum/failed/%s/etersoft' % data['run'])


def test_hooks(stack, tmp_path, capsys):
	up, work = stack
	events = tmp_path / 'events'
	load_config(work, tmp_path, '[hooks]\nchange = "cat >> %s; echo >> %s"\nrun = "cat > %s.run"\n'
		    % (events, events, events))
	up.commit('etersoft: feature', {'eter': 'eter\n'})
	release(up, '1.1')
	assert gitum(work, 'sync', '--next-release') == 0
	lines = [json.loads(l) for l in events.read_text().splitlines() if l.strip()]
	assert [(e['layer'], e['result']) for e in lines] == [('etersoft', 'dropped-upstream')]
	assert json.loads((tmp_path / 'events.run').read_text())['event'] == 'sync'


def test_busy(stack):
	up, work = stack
	with GitumRepo(work.path).lock():
		assert gitum(work, 'sync') == EXIT_BUSY


# publish and pull

@pytest.fixture
def published(stack, tmp_path, capsys):
	up, work = stack
	bare = Repo(tmp_path / 'origin.git', bare=True)
	work.run('remote', 'add', 'origin', str(bare.path))
	load_config(work, tmp_path, '[publish]\nremote = "origin"\n')
	assert gitum(work, 'publish') == 0
	return up, work, bare


def clone_gitum(bare, path, up):
	other = Repo(path, init=False)
	other.git.at(path.parent).run('clone', '-q', '--no-checkout', str(bare.path), str(path))
	other.git = other.git.at(path)
	other.run('remote', 'add', 'winehq', str(up.path))
	other.run('fetch', '-q', 'winehq')
	assert main(['--repo', str(path), 'pull']) in (0, 10)
	return other


def test_publish_and_pull(published, tmp_path, capsys):
	up, work, bare = published
	for b in BRANCHES + ('gitum/journal', 'gitum/history/etersoft'):
		assert bare.rev(b) == work.rev(b)
	assert bare.rev('refs/tags/rebased-etersoft-START') == work.rev('rebased-etersoft-START')
	other = clone_gitum(bare, tmp_path / 'other', up)
	assert other.rev(JOURNAL_REF) == work.rev(JOURNAL_REF)
	assert tips(other) == tips(work)
	# a sync published from the first clone reaches the second one by pull
	release(up, '1.1')
	assert gitum(work, 'sync', '--next-release', '--publish') == 0
	code, data = gitum_json(other, capsys, 'pull')
	assert code == 0 and data['changed'] and set(data['moved']) == set(BRANCHES)
	assert tips(other) == tips(work)


def test_publish_integrates_commits_of_others(published, tmp_path, capsys):
	up, work, bare = published
	other = clone_gitum(bare, tmp_path / 'other', up)
	# somebody adds a patch to the leaf layer and pushes it with plain git
	other.run('checkout', '-q', 'rebased-etersoft')
	other.commit('etersoft: from a colleague', {'colleague': 'c\n'})
	other.run('push', '-q', 'origin', 'rebased-etersoft')
	# meanwhile the first clone syncs and publishes: the new commit is kept
	release(up, '1.1')
	code, data = gitum_json(work, capsys, 'sync', '--next-release', '--publish')
	assert code == 0
	assert data['published']['integrated'] == {'etersoft': ['etersoft: from a colleague']}
	assert work.subjects('rebased-basealt..rebased-etersoft')[-1] == 'etersoft: from a colleague'
	assert bare.rev('rebased-etersoft') == work.rev('rebased-etersoft')
	assert GitumRepo(work.path).journal().layer('etersoft').tip == work.rev('rebased-etersoft')


def test_publish_refuses_when_journal_diverged(published, tmp_path, capsys):
	up, work, bare = published
	other = clone_gitum(bare, tmp_path / 'other', up)
	other.run('checkout', '-q', 'rebased-etersoft')
	other.commit('etersoft: other', {'o': 'o\n'})
	other.run('checkout', '-q', '--detach')
	assert main(['--repo', str(other.path), 'save']) == 0
	assert main(['--repo', str(other.path), 'publish']) == 0
	work.run('checkout', '-q', 'rebased-basealt')
	work.commit('basealt: mine', {'m': 'm\n'})
	work.run('checkout', '-q', '--detach')
	assert gitum(work, 'save') == 0
	code, data = gitum_json(work, capsys, 'publish')
	assert code == EXIT_NEEDS_HUMAN


def test_step_mode_records_each_upstream_commit(stack, capsys):
	up, work = stack
	up.commit('upstream: one', {'one': '1\n'})
	up.commit('upstream: two', {'two': '2\n'})
	release(up, '1.1')
	code, data = gitum_json(work, capsys, 'sync', '--step', '--next-release')
	assert code == 0
	assert len(data['steps']) == 4
	# the history of the layer has a state per upstream commit: bisectable
	assert work.git.count(history_ref('etersoft')) == 5
	assert GitumRepo(work.path).journal().upstream('winehq')['tag'] == 'v1.1'
	assert work.git.is_ancestor(up.rev('v1.1'), 'rebased-etersoft')


def test_sync_pulls_published_state_first(published, tmp_path, capsys):
	up, work, bare = published
	other = clone_gitum(bare, tmp_path / 'other', up)
	release(up, '1.1')
	assert gitum(work, 'sync', '--next-release', '--publish') == 0
	# the other clone is behind: its sync first takes the published state
	code, data = gitum_json(other, capsys, 'sync', '--next-release')
	assert code == 0 and data['changed'] is False
	assert tips(other) == tips(work)


def test_rewritten_upstream(tmp_path, capsys):
	"""A base regenerated for every version (like the wine-staging branch) is not a descendant."""
	up, work = make_stack(tmp_path)
	assert gitum(work, 'init', '--upstream', 'staging=rebased', '--layer', 'basealt=rebased-basealt:staging',
		     '--layer', 'etersoft=rebased-etersoft:basealt') == 0
	release(up, '1.1')
	work.run('fetch', '-q', 'winehq')
	work.run('checkout', '-q', '-b', 'staging-new', 'v1.1')
	work.commit('staging: patch one (regenerated)', {'staging1': 'one\n'})
	work.run('checkout', '-q', '--detach')
	code, data = gitum_json(work, capsys, 'sync', '--no-fetch', '--to', 'staging-new')
	assert code == 1 and 'not newer' in data['error']
	code, data = gitum_json(work, capsys, 'sync', '--no-fetch', '--to', 'staging-new', '--allow-rewrite')
	assert code == 0
	assert work.git.merge_base('staging-new', 'rebased-basealt') == work.rev('staging-new')
	assert work.subjects('staging-new..rebased-etersoft') == ['basealt: tweak', 'etersoft: feature',
								   'etersoft: change base line']


def test_hook_environment_and_nested_gitum(stack, tmp_path, capsys):
	"""Commands run by gitum get GITUM_* and may call gitum again without waiting for the lock."""
	up, work = stack
	envfile = tmp_path / 'env'
	src = Path(gitum_pkg.__file__).resolve().parents[1]
	check = ('env | grep ^GITUM_ | sort > %s; PYTHONPATH=%s %s -m gitum --repo "$GITUM_REPO" save >> %s.save'
		 % (envfile, src, sys.executable, envfile))
	load_config(work, tmp_path, '[publish]\ncheck = "%s"\n' % check.replace('"', '\\"'))
	release(up, '1.1')
	code, data = gitum_json(work, capsys, 'sync', '--next-release')
	assert code == 0, data
	env = dict(line.split('=', 1) for line in envfile.read_text().splitlines())
	g = GitumRepo(work.path)
	assert env['GITUM_REPO'] == str(work.path)
	assert env['GITUM_WORKTREE'].startswith(str(g.gitum_dir / 'wt'))
	assert env['GITUM_LAYER'] == 'etersoft'
	assert env['GITUM_TARGET_TAG'] == 'v1.1' and env['GITUM_VERSION'] == '1.1'
	assert env['GITUM_LOCK_HELD'] == env['GITUM_LOCK_FILE'] == str(g.gitum_dir / 'lock')
	assert env['GITUM_RUN'] == data['report']['run']
	assert 'Nothing to save' in (tmp_path / 'env.save').read_text()


def test_publish_event(published, tmp_path, capsys):
	up, work, bare = published
	events = tmp_path / 'publish.json'
	load_config(work, tmp_path, '[hooks]\npublish = "cat > %s; env | grep ^GITUM_VERSION= >> %s.env"\n'
		    % (events, events))
	old = {b: work.rev(b) for b in BRANCHES}
	release(up, '1.1')
	assert gitum(work, 'sync', '--next-release', '--publish') == 0
	event = json.loads(events.read_text())
	assert event['event'] == 'publish' and event['remote'] == 'origin'
	assert event['upstreams']['winehq'] == {'ref': 'winehq/master', 'sha': up.rev('v1.1'), 'tag': 'v1.1',
						'version': '1.1'}
	eter = event['layers']['etersoft']
	assert eter['branch'] == 'rebased-etersoft' and eter['old'] == old['rebased-etersoft']
	assert eter['new'] == work.rev('rebased-etersoft') and eter['start_tag'] == 'rebased-etersoft-START'
	assert (tmp_path / 'publish.json.env').read_text().strip() == 'GITUM_VERSION=1.1'


def test_publish_without_local_markers(published, tmp_path, capsys):
	"""A fresh clone without refs/gitum/published: the tips in the remote journal count as published."""
	up, work, bare = published
	other = clone_gitum(bare, tmp_path / 'other', up)
	for ref in other.git.lines('for-each-ref', '--format=%(refname)', 'refs/gitum/published/'):
		other.run('update-ref', '-d', ref)
	release(up, '1.1')
	other.run('fetch', '-q', 'winehq')
	code, data = gitum_json(other, capsys, 'sync', '--next-release', '--publish')
	assert code == 0, data
	assert bare.rev('rebased-etersoft') == other.rev('rebased-etersoft')


def test_accept_keeps_remote_candidate_until_publish(published, tmp_path, capsys):
	up, work, bare = published
	release(up, '1.1')
	assert gitum(work, 'sync', '--next-release', '--candidate', 'always') == EXIT_NEEDS_HUMAN
	assert bare.rev('candidate/rebased-etersoft')
	assert gitum(work, 'accept') == 0
	assert bare.rev('candidate/rebased-etersoft')		# nothing published yet: the candidate stays
	assert gitum(work, 'publish') == 0
	assert not bare.rev('candidate/rebased-etersoft')
	assert bare.rev('rebased-etersoft') == work.rev('rebased-etersoft')
