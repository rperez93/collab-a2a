"""Validated operational settings, read again at each operation boundary."""
from __future__ import annotations
import json
import math

FIELDS = ('model', 'context', 'quota', 'cost', 'subagents', 'worker', 'location')

def _integer(low, high):
    def parse(value):
        if isinstance(value, bool):
            raise ValueError('expected a whole number')
        number = int(str(value))
        if not low <= number <= high:
            raise ValueError(f'expected {low} through {high}')
        return number
    return parse

def _percent(value):
    if isinstance(value, bool):
        raise ValueError('expected percent from 0 through 100')
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise ValueError('expected percent from 0 through 100')
    return number

def _bool(value):
    from .config import _as_bool
    return _as_bool(str(value))

def _fields(value):
    fields = value if isinstance(value, list) else str(value).replace(',', ' ').split()
    if any(item not in FIELDS for item in fields) or len(set(fields)) != len(fields):
        raise ValueError('use each field at most once: ' + ', '.join(FIELDS))
    return list(fields)

def _text(value):
    if not isinstance(value, str) or len(value) > 8000:
        raise ValueError('expected text of at most 8000 characters')
    return value

def _model(value):
    import re
    if not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}", value)):
        raise ValueError("expected a model identifier or empty")
    return value

def _worker_agent(value):
    if value not in ("codex", "claude"):
        raise ValueError("expected codex or claude; use worker start for other providers")
    return value

def _prices(value):
    data = json.loads(value) if isinstance(value, str) else value
    if not isinstance(data, dict) or len(data) > 100:
        raise ValueError('expected a JSON map of at most 100 exact model names')
    allowed = {'input', 'output', 'cached_input', 'cache_write'}
    for model, rates in data.items():
        if not isinstance(model, str) or len(model) > 150 or not isinstance(rates, dict) or not {'input', 'output'} <= rates.keys() or rates.keys() - allowed:
            raise ValueError('each model needs input/output USD per million tokens; optional cached_input/cache_write')
        for rate in rates.values():
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
                raise ValueError('prices must be finite nonnegative numbers')
    return data

# Safety bounds and authentication policy are deliberately not settings.
SPECS = (
 ('watch_participant_fields', list(FIELDS), _fields, 'participant details to show, in order'),
 ('watch_participant_details', False, _bool, 'expand participant details initially'),
 ('stats_stale_after', 1800, _integer(10, 86400), 'usage observation age in seconds before showing stale'),
 ('stats_prices', {}, _prices, 'exact model prices in USD per million tokens; estimates only'),
 ('attention_settle', 20, _integer(0, 3600), 'seconds to collect a burst before an inbox notice'),
 ('attention_gap', 90, _integer(1, 86400), 'minimum seconds between inbox notices'),
 ('worker_auto_start', True, _bool, 'enable a worker on session setup unless explicitly turned off'),
 ('worker_agent', 'codex', _worker_agent, 'provider for automatic worker setup; codex or claude'),
 ('worker_turn_gap', 5, _integer(1, 3600), 'minimum seconds between conversation worker turns'),
 ('worker_timeout', 60, _integer(1, 600), 'deadline in seconds for a conversation worker call'),
 ('worker_max_attempts', 60, _integer(1, 10000), 'maximum model calls in each worker budget window'),
 ('worker_budget_window', 3600, _integer(60, 86400), 'rolling worker budget window in seconds'),
 ('worker_retry_delay', 30, _integer(1, 3600), 'seconds before retrying worker or delivery failures'),
 ('worker_delivery_timeout', 15, _integer(1, 120), 'deadline in seconds for publishing a worker reply'),
 ('worker_delivery_batch', 4, _integer(1, 32), 'maximum queued replies delivered per worker pass'),
 ('worker_page_size', 24, _integer(1, 200), 'maximum inbox events examined per worker turn'),
 ('worker_notice_repeat', 300, _integer(15, 86400), 'seconds before repeating an unresolved worker notice'),
 ('worker_notice_gap', 15, _integer(1, 3600), 'minimum seconds between changed worker notices'),
 ('worker_codex_model', 'gpt-5.6-luna', _model, 'default Codex conversation model; next default-model turn'),
 ('worker_claude_model', 'claude-haiku-4-5', _model, 'default Claude conversation model; next default-model turn'),
 ('worker_opencode_model', '', _model, 'default OpenCode conversation model; empty requires explicit model'),
 ('worker_cursor_model', '', _model, 'default Cursor conversation model; empty requires explicit model'),
 ('rules_text', '', _text, 'local replacement briefing; empty uses shipped rules, next rules read'),
 ('worker_instructions', '', _text, 'additional local conversation guidance, read on each worker turn'),
 ('delegation_max_children', 0, _integer(0, 10000), 'native child concurrency limit; zero means unknown'),
 ('delegation_reserve_pct', 20, _percent, 'percentage points reserved in every applicable quota window'),
 ('delegation_cost_per_child_pct', 0, _percent, 'calibrated percentage points per child; zero means uncalibrated'),
)

def get(name):
    from .config import load_config
    for key, default, parse, _ in SPECS:
        if key == name:
            try:
                return parse(load_config().get(key, default))
            except (TypeError, ValueError, OverflowError):
                return default.copy() if isinstance(default, (dict, list)) else default
    raise KeyError(name)

def set_value(name, value):
    from .config import load_config, save_config
    parse = next(spec[2] for spec in SPECS if spec[0] == name)
    result = parse(value)
    save_config({**load_config(), name: result})
    return result

def settings():
    from .config import Setting
    return tuple(Setting(name, about + ' (hot reload)', default, parse,
                         lambda n=name: get(n), lambda value, n=name: set_value(n, value),
                         multiline=parse is _text)
                 for name, default, parse, about in SPECS)
