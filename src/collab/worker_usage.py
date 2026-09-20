"""Accounting from native provider envelopes, never model-proposed JSON."""
import json
from .telemetry import number, estimate, mapping


def extract(agent, output, model):
    records = []
    for line in output.splitlines() if agent in ('codex', 'opencode') else [output]:
        try:
            records.append(json.loads(line))
        except (ValueError, UnicodeError):
            # Diagnostic chatter cannot erase a valid native usage envelope.
            continue
    totals = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        if agent == 'codex':
            if record.get('type') != 'turn.completed':
                continue
            usage = mapping(record.get('usage'))
            fields = {'tokens_in': usage.get('input_tokens'), 'tokens_out': usage.get('output_tokens'), 'tokens_cached': usage.get('cached_input_tokens')}
            if number(fields['tokens_in']) is not None and number(fields['tokens_cached']) is not None:
                fields['tokens_in'] = max(0, fields['tokens_in'] - fields['tokens_cached'])
        elif agent == 'claude':
            usage = mapping(record.get('usage'))
            fields = {'tokens_in': usage.get('input_tokens'), 'tokens_out': usage.get('output_tokens'), 'tokens_cached': usage.get('cache_read_input_tokens'), 'tokens_cache_write': usage.get('cache_creation_input_tokens'), 'cost_usd': record.get('total_cost_usd')}
        elif agent == 'opencode':
            if record.get('type') != 'step_finish':
                continue
            part = mapping(record.get('part'))
            usage = mapping(part.get('tokens'))
            fields = {'tokens_in': usage.get('input'), 'tokens_out': usage.get('output'), 'tokens_cached': mapping(usage.get('cache')).get('read'), 'tokens_cache_write': mapping(usage.get('cache')).get('write'), 'cost_usd': part.get('cost')}
        else:
            # Cursor CLI output does not promise SDK getUsage figures.
            continue
        for key, value in fields.items():
            if number(value) is not None:
                totals[key] = totals.get(key, 0) + value
    if not totals:
        return {}
    return estimate({**totals, 'model': model})
