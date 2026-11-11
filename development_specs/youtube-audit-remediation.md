# YouTube audit remediation — implementation and reviewer evidence

Prepared 2026-10-13 for the eight-page report “Jan Schmitz: ToS Violations Report
V.1”, dated 2026-10-10, identifying project **594155589966**.

The implementation is local and has not been deployed or submitted to YouTube.
Remote staging and production runtime configuration have not been accessed.
Registered OAuth clients and the project number were verified through read-only
Cloud Console access; no client secrets or API key values were opened.
Intelligence is excluded from this implementation and its compliance claims.
Existing connections do not acquire a reconnect requirement or lose their grants
because their authorizer is unknown.

## Reviewer response draft

### Analytics — report pages 1 and 3, policy III.E.4.h

The YouTube engagement-rate card and subscriber-based calculation have been
removed. The dashboard displays the following separately, using the native
YouTube Analytics report for the connected channel (`ids=channel==<channel ID>`).
The same cached reports serve HTML, HTMX, REST and MCP.

| Display / existing response key | Native metric | Display rule |
|---|---|---|
| Views / `views` | `views` | Native whole-period count |
| Watch time / `watch_time` | `estimatedMinutesWatched` | Native whole-period minutes, formatted as hours for the UI; never a daily average |
| Avg view % / `avg_view_pct` | `averageViewPercentage` | Native whole-period percentage; values above 100% remain unchanged |
| Likes / `likes` | `likes` | Separate native count |
| Comments / `comments` | `comments` | Separate native count |
| Shares / `shares` | `shares` | Separate native count |
| New subscribers / `subscribers` | `subscribersGained` | Native gained count, rather than growth inferred from current subscriber totals |

Each daily refresh fetches one 180-day report with `dimensions=day`, and six native
period reports: current and previous 7-, 30- and 90-day windows. Windows use the
Pacific calendar and end yesterday. Dates are chosen once per refresh, including
when network calls cross midnight. Daily rows use the existing snapshot tables;
whole-period values use `YouTubePeriodReport`, keyed by account/start/end.

YouTube charts never reconstruct daily observations from lifetime snapshots.
Unreported observations produce `null` chart gaps. An unavailable card displays
an em dash; numeric API metric entries are omitted when unavailable. A reported
zero remains numeric zero. REST/MCP return `engagement: null`, with
likes/comments/shares in `hero_metrics`, preserving numeric types and routes.
Responses include `reporting_period` (start, end, timezone, latest reported day)
and per-metric `comparison_available`. A percentage comparison is shown only when
both periods have complete daily coverage for that metric, both period values
exist, and the previous value is nonzero. No additional derived analytics approval
is assumed. Other platforms keep their existing analytics behaviour.

Legacy YouTube channel estimates are excluded by the native source marker.
The daily refresh/backfill replaces the reporting window with native observations,
including removing observations that YouTube no longer returns. API requests from
page loads are not used to fill missing observations.

### Branding — report pages 2–8, policy III.F.2.a

Source attribution uses the exact trade name “YouTube” in BrightBean's normal
typeface. The name is not rendered as a recreated wordmark or custom logo tile.
Navigation shows “YouTube” on the first line and the channel
name on the second, beside the channel avatar or a neutral initial fallback.
Connected-account cards, compact selectors, publishing settings/preview, calendar
and analytics use nearby text labels. The connect-platform card, public client
onboarding row and account-selection header also display the unchanged official
red icon through one shared component. Mixed-source inbox rows and detail panels
identify YouTube within each YouTube content block. Source links target the
associated channel/video, or YouTube for an unconnected component.

