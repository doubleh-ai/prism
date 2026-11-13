# Quality acceptance

This checklist defines the v0.25 quality hardening scope. It describes required
behavior, not a production deployment receipt. DoubleH retains shared accounts;
Prism and Work retain separate business data and navigation.

## Required before promotion

| Area | Acceptance |
| --- | --- |
| Source reading | PDFs larger than the HTML limit reach the bounded PDF parser; PDF, HTML, JSON, framing and time limits remain enforced. |
| Prices | Requests, provider fallbacks and caches preserve an explicit price adjustment. Unknown/mismatched records cannot silently replace the requested series. |
| Traded amount | Missing amount stays unknown, distinct from zero. Aggregated amount requires every constituent bar; indicators and scans cannot fabricate it from volume × close. |
| Research delivery | Progress reads perform no schema initialization or write transaction; slow stream reads do not block the event loop. Cancellation, replay and account isolation remain intact. |
| Unconfirmed writes | Report/terminal persistence failures stay visible and retain task protection. Logs identify the phase and exception class without prompts or exception payloads. Never force success in a `finally` block. |
| Historical execution | Reading or attempting to cancel an old sidecar record cannot contact the retired engine or relabel an unknown task as stopped. |
| Dependencies | Application installation and vulnerability auditing use the same lock; test tools cannot replace application versions. A fresh target-platform install must pass dependency validation. |
| Tests | Behavioral checks execute application code. Retain permission, cancellation, backup/recovery and failure-path protections; remove duplicate implementations, not the protections they were meant to test. |

## Data conventions

K-line requests default to `adjustment=none` (unadjusted). The HTTP K-line API also
accepts `qfq` and `hfq`; unavailable adjusted sources fail explicitly. The direct
Sina daily adapter supports only unadjusted prices. AkShare uses `""`, `qfq`, `hfq`;
BaoStock uses `3`, `2`, `1`, respectively. The cache identity includes the adjustment.
Charts show the recorded convention alongside existing provenance. Historical data
without recorded adjustment is unknown, not retroactively relabeled.

An adjustment label does not establish identical adjustment-factor algorithms or
point-in-time factors across sources. Unadjusted series can jump at corporate
actions; research must not treat those jumps alone as investment signals.
See the [AkShare source](https://github.com/akfamily/akshare/blob/main/akshare/stock/stock_zh_a_sina.py)
and [BaoStock adjustment explanation](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf).

The existing RSI uses a rolling mean of gains/losses, not Wilder smoothing. KDJ
initializes its exponential smoothing from the first available RSV. These are
calculation conventions, not demonstrated predictive accuracy. Changing them
requires separate reference fixtures and compatibility review. Scanner freshness
scores and raw indicator rankings are not win-rate or cross-stock confidence scores.

## Acceptance sequence

1. Reproduce and fix each defect with synthetic fixtures and affected-component tests.
2. Freeze one application commit. Run full regression, fresh Linux dependency install,
   static/security checks and packaging for that exact commit once.
3. In isolated Beta, verify stock source/evidence and saved replay; futures sources,
   missing-data handling and replay; Work proposal, confirmation, calendar reminder
   and reversal. Confirm account boundaries throughout.
4. Promote through the private controller with STOP, a unique writer, task protection,
   encrypted backup and actual recovery validation. Verify served identity and user
   flows before synchronizing the release, master and release documentation.

Model calls require a new bounded permit for the frozen candidate. Passing offline
checks does not establish research quality, Beta availability or production release.
Private execution receipts, feedback text, credentials and instance logs stay outside
this repository.

## Deferred scope

No new product navigation, extra automation framework, mail service, broad rewrite,
large-scale factor evaluation or full provider-units normalization is included here.
Unresolved research quality and external-data availability remain explicit in Beta
acceptance; the twelve synthetic research examples are regression inputs, not a
completed quality benchmark.
