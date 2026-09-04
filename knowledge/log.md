# Bundle Update Log

## 2026-09-04
* **Update**: Corrected [the envelope](architecture/envelope.md) for commit `a310d77`, which changed the file it cites. The sentence saying `ALL_KINDS` named six of the seven kinds is now dated to the pin rather than left to read as the present; a section says which kinds a client may send (one — `chat`), that both the message route and A2A `SendMessage` refuse the rest by name, and that `ts` and `toId` are the hub's on both. The pin itself stays at `23db6d0`, because the bundle is pinned to one revision and most of what it cites has moved since; the bundle-wide re-pin is a separate act under the rule in [how to read this bundle](how-to-read-this-bundle.md), and the new claims carry no `verified` stamp until it happens.

## 2026-09-02
* **Update**: Re-pinned the bundle from `f9abc76` to `23db6d0` under the rule now written into [how to read this bundle](how-to-read-this-bundle.md): 14 concepts whose cited files were byte-identical were re-pinned outright and keep their original `verified` stamps; 8 citing a file that had moved were re-read against the new tree and stamped afresh.
* **Update**: Corrected [the daemon lock](architecture/daemon-lock.md), which had become false. It described a filesystem that cannot lock and a platform with no locking primitive as one case with one answer; a later commit split them, and the platform is now refused rather than run with the exclusion silently absent.
* **Update**: Recorded the scope-change marker in [batches](collaboration/batches.md), now that summing the changes inside its window is what the code does, and said which reading of it is a true statement rather than an explanation.

## 2026-09-01
* **Update**: Moved every `resource` and `sources[].resource` off a relative path and onto a URL pinned to `f9abc76`, so a claim says which tree it was checked against and stays citable by someone without this checkout. Source entries whose evidence is a command that was run became scope descriptors, which is what they always were. Reasoning in [how to read this bundle](how-to-read-this-bundle.md).
* **Update**: Removed a colon from nine source titles. `Live run: the room list` parses under the bundle's own reader and is rejected outright by a real YAML engine, which is not a property an exchange format may have; `tests/test_okf_bundle.py` now holds the frontmatter to the subset every reader accepts.
* **Initialization**: Established the bundle against collab 1.20.2 at commit `f9abc76`, with the [architecture](architecture/), [collaboration](collaboration/) and [operating](operating/) groups.
* **Creation**: Wrote [how to read this bundle](how-to-read-this-bundle.md), stating which verifier actors were used and which credibility signals were deliberately left empty rather than guessed at.
* **Creation**: Wrote [a fact that was true when it was recorded](stale-facts.md), which sets the `stale_after` policy every other concept here follows.
