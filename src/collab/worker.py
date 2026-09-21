"""Durable, session-local coordination state, independent of the main inbox.

The worker's cursor belongs to the worker. Reading a batch here never marks a
message read by the coding agent. A turn advances that cursor, queues replies,
and records decisions in one transaction, so a restart cannot lose a blocker
between consuming its message and notifying the main agent.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MAX_TEXT = 16000
MAX_PENDING = 1000
DEFAULT_MODELS = {"codex": "gpt-5.6-luna", "claude": "claude-haiku-4-5"}
# Automatic delegation has no private task context. Permit coordination only;
# promises about code, scope, and progress must come from the main agent.
DEFAULT_SCOPE = (
    "Coordinate conversation and ownership using only supplied facts. "
    "Escalate blockers, decisions, conflicting edits, scope changes and missing "
    "task context to the main agent. Never invent progress or promise code changes."
)
SUPPORTED_AGENTS = ("codex", "claude", "opencode", "cursor", "command")


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be non-empty text")
    if len(value.encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"{label} exceeds {MAX_TEXT} UTF-8 bytes")
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"{label} exceeds {MAX_TEXT} serialized JSON bytes")
    return value


def _config(config: dict[str, Any], *, allow_empty_model: bool = False) -> dict[str, Any]:
    agent = config.get("agent")
    if agent not in SUPPORTED_AGENTS:
        raise ValueError("worker agent must be codex, claude, opencode, cursor or command")
    result = {"agent": agent, "scope": _text(config.get("scope"), "scope"),
              "enabled": bool(config.get("enabled", True))}
    if agent == "command":
        argv = config.get("command")
        if (not isinstance(argv, list) or not argv or len(argv) > 64
                or any(not isinstance(arg, str) or not arg or len(arg) > 4096
                       or "\0" in arg for arg in argv)):
            raise ValueError("--command must be a JSON array of non-empty argv strings")
        result["command"] = argv
        if config.get("model"):
            raise ValueError("--model requires a model provider, not --agent command")
    else:
        if config.get("command"):
            raise ValueError("--command requires --agent command")
        from .runtime_settings import get
        result["model_default"] = bool(config.get("model_default", not bool(config.get("model"))))
        result["model"] = _text(config.get("model") or get(f"worker_{agent}_model"), "model (required when no default is configured)", empty=allow_empty_model)
    return result


class Store:
    def __init__(self, root: Path | str):
        self.path = Path(root) / "worker.db"

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            self.path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS repaired (seq INTEGER PRIMARY KEY)")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db):
        row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {
            "config": None, "generation": 0, "turn": 0, "cursor": 0, "summary": "",
            "running": False, "error": "", "retry_at": 0, "last_progress": 0,
        }

    @staticmethod
    def _save(db, state):
        db.execute("INSERT OR REPLACE INTO state VALUES (1,?)", (json.dumps(state),))

    @staticmethod
    def _records(db, kind):
        return [{"id": row["id"], **json.loads(row["data"]), "created_at": row["created_at"]}
                for row in db.execute("SELECT * FROM records WHERE kind=? ORDER BY created_at,id", (kind,))]

    @staticmethod
    def _add(db, kind, data):
        if db.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= MAX_PENDING:
            raise ValueError("worker has too many pending records; resolve its decisions or delivery errors")
        rid = kind[0] + "_" + uuid.uuid4().hex
        db.execute("INSERT INTO records VALUES (?,?,?,?)", (rid, kind, json.dumps(data), time.time()))
        return rid

    def configure(self, config: dict[str, Any]) -> dict[str, Any]:
        config = _config(config)
        with self._db() as db:
            state = self._state(db)
            state.update(config=config, generation=state["generation"] + 1,
                         running=False, error="", retry_at=0)
            self._save(db, state)
        return config

    def ensure_default(self) -> None:
        """Initialize once, without undoing an explicit off or racing start.

        Read-only status can create the database before onboarding, so file
        existence is not a choice. A changed generation records even an off
        issued before the first configuration and must survive reconnects.
        """
        from .runtime_settings import get
        if not get("worker_auto_start"):
            return
        with self._db() as db:
            state = self._state(db)
            if state["config"] is not None or state["generation"]:
                return
            # Empty model settings are valid (other providers require a local
            # choice). Automatic setup must not turn that into a failed join:
            # retain the choice and expose an error without choosing a fallback.
            # The runtime rejects empty models before spawning any provider and
            # reads the corrected default on the next turn.
            config = _config({"agent": get("worker_agent"), "scope": DEFAULT_SCOPE},
                             allow_empty_model=True)
            error = ("Worker requires a model; set worker_" + config["agent"] + "_model"
                     if not config["model"] else "")
            state.update(config=config, generation=1, error=error)
            self._save(db, state)

    def off(self) -> None:
        with self._db() as db:
            state = self._state(db)
            if state["config"]:
                state["config"]["enabled"] = False
            state.update(generation=state["generation"] + 1, running=False)
            self._save(db, state)

    def configuration(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self._db() as db:
            return self._state(db)["config"]

    def snapshot(self) -> dict[str, Any]:
        with self._db() as db:
            state = self._state(db)
            return {**state, "context": self._records(db, "context"),
                    "answers": self._records(db, "answer")}

    def status(self) -> dict[str, Any]:
        with self._db() as db:
            state = self._state(db)
            outbox = self._records(db, "outbox")
            return {**state, "enabled": bool((state["config"] or {}).get("enabled")),
                    "pending": self._records(db, "escalation"),
                    "outbox_count": len(outbox),
                    "outbox": [{"id": r["id"], "to": r["to"], "room": r["room"],
                                "error": r.get("error", ""), "retry_at": r.get("retry_at", 0)}
                               for r in outbox]}

    def context(self, text: str) -> str:
        text = _text(text, "context")
        with self._db() as db:
            return self._add(db, "context", {"text": text})

    def send(self, text: str, *, to: str, room: str = "") -> str:
        """Queue the main agent's exact message without asking a model to repeat it.

        Context supplies facts and can legitimately produce no reply. Treating
        it as a send instruction used to silently consume intended handoffs.
        Explicit sends use the same durable, idempotent outbox as model replies;
        the hub validates the address, and failed delivery remains visible.
        """
        text = _text(text, "message")
        to = _text(to, "recipient")
        room = _text(room, "room", empty=True)
        if len(text) > 8000:
            raise ValueError("message exceeds 8000 characters")
        if len(to) > 200 or len(room) > 200 or any("\0" in value for value in (text, to, room)):
            raise ValueError("invalid message routing or NUL in message")
        with self._db() as db:
            if not (self._state(db)["config"] or {}).get("enabled"):
                raise ValueError("start the worker before queueing a message")
            return self._add(db, "outbox", {"text": text, "to": to, "room": room})

    def pending(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return self._records(db, "escalation")

    def answer(self, escalation_id: str, text: str) -> None:
        text = _text(text, "answer")
        with self._db() as db:
            row = db.execute("SELECT data FROM records WHERE id=? AND kind='escalation'", (escalation_id,)).fetchone()
            if row is None:
                raise ValueError(f"no pending worker decision {escalation_id}")
            prompt = json.loads(row[0])
            # Reuse the decision id; consuming the answer in a committed turn
            # is the only operation that removes the coding agent's response.
            db.execute("UPDATE records SET kind='answer',data=? WHERE id=?",
                       (json.dumps({"text": text, "prompt": prompt}), escalation_id))

    def commit_turn(self, *, expected_cursor: int, cursor: int, summary: str,
                    replies: list[dict[str, Any]], escalations: list[dict[str, Any]],
                    context_ids: list[str] = (), answer_ids: list[str] = (),
                    expected_generation: int | None = None,
                    expected_turn: int | None = None) -> bool:
        # Match the provider schema: 4,000 characters may require 24KB when
        # JSON escapes control characters. Main input has a separate byte cap.
        if not isinstance(summary, str) or len(summary) > 4000:
            raise ValueError("summary exceeds 4000 characters or is not text")
        if not isinstance(cursor, int) or cursor < expected_cursor:
            raise ValueError("worker cursor cannot move backwards")
        if len(replies) > 32 or len(escalations) > 32:
            raise ValueError("a worker turn may produce at most 32 replies and decisions")
        outgoing = [{"text": _text(r.get("text"), "reply"),
                     "to": _text(r.get("to", ""), "recipient", empty=True),
                     "room": _text(r.get("room", ""), "room", empty=True)} for r in replies]
        decisions = []
        for e in escalations:
            if e.get("reason") not in ("blocker", "decision", "conflict", "scope"):
                raise ValueError("invalid worker escalation reason")
            seq = e.get("seq", cursor)
            if not isinstance(seq, int) or seq < 0 or seq > cursor:
                raise ValueError("invalid worker escalation sequence")
            decisions.append({"reason": e["reason"], "question": _text(e.get("question"), "question"), "seq": seq})
        with self._db() as db:
            state = self._state(db)
            if (state["cursor"] != expected_cursor or not (state["config"] or {}).get("enabled")
                    or (expected_turn is not None and state.get("turn", 0) != expected_turn)
                    or (expected_generation is not None and state["generation"] != expected_generation)):
                return False
            for kind, ids in (("context", context_ids), ("answer", answer_ids)):
                if kind == "context" and ids:
                    # The model summary dropped a supplied validation token
                    # after several successful turns. Explicit main-agent facts
                    # must survive summarisation. Retain whole recent records,
                    # oldest first, capped at eight records and 16 KiB total.
                    retained = state.get("retained_context", []) + [
                        row for row in self._records(db, "context") if row["id"] in ids]
                    retained = retained[-8:]
                    while retained and len(json.dumps(retained, ensure_ascii=False).encode()) > 16_384:
                        retained.pop(0)
                    state["retained_context"] = retained
                db.executemany("DELETE FROM records WHERE id=? AND kind=?", [(rid, kind) for rid in ids])
            for reply in outgoing:
                self._add(db, "outbox", reply)
            pending = {(entry["seq"], entry["reason"], entry["question"])
                       for entry in self._records(db, "escalation")}
            for decision in decisions:
                identity = (decision["seq"], decision["reason"], decision["question"])
                if identity not in pending:
                    self._add(db, "escalation", decision)
                    pending.add(identity)
            state.update(cursor=cursor, summary=summary, turn=state.get("turn", 0) + 1, last_progress=time.time(),
                         error="", retry_at=0, running=False)
            self._save(db, state)
            return True

    def outbox(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return self._records(db, "outbox")

    def acknowledge(self, record_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM records WHERE id=? AND kind='outbox'", (record_id,))

    def defer(self, record_id: str, *, retry_at: float, error: str) -> bool:
        """Defer one reply without holding up unrelated peers' conversations."""
        if not isinstance(retry_at, (int, float)) or not math.isfinite(retry_at) or retry_at < 0:
            raise ValueError("outbox retry time must be a finite non-negative timestamp")
        bounded_error = str(error).encode("utf-8", errors="replace")[:MAX_TEXT].decode("utf-8", errors="ignore")
        with self._db() as db:
            row = db.execute("SELECT data FROM records WHERE id=? AND kind='outbox'", (record_id,)).fetchone()
            if row is None:
                return False
            record = json.loads(row[0])
            record.update(retry_at=retry_at, error=bounded_error)
            db.execute("UPDATE records SET data=? WHERE id=? AND kind='outbox'", (json.dumps(record), record_id))
            return True

    def health(self, *, error: str = "", retry_at: float = 0, running: bool = False,
               expected_generation: int | None = None) -> None:
        with self._db() as db:
            state = self._state(db)
            if expected_generation is not None and state["generation"] != expected_generation:
                return
            bounded_error = str(error).encode("utf-8", errors="replace")[:MAX_TEXT].decode("utf-8", errors="ignore")
            state.update(error=bounded_error, retry_at=retry_at, running=running)
            self._save(db, state)

    def repaired(self, seq: int) -> None:
        """Deduplicate a recovered older message without rewinding reply history."""
        with self._db() as db:
            state = self._state(db)
            if seq > state["cursor"] or db.execute("SELECT 1 FROM repaired WHERE seq=?", (seq,)).fetchone():
                return
            self._add(db, "escalation", {"reason": "decision", "seq": seq,
                "question": f"Recovered older message #{seq}. Inspect collab recv and supply a decision if a reply is still needed; prior worker replies were not replayed."})
            db.execute("INSERT INTO repaired VALUES(?)", (seq,))

    def report_stats(self, figures: dict) -> dict:
        """Merge a bounded, explicit worker snapshot without changing runtime state.

        Quotas are account observations, never inferred from the main agent's
        model or copied from its allowance. No credentials/account IDs survive
        the fixed telemetry allow-list.
        """
        from .telemetry import worker_report, stamp_observations
        from .stats import QUOTA_FIELDS
        cleaned = worker_report(figures)
        for key in ("enabled", "running", "agent", "turns", "attempts", "pending", "errors"):
            cleaned.pop(key, None)
        if "model" in cleaned:
            cleaned.setdefault("usage_model", cleaned["model"])
        cleaned = stamp_observations(cleaned)
        if "quotas" in cleaned:
            cleaned.setdefault("quota_observed_at", cleaned.get("observed_at") or time.time())
        with self._db() as db:
            state = self._state(db)
            current = state.setdefault("reported_stats", {})
            # A token-only update can be the first observation from a new
            # account. Withdraw the old relationship then, before that source
            # later supplies quota; otherwise its name already matches and the
            # previous account's independent/shared scope survives incorrectly.
            source_changed = "source" in cleaned and cleaned["source"] != current.get("source")
            if source_changed and ("quota_scope" in current or "quotas" in current):
                cleaned.setdefault("quota_scope", "unknown")
            if "quotas" in cleaned:
                cleaned.setdefault("quota_scope", "unknown" if source_changed else current.get("quota_scope", "unknown"))
                for key in QUOTA_FIELDS:
                    current.pop(key, None)
            # Keep explicit null as a mask over native historical totals;
            # deleting the override would resurrect the old measurement.
            current.update(cleaned)
            self._save(db, state)
            return dict(current)

    def record_usage(self, figures: dict) -> None:
        from .telemetry import number, stamp_observations
        with self._db() as db:
            state = self._state(db)
            usage = state.setdefault("usage", {})
            model = str(figures.get("model") or "unknown")[:150]
            if isinstance(figures.get("last_model"), str):
                usage["last_model"] = figures["last_model"][:150]
            previous_model = usage.get("usage_model")
            usage["usage_model"] = model if previous_model in (None, model) else "mixed models"
            # Keep observed costs computed at the original model/rate, even if
            # defaults or pricing change later. Bounded buckets retain useful
            # per-model accounting without a model-name cardinality leak.
            buckets = state.setdefault("usage_by_model", {})
            bucket_key = model if model in buckets or len(buckets) < 32 else "other models"
            bucket = buckets.setdefault(bucket_key, {})
            for key in ("tokens_in", "tokens_out", "tokens_cached", "tokens_cache_write", "cost_usd"):
                value = number(figures.get(key))
                if value is not None:
                    for target in (usage, bucket):
                        total = target.get(key, 0) + value
                        if number(total) is not None:
                            target[key] = total
            if figures.get("cost_kind"):
                old_kind = usage.get("cost_kind")
                usage["cost_kind"] = "mixed" if old_kind and old_kind != figures["cost_kind"] else figures["cost_kind"]
            usage.update(observed_at=time.time(), cost_scope="observed worker lifetime")
            if "quotas" in figures:
                from .telemetry import worker_report
                native = worker_report(figures)
                if native.get("quotas") or figures["quotas"] == {}:
                    usage["quotas"] = native.get("quotas", {})
                    usage["quota_observed_at"] = number(figures.get("quota_observed_at")) or time.time()
            # Only groups present in this native envelope become fresh. A
            # token-only result must not freshen an older cost observation.
            stamps = stamp_observations(figures)
            for field in ("context_tokens", "context_limit", "context_pct"):
                if number(figures.get(field)) is not None:
                    usage[field] = figures[field]
            for group in ("tokens", "cost", "context"):
                key = group + "_observed_at"
                if key in stamps:
                    usage[key] = stamps[key]
            self._save(db, state)

    def reserve_turn(self, *, expected_generation: int, now: float | None = None,
                     limit: int = 60, window: float = 3600) -> float | None:
        """Reserve a model attempt before launch, including attempts that fail.

        Restarting the listener must not reset its spending limit. Successful
        replies, invalid provider output and provider failures all spend the
        same durable reservation, preventing a busy peer or broken provider
        from keeping the model running indefinitely.
        """
        if not isinstance(limit, int) or limit < 1 or window <= 0:
            raise ValueError("worker turn limit and window must be positive")
        now = time.time() if now is None else now
        with self._db() as db:
            state = self._state(db)
            if (state["generation"] != expected_generation
                    or not (state["config"] or {}).get("enabled")):
                return None
            history = [stamp for stamp in state.get("model_attempts", []) if stamp > now - 86400]
            attempts = [stamp for stamp in history if stamp > now - window]
            state["model_attempts"] = history
            if len(attempts) >= limit or len(history) >= 10000:
                retry_at = min(attempts) + window if len(attempts) >= limit else min(history) + 86400
                state.update(error="Worker hourly turn limit reached", retry_at=retry_at,
                             running=False)
                self._save(db, state)
                return retry_at
            history.append(now)
            state["attempts_total"] = state.get("attempts_total", 0) + 1
            self._save(db, state)
            return 0.0