The copy identifies the source and publishing destination without implying
YouTube endorsement or changing the application name. Any supplied player branding
must remain unobscured. The displayed icons preserve the official proportions,
intrinsic clear space and white background. At the user's request, the three
connection pages restore the original compact layout: a 20px visible icon width
in the platform card and onboarding row, and 24px in the selection header. These
compact graphics fall below the previously selected 100px visible-height minimum;
this implementation does not claim that minimum is satisfied. The icon links to
YouTube and is kept within its corresponding feature
component. It is not dimmed when YouTube credentials are unavailable.
The numeric minimum is not a text font-size requirement.
See `theme/static/brand/youtube/README.md` for source references.
This describes the implemented combination of icons and text; reviewer acceptance
has not been obtained.

### Authorization, refresh and deletion — report page 1, policy III.E.4.a–g

New connections and voluntary renewals record the actual authorizing BrightBean
user. Existing ownership stays nullable; workspace ownership is not substituted.
Successful checks use the existing OAuth grant and record authorization/profile
retrieval timestamps. Per-video existence checks and per-comment successful
retrieval timestamps are distinct from publication time and polling attempts.

| Data / trigger | Implemented schedule or action |
|---|---|
| Authorization and copied channel profile | Daily compliance check in every status; existing six-hour health checks also use the same verifier |
| Copied profile/comment refresh | Successful retrieval timestamps; comments due by day 25, profiles refreshed on each successful check |
| Copies still unrefreshed | Delete by day 29; legacy comment `created_at` and profile `connected_at` represent known first retrieval, never publication or a failed poll |
| Retained videos and lifetime statistics | Verify all retained IDs, including old videos and comment-only IDs, independently of the 90-day analytics cadence cutoff |
| Deleted video/comment | Remove local copies after confirmed successful existence/retrieval check |
| Historical authorized statistics | Retain 180 days; retain a latest lifetime statistic only while grant and video existence are verified |
| Raw API response/error caches | Clear expired response payloads; remove unused copied upload metadata; YouTube publish logs/error notification copies expire by day 29 |
| Quota exhaustion / transient network failure | Retry without extending successful verification timestamps; unverified grants cannot extend retention indefinitely |
| Confirmed `invalid_grant` | Immediate local purge, even while age-based enforcement is gated |
| Disconnect, recorded authorizer deletion, organization-deletion request | Immediate YouTube connection/API-data purge and grant revocation attempt |
| Temporary revocation failure | Encrypted retry outbox, hourly retries with backoff, maximum seven-day lifetime; local deletion is already committed |

Central cleanup covers channel/post snapshots, period-report caches, copied
profiles/comments, publish logs, copied upload payloads in version history,
inbox and publish-error notification copies, credentials and account-scoped
pending jobs. Generation checks and database locks
prevent stale workers from recreating deleted data or overwriting a voluntary
renewal. New uploads do not persist unused resource responses, and clones/new
versions keep authored settings without copying those API payloads. Confirmed
permanent rejection from an obsolete grant cannot purge a newly renewed connection.

The original authored Post and original uploaded media are preserved when its
YouTube connection is removed. Other-platform targets/data remain. An organization
deletion request immediately removes YouTube data; the existing 14-day grace
period applies to the remaining organization data. Cancellation messaging explains
that reconnecting YouTube will be required.

The automatic age-based enforcement gate defaults to **off** pending staging
review. Explicit deletion and confirmed permanent grant rejection always execute
immediately. This draft describes implemented behaviour; do not claim production
enforcement until the rollout below has been completed.

### Project inventory — report page 1, policy III.D.1.c

