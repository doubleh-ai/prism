# Prism development and release operations

Follow the human's current authorization and these repository safety defaults. Carry routine development, isolated validation and draft-PR work through to a reviewable result without asking for approval at every step.

## Current delivery capability

This repository provides CI, inert candidate packaging/verification and a read-only operations console. The active application must use Prism Native and direct model HTTP only. Claude Code must not be installed, invoked, required by acceptance or used as a fallback, including auxiliary tasks and recovery candidates. Historical instructions and immutable backups are data, not executable engine configuration.

The older fixed-target controller (`scripts/release_controller.py` with `scripts/remote_release.py`) and its CLI-based probes are retired for Native-only releases. They remain historical source, and must not be run against the current candidate. Keep replacement operational code private in prism-ops. A Native-specific runtime identity/evidence contract and an independently accepted CLI-free recovery candidate are prerequisites for the replacement controller. Actual Linux isolation, candidate acceptance, recovery acceptance and production cutover require their own real receipts. Implementation and mock tests do not mean a deployment has occurred. A successful CI run, candidate artifact, `/health/ready` response, environment label or local `.deploy-revision` alone is not evidence of a production release.

Use only a reviewed Native-only private controller and fixed candidate for authorized release work. Do not bypass a failed gate with ad-hoc SSH copies, `cp -r`, bootstrap/install scripts, `/tmp` deploy scripts or in-place dependency upgrades. Each invocation performs one explicit stage; do not combine inspection with a later production switch. The application UI remains read-only and is not an execution authorization source.

## Required record for every future production change

- Use an independent, user-visible workflow record that remains accessible if Prism is down. Show the exact commit, artifact digest, environment, actor, start/end time and current stage. Never put credentials, raw prompts or runtime databases in public logs/artifacts.
- Promote an immutable, verified candidate that passed checks for that same commit. Serialize production writers; do not automatically cancel an existing production writer to start another.
- Before any write, establish the expected current production revision, preservation of unsynced work, verified encrypted backup/recovery evidence, separate runtime/configuration/data paths, dependency environment, database compatibility and a usable previous version.
- Expose a real operator intervention path. Check cancellation before each irreversible boundary. Cancelling a workflow stops remaining work; it does not prove that a completed cutover was undone. Report the last completed stage, then reconcile the actual state.
- Verify readiness, the exact served revision and core user flows after switching. Check model/data integrations separately; a database probe cannot certify research quality or live data.
- Roll back code/environment only when compatible with the current database. Do not silently restore a database or discard writes after a backup. A destructive data restore needs explicit, scoped human authorization and a stated loss window.
- Mark a release successful only after verification; preserve failure, cancellation and rollback records. Never rewrite them as a successful deployment.
- Honor the independent operator issue and stop flags before every controlled boundary. A closed, unavailable or unknown gate fails closed. After connection loss, reconcile the private journal and actual state; terminating SSH is not proof that a remote step stopped.
- Build with trusted system Python and an isolated non-production UID. Never execute inherited virtual-environment startup hooks as root. Exclude the legacy TLS verification bypass and bytecode caches; bind inherited packages and separately copied external engine source to actual content manifests. A core dependency lock does not cover the entire inherited environment.
- Keep candidate checks on isolated database copies with copied sessions revoked, a private network namespace and bounded resources. Real Linux canary checks must verify process, filesystem and network isolation; flags in a mock command are insufficient.
- Before switching, require the controlled API admission hold, no active tasks, a matching actual current runtime/source fingerprint and an accepted compatible recovery candidate. Never assume the original production directory is a safe recovery target. Program rollback does not restore the database.
- Distinguish internal service verification while admission is held from a completed release. Public `production_verified` is true only after successful verification and resume, with no rollback for that release.

## Development boundaries

Keep production credentials, `.env`, runtime data, logs, backups and old private Git history outside commits and candidate artifacts. Use temporary HOME/data paths and mock external engines for offline tests. Do not point an unverified candidate at the production database: importing the app initializes/migrates data.

Update `docs/release-process.md` when delivery capabilities change. Keep implemented controls and planned controls visibly separate. Prefer the connected GitHub tools for repository changes; attach created PRs to the task. Use the existing `codex/` branch convention.
