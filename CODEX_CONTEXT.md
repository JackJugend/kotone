# Kotone — Codex handoff context

> **Purpose:** This file is the current handoff for continued development of the Kotone Discord bot. Read it before changing code, then inspect the actual repository and treat the repository as authoritative for filenames, exact SQL columns, configuration defaults, and command signatures.
>
> **State represented here:** the repository folder is still named `kotone 5.1`, but the inspected source is the SQLite v10/change-history generation commonly referred to as Kotone 5.2. This document was reconciled against the complete local repository on 2026-08-17 after the safety-hardening work described below. The resulting changes have passed the full offline suite but have **not** been deployed to Railway yet. The production Railway Volume was not opened or modified during this work.

## 1. Product goal

Kotone is a Python Discord bot centered on Album of the Year (AOTY) user profiles, ratings, reviews, likes, Track Ratings, favorites, album/release metadata, monitoring, and notifications. It is deployed as one long-running Railway service with a persistent Railway Volume containing SQLite.

The bot should behave as a durable, local archive and cache for the small set of AOTY users listed in `config.json`, while remaining useful during AOTY outages. It must minimize dependence on AOTY HTML stability, rate limits, Cloudflare behavior, and Railway restarts. It should be easy to extend without duplicating networking, persistence, fallback, or presentation logic.

The user is not deeply technical. Prefer maintainable code, clear names, focused comments explaining non-obvious policy, safe automatic migrations, actionable logs, and commands such as `/dbstats` that expose operational state without requiring shell access.

## 2. Read-first workflow for Codex

Before making any change:

1. Read this file and inspect the complete repository.
2. Identify the real entry point, current module boundaries, database schema version, migrations, config loader, tests, and deployment files.
3. Run the existing offline test suite and compile all Python files to establish a baseline.
4. Reconcile this handoff with the code. Do not invent a column, table, command option, or config key solely because it is mentioned conceptually here.
5. Preserve all invariants in section 15. If a requested change conflicts with one, explain the conflict before implementing it.
6. Make the smallest coherent change, add regression tests, run the full relevant suite, and inspect the final diff for accidental UX/schema/config changes.

Do not add README files. This `CODEX_CONTEXT.md` is the project handoff, not a replacement request for a README.

## 3. Intended architecture

The refactored project uses explicit layers. Exact filenames must be verified, but the known core modules include:

```text
Discord commands, views, buttons, embeds
                |
                v
            services.py
          /             \
         v               v
   database.py         aoty.py
                           |
                           v
                     http_client.py

Separate long-running concerns:
  monitor.py      -> quick/full rating checks and Discord notifications
  background.py   -> slow archive bootstrap, maintenance, detail enrichment
  health server   -> Railway /health and /live; never depends on live AOTY
```

Responsibilities:

- **Discord/UI layer:** parse interactions, defer/respond correctly, render existing embeds/views, and call services. It should not contain raw SQL, ad-hoc HTTP retries, or duplicated AOTY parsing.
- **Service layer:** decide SQLite-first/live-refresh/stale-fallback behavior and return stable domain data to commands.
- **AOTY parser:** parse HTML into domain values and explicitly distinguish a complete/authoritative detail response from a partial or invalid response.
- **HTTP client:** the only normal path for AOTY requests. Owns scheduling, throttling, cooldowns, retries, caching, and priorities.
- **Database:** owns schema/migrations, transactions, persistence eligibility, backups/recovery, current-state upserts, and append-only history.
- **Monitor:** owns authoritative rating-score change detection and notifications.
- **Background worker:** fills and maintains the archive without starving interactive requests or stealing monitor notifications.

Avoid circular imports and global state spread across modules. Prefer small typed/domain-oriented interfaces. Comments should explain policy and failure modes, not restate obvious code.

## 4. AOTY HTTP and scraping constraints

AOTY is an HTML source, not a stable API. Assume layouts can change, pages can be incomplete, Cloudflare/error HTML can return unexpectedly, and responses can be 429, 5xx, time out, or temporarily disagree.

