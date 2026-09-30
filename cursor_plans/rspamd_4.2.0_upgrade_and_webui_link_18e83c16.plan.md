---
name: Rspamd 4.2.0 upgrade and WebUI link
overview: Bump Rspamd from 4.1.3 to 4.2.0 across all pinned locations and add a dashboard nav link that opens the Rspamd WebUI in a new tab, gated by a new optional env var and requiring a small compose/Caddy change to actually reach it from a browser.
todos:
  - id: bump-pins
    content: Bump rspamd/rspamd 4.1.3 -> 4.2.0 in compose files, unraid xml, README, slice7 doc
    status: pending
  - id: deploy-bump
    content: Pull + recreate spamfilter-rspamd on VPS, verify version/health/scan/learn
    status: pending
  - id: watch-scores
    content: Observe scores for a day in shadow mode before any threshold/mode changes
    status: pending
  - id: webui-port
    content: Publish 127.0.0.1:11334 in deploy/bytelord-compose.yaml so Caddy can reach the controller (no SSH tunnel)
    status: pending
  - id: webui-caddy-snippet
    content: Hand Rich the confirmed Caddy edit (redir + handle_path /rspamd/* + handle catch-all) for /etc/caddy/Caddyfile; he applies and reloads
    status: pending
  - id: webui-env-var
    content: Add RSPAMD_WEBUI_URL env var read in filter/dashboard.py
    status: pending
  - id: webui-nav-link
    content: Add gated nav link (new tab) in dashboard base template when RSPAMD_WEBUI_URL is set
    status: pending
  - id: webui-tests
    content: Add filter/test_dashboard.py coverage for link presence/absence
    status: pending
  - id: docs-update
    content: Document RSPAMD_WEBUI_URL and the version bump in README.md and IMPLEMENTATION_STATUS.md
    status: pending
isProject: false
---


# Rspamd 4.1.3 -> 4.2.0 upgrade + dashboard WebUI link

## Part A - What 4.2.0 buys us over 4.1.3

Researched live via GitHub release notes (4.1.4, 4.1.5, 4.2.0) and the upstream ChangeLog. Four releases between here and there: 4.1.4 (29 Jul 2026), 4.1.5 (15 Aug 2026), 4.2.0 (18 Sep 2026).

**Security-relevant fixes (the real reason to move), most severe first:**
- **4.1.4 - Controller auth bypass (critical):** a malformed password hash was previously treated as an accepted password on the controller (`/checkv2` auth, WebUI login, `/learnspam`/`/learnham`). This is the same controller our filter and dashboard authenticate against with `RSPAMD_PASSWORD`. Worth having fixed regardless of whether the hash is ever malformed today.
- **4.1.5 - Arbitrary file read via protocol (critical-ish):** any TCP client could previously get rspamd to parse arbitrary local files/paths/shm through message-source directives; 4.1.5 gates this behind `allow_file_and_shm_inputs` (default true today, default flips to false next major). Not directly reachable through our HTTP-only `/checkv2` POST-body usage, but it is a real hardening fix on a worker we run.
- **4.1.5 - Controller auth-failure rate limiting:** `max_auth_failures` throttles repeated bad-password attempts on the controller - directly relevant since the controller is what our dashboard's Rspamd WebUI link would expose to a browser.
- **4.2.0 - Multiple DoS/memory-safety hardening fixes:** bounded UCL parsing on untrusted controller/proxy JSON/msgpack bodies, bounded PDF/archive/HTML/CSS parsing (deep-nesting DoS, quadratic backtracking), bounded zstd decompression, rdns hardening against malformed replies. These matter because we feed rspamd arbitrary attacker-controlled email bytes every scan.

**Detection-quality improvements (the "better spam detection" ask):**
- **4.2.0 - Attachment content extraction:** SVG (`SVG_SCRIPT`, `SVG_FOREIGN_OBJECT`, `SVG_DATA_URI`, smuggling indicators), XLSX/PPTX (`OOXML_MACROS`, `OOXML_OLE_OBJECT`, `OOXML_REMOTE_TEMPLATE`, `OOXML_EXTERNAL_DATA`), DOCX bounded content extraction. New symbols that can catch malicious-attachment phishing that currently scores as clean.
- **4.2.0 - DKIM alignment reworked:** `R_DKIM_ALIGNED` is now a distinct, separately-scored symbol from `R_DKIM_ALLOW`. Directly relevant to the M365-rewrite false-positive investigation already tracked in `IMPLEMENTATION_STATUS.md` "What's next" (item 3, `BROKEN_HEADERS`/DMARC/DKIM after rewrite) - this may shift or fix some of those false positives, for better or worse; needs a before/after `explain_score.py` comparison.
- **4.2.0 - Public suffix list overhaul:** TLD/domain matching (used by RBL URL maps, `top` filters, free-text URL discovery) now goes through real PSL semantics instead of the old flat TLD table. More correct multi-label public suffixes (e.g. `co.uk`, `com.au`).
- **4.1.5 - Fuzzy improvements:** shares SPF/DKIM/DMARC/PTR/TLS sender facts with fuzzy storages over encrypted rules - not used today (no fuzzy storage configured beyond local), low relevance until fuzzy is expanded.
- **4.2.0 - HTML/CSS hardening also fixes false negatives:** hidden-text detection (`R_WHITE_ON_WHITE` and friends) no longer misses text hidden via transparent tags, relative font sizing, or layout padding - a real phishing-obfuscation technique that previously slipped past.

**Nothing found that breaks this project's usage.** No JSON shape change to `/checkv2`, `/learnspam`, `/learnham`. No change to Bayes Redis schema for a single, unsharded Redis (`new_schema = true`, `backend = "redis"`) - the 4.0.0 Ring Hash migration only affects sharded per-user Bayes across multiple Redis servers, which this deployment does not use. `autolearn = false` config keys are untouched. `local.d/rbl.conf`, `local.d/options.inc` (DNS via Unbound), `local.d/classifier-bayes.conf` all use config keys still valid in 4.2.0.

**Score drift is possible, not a breakage:** new symbols (OOXML/SVG, `R_DKIM_ALIGNED`) and the PSL rework can move messages across the threshold in either direction. Accounts stay in `shadow` mode already, so nothing auto-moves mail during the observation window regardless.

## Part B - Upgrade steps

1. Bump the pin `rspamd/rspamd:4.1.3` -> `rspamd/rspamd:4.2.0` in every location it is written today:
   - [deploy/bytelord-compose.yaml](deploy/bytelord-compose.yaml) line 50 (source of truth)
   - [docker-compose.yml](docker-compose.yml) line 45 (generic/dev compose)
   - [unraid/spamfilter-rspamd.xml](unraid/spamfilter-rspamd.xml) line 4
   - [README.md](README.md) line 17 (architecture table)
   - [design-arch/slice7_ops_secrets_supply_chain.md](design-arch/slice7_ops_secrets_supply_chain.md) line 86 (image/pin table)
   - `IMPLEMENTATION_STATUS.md` if it references the pin (add a dated note under "What we accomplished" once this ships)
2. On the VPS: `docker compose -f /opt/bytelord/compose/imap-spamfilter/compose.yaml pull spamfilter-rspamd`, then `up -d --no-deps spamfilter-rspamd`. Leave `spamfilter-redis` alone - Bayes/fuzzy state lives there, untouched by this bump.
3. Verify: `docker exec spamfilter-rspamd rspamd --version` reports `4.2.0`; container healthy; one live scan (via `explain_score.py` or the dashboard) still returns a score and symbol list; a Train-* drain still learns successfully; Redis still holds the existing Bayes keys.
4. Watch scores for a day in shadow before touching thresholds or modes - some symbols are new (OOXML/SVG/`R_DKIM_ALIGNED`) and could shift totals.
5. Optional follow-up (separate task, not required for the bump itself): re-run the M365 DKIM/DMARC false-positive investigation from `IMPLEMENTATION_STATUS.md` "What's next" item 3 now that `R_DKIM_ALIGNED` exists, since it may explain or fix some of the currently-weighted symbols.

No Python code changes are expected in `filter/filter.py` or `filter/dashboard.py` for the version bump itself - the HTTP contract (`/checkv2`, `/learnspam`, `/learnham`, JSON `score`/`action`/symbols shape) is unchanged across 4.1.3 to 4.2.0.

## Part C - Rspamd WebUI link in the dashboard

**What exists today:** Rspamd ships a built-in web UI (login, Bayes stats, symbol history/search, Selectors tab, Errors tab) served by the same controller worker as `/checkv2`'s sibling endpoints, on port 11334. Confirmed in [rspamd/local.d/worker-controller.inc.template](rspamd/local.d/worker-controller.inc.template): `bind_socket = "*:11334"`, guarded by `password`/`enable_password` (same `RSPAMD_PASSWORD` secret already used by the filter and dashboard), with `secure_ip = "127.0.0.1"` exempting only true loopback. No `ports:` mapping exists today in [deploy/bytelord-compose.yaml](deploy/bytelord-compose.yaml), so 11334 is reachable only from other containers on `spamnet` right now (the dashboard already calls it server-side for `_rspamd_stats()` and bulk learn); nothing outside the Docker network can reach it yet.

**Real live Caddy block today** (read directly from `/etc/caddy/Caddyfile` on the VPS, NetBird-private like every other admin tool):
```caddyfile
spam.bytelord.net {
	import access_log
	bind {$BYTELORD_PRIVATE_IP}
	tls internal
	reverse_proxy 127.0.0.1:8099
}
```

**Decision (confirmed with Rich): path-based, same block** - `spam.bytelord.net/rspamd/` rather than a separate subdomain. Verified this is safe before committing to it: inspected the actual shipped WebUI (`docker exec spamfilter-rspamd`, `/usr/share/rspamd/www/index.html` and `js/app/*.js`) - every asset reference is relative (`./css/...`, `./js/...`, `./img/...`), not root-absolute, so mounting under a subpath will not break asset loading as long as the browser sees a trailing-slash base URL for the page. Caddy's `handle_path` (auto-strips the matched prefix, cleaner than manual `uri strip_prefix`) plus a bare-`/rspamd` redirect to `/rspamd/` covers that:
```caddyfile
spam.bytelord.net {
	import access_log
	bind {$BYTELORD_PRIVATE_IP}
	tls internal

	redir /rspamd /rspamd/ permanent

	handle_path /rspamd/* {
		reverse_proxy 127.0.0.1:11334
	}

	handle {
		reverse_proxy 127.0.0.1:8099
	}
}
```
This is additive to the existing block - the current bare `reverse_proxy 127.0.0.1:8099` line becomes the `handle {}` catch-all, unchanged in behavior for every path the dashboard already serves. Rich applies this to `/etc/caddy/Caddyfile` and reloads Caddy; not done by the agent (Caddy is operator-owned per existing project convention).

Also confirmed: even proxied through Caddy on the host, the container sees the connecting peer as the Docker bridge gateway, not literal `127.0.0.1` - so `secure_ip = "127.0.0.1"` in `worker-controller.inc` is **not** bypassed by this routing. The WebUI still shows its password screen; nothing about this setup weakens the existing auth gate.

**Reachability gap to close (compose side):**
Add a loopback port publish to the `spamfilter-rspamd` service in [deploy/bytelord-compose.yaml](deploy/bytelord-compose.yaml), matching the existing dashboard pattern (`127.0.0.1:8099:8099`):
```yaml
ports:
  - "127.0.0.1:11334:11334"
```
Caddy runs on the host and already reaches `127.0.0.1:8099` the same way; no SSH tunnel needed for either the dashboard or the WebUI.

**Login / auto-fill (confirmed with Rich, no code patch):** inspected the shipped auth JS (`js/app/rspamd.js`, `js/app/common.js`) directly - the password lives only in `sessionStorage.setItem("Password", ...)`, written by the login-form submit handler. `location.hash`/`location.search` are read elsewhere only for tab-anchor navigation (`#status`, `#symbols`), never for auth. Stock Rspamd WebUI has **no** URL-based auto-login hook. Building one would mean volume-mounting a patched copy of `rspamd.js` over the stock file and re-diffing it against upstream on every Rspamd version bump - not worth it here. Decision: no patch. The link opens the login screen; the browser's saved-password autofill fills the single password field after the first manual login, one click to submit thereafter.

**Dashboard change:**
1. New optional env var `RSPAMD_WEBUI_URL` (e.g. `https://spam.bytelord.net/rspamd/`), read in [filter/dashboard.py](filter/dashboard.py) near the existing `RSPAMD_CONTROLLER_URL` (~line 69). Left unset by default so environments without the Caddy path configured yet don't show a dead link.
2. In the nav template (~[filter/dashboard.py:1263-1279](filter/dashboard.py)), add a link after "User lists" only when `RSPAMD_WEBUI_URL` is non-empty:
   ```html
   <a href="{{ rspamd_webui_url }}" target="_blank" rel="noopener noreferrer">Rspamd &#8599;</a>
   ```
   Rendered from the base template context alongside `active`/`is_admin`, visible to all logged-in dashboard users (not admin-gated - it's read-only from this dashboard's point of view; rspamd's own login still gates real access).
3. Add `RSPAMD_WEBUI_URL` to [deploy/bytelord-compose.yaml](deploy/bytelord-compose.yaml) `environment:` block for the `spamfilter` service (set to `https://spam.bytelord.net/rspamd/`) and to the `README.md` dashboard/env-var section.
4. Test: extend `filter/test_dashboard.py` to assert the link is absent when the env var is unset and present (correct `href`, `target="_blank"`) when set.

No changes needed to `worker-controller.inc` / `worker-controller.inc.template` - the password gate stays exactly as-is; this is purely "give the dashboard a link out," not "change WebUI auth."

## Sequencing

Part B (version bump) and Part C (WebUI link) are independent and can ship in either order or together. Part C's compose/env changes touch the same files as Part B, so doing them in one pass avoids two round-trips through the compose file.
