"""Hooks: external commands that get JSON events on stdin (bug trackers, chats)."""

import json
import os
import subprocess

from .sync_events import change_events


def _run(repo, cmd, event, log):
	try:
		proc = subprocess.run(['sh', '-c', cmd], cwd=repo.worktree, input=json.dumps(event, ensure_ascii=False).encode(),
				      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
				      env={**os.environ, 'GITUM_EVENT': event['event']})
		log.append({'hook': cmd, 'event': event['event'], 'exit': proc.returncode,
			    'output': proc.stdout.decode('utf-8', errors='replace')[-2000:]})
	except OSError as e:
		log.append({'hook': cmd, 'event': event['event'], 'error': str(e)})


def run_hooks(repo, report, candidate=False):
	"""hooks.change gets one event per changed or dropped change; hooks.run gets the whole report.

	Hook failures never fail the run; they are returned for the log.
	"""
	cfg = repo.journal().config
	log = []
	if cfg.hooks.get('change'):
		for event in change_events(report, candidate):
			_run(repo, cfg.hooks['change'], event, log)
	if cfg.hooks.get('run'):
		_run(repo, cfg.hooks['run'], {'event': 'candidate' if candidate else 'sync', 'report': report}, log)
	return log