All AOTY traffic must use the central client and preserve these policies:

- At most **one in-flight request to AOTY** at a time.
- Normal global minimum spacing was designed around **1.25 seconds**; background archive requests have an additional minimum around **2 seconds**, with roughly **4 seconds between formats**. Verify current config defaults rather than hard-coding these values elsewhere.
- Priorities: interactive user commands first, normal fetching next, monitor/cache work below that, archive/enrichment lowest.
- Respect `Retry-After`.
- Use exponential backoff with a global cooldown after 429.
- Use a circuit breaker after repeated upstream failures.
- Cache fetched pages in memory and allow stale cache when appropriate.
- After 429, AOTY outage, or a serious archive error, background work should back off for roughly **5 minutes** rather than immediately continuing.
- Do not retry every layer independently. Retry/cooldown policy belongs in the HTTP client.
- Interactive work must be able to get ahead of queued background work.

Monitoring is intentionally tiered:

- **Quick sync:** frequent, uses combined/recent routes where possible (historically about three main routes rather than one request per format).
- **Full sync:** less frequent, needed to find edits/removals of older ratings.
- **Profile sync:** independent of quick/full rating scans.
- **Archive sync:** slow and resumable.
- **Detail enrichment/recheck:** slowest and selective.

Only one configured user per monitor cycle should perform a heavy full scan, so large profiles do not create back-to-back request bursts.

### Authoritative versus incomplete detail pages

Never interpret missing fields from an incomplete or invalid detail fetch as real deletions. If SQLite contains a review, like, or Track Ratings and AOTY returns a 429 page, Cloudflare page, truncated HTML, changed layout, or a parser result marked incomplete, keep the known data and retry later.

Only a successfully fetched and fully/authoritatively parsed detail page may confirm:

- review removal,
- like removal,
- Track Rating removal,
- or replacement of complete detail state with an empty state.

Parser output needs an explicit completeness/authority signal. Absence of a value is not sufficient proof of deletion.

## 5. SQLite as the durable source of truth

SQLite replaced `data.json`. The database lives on the Railway Volume; the exact path comes from settings/environment (historically `/app/data/kotone.sqlite3`, but never assume this without checking `settings.DATABASE_FILE`, `DATA_DIR`, and/or `RAILWAY_VOLUME_MOUNT_PATH`). Do not commit or package a runtime database.

Expected SQLite settings include:

```text
PRAGMA journal_mode=WAL
PRAGMA synchronous=NORMAL
PRAGMA foreign_keys=ON
busy timeout around 30 seconds
```

Do not delete `-wal` or `-shm` files while the bot is running. Keep writes transactional. Preserve backup, integrity-check, WAL-checkpoint, and corrupt-database recovery behavior.

### Known logical schema areas

The exact v10 DDL must be read from `database.py` and migrations. The conversation confirms these logical tables/areas:

- `meta` — schema/state version and technical markers.
- `users` — configured profile state, profile counts/averages/distribution, sync state, and archive state as implemented.
- `ratings` — current rating per `(username, album_id)`; known fields include score, artist, album, date, format, URLs/cover, review/detail flags, like state, completeness/dirty/pending state as implemented.
- `favorites` — current favorites and favorite type.
- `user_track_ratings` — current per-user, per-release/per-track scores.
- `releases` — cached release metadata, scoped by configured-user relevance.
- `release_tracks` — tracklists and public track data/scores.
- `rating_format_sync` — resumable per-user/per-format archive progress and errors.
- `rating_history` — legacy score history retained for compatibility.
- `change_history` — v10 append-only unified history.

There may be more tables, indexes, triggers, or columns in the actual v10 schema. Never replace the real DDL with this summary.

The core rating identity is expected to be `(username, album_id)`, preventing duplicate current-state rows while allowing history to contain multiple events.

## 6. Migrations and compatibility

Migration history that must remain safe:

