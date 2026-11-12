"""Events derived from a sync report."""

from .journal import report_layers


def change_events(report, candidate=False):
	"""One event per change that did not apply cleanly as is."""
	for layer, data in report_layers(report):
		for r in data['results']:
			if r['result'] == 'picked':
				continue
			yield {'event': 'change', 'candidate': candidate, 'run': report['run'], 'target': report['target'],
			       'layer': layer, **r}
