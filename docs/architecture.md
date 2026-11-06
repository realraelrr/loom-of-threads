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

## Reference scope

Capture and ingestion share one reference-scope calculation: author-thread and required parent-context nodes are roots; their explicit quotes, repost targets, and used article TWEET entities are direct references. References inside those referenced records remain links. Reports and entries preserve `reference_depth: 1` and `excluded_reference_edges`. This boundary avoids unbounded traversal into unrelated topics. Missing direct references remain explicit gaps; out-of-scope edges do not imply failed capture. Existing evidence and previously stored deeper records are retained.


## Evidence reuse and retries

A stale selected observation cannot replace the entry's latest membership, context, coverage or scope. Associated records may still merge by their own observation timestamps and identity/relationship checks. `ignored_stale` refers to the selected entry decision; `merged_records` reports useful record updates. Imported evidence does not automatically clear an older entry's gaps.

Sync records retry reasons and a bounded cooldown in existing attempt state. New discoveries precede historical retries. Deferred work stays pending with a next retry time; an explicit resume may bypass cooldown after inspection. Extraction issues can require reviewing stored observations before re-ingest; stored extracted JSON is not a replayable DOM. Expired cooldown is permission to attempt, not proof of recoverability or a reset of cumulative scroll budgets.

## Reviewed removal

Cleanup uses preview and apply over a snapshot-bound plan. The host obtains confirmation of the exact entries, reasons, resource impacts and explicit note/synthesis replacements. The approval token binds plan contents, not human identity. Apply re-derives impact under the archive writer lock and rejects changed source or plan contents before mutation.

Removal is limited to selected entries and their dependency closure that no retained record or entry needs. Unrelated orphan records are not opportunistically collected. Shared quote/reply/member/context records and shared media metadata survive. Physical media, immutable evidence, old published generations and an operation snapshot remain; this is active-archive removal, not secure erasure. A subsequent build publishes the new view.

Optional `deleted_entries` in the source snapshot records entry/source identities, reason and time. X discovery and ingest respect these decisions to avoid silently resurrecting deleted collection entries, while retained entries may still need the same source as context. Deletion decisions do not change the external account. Restoring an entry or purging all historical copies is a separate explicitly scoped operation.