1. `data.json` -> SQLite: on successful import, the old file was renamed to `data_migrated.json.bak`. Never delete or rename the source before a successful committed migration.
2. Schema **v8 -> v9**: introduced/resolved background archive state and `notify_pending` coordination.
3. Schema **v9 -> v10**: introduced unified `change_history`, detail dirty/recheck support, and related change tracking.
4. Existing `rating_history` remains for backward compatibility. Its old `score`, `new`, `removed`, and `restored` events are imported **once** into `change_history`.

Migration requirements:

- automatic and idempotent;
- transactional where SQLite permits;
- safe to run more than once or after an interrupted deploy;
- preserve every existing rating, review, like, Track Rating, favorite, profile snapshot, sync marker, and historical event;
- avoid duplicate legacy-history imports;
- never require deleting the Railway Volume;
- include tests starting from representative older schemas/data;
- keep the old database recoverable until the new schema is validated.

Never tell the user to recreate the Volume as a routine migration step.

## 7. Config-only persistence rule

This is a hard privacy/scope invariant enforced in the database layer:

> Persist user-specific data only for AOTY usernames currently listed in `config.json` under `users`.

Commands may fetch an unconfigured user to answer a one-off request, but must not create durable profile, rating, review, like, Track Rating, favorite, history, archive, or related release-cache records for that user.

Public release data may be persisted only when the release is connected to a rating of a configured user. Do not let `/album`, `/artist`, `/profile`, `/last`, `/recent`, views, buttons, or parser callbacks bypass this rule.

On startup, the intended behavior is to prune persistent user data for usernames removed from config. Verify the existing implementation and cascading behavior before changing it. Tests must prove that one-off requests for outsiders leave no durable rows.

Case normalization and user identity rules must be centralized so variants cannot circumvent the allowlist.

## 8. Background archive and enrichment

`background.py` is independent from the 20-minute monitor cycle. Its job is to build a complete archive without making the monitor wait hours or overloading AOTY.

Bootstrap behavior:

- Walk configured users in alternating/round-robin order so one huge account does not block another.
- Walk all supported AOTY rating formats, including formats whose active notification fetch limit is `0`. Here, `0` means “do not actively monitor for notifications,” not “do not archive.”
- `profile_rating_archive_limit_per_format: 0` means paginate to the end, not fetch zero.
- A technical safety cap of **500 pages per format** prevents infinite loops after an AOTY layout/navigation bug. If hit, do not mark the format complete.
- Persist progress/errors in SQLite so Railway restart/redeploy resumes rather than starts over.
- Prioritize collecting all rating cards first; then perform slower album metadata, tracklist, review, like, and Track Rating enrichment.
- After initial bootstrap, switch to slow maintenance/refresh rather than repeatedly rebuilding everything.
- Unexpected worker exceptions must be logged, delayed, and retried; they must not permanently kill the worker or the Discord bot.

Supported format names historically included LP, EP, Mixtape, Single, Compilation, Live, Reissue, Soundtrack, Holiday, DJ Mix, Box Set, Instrumental, Unofficial, Video, Demo, Miscellaneous, Music Video, Remix, and Audiobook. Treat parser/config definitions in the repository as authoritative.

### `notify_pending`: background must not eat notifications

After the initial baseline/bootstrap, if the archive sees a new rating before the monitor, it may save it but must set `notify_pending=1` (or the equivalent current state). The monitor must later emit the Discord notification and clear the flag.

```text
AOTY -> background discovers new rating
     -> SQLite current state + notify_pending
     -> monitor consumes pending event
     -> Discord notification
     -> pending flag cleared transactionally
```

Do not reintroduce the race where background writes a new score/current record, then the monitor sees no delta and silently skips the notification. Baseline seeding must not generate notification storms.

## 9. Ownership of score versus detail state

This boundary is critical:

```text
monitor/archive rating-card path -> authoritative current rating score
background detail path            -> review, like, Track Ratings, detail metadata
```

The detail scraper must not update the main rating `score`. Otherwise it can write the new score before the monitor, causing the monitor to miss both change history and the Discord notification.

Rating-score changes, additions, removals, and restorations remain owned by the monitor/archive coordination path. Review/like/Track Rating enrichment and revalidation remain owned by the detail path.

