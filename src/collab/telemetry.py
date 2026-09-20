"""Source-specific usage snapshots. No transcript scraping or guessed allowances."""
from __future__ import annotations
import math
import time

NUMBERS = {'quota_observed_at', 'observed_at', 'tokens_cached', 'tokens_cache_write', 'context_tokens', 'context_limit', 'model_observed_at', 'context_observed_at', 'cost_observed_at', 'tokens_observed_at'}
TEXT = {'source', 'cost_kind', 'cost_scope'}
NESTED = {
 'subagents': {'active', 'total', 'source', 'observed_at'},
 'worker': {'enabled', 'running', 'agent', 'model', 'turns', 'attempts', 'pending', 'errors',
            'tokens_in', 'tokens_out', 'tokens_cached', 'cost_usd', 'cost_kind', 'observed_at', 'source'},
}

def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None

def mapping(value):
    return value if isinstance(value, dict) else {}

def extended(data):
    out = {}
    for key in NUMBERS | TEXT:
        value = data.get(key)
        if value is None and key in data:
            out[key] = None
        elif key in NUMBERS and number(value) is not None:
            out[key] = value
        elif key in TEXT and isinstance(value, str):
            out[key] = value[:150]
    for key, fields in NESTED.items():
        raw = data.get(key)
        if raw is None and key in data:
            out[key] = None
        elif isinstance(raw, dict):
            kept = {}
            for field in fields:
                value = raw.get(field)
                if field in ('enabled', 'running') and isinstance(value, bool):
                    kept[field] = value
                elif field in ('source', 'agent', 'model', 'cost_kind') and isinstance(value, str):
                    kept[field] = value[:150]
                elif number(value) is not None:
                    kept[field] = value
            out[key] = kept
    return out

GROUPS = {
    'model': ('model',), 'context': ('context_pct', 'context_tokens', 'context_limit'),
    'cost': ('cost_usd',), 'tokens': ('tokens_in', 'tokens_out', 'tokens_cached', 'tokens_cache_write'),
}

def stamp_observations(figures, *, now=None):
    out = dict(figures)
    stamp = out.get('observed_at') or (time.time() if now is None else now)
    for group, keys in GROUPS.items():
        if any(key in out for key in keys):
            out.setdefault(group + '_observed_at', stamp)
    return out

def estimate(figures):
    from .runtime_settings import get
    out = dict(figures)
    if out.get('cost_usd') is not None:
        out.setdefault('cost_kind', 'reported')
        return out
    rates = get('stats_prices').get(out.get('model'))
    if not rates or any(number(out.get(k)) is None for k in ('tokens_in', 'tokens_out')):
        return out
    # Canonical input is uncached input; cache counters are separate.
    cost = out['tokens_in'] * rates['input'] + out['tokens_out'] * rates['output']
    for key, rate in (('tokens_cached', 'cached_input'), ('tokens_cache_write', 'cache_write')):
        count = out.get(key, 0)
        if count is None:
            return out  # explicit unknown cache usage cannot be priced as zero
        if count and rate not in rates:
            return out  # absent cache prices cannot be guessed
        cost += count * rates.get(rate, 0)
    out.update(cost_usd=cost / 1_000_000, cost_kind='estimated', cost_scope='session')
    return out