| Project number | Environment | Credential source | Purpose | Verification |
|---|---|---|---|---|
| 594155589966 | Reviewer-reported quota project | Reviewer PDF, page 1; Cloud Console Project info | YouTube quota request under review | Verified: BrightBean Studio, project ID `social-intelligence-app-490321` |
| 594155589966 | Primary development checkout | `.env`: `PLATFORM_GOOGLE_CLIENT_ID`; fingerprint `59f108ea63616d04` | YouTube Data/Analytics, upload/comments; shared environment client also supports Google Business Profile | Exact fingerprint match to Cloud client “BrightBean Studio Social Accounts Development” |
| 594155589966 | Primary development checkout | `.env`: `GOOGLE_AUTH_CLIENT_ID`; fingerprint `70cbb1847d19c5ee` | BrightBean Google sign-in, separate client from YouTube | Exact fingerprint match to Cloud client “BrightBean Development Login” |
| 594155589966 | Registered production client | Cloud client “BrightBean Studio Social Accounts Production”; fingerprint `ab36d558fda1d92b` | Registered social-account OAuth client | Project membership verified; actual deployed credential resolution unverified |
| 594155589966 | Registered production login | Cloud client “BrightBean Production Login”; fingerprint `8b2cd24607c857bc` | Registered Google sign-in client | Project membership verified; deployed configuration unverified |
| 594155589966 | Registered staging client | Cloud client “BrightBean Studio Social Accounts Staging”; fingerprint `556ea3d3aed9f9fd` | Registered social-account OAuth client | Project membership verified; deployed configuration unverified |
| 594155589966 | Registered staging login | Cloud client “BrightBean Studio Staging Login”; fingerprint `d8158e9c335550f0` | Registered Google sign-in client | Project membership verified; deployed configuration unverified |
| None observed | Primary development database | Read-only `PlatformCredential` query: zero YouTube override rows | Potential per-organization fallback credentials | Development store inspected; no production-store inference |
| Unverified | Production | Environment / deployed secret configuration | Production YouTube requests | Configuration not accessed |
| Unverified | Production organization overrides | Deployed credential store | Organization-specific YouTube requests if complete environment credentials are absent | Store not accessed |

The local Cloud CLI has no authenticated account, and `projects list` refused the
read-only query. The browser's existing Cloud Console session subsequently allowed
verification of project **594155589966** and its registered OAuth clients. Both
development fingerprints match the Cloud list exactly. Actual production/staging
environment resolution and organization overrides remain unverified, so no
“single project used by all deployments” claim is made. Complete environment credentials
take precedence over organization credentials in the actual resolver.
`youtube_project_inventory --environment <actual environment>` exports fingerprints,
candidate prefixes, active source resolution and inactive overrides without
exporting tokens, full client IDs or secrets. Its `project_number` remains null
until separately verified. The current findings and sanitized Cloud verification
are in `output/youtube-review/project-inventory.json` and
`output/youtube-review/cloud-project-verification.json`. The Cloud list also contains
a named “YouTube API Key - Public Pages”; its value was not accessed and its Studio
runtime use is unverified. Other registered BrightBean Platform clients are listed
as metadata in the verification file, without claims about their functionality.

## Screenshot evidence

Open `output/youtube-review/index.html` for matching before/after comparisons and
full-resolution links. `before/` runs the unchanged baseline commit
`30ce27e3ef14b5ccd5b2f304edae3936916a6f0a`; `initial/` is the initial implementation
review; `logo-review/` preserves the previous large-icon presentation;
`text-review/` preserves the subsequent text-only presentation;
`restored-100px-review/` preserves the three-page 100px restoration; `after/`
is the final implementation with original compact icons on the three selected pages.
These are screenshots from running
Django applications, using fictional accounts and identical representative report
fixtures. No real OAuth calls, production accounts or real comment authors are
used. Desktop viewports are **1440 × 1000** and mobile viewports **390 × 844**.
There are 39 matching before/after pairs, including one direct component crop,
eight additional final captures covering post analytics, organization calendar,
public onboarding and the shared account-selection template, and 16 initial
review captures. `all.html` displays all 47 current screenshots together;
`youtube-ui-screenshots.zip` contains the current full-resolution images.
The account-selection capture uses a fictional pending-selection session to cover
the template; the normal YouTube OAuth flow currently connects a channel directly.
The initial, large-icon and text-only captures are superseded
evidence. Final captures include the selectively restored icons, text source labels, corrected fictional
empty-channel handle, settled chart/layout animations and mobile refinements.