## 10. Unified append-only change history

`change_history` stores events; normal tables store the current state. History is append-only from the application’s perspective: changing the current state must not overwrite or erase prior events.

Known event classes include:

- rating added, changed, removed, restored;
- review added, edited, removed;
- like added and removed;
- individual Track Rating added, changed, removed;
- favorites changes and favorite-type changes;
- profile followers/following/reviews/ratings/lists count changes;
- profile average-rating changes;
- Rating Distribution changes.

Store sufficient old/new values and context to render useful history. Review edits retain the full previous and new text in SQLite. Track Rating events are per track, not a single undifferentiated “track ratings changed” flag, and should retain track identity/title/position as implemented.

### Baseline semantics

The first authoritative observation is a baseline, not a user change. Initial profile/archive/detail import must not create thousands of false `*_added` events. Events begin when a later authoritative observation differs from the stored baseline.

Likewise, a parser failure must not create removals. History and current-state updates should occur in one transaction wherever practical, so crashes cannot leave one without the other.

The bot can only record transitions it observes. If it is offline during `70 -> 90 -> 40 -> 85`, it can only record `70 -> 85` unless AOTY exposes intermediate history.

### Detail rechecks

Old details cannot be considered permanently complete. Two mechanisms are expected:

- If a rating card changes detail indicators (review/like/Track Ratings), mark the detail dirty and queue a fast authoritative recheck.
- Periodically recheck already stored mutable details. The known default `detail_change_scan_interval` is `43200` seconds (about 12 hours), focused on ratings that have mutable detail state rather than blindly requesting every rating.

## 11. Commands and user-facing behavior

Verify the complete command registry in the repository. Known commands/features include:

- `/profile` — configured users should work from SQLite/stale data during AOTY outages.
- `/last` — last rating(s), with SQLite fallback for configured users.
- `/recent` — recent ratings, with SQLite fallback for configured users.
- `/album` — album lookup/details; historically live artist/discography resolution could still be required for unknown albums.
- `/artist` — artist/discography lookup; historically more dependent on live AOTY for unknown artists.
- `/history username amount category` — ephemeral unified history view.
- `/dbstats` — ephemeral database/archive/integrity diagnostics.
- Discord monitor notifications for new, changed, removed, and restored ratings as implemented.
- Existing buttons/views for album details, review, Track Ratings, navigation, and related actions.

Known `/history` categories (preserve Polish labels unless the actual current UI differs):

```text
Wszystko
Oceny
Recenzje
Likes
Track Ratings
Profil + Favorites
```

`/history` and `/dbstats` are intentionally ephemeral so operational/private information does not spam a channel.

`/dbstats` should expose actionable information without shell access, including overall and per-configured-user counts, reviews, likes, albums with Track Ratings, track-score counts, complete/dirty details, favorites, history count, archive formats completed/total, archive item count, last syncs, pending notifications, last archive error, DB/WAL/backup sizes as available, the resolved DB path, and a SQLite quick/integrity check.

When AOTY is down:

- cached `/profile`, `/last`, `/recent`, known reviews, known Track Ratings, avatars, and known release data should remain usable for configured users;
- monitor/background processes should survive and resume later, but cannot discover new upstream changes while offline;
- `/dbstats`, `/health`, and local database operations remain independent of AOTY;
- `/artist` and resolution of unknown `/album` requests may still fail gracefully if no local catalog entry exists.

Never present stale cached data as freshly fetched. Preserve any existing stale/offline indicator.

## 12. UI, embeds, views, and romanization invariants

The existing Discord UX is part of the product and must not be casually redesigned during backend work.

- Preserve slash-command names, option names/order/defaults, autocomplete, permissions, ephemeral/public behavior, and response timing unless the requested feature explicitly changes them.
- Preserve existing embed layouts, field order, wording, emoji, colors, thumbnails/images, footer/timestamps, pagination, button labels/styles/order, enabled/disabled states, and navigation behavior.
- Preserve the current Polish-facing labels and tone.
- Preserve existing artist/album/track romanization behavior and fallbacks. Do not independently romanize in multiple layers or replace displayed original text without matching the current UX.
- Keep Discord field/value/description limits and interaction timeouts in mind. Defer interactions before slow work and handle expired/deleted interactions safely.
- Backend refactors must produce no visible embed or command changes unless explicitly requested.