def parse(provider, data):
    """Parse an explicitly scoped provider snapshot supplied by a local adapter.

    Child lists must be complete; partial pages must not claim a total. Provider
    hooks remain opt-in and must select the matching Collab session explicitly.
    """
    if not isinstance(data, dict):
        raise ValueError('provider telemetry must be a JSON object')
    out = {'source': provider, 'observed_at': time.time(), 'cost_scope': 'session'}
    def take(key, value):
        if number(value) is not None:
            out[key] = value
    if provider == 'claude':
        from .stats import collect_quotas
        model = data.get('model', {})
        if isinstance(model, dict):
            out['model'] = model.get('id') or model.get('display_name') or ''
        cost = mapping(data.get('cost'))
        take('cost_usd', cost.get('total_cost_usd'))
        cw = mapping(data.get('context_window'))
        take('context_pct', cw.get('used_percentage'))
        take('context_limit', cw.get('context_window_size'))
        usage = cw.get('current_usage')
        if isinstance(usage, dict) and number(usage.get('input_tokens')) is not None:
            take('context_tokens', sum(usage.get(k, 0) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens') if number(usage.get(k, 0)) is not None))
        # Statusline totals describe the current context, not lifetime spend.
        if 'rate_limits' in data:
            out['quotas'] = collect_quotas(data)
        # Subagent statusline describes visible tasks, not lifetime children.
        # This documented hook contains only visible subagent rows.
        if isinstance(data.get('tasks'), list):
            tasks = list({t['id']: t for t in data['tasks'] if isinstance(t, dict) and isinstance(t.get('id'), str)}.values())
            out['subagents'] = {'total': len(tasks), 'source': 'claude-visible-agent-tasks', 'observed_at': out['observed_at']}
            if not any(k in data for k in ('context_window', 'cost', 'rate_limits')):
                for key in ('observed_at', 'source', 'cost_scope', 'model'):
                    out.pop(key, None)
            if all(t.get('status') in ('running', 'completed', 'failed', 'killed') for t in tasks):
                out['subagents']['active'] = sum(t['status'] == 'running' for t in tasks)
    elif provider == 'cursor':
        usage = mapping(data.get('usage'))
        for key, native in (('tokens_in', 'inputTokens'), ('tokens_out', 'outputTokens'), ('tokens_cached', 'cacheReadTokens'), ('tokens_cache_write', 'cacheWriteTokens')):
            take(key, usage.get(native))
        cost = mapping(data.get('cost'))
        if number(cost.get('chargedCents')) is not None:
            take('cost_usd', cost['chargedCents'] / 100)
        # runs are components of usage; never add them to the aggregate again.
    elif provider == 'codex':
        params = mapping(data.get('params', data))
        usage = mapping(params.get('tokenUsage', params))
        total = mapping(usage.get('total'))
        for key, native in (('tokens_in', 'inputTokens'), ('tokens_out', 'outputTokens'), ('tokens_cached', 'cachedInputTokens')):
            take(key, total.get(native))
        if 'tokens_in' in out and 'tokens_cached' in out:
            out['tokens_in'] = max(0, out['tokens_in'] - out['tokens_cached'])
        last = mapping(usage.get('last'))
        take('context_tokens', last.get('totalTokens'))
        take('context_limit', usage.get('modelContextWindow'))
        children = data.get('children')
        if isinstance(children, list) and data.get('children_complete') is True:
            out['subagents'] = {'total': len(children), 'source': 'codex-thread-list', 'observed_at': out['observed_at']}
            # A different app-server instance cannot prove live activity.
    elif provider == 'opencode':
        messages = data.get('messages')
        if isinstance(messages, list) and data.get('messages_complete') is True:
            seen = set()
            rows = []
            for message in messages:
                row = mapping(message.get('info', message)) if isinstance(message, dict) else {}
                if row.get('role') == 'assistant' and row.get('id') and row['id'] not in seen:
                    rows.append(row); seen.add(row['id'])
            for key, native in (('tokens_in', 'input'), ('tokens_out', 'output')):
                if rows and all(number(mapping(r.get('tokens')).get(native)) is not None for r in rows):
                    take(key, sum(r['tokens'][native] for r in rows))
            for key, native in (('tokens_cached', 'read'), ('tokens_cache_write', 'write')):
                if rows and all(number(mapping(mapping(r.get('tokens')).get('cache')).get(native)) is not None for r in rows):
                    take(key, sum(r['tokens']['cache'][native] for r in rows))
            if rows and all(number(r.get('cost')) is not None for r in rows):
                take('cost_usd', sum(r['cost'] for r in rows))
        children = data.get('children')
        if isinstance(children, list) and data.get('children_complete') is True:
            out['subagents'] = {'total': len(children), 'source': 'opencode-session-children', 'observed_at': out['observed_at']}
            statuses = mapping(data.get('statuses'))
            if all(isinstance(c, dict) and c.get('id') in statuses for c in children):
                out['subagents']['active'] = sum(mapping(statuses[c['id']]).get('type') in ('busy', 'retry') for c in children)
    else:
        raise ValueError('provider must be codex, claude, opencode or cursor')
    if 'quotas' in out:
        out['quota_observed_at'] = time.time()
    if isinstance(data.get('model'), str):
        out['model'] = data['model'][:150]
    if out.get('context_limit') and 'context_tokens' in out and 'context_pct' not in out:
        out['context_pct'] = min(100, out['context_tokens'] * 100 / out['context_limit'])
    out = estimate(out)
    if not any(key in out for keys in GROUPS.values() for key in keys):
        for key in ('observed_at', 'source', 'cost_scope'):
            out.pop(key, None)
    if 'cost_usd' not in out:
        out.pop('cost_scope', None)
    return stamp_observations(out)
