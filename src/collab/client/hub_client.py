"""HTTP client for a collab hub.

Sending goes over standard A2A ``SendMessage`` rather than a private endpoint,
so the path our CLI exercises is the same one any conformant A2A client would
use.  Everything multi-party (join, roster, rooms, tasks) uses the extension.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from ..protocol import EXT_PREFIX, Envelope, RPC_PATH, new_id

# httpx IS IMPORTED WHERE IT IS CALLED, not here. cli.py imports this module
# for every command so that `except HubError` can be written, and httpx at the
# top of it — with its own CLI's rich and click behind it — was 80 ms of a
# 180 ms `import collab.cli`, paid by `recv`, `send` and `status`, which never
# construct a client. A second `import httpx` inside a method is a dictionary
# lookup once the first has run.

#: The 1.0 dispatcher assumes 0.3 unless told otherwise, so this header is
#: required on every JSON-RPC call, not optional.
A2A_HEADERS = {"A2A-Version": "1.0"}

DEFAULT_TIMEOUT = 15.0


class HubError(RuntimeError):
    pass


class HubClient:
    def __init__(self, base_url: str, token: str | None = None,
                 *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        import httpx

        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- plumbing -------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(extra or {})
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        import httpx

        try:
            r = self._client.request(
                method, f"{self.base_url}{path}", headers=self._headers(kw.pop("headers", None)), **kw
            )
        except httpx.HTTPError as exc:
            raise HubError(f"cannot reach the hub at {self.base_url}: {exc}") from exc
        if r.status_code == 401:
            # The hub says *why* — a stale invite and a revoked token are very
            # different problems, and guessing sends people the wrong way.
            detail = ""
            try:
                detail = str(r.json().get("detail") or "")
            except ValueError:
                detail = ""
            raise HubError(detail or "the hub rejected this token — you may have "
                                     "been removed from the session")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = r.json().get("detail") or r.text
            except ValueError:
                detail = r.text
            raise HubError(f"{method} {path} failed ({r.status_code}): {detail}")
        return r.json()

    # --- session --------------------------------------------------------------

    def join(self, invite: str, name: str, hello: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"{EXT_PREFIX}/join",
            json={"invite": invite, "name": name, "hello": hello},
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", f"{EXT_PREFIX}/health")

    def snapshot(self) -> dict[str, Any]:
        return self._request("GET", f"{EXT_PREFIX}/snapshot")

    def participants(self) -> dict[str, Any]:
        return self._request("GET", f"{EXT_PREFIX}/participants")

    def history(self, room: str | None = None, limit: int = 50) -> list[Envelope]:
        params: dict[str, Any] = {"limit": limit}
        if room:
            params["room"] = room
        data = self._request("GET", f"{EXT_PREFIX}/history", params=params)
        return [Envelope.from_dict(e) for e in data["events"]]

    def rooms(self) -> list[str]:
        return self._request("GET", f"{EXT_PREFIX}/rooms")["rooms"]

    def create_room(self, name: str) -> list[str]:
        return self._request("POST", f"{EXT_PREFIX}/rooms", json={"name": name})["rooms"]

    def rename(self, name: str) -> str:
        return self._request("POST", f"{EXT_PREFIX}/rename", json={"name": name})["name"]

    def revoke(self, name: str) -> str:
        return self._request("POST", f"{EXT_PREFIX}/revoke", json={"name": name})["removed"]

    # --- tasks ----------------------------------------------------------------

    def tasks(self, open_only: bool = False) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"{EXT_PREFIX}/tasks", params={"open_only": str(open_only).lower()}
        )["tasks"]

    def task_action(self, action: str, *, task_id: str | None = None, title: str = "",
                    detail: str | None = None, room: str | None = None,
                    project: str | None = None) -> dict[str, Any]:
        """One action on one task.

        `project` is three-valued and the middle value is the useful one:
        `None` leaves the task where it is, a project id moves it into that
        project, and `""` takes it out of the one it is in. A single falsy
        check would have run «say nothing about it» and «take it out» together,
        and there would be no way to express the second.
        """
        # `detail` IS SENT ONLY WHEN THE CALLER SAID SOMETHING. Always sending
        # it — `""` by default — reached the route as «clear it», and a claim
        # or a move erased the description the claimant had just read. None
        # means the key is absent; "" is a real request to clear.
        payload: dict[str, Any] = {"action": action, "title": title}
        if detail is not None:
            payload["detail"] = detail
        if task_id:
            payload["id"] = task_id
        if room:
            payload["room"] = room
        if project is not None:
            payload["project"] = project
        return self._request("POST", f"{EXT_PREFIX}/tasks", json=payload)["task"]

    # --- projects --------------------------------------------------------------
    #
    # A project is a bundle of tasks that belongs to somebody. It does not
    # interact with a batch: see `batch` below, which counts every task in its
    # window whether or not the task is in a project.

    def projects(self, *, owner: str = "",
                 archived: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if owner:
            params["owner"] = owner
        if archived:
            params["archived"] = "true"
        return self._request("GET", f"{EXT_PREFIX}/projects",
                             params=params or None)["projects"]

    def project(self, project_id: str) -> dict[str, Any]:
        """One project with its tasks and its comments, in a single request."""
        return self._request("GET", f"{EXT_PREFIX}/projects/{project_id}")

    def project_action(self, action: str, *, project_id: str | None = None,
                       title: str = "", detail: str | None = None,
                       owner: str | None = None) -> dict[str, Any] | None:
        """propose / update / assign / delete.

        `owner` is sent only when given, and an empty string is a real value
        meaning «belongs to nobody just now» — which somebody is entitled to
        say, and which omitting the key could not express.
        """
        payload: dict[str, Any] = {"action": action}
        if project_id:
            payload["id"] = project_id
        if title:
            payload["title"] = title
        if detail is not None:            # "" is a real request: clear it
            payload["detail"] = detail
        if owner is not None:
            payload["owner"] = owner
        return self._request("POST", f"{EXT_PREFIX}/projects",
                             json=payload)["project"]

    # --- comments, and the pull requests a task produced ------------------------

    def comments(self, subject: str, subject_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"{EXT_PREFIX}/comments",
                             params={"subject": subject,
                                     "id": subject_id})["comments"]

    def add_comment(self, subject: str, subject_id: str, text: str) -> dict[str, Any]:
        return self._request("POST", f"{EXT_PREFIX}/comments",
                             json={"subject": subject, "id": subject_id,
                                   "text": text})["comment"]

    def task_prs(self, task_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"{EXT_PREFIX}/task-prs",
                             params={"id": task_id})["prs"]

    def task_pr_action(self, action: str, *, task_id: str, url: str,
                       number: int | None = None) -> list[dict[str, Any]]:
        """add / remove. Returns every pull request the task has afterwards.

        The whole list rather than the one row, because a task has as many as
        the work took and the caller is about to print all of them.
        """
        payload: dict[str, Any] = {"action": action, "id": task_id, "url": url}
        if number is not None:
            payload["number"] = number
        return self._request("POST", f"{EXT_PREFIX}/task-prs",
                             json=payload)["prs"]

    # --- batches ---------------------------------------------------------------

    def batch(self) -> dict[str, Any] | None:
        """The hub's count of the current batch. Never computed here.

        Two clients asking this get byte-identical arithmetic because neither
        of them does any — see collab.batch.
        """
        return self._request("GET", f"{EXT_PREFIX}/batch")["batch"]

    def batch_action(self, action: str, *, name: str = "",
                     batch_id: str | None = None) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"action": action}
        if name:
            payload["name"] = name
        if batch_id:
            payload["id"] = batch_id
        return self._request("POST", f"{EXT_PREFIX}/batch", json=payload)["batch"]

    # --- sending, over real A2A ------------------------------------------------

    def send(self, env: Envelope) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": new_id("rpc"),
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": new_id("msg"),
                    "role": "ROLE_USER",
                    "parts": [{
                        "data": env.to_dict(),
                        "mediaType": "application/json",
                    }],
                }
            },
        }
        result = self._request("POST", RPC_PATH, json=payload, headers=A2A_HEADERS)
        if "error" in result:
            raise HubError(f"hub rejected the message: {result['error'].get('message')}")
        return result.get("result", {})

    def report_stats(self, figures: dict[str, Any],
                     identity: dict[str, str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"stats": figures}
        payload.update(identity or {})
        return self._request("POST", f"{EXT_PREFIX}/stats", json=payload)

    def report_activity(self, activity: dict[str, Any]) -> dict[str, Any]:
        """Say what this agent is doing now. Everyone's roster follows."""
        return self._request("POST", f"{EXT_PREFIX}/activity",
                             json=activity)["activity"]

    # --- files ------------------------------------------------------------

    def upload_file(self, path: Path, *, to: str | None = None,
                    room: str | None = None) -> dict[str, Any]:
        import httpx

        params: dict[str, Any] = {}
        if to:
            params["to"] = to
        if room:
            params["room"] = room
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            try:
                r = self._client.post(
                    f"{self.base_url}{EXT_PREFIX}/files",
                    headers=self._headers(), files=files, params=params,
                    timeout=120.0,  # a 10MB upload over a tunnel needs room
                )
            except httpx.HTTPError as exc:
                raise HubError(f"upload failed: {exc}") from exc
        if r.status_code == 413:
            raise HubError(r.json().get("detail", "file too large"))
        if r.status_code >= 400:
            raise HubError(f"upload failed ({r.status_code}): {r.text}")
        return r.json()

    def list_files(self) -> list[dict[str, Any]]:
        return self._request("GET", f"{EXT_PREFIX}/files")["files"]

    def download_file(self, file_id: str, dest_dir: Path) -> tuple[Path, str]:
        """Stream a file to disk and return its path plus the server's checksum."""
        import httpx

        url = f"{self.base_url}{EXT_PREFIX}/files/{file_id}/content"
        try:
            with self._client.stream("GET", url, headers=self._headers(),
                                     timeout=120.0) as r:
                if r.status_code == 404:
                    raise HubError("no such file — it may already have been collected")
                if r.status_code >= 400:
                    r.read()
                    raise HubError(f"download failed ({r.status_code}): {r.text}")
                name = _filename_from(r.headers) or file_id
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / name
                digest = hashlib.sha256()
                with dest.open("wb") as out:
                    for chunk in r.iter_bytes():
                        digest.update(chunk)
                        out.write(chunk)
                return dest, digest.hexdigest()
        except httpx.HTTPError as exc:
            raise HubError(f"download failed: {exc}") from exc

    def ack_file(self, file_id: str) -> dict[str, Any]:
        return self._request("POST", f"{EXT_PREFIX}/files/{file_id}/ack")

    def delete_file(self, file_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"{EXT_PREFIX}/files/{file_id}")

    def agent_card(self) -> dict[str, Any]:
        """Fetch the hub's A2A agent card.

        NO CALLER IN THIS REPOSITORY, AND THAT IS NOT WHAT MAKES IT DEAD. A
        dead-code sweep removed this on the strength of the reference graph and
        it had to come back: `/.well-known/agent-card.json` is A2A's discovery
        endpoint, this class is collab's A2A client, and the client half of a
        protocol surface is not decided by whether anything in the same
        repository happens to call it. The server publishes the card; something
        has to be able to ask for it, and a third party writing against this
        client should find the method where the specification says it is.

        Read `server/card.py` for what the card carries.
        """
        return self._request("GET", "/.well-known/agent-card.json")


def _filename_from(headers: Any) -> str | None:
    disposition = headers.get("content-disposition", "")
    if match := re.search(r'filename="?([^";]+)"?', disposition):
        return Path(match.group(1)).name
    return None
