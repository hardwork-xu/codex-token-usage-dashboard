# Public release checks

These are reproducible maintainer checks, not evidence of any particular account's usage or a guarantee of compatibility with every Codex release. Use synthetic inputs and temporary data directories. Keep live account results and machine-generated configuration out of the repository.

## Before packaging

1. Review the exact file list. Include plugin source, tests, the local web UI, skill and hook definitions, manifests, the installer, documentation, and licenses. Exclude task logs, runtime JSON, local screenshots, shell history, account output, credentials, development folders, and installed plugin copies.
2. Keep the source `.mcp.json` as an empty `mcpServers` scaffold. Native marketplace installs use the bundled skill and `scripts/run.py`; they must not depend on generating MCP configuration. The optional legacy installer generates machine-specific MCP configuration only in its installed copy. Do not assume hook environment variables are also expanded in MCP arguments.
3. Inspect every outgoing file for personal absolute paths, user names, email addresses, session or task identifiers, copied conversations, raw preferences, and credentials. Synthetic test identifiers, public source URLs, and upstream license notices have different purposes; inspect their context before redacting them.
4. Keep both the project MIT license and the original PyYAML license. Recheck any newly added dependency before including it.

## Synthetic validation

Run from the release source folder:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
node tests/test_web_ui.cjs
```

The suite covers token increments and duplicate snapshots, malformed or incomplete logs, turn attribution, pricing and currency conversion, unavailable prices, local HTTP restrictions, and MCP behavior. It also checks bounded task-name reads that discard previews and messages, display-ID collisions and local catalog behavior where corresponding tests are present. The tests must not query a real account, launch a model task, or open real task transcripts.

On Windows PowerShell, run `py -3 -m unittest discover -s tests -v`. The compatibility workflow runs the suite on native Windows and macOS with Python 3.10, 3.12 and 3.14. Platform checks include real subprocess pipes, Unicode paths, cross-process file locks, isolated HTTP/MCP startup and authenticated shutdown, reuse of the saved address, failure when the saved port is occupied, and supported compressed-log decoding. Installer tests use temporary scaffolds, never the user's actual marketplace. Review the workflow result for the exact published commit; automated tests do not establish live Codex login or manual hook trust on Windows.

Validate the compatibility manifest with the installed official `plugin-creator` validator. Keep the plugin folder name identical to its manifest name. The installation script runs the same validation during normal setup. Do not alter the validator or suppress its checks.

Check `.agents/plugins/marketplace.json` with Codex's public plugin reader. Its Git root source must resolve to this public repository and expose the released version, skill and five Hooks. `plugin/list` and `plugin/read` do not establish that a user has installed or trusted the plugin. Verify installation from the published Git source when a disposable Codex test environment is available; never modify a working user's hook trust for a packaging test. New-user instructions use normal `codex plugin marketplace add` and `codex plugin add` commands. The generated marketplace is independent of any existing personal install.

The native-entry tests execute the published Hook in a temporary cache path from an unrelated working directory, verify the registered synthetic log reaches the shared data directory, and check that host `PLUGIN_DATA` cannot split Hook and skill statistics. Browser-entry tests verify runtime copying, cache independence, update rollback, and the allowlist that excludes local data and links. No account credentials or model calls are needed.

For a UI check, serve only generated sample data or an empty temporary data directory. Confirm empty, partial, unavailable-price and currency states. If taking a publication screenshot, use visibly synthetic data and inspect the final image before adding it to the release.

The dependency-free Node UI checks exercise a synthetic DOM, including stale quota, disconnects, refresh acknowledgements, API subtotals and currency persistence. They supplement real browser inspection; they do not prove layout or integration with a signed-in account. Confirm that a failed 100% cache never appears as current remaining quota, and that only a successful observation after restart restores the main percentage.

Verify that each period's model groups add up to its six token counters and known monetary totals. Include unknown models, missing rates, same-turn model switches, late context, mixed tiers, event dates, and history beyond the display cap. Model slices must conserve every counter; multiple model rows may reference the same turn, so row turn counts are not additive. Unknown prices must not appear as zero.

Verify the independent `creditEstimate` alongside API and custom money values, at turn, period and model scope. Sum Credits by model, preserve Credit-specific unpriced Tokens, and confirm currency, USD/Credit settings and API context multipliers do not alter this conversion. Saved speed scenarios affect only the corresponding Credit estimate. Missing model rates stay unavailable. UI checks must cover Credits in API mode, partial subtotals, the history view and mode/currency switches without relabeling official account records.

For macOS demand activation, use a temporary data directory, a unique temporary launchd label, an unused loopback port, and a wrapper that disables account and title queries. Check that there is no worker before a request, a request starts it, it exits after a short test idle timeout, and another request starts a different worker at the same URL. Always unload only that exact temporary job afterward. Never modify hook trust to make integration checks pass. Windows must continue to use the ordinary service path without calling macOS APIs.

## Final publication review

- Refresh release source from the final implementation, then reapply public-documentation changes and portable configuration. Do not copy a generated installed `.mcp.json` back into the release.
- Run tests on that final release copy. Review the exact staged file list and diff, including ignored or untracked files that might be included by a later packaging step.
- Check for absolute home paths, credential-shaped strings, private keys, access tokens, live task identifiers and names, local title caches and aliases, runtime data and binary screenshots. A pattern scan supplements manual review; it does not prove absence of all sensitive data.
- Confirm that README descriptions match implemented behavior and that dated rate and FX references are accurate. Distinguish estimations, synthetic tests, and live integration verification.
- Inspect Git commit author metadata and repository visibility before publication. Do not publish personal verification notes or the installed plugin directory.

After any code, packaging or documentation changes, repeat the relevant checks on the new release snapshot. A prior audit does not cover later edits.

## Official credit observations

- Exercise decimal-preserving official balances independently of token pricing and currency mode. Missing/null fields stay unavailable; repeated model buckets are not summed. Startup, failure, stale and disconnected observations must not appear live.
- Check account-scoped balance history, positive/negative net changes, expiry/top-up ambiguity, and protection of `credit-balances.json`. Never commit an actual account snapshot, opaque account identifier, or real thread usage.
- Test public per-thread usage micro-unit conversion, mismatched IDs, absent `threadUsage`, read failure, unregistered IDs, concurrent requests and the refresh cooldown. Server estimates are not wallet debits; local estimates must not fill a missing server record.
- Verify the existing macOS owned runtime upgrades atomically when a newly added runtime module was absent in the previous version; a damaged current runtime must still fail safely.

## Historical coverage and timezone stability

- Page the complete registered history after task filtering, including records older than the overview cap. Check insertion between pages, equal timestamps, settings changes, failed requests, and stale responses. A cursor reaching the end cannot by itself prove all currently reported records were shown.
- Keep per-conversation source coverage separate from global coverage. Test a selected task whose source is caught up while other tasks remain unreadable, and a selected task whose source is readable but contradicts its saved archive. Source-conflict retention must remain incomplete and must not display a 100% progress bar as recovery success; paging all saved rows must not clear that warning.
- Exercise 0.16.0 paginated-source continuation with retained older turns and newly observed turns: verify ordinal/event identity, six-counter conservation, model/tier slices and duplicate elimination. Test verified overlap, explicitly eligible time-forward continuation without overlap, and conflicting or non-forward sources. `continuation_with_gap` must remain historically incomplete even after every saved row is paged; `overlap_verified` proves the accepted overlap only, not full account billing coverage.
- For an explicitly registered missing `.jsonl`, test only its exact same-name `.jsonl.zst` sibling, using synthetic compressed fixtures. Verify supported Python 3.14, installed `zstandard`, or installed `zstd` decoding paths, no automatic dependency installation, private temporary extraction and cleanup, identity rejection, corruption and unavailable decoder failures. A verified compressed source remains distinguishable from unavailable-source archive fallback; real source files and recovered private statistics must stay out of Git.
- Restart descendant discovery during active and archived pagination. Preserve checkpoints and registry merges; failed metadata-only root verification must never expand into unfiltered discovery.
- Oversized non-accounting records require full bounded JSON envelope validation before preserving counter/model context. Oversized identity or accounting metadata must remain rejected or uncertain. Compaction continuity needs an unchanged, attributable counter; model/speed changes after a confirmed zero increment must not inherit an obsolete boundary.
- Bind date-specific speed declarations to UTC windows and verify identical all-history Credits across display timezones, including exact start/end boundaries and same-question mixed recorded speeds. Missing legacy timezone metadata must stay explicit. Keep original event-day summaries and six-counter conservation intact.

## Durable numeric observations

- Test validated response receipts and ordinary snapshots together, including compaction, duplicate/conflicting response identities, partial writes, model/speed switches, restart and migration of existing archives. Normal matched responses count once; only independently supported missing usage can be added. Replaying the same source must preserve exact counters and per-segment attribution.
- Verify each persisted event retains its timestamp, six token counters, observed model and recorded service tier without messages, credentials or computed billing amounts. Unknown fields stay unknown.
- In an isolated fixture, remove source logs and restart the worker; preserved counters, model subtotals, API estimates and Credits estimates must stay equal while source availability becomes historical/incomplete. Reintroducing the same log must not duplicate usage. Never move a user's real source log for this test.
- Test incomplete restart prefixes, growing partial scans, truncation, corrupted storage, failed writes, concurrent writers and durable identity invalidation. A failed archive write must not be reported as saved. Verify native Windows and macOS paths.
- Keep `usage-archive/`, SQLite sidecars, recovery records and real source snapshots out of Git and plugin packages. Partial historical imports preserve their provenance and do not invent per-event timestamps or speeds.

## Collection without an open dashboard

- In an isolated data directory, trigger normal verified Hook registration and perform no dashboard, summary or HTTP state requests. Verify the detached collector starts, a source larger than one bounded read eventually reaches the archive, and the preserved counters exactly match the synthetic source. Check native Windows and macOS wake-up paths without changing Hook trust.
- Append synthetic usage while collection runs and after the completion event. Verify late tail writes are collected before idle exit, a recent running task prevents premature exit, an old stale running flag does not keep a worker alive indefinitely, and a later Hook wakes an exited worker. Failed wake-up must preserve Hook best-effort behavior and not inject or continue a model turn.
- In one turn, switch model and Standard/Fast, include unknown metadata, and verify each observed event retains its original time, six counters, model and tier after restart. Repeated collection must not duplicate usage or apply a final model/speed to earlier increments. Reuse reader offsets; repeated Hook launches must not strand large histories at the same prefix.
- Verify missing/corrupt registry, source disappearance, identity rejection, unwritable archive and unwritable status paths have explicit non-success outcomes. Read-only startup state and a fresh heartbeat must not masquerade as successful persistence or complete coverage. Test collection concurrently with page reads, Hook registration, settings changes and idle shutdown.
- Keep `monitoring.collection` distinct from archive retention and source coverage. UI checks must preserve pending/unreadable/write-error counts, show only last-known state when disconnected or heartbeat-expired, and avoid claiming all tasks or zero gaps. Old service payloads lacking collector metadata remain usable without asserting background collection is active.
- Keep collector status, synthetic integration databases and real verification logs outside source releases. Confirm collection makes no model requests and only processes verified registered sources; official quota polling must retain its separate activity and cooldown rules.

## Acknowledged historical gaps

- Compare all token counters, per-model/per-speed slices, API/Credits estimates and period totals before and after acknowledgement; none may change merely from archiving a gap. Missing costs remain null/dash, never an invented zero or manual amount. Original status and pricing/period partial evidence remain available alongside additive active status.
- Recheck every exact candidate at POST time: both the registered original and its same-name compressed source must be absent. Reject arbitrary/unregistered IDs, permission or parsing failures, identity rejection and sources that reappear before acknowledgement. Protect the endpoint with the same local origin and CSRF rules as settings; repeated identical acknowledgement must be safe.
- UI checks must distinguish acknowledged history from new unreadable/pending/write-failed tasks, preserve known amounts, hide neither unknown costs nor historical provenance, and show unconfirmed state on disconnection. A metadata-only `archive.source: none` marker cannot imply saved numeric data. A new conflict must remain visible even if an obsolete historical flag is present.
- Verify the collapsed historical disclosure retains its open state during polling, provides per-task selection, and does not declare full historical or account coverage. Test acknowledgement success readback, failed or stale responses, double clicks, and candidate changes during a pending request without expanding the submitted scope. Keep real acknowledgement records and task identifiers outside source releases.
