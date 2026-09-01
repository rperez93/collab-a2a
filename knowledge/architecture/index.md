# The two processes

* [The hub](hub.md) - One FastAPI application serving the A2A surface and collab's extension, backed by an append-only SQLite log.
* [The client daemon](client-daemon.md) - The only thing that talks to the hub continuously, and the reason an agent never has to know a reconnect happened.

# The wire

* [The event feed](event-feed.md) - One long-lived SSE response per participant, framed so a reconnect resumes without a gap.
* [The envelope](envelope.md) - The one JSON object every event is, and the eight kinds it comes in.

# On disk

* [Sessions](session.md) - What a session is, what resuming one keeps, and what it retires.
* [The state directory](state-directory.md) - Every file collab writes, where, and with what permissions.
* [The daemon lock](daemon-lock.md) - Which process is this session's listener, answered by the kernel rather than by a pid file.
* [State ownership](state-ownership.md) - Which state directory belongs to which agent when two of them share a checkout.
