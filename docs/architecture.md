# Architecture and data ownership

The core accepts normalized source records. It has no Chrome, X, React, or Draft.js dependency. Adapters translate provider data at ingestion; the CLI dispatches to them. Skills perform semantic curation through commands, never by changing provider facts.

## Authoritative data

- `archive.toml`: instance title and network policy.
- `raw/archive.json`: schema-versioned current records, collection entries, assets, taxonomy, labels, and synthesis.
- `raw/evidence/`: immutable provider observations, retained separately from the current normalized snapshot.
- `raw/sync-state.json`: authoritative unfinished scan baselines, pending collection decisions, and attempt rotation; included in source backups. `.archive/sync/` holds per-run reports and resumable capture checkpoints.
- `media/`: archived bytes. Each asset has a URL identity and verified byte digest. These files require backups.

Entries identify collection decisions. Records identify source content and can be members of several entries. Captured thread/context records are not additional user likes. New adapter entry IDs are namespaced; migrated IDs remain stable so old synthesis citations survive.

A source observation may be incomplete. Never replace complete content with a known truncated preview, or treat absence on a later page as proof of deletion. Older observations must not roll back newer known content. Preserve unexplained differences and gaps rather than inventing facts.

## Persistence

Writers use a single-host advisory file lock. A source update is one temporary file plus atomic replace; source parsing and its expected hash use the same bytes. External edits are checked before committing. Advisory locking does not serialize editors or other machines; cloud-folder multi-host writing is unsupported.

Capture observations are saved before later navigation. Checkpoints carry file hashes. Interrupted scans can replay the already observed scroll prefix to reconstruct X's virtualized DOM, then continue under page/time/scroll budgets. Observed coverage is not a guarantee of platform completeness.

## Publication

Each build writes a fresh generation containing Markdown, SQLite, and a source fingerprint. It verifies assets and input references before switching the `view` symlink once. Earlier generations remain available. This isolates reading from failed builds; it is not a general multi-machine transaction or filesystem backup system.

## Extension

Add another source adapter producing normalized blocks and evidence. Core blocks are markdown, image, video, quote, and divider. Media and textual formats coexist in a record. Do not add speculative plugin registries or parallel models without a concrete source requirement.

The synthetic demo exercises a non-X source without browser access. Source adapters and browser transport can evolve independently of local knowledge bases.

## Incremental collection discovery

Discovery verifies the logged-in account, collection route, and explicit viewer action flags. Reposts use the dedicated `/profile/reposts` route; wrapper event IDs and actor identity remain in evidence while the original post ID identifies the captured content. A known-ID overlap is a bounded incremental boundary, not proof of full platform history. Partial scans retain their original baseline and advance replay depth across runs; newly ingested prefixes cannot prematurely become the stop boundary. Capture attempt counts rotate historical work so one unresolved item does not starve the rest. Runtime reports distinguish source gaps from pending semantic curation. Scheduling is a host concern, not an internal daemon.
