# Issue 43: Worker/calendar implementation and remaining operations

Reviewed #43, #27, #38, #41, #42 and their comments on 2026-10-26. This PR changes Worker code/tests only, plus this report. It does not change config/, collector/, D1 schema, production data, secrets, or deployments. Claude PR #44 owns collector policy and pipeline.js/recommend-e2e.mjs; those files are not edited here.

## Calendar

`/sports` now serves the compact responsive design and reads `/sports/events`. There is no production demo fallback. School level, sport (including gymnastics), spectator admission and driving-time filters support multiple selections: OR within a group, AND across groups and search. All clears its group. Default shows every distance; same-day games sort by known driving minutes, unknown last. Blank calendar cells are not buttons. Meets show participants without fabricated home/away teams. Youth leagues and clubs are labelled separately. Empty dates mean no collected records, not proof of no games.

API retains its existing two-month range and adds level_detail, participants, event_type, driving fields, aggregate counts and source-health summary. It exports no recipient data. Fees described as registration/entry fees do not become spectator admission. Duplicate date/sport/participants/venue records collapse; inconsistent start times are flagged. Ordinary championships remain calendar-only unless explicitly sports_special_verified. Saturday 09:00 America/Denver weekly links retain duplicate protection and owner-only delivery; DST and availability are tested.

### Venue-route contract (not yet populated in production)

The existing city-distance estimate is not a driving estimate. Only an explicit road-route record for the actual venue is accepted:

```json
{"venue_route":{"origin":"Westminster north, CO 80234","mode":"driving","venue":"exact event.venue or event.location","city":"exact event.city","minutes":45,"km":38,"source_url":"https://routing-provider.example/route","checked_at":"2026-10-26T00:00:00Z"}}
```

The above is a schema example, not measured travel. Missing/mismatched evidence stays unknown. Buckets are <=30, >30–60, >60–90, >90 minutes, unknown. Public-area origin is approximate, never the family's private address. UI says this is not live traffic and shows route provenance. No paid map request or geocoding was made. Remaining work: agree a routing provider/public origin with the collector owner, resolve actual venue addresses, obtain and cache road routes with provenance, and refresh them. No fabricated numeric distances were backfilled.

### Gymnastics evidence and actual coverage

`worker/test/fixtures/gymnastics-official.json` contains six source records for five real 2026 meets, manually checked against official schedules (DU, Air Force, Valor Christian). These are historical regression fixtures, not live ingestion or guessed 2027 schedules. They cover high school invitational and university dual/multi-team representations; missing admission remains unknown. Air Force and DU disagree on the March 9 start time, which the API flags after deduplication.

Claude's Oct 25 `eval/sports_diag.json` reports 889 total DB events, 233 sports-like, 213 future sports-like and 202 future calendar matches, all university. It reports no gymnastics matches (registration only). This is the existing PR's recorded audit, not a new production query by this implementation. A monthly API count of 94 and an all-future count of 202 have different scopes. Claude's source dry run reports 624 raw entries and zero new AI-normalized events; middle/K8 sources still had blocked/empty responses. Five test meets are not five newly collected production events. This work performs zero live DB writes and zero paid AI collection runs. Expanded production coverage remains unverified.

## Subscription decisions

All three bots include owner approval/rejection buttons on new applications. Callback data includes bot+applicant; sender, owner chat and SYSTEM_BOT must all match. Pending-only conditional UPDATE makes repeated/concurrent decisions idempotent. Rejection blocks /start and legacy /approve. Pending /stop becomes withdrawn, so stopping and restarting cannot bypass approval. Previously approved stopped subscriptions retain their resume behavior. Applicant welcome follows existing owner-only delivery policy.

The hourly heartbeat checks pending applications after 09:00 OWNER_TZ. With zero pending it sends nothing and makes no daily claim. With pending, one daily attempt is claimed atomically. Pages contain at most 20 applicants/40 buttons; /pending N opens remaining pages. Failed/uncertain sends retain the claim to avoid duplicate retries and record aggregate pending_reminder_last status. An uncertain send may require manual inspection; it is not silently retried.

## Collection delays and recovery

GitHub run evidence on Oct 25: scheduled collect 40164841590 began 05:15 UTC; the next observed collect was manual 40192107306 at 12:12 UTC, about 6h57 later. Subsequent scheduled runs and dispatch runs succeeded. This proves an observed collection gap, not its exact root cause and not a total Telegram outage. GitHub documents that scheduled runs can be delayed/dropped: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows .

last_run_at is a collector report; last_collect_ok_at is completed full-collection evidence. Light runs must not be treated as full successes. The existing guard's six-hour slot was hardened to a rolling six-hour atomic claim, preventing duplicate requests across slot boundaries. Full success older than 26h requests full; run older than 6h requests light. Invalid timestamps report a diagnostic. Dispatch timeout/403/network outcomes are recorded separately and do not stop delivery/reminders.

No recovery was dispatched live and no token was read/provisioned. Operation still requires GH_DISPATCH_TOKEN and GH_REPO. Use a fine-grained token restricted to this repository with Actions write, expiration and entry in the existing expiration registry, after owner approval. Token setup is operational, not source code. GitHub permission reference: https://docs.github.com/en/rest/actions/workflows . Recovery remains dependent on Worker heartbeat and GitHub availability and cannot promise uninterrupted collection.

## Policy boundary and validation

Claude #44 owns exclusion of ordinary paid shooting advertisements, verification/holding of foreign-national military access, and prioritizing five-person family rafting/kayaking over competitions. Integration must retain its hold_reason pipeline gate: opt-in/push must not bypass unverified access. No new ski-pass automation was created; the existing ChatGPT monitoring remains separate.

Local tests use disposable D1 and a fake Telegram/GitHub server. Browser QA uses labelled synthetic API responses, never production collection counts. `worker/test/sports-browser.mjs` exercises 16,384 filter combinations, 1440×900 and 390×844, search/reset, sort, modal, month change, blank cells, API failure and no horizontal overflow. The normal test command includes issue43 E2E and unit suites. Windows harness uses Node 22 SQLite for local inspection because concurrent wrangler inspection processes caused SQLITE_IOERR_TRUNCATE; startup errors and temporary cleanup are also handled.

Do not close the coverage issues yet. Approval is still needed before main merge/deployment; live route population, blocked school-source recovery, future gymnastics ingestion, recovery-token provisioning and post-deployment read-only verification remain outstanding.
