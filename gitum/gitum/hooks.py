"""Hooks: external commands that get JSON events on stdin (bug trackers, chats)."""

import json
import subprocess

from .sync_events import change_events
from .util import upstream_values


def report_env(repo, report):
	"""GITUM_* values of a sync report: run, target tag and version of the (first) upstream."""
	cfg = repo.journal().config
	values = {'run': report.get('run')}
	for name, t in (report.get('targets') or {}).items():
		up = cfg.upstreams.get(name)
		values.update(upstream_values(name, up.release_tags if up else '', t.get('sha'), t.get('tag')))
		break
	return values


def _run(repo, cmd, event, log, values=None):
	try:
		proc = subprocess.run(['sh', '-c', cmd], cwd=repo.worktree, input=json.dumps(event, ensure_ascii=False).encode(),
				      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
				      env=repo.hook_env(event=event['event'], **(values or {})))
		log.append({'hook': cmd, 'event': event['event'], 'exit': proc.returncode,
			    'output': proc.stdout.decode('utf-8', errors='replace')[-2000:]})
	except OSError as e:
		log.append({'hook': cmd, 'event': event['event'], 'error': str(e)})


def run_hooks(repo, report, candidate=False):
	"""hooks.change gets one event per changed or dropped change; hooks.run gets the whole report.

	Hook failures never fail the run; they are returned for the log.
	"""
	cfg = repo.journal().config
	values = report_env(repo, report)
	log = []
	if cfg.hooks.get('change'):
		for event in change_events(report, candidate):
			_run(repo, cfg.hooks['change'], event, log, {**values, 'layer': event['layer']})
	if cfg.hooks.get('run'):
		_run(repo, cfg.hooks['run'], {'event': 'candidate' if candidate else 'sync', 'report': report}, log, values)
	return log


def run_event(repo, name, event, values=None):
	"""Run hooks.<name> with the event on stdin (publish and others)."""
	cmd = repo.journal().config.hooks.get(name)
	log = []
	if cmd:
		_run(repo, cmd, event, log, values)
	return log