| Evidence names | Report reference | Coverage |
|---|---|---|
| `analytics`, `analytics_metrics`, `analytics_selector`, `analytics_gaps` | Page 3; analytics requirement on page 1 | Native headline values/counts, 7/30/90 dates, removed engagement card, account selection, missing observations, zero versus unavailable, unclamped percentage |
| `analytics_empty` | Pages 7–8 | Empty state with text source attribution and reporting dates |
| `analytics_post` | Pages 2–3 | Video analytics drawer with direct metrics and a video source link |
| `accounts`, `accounts_youtube`, `connect`, `connect_youtube`, `sidebar`, `sidebar_attribution` | Pages 4–6 | Connection cards and navigation with two-line YouTube/channel labels beside channel avatars |
| `onboarding`, `account_selection` | Pages 4–8 | Public client connection page and shared account-selection template coverage |
| `composer`, `composer_settings`, `composer_preview` | Page 2 | Account selection, video settings and publishing preview |
| `calendar`, `organization_calendar` | Page 3 | Workspace/organization calendar labels and component attribution; mobile month grids scroll horizontally |
| `inbox`, `inbox_detail` | Page 3 | Mixed-platform comments and YouTube attribution in each content block |
| `disconnect_confirmation`, `organization`, `organization_confirmation` | Page 1 retention/deletion request | Immediate-purge and cancellation messaging; confirmations opened without submitting |

The normal fixture returns native whole-period `averageViewPercentage=72.4` while
daily percentages alternate 61.2/103.1. This intentionally distinguishes native
period reporting from an unweighted average of daily percentages. The gap fixture
omits trailing/interior observations, returns reported likes=0 and
averageViewPercentage=103.1, and omits shares. Its unavailable comparisons stay
hidden. `fixture-responses.json` records the fictional native reports.

Every final screenshot is visually reviewed for clipping, wrapping and source
attribution. DOM measurements in `layout-audit.json` record text attribution,
20px/24px visible icon widths on the three icon pages and no document-level horizontal
overflow for the reviewed screens. Scrollable calendar grids and post tables retain their own
scroll areas. Earlier logo measurements are preserved separately.

## Validation and rollout

Local validation uses isolated PostgreSQL 16 and copied fixture SQLite databases,
not production data. The remediation full suite passed **2,521 tests**, with **one
existing skip**, before the subsequent text-attribution presentation refinement.
After that refinement, **85 analytics/account view and native-report tests passed**;
Django checks, Ruff, diff whitespace checks and the frontend build also passed.
The supplementary screenshot review corrected YouTube spelling/initials in the
shared account picker and mobile organization-calendar wrapping. All 57 account
view tests passed again after the picker copy change. The organization-calendar
layout was verified in the running application at both viewports.
The compact icon restoration on the platform grid, public onboarding and
account-selection header passed all 57 account view tests. The official
icon retains its supplied clear space, with original 20px/24px visible widths
on desktop and mobile and no document-level horizontal overflow on these pages.
The review's post-snapshot race is fixed by passing the request's captured YouTube
generation to the snapshot writer. Eight regression cases cover renewal during
optional-report, batch, individual and single-post requests, plus successful
writes under unchanged authorization. All 115 targeted analytics/native-report
and compliance tests passed after the fix; Ruff, formatting and Django checks
also passed.
PR preparation rebased the change on current origin/main
`7aecb7b65d2030351321d49f6ebd381b1db9846e`. All 193 targeted analytics,
compliance and account-view tests passed on that combined tree; lint, formatting,
type checking, Django checks, migration consistency and the commit secret scan
passed. The full suite at the final PR revision is left to GitHub CI; the 2,521-test
local result above predates the presentation refinements and generation fix.
Focused checks and the final
results are recorded in `output/youtube-review/validation.json`. Ruff matches
CI's 0.15.9. Lint, formatting, mypy, Django checks, migration consistency checks,
diff whitespace checks and the frontend build all passed. The final run used a
memory-backed disposable PostgreSQL database after Docker disk exhaustion
prevented an earlier rerun from creating its test database. Only the disposable
test container was replaced; no unrelated Docker data was removed.
Native-report tests cover whole-period percentages versus daily averaging,
reported zero/missing data, values above 100%, trailing/interior gaps, Pacific
dates/DST/midnight, transport parity and obsolete writes. Retention tests cover
25/29/180-day boundaries, old videos, grant revocation, deleted comments/videos,
encrypted revocation retries, cleanup idempotence, new/voluntary authorizers,
legacy NULL ownership, pending-job races, organization deletion and preserved
other-platform content. Credential inventory tests verify source precedence and
that no credentials are disclosed.