def configure(root, config):
    return Store(root).configure(config)


def configuration(root):
    return Store(root).configuration()


def status(root):
    return Store(root).status()


def context(root, text):
    return Store(root).context(text)


def pending(root):
    return Store(root).pending()


def answer(root, escalation_id, text):
    return Store(root).answer(escalation_id, text)


def metrics(root):
    store = Store(root)
    if not store.path.exists():
        return None
    state = store.status()
    cfg = state["config"] or {}
    from .runtime_settings import get
    model = get(f"worker_{cfg['agent']}_model") if cfg.get("model_default") else cfg.get("model", "")
    return {**state.get("usage", {}), **state.get("reported_stats", {}), "enabled": state["enabled"], "running": state["running"],
        "agent": cfg.get("agent", ""), "model": model or state.get("reported_stats", {}).get("model", ""), "turns": state["turn"],
        "attempts": state.get("attempts_total", 0), "pending": len(state["pending"]),
        "errors": int(bool(state["error"])), "source": state.get("reported_stats", {}).get("source") or "collab-worker",
        "observed_at": max(state.get("last_progress", 0), state.get("usage", {}).get("observed_at", 0),
                           (state.get("reported_stats", {}).get("observed_at") or 0))}


def notify_repairs(root, inbox):
    store = Store(root)
    if not store.path.exists():
        return
    for seq in inbox.pending_repairs():
        store.repaired(seq)
        inbox.acknowledge_repairs([seq])