Before altering UI code, capture representative outputs or add snapshot/structure tests for fields and button state where feasible.

## 13. Railway deployment and lifecycle

Kotone runs as one Railway service with a persistent Volume. The Volume is the durable boundary; deployments and containers are disposable.

Known deployment intent in `railway.json`:

```json
{
  "healthcheckPath": "/health",
  "healthcheckTimeout": 120,
  "restartPolicyType": "ON_FAILURE",
  "restartPolicyMaxRetries": 10,
  "drainingSeconds": 15
}
```

Verify the current file before editing. Preserve these lifecycle principles:

- Bind the health server to `0.0.0.0` and Railway’s `PORT`.
- `/live` answers whether the process/event loop is alive.
- `/health` answers ready only when Discord, SQLite, monitor, and background worker are ready.
- Neither endpoint calls AOTY. An AOTY outage must never cause Railway to restart an otherwise healthy bot.
- Initial 503 responses before Discord/SQLite readiness are acceptable; Railway should receive 200 once ready.
- Fail before importing/creating SQLite if Railway is detected without `RAILWAY_VOLUME_MOUNT_PATH`, or if an explicit Railway `DATA_DIR` points outside that Volume.
- Handle `SIGTERM` within one shared deadline below the 15-second drain: stop monitor/background tasks concurrently, reserve most of the window for WAL checkpoint and verified backup, then finish optional cleanup.
- SQLite checkpoint/backup is an idempotent boundary that completes even if Discord shutdown hangs. A Railway-only hard-exit watchdog remains below `drainingSeconds` so `asyncio.run()` cannot wait indefinitely for a non-cancellable `to_thread` request.
- A monitor/background task that dies unexpectedly initiates the same safe shutdown with a non-zero exit code, allowing Railway `ON_FAILURE` to restart the service. Do not rely on `/health` as continuous monitoring after deployment activation.
- Do not write durable state to the ephemeral application filesystem when it belongs on the Volume.
- Never package `kotone.sqlite3`, `data.json`, WAL/SHM files, backups, secrets, Discord tokens, Railway credentials, or SSH keys.

A previous project ZIP accidentally contained a tracked private OpenSSH key named `railway ssh` plus its public key. The current working tree removes the key pair and `data.json` from Git tracking while leaving the ignored local files on disk. The private key still exists in earlier Git history (confirmed in five commits), so it must be treated as compromised: rotate/revoke it before any history rewrite, then clean the remote history as a separate coordinated operation. Never restore these files to tracking.

## 14. Known bugs already fixed — regression checklist

Do not reintroduce any of these:

