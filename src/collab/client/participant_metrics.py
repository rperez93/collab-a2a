"""Participant facts for the human panel, keeping unknown and zero distinct.

A worker consumes its own turns and tokens; counting it as a coding subagent,
or adding its bill to the parent's bill, would imply a scope the providers do
not promise. These deliberately remain separate labelled rows.
"""
from __future__ import annotations

import math
from typing import Any

from ..stats import is_stale, quota_summary, reported_age
from .statusbar import money_text

FIELDS = ("model", "context", "quota", "cost", "subagents", "worker", "location")
RESOURCE_FIELDS = ("context", "quota", "cost", "subagents", "worker")
IDENTITY_FIELDS = ("model", "location")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def _count(value: Any) -> str:
    number = _number(value)
    return f"{number:,.0f}" if number is not None else "unknown"


def freshness(stats: dict[str, Any]) -> str:
    # The hub receiving an old measurement does not make it fresh. Older
    # participants have no observation timestamp; say that instead of dating
    # the observation with the heartbeat's arrival time.
    stamp = {"reported_at": stats.get("observed_at")}
    age = reported_age(stamp)
    return f"stale · {age}" if is_stale(stamp) and age != "age unknown" else age


def provenance(stats: dict[str, Any]) -> str:
    return f"source {stats.get('source') or 'unknown'} · {freshness(stats)}"


def cost_text(stats: dict[str, Any]) -> str:
    value = _number(stats.get("cost_usd"))
    if value is None:
        return "cost unknown"
    kind = stats.get("cost_kind")
    label = "est. cost" if kind == "estimated" else "reported cost"
    # A legacy number has no provenance; calling it reported would promise
    # more than the old schema says, so leave its kind explicit.
    if kind == "mixed":
        label = "mixed reported/est. cost"
    elif kind not in ("reported", "estimated"):
        label = "cost (kind unknown)"
    return f"{label} {money_text(value)} · {stats.get('cost_scope') or 'scope unknown'}"


def metric_lines(person: dict[str, Any], fields: list[str] | tuple[str, ...],
                 *, detailed: bool) -> list[str]:
    """Ordered visible fields; expanded rows retain source and measurement age."""
    stats = person.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    result: list[str] = []
    for field in fields:
        line = ""
        extra: list[str] = []
        if field == "model":
            line = f"model {stats.get('model') or 'unknown'}"
        elif field == "context":
            pct = _number(stats.get("context_pct"))
            line = f"ctx {pct:.0f}%" if pct is not None else "ctx unknown"
            if detailed:
                line += f" · {_count(stats.get('context_tokens'))} / {_count(stats.get('context_limit'))} tokens"
        elif field == "quota":
            quota = quota_summary(stats, with_resets=detailed)
            line = quota or "quota unknown"
            if quota:
                # A fresh token report must not rejuvenate an older allowance
                # reading. Quota polling has its own observation clock.
                age = freshness({"observed_at": stats.get("quota_observed_at")})
                if detailed:
                    extra.append(f"quota {age}")
                else:
                    line += f" · {age}"
        elif field == "cost":
            line = cost_text(stats)
            if detailed:
                extra.append(f"tokens in {_count(stats.get('tokens_in'))} · out {_count(stats.get('tokens_out'))} · cached {_count(stats.get('tokens_cached'))}")
                extra.append("tokens " + freshness({"observed_at": stats.get("tokens_observed_at")}))
        elif field == "subagents":
            sub = stats.get("subagents")
            sub = sub if isinstance(sub, dict) else {}
            line = f"subagents {_count(sub.get('active'))} active / {_count(sub.get('total'))} total"
            if detailed:
                extra.append(provenance(sub))
        elif field == "worker":
            worker = stats.get("worker")
            worker = worker if isinstance(worker, dict) else {}
            if worker.get("enabled") is False:
                line = "worker disabled"
            elif worker:
                state = "running" if worker.get("running") is True else "idle" if worker.get("running") is False else "state unknown"
                line = f"worker {state} · {_count(worker.get('turns'))} turns"
            else:
                line = "worker unknown"
            if detailed and worker:
                extra.extend([
                    f"worker {worker.get('agent') or 'agent unknown'} · configured {worker.get('model') or 'unknown'}",
                    f"attempts {_count(worker.get('attempts'))} · pending {_count(worker.get('pending'))} · errors {_count(worker.get('errors'))}",
                    f"worker {cost_text(worker)}",
                    f"worker tokens in {_count(worker.get('tokens_in'))} · out {_count(worker.get('tokens_out'))} · cached {_count(worker.get('tokens_cached'))}",
                    f"worker last model {worker.get('last_model') or 'not yet reported'}",
                    "worker tokens " + freshness({"observed_at": worker.get("tokens_observed_at")}),
                    "worker cost " + freshness({"observed_at": worker.get("cost_observed_at")}),
                    f"worker cache writes {_count(worker.get('tokens_cache_write'))}",
                    f"worker {quota_summary(worker, with_resets=True) or 'quota unknown'} · {worker.get('quota_scope') or 'unknown'} scope",
                    "worker quota " + freshness({"observed_at": worker.get("quota_observed_at")}),
                    (f"worker ctx {_count(worker.get('context_pct'))}% · {_count(worker.get('context_tokens'))} / {_count(worker.get('context_limit'))} tokens"
                     if any(_number(worker.get(key)) is not None for key in ('context_pct', 'context_tokens', 'context_limit'))
                     else "worker context not exposed by Codex exec" if worker.get('agent') == 'codex'
                     else "worker context not yet reported"),
                    provenance(worker),
                ])
        elif field == "location":
            repo = person.get("repo") or "repo unknown"
            branch = person.get("branch")
            line = f"{repo}/{branch}" if branch else str(repo)
            line += f" · {person.get('machine') or 'machine unknown'}"
        if line:
            group = "context" if field == "context" else field
            if group in ("context", "cost", "model") and group + "_observed_at" in stats:
                age = freshness({"observed_at": stats[group + "_observed_at"]})
                if detailed:
                    extra.append(f"{group} {age}")
                else:
                    line += f" · {age}"
            result.append(line)
            result.extend(extra)
    # Freshness is not a switchable metric. Hiding a field must not silently
    # turn an old or untraceable reading into an apparently live figure.
    if result and any(field != "location" for field in fields):
        if detailed:
            result.append(provenance(stats))
        else:
            # The compact row is clipped in narrow panels. Its qualification
            # must precede the figures, or a stale value can look current.
            result.insert(0, freshness(stats))
    return result
