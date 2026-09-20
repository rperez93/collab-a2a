"""Conditional native-child capacity estimates; never launches or reserves work."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _stamp(value: Any) -> float | None:
    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
        except ValueError:
            pass
    return None


def estimate_capacity(stats: dict[str, Any], *, concurrency_limit: int | None = None,
                      active_children: int | None = None, reserve_percent: float | None = None,
                      percent_per_child: float | dict[str, float] | None = None,
                      task_budget_percent: float | dict[str, float] | None = None,
                      max_age_seconds: float = 120, windows: list[str] | None = None,
                      now: float | None = None) -> dict[str, Any]:
    """Maximum *additional* children under explicitly stated budget assumptions.

    A percent is percentage points of each selected account window. A scalar
    applies to every window; a map calibrates them separately. Existing active
    children reserve a full further budget each because their future use is
    unknown. An unobserved quota, unknown active count or missing calibration
    cannot become a positive estimate just because concurrency has space.
    """
    from .runtime_settings import get
    if not isinstance(stats, dict):
        raise ValueError('stats must be an object')
    if concurrency_limit is None:
        concurrency_limit = get('delegation_max_children') or None
    if reserve_percent is None:
        reserve_percent = get('delegation_reserve_pct')
    if percent_per_child is None and task_budget_percent is None:
        percent_per_child = get('delegation_cost_per_child_pct') or None
    if percent_per_child is not None and task_budget_percent is not None:
        raise ValueError('choose calibrated percent-per-child or an explicit task budget')
    for value, label in ((concurrency_limit, 'concurrency limit'), (active_children, 'active children')):
        if value is not None and (type(value) is not int or not 0 <= value <= 10000):
            raise ValueError(f'{label} must be a whole number from 0 through 10000')
    reserve = _number(reserve_percent)
    freshness = _number(max_age_seconds)
    if reserve is None or not 0 <= reserve <= 100 or freshness is None or freshness <= 0:
        raise ValueError('reserve must be 0..100 percent and maximum age must be positive')
    now = time.time() if now is None else now
    cost = task_budget_percent if task_budget_percent is not None else percent_per_child
    if cost is not None:
        costs = cost.values() if isinstance(cost, dict) else [cost]
        if any(_number(item) is None or not 0 < _number(item) <= 100 for item in costs):
            raise ValueError('child budgets must be finite percentage points greater than 0 and at most 100')
    available_slots = None if concurrency_limit is None or active_children is None else max(0, concurrency_limit - active_children)
    result: dict[str, Any] = {
        'status': 'unknown', 'maximum_additional_children': None,
        'concurrency_limit': concurrency_limit, 'active_children': active_children,
        'available_slots': available_slots, 'reserve_percent': reserve,
        'budget_source': 'explicit task budget' if task_budget_percent is not None else 'calibration' if cost is not None else 'unknown',
        'quota_source': stats.get('source') or 'unknown',
        'binding_window': None, 'binding_account': 'unknown', 'binding_allowance': None,
        'windows': [], 'reasons': [],
        'assumptions': ['This is an estimate, not a quota reservation or permission to launch.',
                        'Other consumers of the same account add no usage during this work.',
                        'Each active child is reserved one further full child budget.',
                        'A scalar child budget applies to every selected window.'],
    }
    if available_slots == 0:
        result.update(status='known-zero', maximum_additional_children=0)
        result['reasons'].append('the supplied concurrency limit is exhausted')
        return result
    quota = stats.get('quotas')
    if not isinstance(quota, dict) or not quota:
        single = _number(stats.get('quota_used_pct'))
        quota = {'account': {'used_pct': single, 'resets_at': stats.get('quota_reset_at')}} if single is not None else {}
    if windows is not None:
        if len(windows) > 32 or len(set(windows)) != len(windows):
            raise ValueError('select at most 32 distinct quota windows')
        quota = {key: quota.get(key) for key in windows}
    if not quota or len(quota) > 32:
        result['reasons'].append('quota windows are missing or exceed the supported 32-window bound')
        return result
    capacities = []
    unavailable = []
    for name, window in quota.items():
        window = window if isinstance(window, dict) else {}
        observed = _stamp(window.get('observed_at', stats.get('quota_observed_at', stats.get('observed_at'))))
        used = _number(window.get('used_pct'))
        reset = _stamp(window.get('resets_at'))
        age = now - observed if observed is not None else None
        row = {'window': name, 'used_percent': used, 'observation_age_seconds': age,
               'resets_at': window.get('resets_at'), 'available_after_reserve_percent': None,
               'per_child_percent': None, 'maximum_additional_children': None}
        result['windows'].append(row)
        if used is None or not 0 <= used <= 100 or age is None or age < 0 or age > freshness or (reset is not None and reset <= now):
            unavailable.append(str(name))
            continue
        available = max(0.0, 100 - used - reserve)
        row['available_after_reserve_percent'] = available
        if available == 0:
            # A fresh exhausted window proves zero without cost calibration,
            # even when another window is unknown. Every child needs all of
            # its applicable limits to have room.
            from .stats import split_window
            result.update(status='known-zero', maximum_additional_children=0, binding_window=name,
                          binding_allowance=split_window(str(name))[0] or None)
            result['reasons'].append('a fresh quota window has no room after the reserve')
            return result
        per_child = _number(cost.get(name)) if isinstance(cost, dict) else _number(cost)
        row['per_child_percent'] = per_child
        if per_child is None or active_children is None:
            continue
        count = max(0, math.floor((available + 1e-10) / per_child) - active_children)
        row['maximum_additional_children'] = count
        capacities.append((count, name))
    if unavailable:
        result['reasons'].append('missing, stale, future-dated or reset quota: ' + ', '.join(unavailable))
    if concurrency_limit is None:
        result['reasons'].append('native concurrency limit is unknown; supply it explicitly')
    if active_children is None:
        result['reasons'].append('current active native-child count is unknown')
    if len(capacities) != len(quota) and not unavailable:
        result['reasons'].append('a calibrated child cost or explicit task budget is missing for a quota window')
    if result['reasons']:
        return result
    quota_limit, binding = min(capacities)
    from .stats import split_window
    result.update(status='estimate', maximum_additional_children=min(available_slots, quota_limit),
                  binding_window=binding, binding_allowance=split_window(str(binding))[0] or None)
    result['reasons'].append('minimum of free native slots and all selected fresh quota-window budgets')
    return result
