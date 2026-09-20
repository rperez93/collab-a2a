"""Accounting from native provider envelopes, never model-proposed JSON."""
import json
from .telemetry import number, estimate, mapping


def extract(agent, output, model):
    records = []
    # Older Claude versions return one (possibly pretty-printed) envelope.
    try:
        whole = json.loads(output) if agent == 'claude' else None
    except ValueError:
        whole = None
    lines = [output] if isinstance(whole, dict) else output.splitlines()
    for line in lines:
        try:
            records.append(json.loads(line))
        except (ValueError, UnicodeError):
            # Diagnostic chatter cannot erase a valid native usage envelope.
            continue
    totals = {}
    observations = {}
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
            if record.get('type') == 'rate_limit_event':
                from .telemetry import claude_rate_limits
                import time
                windows = claude_rate_limits(record)
                if windows:
                    observations.update(quotas=windows, quota_observed_at=time.time())
                continue
            if record.get('type') == 'assistant':
                native = mapping(mapping(record.get('message')).get('usage'))
                parts = [number(native.get(key)) for key in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
                if all(value is not None for value in parts):
                    observations['context_tokens'] = sum(parts)
                continue
            if record.get('type') not in (None, 'result'):
                continue  # assistant chunks and result totals describe the same usage
            usage = mapping(record.get('usage'))
            fields = {'tokens_in': usage.get('input_tokens'), 'tokens_out': usage.get('output_tokens'), 'tokens_cached': usage.get('cache_read_input_tokens'), 'tokens_cache_write': usage.get('cache_creation_input_tokens'), 'cost_usd': record.get('total_cost_usd')}
            if number(record.get('total_cost_usd')) is not None:
                observations['cost_kind'] = 'estimated'
            models = mapping(record.get('modelUsage'))
            if len(models) == 1:
                name, details = next(iter(models.items()))
                details = mapping(details)
                model = details.get('canonicalModel') or name
                observations['last_model'] = model
                limit = number(details.get('contextWindow'))
                if limit:
                    observations['context_limit'] = limit
                    if 'context_tokens' in observations:
                        observations['context_pct'] = min(100, observations['context_tokens'] * 100 / limit)
            elif models:
                observations['last_model'] = 'multiple models'
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
    if not totals and not observations:
        return {}
    return estimate({**totals, **observations, 'model': model})