1. **Many independent AOTY request paths:** commands, monitor, and buttons bypassed shared throttling.
2. **Monitor request explosion:** frequent checks fetched every format instead of combined recent routes.
3. **Archive tied to the 20-minute monitor:** initial full archive took many hours and appeared stuck.
4. **One large user blocking others:** archive did not alternate users.
5. **Arbitrary 2,000-item format limit:** replaced by unlimited pagination (`0`) plus a 500-page safety cap.
6. **Archive progress lost on restart:** progress now persists in SQLite.
7. **Background swallowing new-rating notifications:** fixed using `notify_pending`/monitor coordination.
8. **Detail scraper swallowing score notifications:** fixed by preventing detail enrichment from changing the main score.
9. **`detail_complete=1` preventing future review/like/Track Rating edits from being noticed:** fixed with dirty markers and periodic rechecks.
10. **Incomplete/429/Cloudflare HTML erasing valid details:** only authoritative complete pages may confirm removals.
11. **Initial import generating false history:** first observation is baseline.
12. **Only score history preserved:** v10 tracks reviews, likes, individual Track Ratings, favorites, and profile statistics.
13. **Unconfigured users being persisted through commands/cache:** enforcement belongs in the database layer.
14. **AOTY outage breaking cached commands:** services should fall back to SQLite/stale cache for configured users.
15. **AOTY health coupled to Railway health:** health endpoints do not query AOTY.
16. **Corrupt SQLite causing unrecoverable startup:** preserve backup/recovery and integrity checks.
17. **Numberless Track Ratings/parser edge cases:** regression tests existed; preserve robust track identity parsing.
18. **`ratings_count` parser regressions:** keep coverage.
19. **Sensitive/runtime files in deployment ZIP/repo:** no DB, JSON state, keys, or secrets.
20. **Interactive `/last`/`/recent` consuming notifications:** monitored-format refreshes no longer overwrite monitor-owned scores or clear `notify_pending`; disabled formats still record post-baseline history.
21. **Partial archive pages creating false removals:** stale pages, wrong redirects, duplicate pagination, Cloudflare/interstitial HTML, and unparsed rating containers are not authoritative snapshots.
22. **Partial release/profile/detail pages erasing cache:** parser completeness is explicit; release sections merge non-destructively, missing Favorites preserve the prior list, and incomplete user detail cannot confirm removals.
23. **Stable 404 probes opening the HTTP circuit:** non-429 4xx responses are not retried or counted as transport outages.
24. **Windows corrupt-DB recovery failing on an open handle:** integrity probes always close before quarantine/restore.
25. **Corruption silently becoming an empty database after restart:** recovery is fail-closed without a verified backup and persists a `recovery-required` marker across Railway restart attempts.
26. **Config typo purging the only copy of user data:** `users` is validated before DB construction, and a verified pre-prune snapshot is created before removing users outside the allow-list.
27. **Rollback losing legacy history imports:** the v10 import uses a numeric watermark plus deduplication rather than relying only on a permanent boolean marker.
28. **Sequential shutdown exceeding Railway drain:** workers share one deadline, persistence completes before a hung Discord close may time out, and a watchdog bounds executor teardown.
29. **Background user starvation/config ignored:** archive and enrichment use independent fair cursors and respect `PROFILE_RATING_ARCHIVE_FORMATS_PER_CYCLE`.
30. **Dead workers remaining invisible:** readiness reports worker state and unexpected worker exit triggers controlled non-zero shutdown/restart.
31. **Detail buttons reporting temporary fetch failure as “no data”:** existing normal UX is unchanged, while incomplete review/Track Rating fetches receive an explicit temporary warning.

## 15. Explicit do-not-break constraints

These are release blockers:

- Do not delete, recreate, overwrite, or require manual replacement of the existing Railway Volume/database.
- Do not make a destructive or irreversible migration without a tested backup and rollback path.
- Do not persist any user-specific data for users outside config.
- Do not let public release cache become an unrestricted global AOTY archive.
- Do not bypass the central AOTY HTTP client.
- Do not increase concurrency or reduce cooldowns merely to speed up bootstrap.
- Do not treat missing values from incomplete HTML as removals.
- Do not let background/detail work update the main score before the monitor.
- Do not let archive/enrichment suppress notifications.
- Do not generate history for baseline imports or duplicate legacy history.
- Do not overwrite append-only history when updating current state.
- Do not make `/health` depend on AOTY.
- Do not remove offline/stale SQLite fallbacks for configured users.
- Do not change command names, visible Polish copy, embed design, button behavior, romanization, or notification UX during unrelated backend work.
- Do not commit or distribute runtime SQLite/JSON data, backups, secrets, SSH keys, `.git` history bundles, or private user exports.
- Do not add README files.

## 16. Coding conventions and maintainability expectations

