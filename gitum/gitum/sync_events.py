"""Events derived from a sync report."""


def change_events(report, candidate=False):
	"""One event per change that did not apply cleanly as is."""
	for layer, data in report['layers'].items():
		for r in data['results']:
			if r['result'] == 'picked':
				continue
			yield {'event': 'change', 'candidate': candidate, 'run': report['run'], 'target': report['target'],
			       'layer': layer, **r}
