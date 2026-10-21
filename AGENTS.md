# Prism development and release operations

Follow the human's current authorization and these repository safety defaults. Carry routine development, isolated validation and draft-PR work through to a reviewable result without asking for approval at every step.

## Current delivery capability

This repository currently provides CI, candidate packaging/verification and a read-only operations console. It does **not** have a production deployment or rollback executor. A successful CI run, candidate artifact, `/health/ready` response, environment label or local `.deploy-revision` alone is not evidence of a production release.

Until a reviewed executor and its recovery checks exist, do not work around that gap with ad-hoc SSH copies, `cp -r`, bootstrap/install scripts, `/tmp` deploy scripts or in-place dependency upgrades. Building this missing capability is development work; declaring it connected before validation is not.

## Required record for every future production change

- Use an independent, user-visible workflow record that remains accessible if Prism is down. Show the exact commit, artifact digest, environment, actor, start/end time and current stage. Never put credentials, raw prompts or runtime databases in public logs/artifacts.
- Promote an immutable, verified candidate that passed checks for that same commit. Serialize production writers; do not automatically cancel an existing production writer to start another.
- Before any write, establish the expected current production revision, preservation of unsynced work, verified encrypted backup/recovery evidence, separate runtime/configuration/data paths, dependency environment, database compatibility and a usable previous version.
- Expose a real operator intervention path. Check cancellation before each irreversible boundary. Cancelling a workflow stops remaining work; it does not prove that a completed cutover was undone. Report the last completed stage, then reconcile the actual state.
- Verify readiness, the exact served revision and core user flows after switching. Check model/data integrations separately; a database probe cannot certify research quality or live data.
- Roll back code/environment only when compatible with the current database. Do not silently restore a database or discard writes after a backup. A destructive data restore needs explicit, scoped human authorization and a stated loss window.
- Mark a release successful only after verification; preserve failure, cancellation and rollback records. Never rewrite them as a successful deployment.

## Development boundaries

Keep production credentials, `.env`, runtime data, logs, backups and old private Git history outside commits and candidate artifacts. Use temporary HOME/data paths and mock external engines for offline tests. Do not point an unverified candidate at the production database: importing the app initializes/migrates data.

Update `docs/release-process.md` when delivery capabilities change. Keep implemented controls and planned controls visibly separate. Prefer the connected GitHub tools for repository changes; attach created PRs to the task. Use the existing `codex/` branch convention.
