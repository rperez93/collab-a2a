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
        self._verified_url: str | None = None
        import httpx

        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- plumbing -------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None, *, authenticated: bool = True) -> dict[str, str]:
        from ..compatibility import request_headers
        if authenticated and self.token:
            self.check_host()
        return request_headers(self.token if authenticated else None, extra)

    def check_host(self, *, force: bool = False) -> None:
        if not force and self._verified_url == self.base_url:
            return
        from ..compatibility import incompatibility
        try:
            peer = self._bounded_request('GET', f'{EXT_PREFIX}/compatibility',
                maximum=64 * 1024, label='compatibility', authenticated=False)
        except HubError as exc:
            raise HubError('cannot establish host compatibility; Collab 2.0.0 or newer stable 2.x is required on both sides — upgrade/restart the host or check its address. No invite or bearer was submitted') from exc
        if reason := incompatibility(peer, role='host'):
            raise HubError(reason + '; no invite or bearer was submitted')
        self._verified_url = self.base_url

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        import httpx

        try:
            r = self._client.request(
                method, f"{self.base_url}{path}", headers=self._headers(kw.pop("headers", None)), follow_redirects=False, **kw
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
        if r.status_code >= 300:
            detail = ""
            try:
                detail = r.json().get("detail") or r.text
            except ValueError:
                detail = r.text
            raise HubError(f"{method} {path} failed ({r.status_code}): {detail}")
        return r.json()

    # --- session --------------------------------------------------------------

    def join(self, invite: str, name: str, hello: dict[str, Any]) -> dict[str, Any]:
        from ..compatibility import advertisement
        self.check_host(force=True)
        return self._request(
            "POST", f"{EXT_PREFIX}/join",
            json={"invite": invite, "name": name, "hello": hello, **advertisement()},
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

    def replay_page(self, after: int = 0, *, through: int | None = None,
                    limit: int = 200) -> dict[str, Any]:
        """Read one viewer-filtered page without guessing from global seqs."""
        params = {"after": after, "limit": limit}
        if through is not None:
            params["through"] = through
        # Eight MiB fits 200 complete 8,000-character messages even when
        # every character occupies four bytes. Compatibility needs only 64 KiB.
        return self._bounded_get(f"{EXT_PREFIX}/replay", params=params,
                                 maximum=8 * 1024 * 1024, label="replay")

    def _bounded_get(self, path: str, *, params: dict[str, Any],
                     maximum: int, label: str) -> Any:
        return self._bounded_request("GET", path, params=params, maximum=maximum, label=label)

    def _bounded_request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                         body: dict[str, Any] | None = None, maximum: int, label: str,
                         authenticated: bool = True) -> Any:
        # Inspect bytes as they arrive. A buffered request has already spent
        # the memory before it can reject a flooding or malformed peer.
        import httpx
        import time

        deadline = time.monotonic() + 15
        try:
            with self._client.stream(
                method, f"{self.base_url}{path}", params=params, json=body,
                headers=self._headers({"Accept-Encoding": "identity"}, authenticated=authenticated), timeout=5.0,
                follow_redirects=False,
            ) as response:
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise HubError(f"hub sent compressed {label} despite identity encoding")
                data = bytearray()
                # No chunk_size: waiting to fill a large chunk lets a peer
                # dribble bytes forever without returning to the clock check.
                for chunk in response.iter_raw():
                    if time.monotonic() > deadline:
                        raise HubError(f"{label} page exceeded its 15-second deadline")
                    if len(data) + len(chunk) > maximum:
                        raise HubError(f"{label} page exceeded the {maximum // 1024} KiB response limit")
                    data.extend(chunk)
                import json
                try:
                    parsed = json.loads(data)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise HubError(f"hub returned an invalid {label} page ({response.status_code})") from exc
                if response.status_code >= 300:
                    detail = str(parsed.get('detail', 'request refused'))[:1000] if isinstance(parsed, dict) else 'request refused'
                    raise HubError(f"{label} failed ({response.status_code}): {detail}")
                return parsed
        except httpx.HTTPError as exc:
            raise HubError(f"cannot read {label} from the hub: {exc}") from exc

    def publish_skill(self, publication: dict[str, Any]) -> dict[str, Any]:
        from ..skill_sharing import validate_publication
        return self._bounded_request("POST", f"{EXT_PREFIX}/shared-skills",
                                     body=validate_publication(publication), maximum=64 * 1024,
                                     label='skill publication')

    def shared_skills(self, *, after: str = '', limit: int = 100) -> dict[str, Any]:
        return self._bounded_get(f"{EXT_PREFIX}/shared-skills",
                                 params={'after': after, 'limit': limit},
                                 maximum=1024 * 1024, label='shared skill inventory')

    def shared_skill(self, skill_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"sk_[0-9a-f]{20}", skill_id):
            raise ValueError("invalid shared skill ID")
        from ..skill_sharing import validate_publication
        data = self._bounded_get(f"{EXT_PREFIX}/shared-skills/{skill_id}", params={},
                                 maximum=512 * 1024, label='shared skill')
        validate_publication(data.get('skill'))
        return data

    def withdraw_skill(self, skill_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"sk_[0-9a-f]{20}", skill_id):
            raise ValueError("invalid shared skill ID")
        return self._bounded_request("DELETE", f"{EXT_PREFIX}/shared-skills/{skill_id}",
                                     maximum=64 * 1024, label='skill withdrawal')

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