- Target the Python version and dependency versions already declared by the repository.
- Keep async Discord/network work non-blocking. Move unavoidable blocking SQLite/file operations behind the project’s existing synchronization/executor strategy.
- Centralize config parsing/defaults/validation. Do not scatter magic intervals or format lists across modules.
- Centralize username and album/track identity normalization.
- Use parameterized SQL only.
- Make multi-step state + history + pending-flag changes transactional.
- Preserve single-writer/locking discipline and the configured busy timeout.
- Return structured domain data from parsers/services; avoid coupling HTML selectors directly to embeds.
- Distinguish `not found`, `not yet cached`, `stale`, `upstream unavailable`, `rate limited`, `parse incomplete`, and `authoritatively empty`.
- Log component-prefixed, actionable messages without tokens, cookies, full private reviews, or secrets. Known prefixes included forms such as `[DATA]`, `[BACKGROUND]`, and `[ARCHIVE]`.
- Add comments for subtle invariants: baseline semantics, authoritative deletion, score ownership, pending notifications, persistence scope, and migration idempotency.
- Avoid broad rewrites when a focused change is safer. Remove duplication only after tests capture behavior.
- Keep failure containment: archive failure must not kill monitor/Discord/health; command failure must not kill shared workers.

## 17. Testing and verification expectations

Every meaningful change should run, at minimum:

```text
compile every Python file
full offline regression suite
fresh-database initialization
upgrade/migration tests for supported old schemas
SQLite quick_check/integrity check on test databases
```

Core regression coverage should include:

- legacy `data.json` migration and backup behavior;
- v8 -> v9 -> v10 upgrade and restart/idempotency;
- one-time `rating_history` import without duplicates;
- configured-user persistence and pruning; unconfigured one-off command leaves no rows;
- release-cache scoping;
- archive pagination, 500-page cap, progress resume, round-robin fairness, and rate-limit pause;
- archive cannot consume monitor notifications;
- detail enrichment cannot change main score;
- add/change/remove/restore rating history;
- review add/edit/remove with full old/new text;
- like add/remove;
- per-track add/change/remove history;
- favorites/profile/distribution history;
- baseline imports create no false changes;
- incomplete detail never erases stored data or creates false removals;
- dirty detail and periodic recheck scheduling;
- corrupt DB recovery from backup;
- WAL/checkpoint/shutdown behavior where practical;
- HTTP priority, spacing, cache/stale-cache, Retry-After, 429 cooldown, backoff, and circuit breaker;
- parser fixtures for current AOTY HTML, layout variations, numberless tracks, missing optional fields, Cloudflare/error pages, and truncated pages;
- offline service fallbacks and honest stale indicators;
- health readiness independent of AOTY;
- command/embed/view structure remains unchanged for backend-only changes.

Current verified checkpoint (2026-08-17): **73/73 offline tests passed on Windows**, including the original 20 tests plus database recovery/migration safety, AOTY completeness and score ownership, lifecycle/worker/UI regressions, and repository hygiene. All 30 Python files also compile successfully. No test contacted Discord or live AOTY.

Live limitations must be reported honestly: offline fixtures cannot prove a real Discord login, current AOTY layout, Cloudflare behavior, Railway Volume mount, or production rate limits. After safe offline verification, the production integration check should review startup/migration logs, `/health`, Discord command sync, first quick sync, archive progress, `/dbstats`, pending notifications, and controlled stale fallback behavior.

`.github/workflows/validate.yml` now runs discovery for every `tests/test_*.py` file on both Ubuntu and Windows with Python 3.13. `.python-version` pins Railway/Railpack to the Python 3.13 line. Preserve this cross-platform gate. If Railway can wait for passing GitHub checks before autodeploy, that is the preferred deployment gate.

## 18. Safe definition of done

A change is complete only when:

1. The requested behavior is implemented through the correct layer.
2. Existing schema and Railway Volume data remain compatible.
3. New/updated tests cover success, upstream failure, restart, and relevant race conditions.
4. All tests and Python compilation pass.
5. No unconfigured-user data, runtime DB/state, secrets, keys, README files, or unrelated UI changes are present.
6. The final diff is reviewed for extra AOTY requests, duplicated retry logic, accidental score writes, baseline-history noise, and command/embed changes.
7. Any unverified live behavior is clearly identified for deployment validation rather than claimed as proven.

When uncertain, protect stored data, history, notification delivery, UI compatibility, and AOTY rate limits before optimizing speed.
