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
```

The suite covers token increments and duplicate snapshots, malformed or incomplete logs, turn attribution, pricing and currency conversion, unavailable prices, local HTTP restrictions, and MCP behavior. It also checks bounded task-name reads that discard previews and messages, display-ID collisions and local catalog behavior where corresponding tests are present. The tests must not query a real account, launch a model task, or open real task transcripts.

On Windows PowerShell, run `py -3 -m unittest discover -s tests -v`. The compatibility workflow runs the suite on native Windows and macOS with Python 3.10 and 3.12. Platform checks include real subprocess pipes, Unicode paths, cross-process file locks, isolated HTTP/MCP startup and authenticated shutdown, reuse of the saved address, and failure when the saved port is occupied. Installer tests use temporary scaffolds, never the user's actual marketplace. Review the workflow result for the exact published commit; automated tests do not establish live Codex login or manual hook trust on Windows.

Validate the compatibility manifest with the installed official `plugin-creator` validator. Keep the plugin folder name identical to its manifest name. The installation script runs the same validation during normal setup. Do not alter the validator or suppress its checks.

Check `.agents/plugins/marketplace.json` with Codex's public plugin reader. Its Git root source must resolve to this public repository and expose the released version, skill and five Hooks. `plugin/list` and `plugin/read` do not establish that a user has installed or trusted the plugin. Verify installation from the published Git source when a disposable Codex test environment is available; never modify a working user's hook trust for a packaging test. New-user instructions use normal `codex plugin marketplace add` and `codex plugin add` commands. The generated marketplace is independent of any existing personal install.

The native-entry tests execute the published Hook in a temporary cache path from an unrelated working directory, verify the registered synthetic log reaches the shared data directory, and check that host `PLUGIN_DATA` cannot split Hook and skill statistics. Browser-entry tests verify runtime copying, cache independence, update rollback, and the allowlist that excludes local data and links. No account credentials or model calls are needed.

For a UI check, serve only generated sample data or an empty temporary data directory. Confirm empty, partial, unavailable-price and currency states. If taking a publication screenshot, use visibly synthetic data and inspect the final image before adding it to the release.

Verify that each period's model groups add up to its six token counters and known monetary totals. Include unknown models, missing rates, same-turn model switches, late context, mixed tiers, event dates, and history beyond the display cap. Model slices must conserve every counter; multiple model rows may reference the same turn, so row turn counts are not additive. Unknown prices must not appear as zero.

For macOS demand activation, use a temporary data directory, a unique temporary launchd label, an unused loopback port, and a wrapper that disables account and title queries. Check that there is no worker before a request, a request starts it, it exits after a short test idle timeout, and another request starts a different worker at the same URL. Always unload only that exact temporary job afterward. Never modify hook trust to make integration checks pass. Windows must continue to use the ordinary service path without calling macOS APIs.

## Final publication review

- Refresh release source from the final implementation, then reapply public-documentation changes and portable configuration. Do not copy a generated installed `.mcp.json` back into the release.
- Run tests on that final release copy. Review the exact staged file list and diff, including ignored or untracked files that might be included by a later packaging step.
- Check for absolute home paths, credential-shaped strings, private keys, access tokens, live task identifiers and names, local title caches and aliases, runtime data and binary screenshots. A pattern scan supplements manual review; it does not prove absence of all sensitive data.
- Confirm that README descriptions match implemented behavior and that dated rate and FX references are accurate. Distinguish estimations, synthetic tests, and live integration verification.
- Inspect Git commit author metadata and repository visibility before publication. Do not publish personal verification notes or the installed plugin directory.

After any code, packaging or documentation changes, repeat the relevant checks on the new release snapshot. A prior audit does not cover later edits.