All four new migrations were applied over a seeded baseline copy retaining its
legacy connections, and against a fresh PostgreSQL test database. A read-only
fixture cleanup report was generated before worker execution. An actual local
background worker executed both recurring `youtube-compliance` tasks with mocked
provider responses, verified both fictional connections and registered the next
daily run. Evidence is in `cleanup-dry-run.json` and `worker-smoke.json`.
**These local checks do not verify remote staging or production worker execution.**

1. Deploy to staging with `YOUTUBE_COMPLIANCE_ENFORCEMENT=false`. Apply migrations,
   build static assets and confirm the existing connections retain their grants,
   statuses and nullable authorizers. No migration marks them for reconnect.
2. Run `python manage.py youtube_compliance_report` with a read-only staging
   database role. Review connection/status counts, unknown authorizers, overdue
   authorizations/comments, old snapshots and retained videos. This command makes
   no API requests and deletes nothing. Unknown authorizers are informational.
3. Run `python manage.py youtube_project_inventory --environment staging` and the
   equivalent read-only production inventory. Correlate fingerprints with actual
   Cloud OAuth credentials and verified project numbers. Read-only examples:
   `gcloud projects list --format='table(projectId,projectNumber,name)'` and
   `gcloud projects describe <verified-project-id> --format='value(projectNumber)'`.
   Document configured environment sources and active/inactive organization
   overrides. Fill the unverified rows rather than assuming a single project.
4. Queue a native report backfill for each staging YouTube connection using
   `python manage.py backfill_analytics --account-id <YouTube-account-UUID> --days 180`.
   Observe worker completion and cache rows. Hourly analytics scheduling also
   refreshes the report cache once per Pacific report date.
5. Confirm the staging worker processes the `youtube-compliance` queue. The normal
   `process_tasks` worker processes all queues; a dedicated worker can use
   `python manage.py process_tasks --queue youtube-compliance`. `post_migrate`
   registers the daily verifier and hourly revocation retry task. Verify both
   task execution and the next repeated jobs in the database, not just queue
   registration.
6. After reviewing staging counts, enable enforcement there and exercise
   retention boundaries, explicit deletion, transient failures and renewal races
   with disposable accounts. Review actual deletion counts and retry results.
   Recheck the supplied screenshots against staging and resolve any differences.
7. Production deployment and enabling enforcement are separate release actions.
   Backfill native reports and repeat the read-only inventory/count/worker checks
   in production before enabling its gate. Monitor failed verification ages,
   stale copy counts, task failures and revocation outbox expiry.
8. Replace local fixture screenshot references with deployment evidence if the
   reviewer requires production screenshots, finalize the project table, and
   submit the reviewed response through the existing quota-review conversation.
   This implementation does not deploy, enable production enforcement or submit.

Policy references: [Developer Policies](https://developers.google.com/youtube/terms/developer-policies),
[policy guide](https://developers.google.com/youtube/terms/developer-policies-guide),
[API branding](https://developers.google.com/youtube/terms/branding-guidelines),
[official icon](https://brand.youtube/youtube-icon/),
[official logo](https://brand.youtube/youtube-logo/),
[channel reports](https://developers.google.com/youtube/analytics/channel_reports),
[metrics](https://developers.google.com/youtube/analytics/metrics),
[reports.query](https://developers.google.com/youtube/analytics/reference/reports/query).
