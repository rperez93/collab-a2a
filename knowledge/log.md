# Bundle Update Log

## 2026-09-01
* **Update**: Moved every `resource` and `sources[].resource` off a relative path and onto a URL pinned to `f9abc76`, so a claim says which tree it was checked against and stays citable by someone without this checkout. Source entries whose evidence is a command that was run became scope descriptors, which is what they always were. Reasoning in [how to read this bundle](how-to-read-this-bundle.md).
* **Update**: Removed a colon from nine source titles. `Live run: the room list` parses under the bundle's own reader and is rejected outright by a real YAML engine, which is not a property an exchange format may have; `tests/test_okf_bundle.py` now holds the frontmatter to the subset every reader accepts.
* **Initialization**: Established the bundle against collab 1.20.2 at commit `f9abc76`, with the [architecture](architecture/), [collaboration](collaboration/) and [operating](operating/) groups.
* **Creation**: Wrote [how to read this bundle](how-to-read-this-bundle.md), stating which verifier actors were used and which credibility signals were deliberately left empty rather than guessed at.
* **Creation**: Wrote [a fact that was true when it was recorded](stale-facts.md), which sets the `stale_after` policy every other concept here follows.
