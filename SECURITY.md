# Security Policy

## Reporting a vulnerability

Do not open a public issue containing a suspected vulnerability, credentials or user data. Contact the maintainer privately using the contact listed on the maintainer's GitHub profile. Include the affected commit/version, impact and a minimal reproduction with synthetic data. A report or a fix in source does not establish that production has been updated.

## Application boundary

Prism uses authenticated accounts with owner checks for conversations, reports, research, task status and tool records. Administrators have explicit management permissions. This is application-level isolation on one trusted host, not a separate operating-system tenant for each account.

Passwords use bcrypt, sessions use `HttpOnly` cookies with configurable `Secure` and `SameSite` attributes, and state-changing browser requests require the existing CSRF marker with optional Origin pinning. Configure HTTPS, `PRISM_COOKIE_SECURE=1` and `PRISM_ALLOWED_ORIGINS` for production. Rendered model content is untrusted and must continue through the frontend sanitizer.

The whole application is **CLI-free**: no Claude Code subprocess, external Skill loading or fallback execution path is permitted. Native research uses a bounded direct HTTP model protocol and an explicit in-process tool allowlist. Auxiliary model calls have no tools or alternate backend. Anthropic-compatible Messages is a protocol, not a Claude Code dependency. Source pages and historical research are untrusted data and cannot grant execution authority.

Tools enforce schemas, account/session ownership, budgets, task leases and saved call records. They do not expose arbitrary Shell, SQL, filesystem, administrator or deployment actions. The public-source reader restricts HTTPS hosts, resolved addresses, redirects, response sizes and time. Model credentials belong only in the corresponding provider authentication request; private thinking/signatures must not enter public events or history.

The source disables Vibe new research and follow-up execution with 503; saved history remains owner-scoped. Enabling an external execution engine requires independent isolation and dependency review. Astock is an optional third-party Python dependency and needs its own execution review. Application tool restrictions are not an operating-system sandbox for third-party code.

## History and data

Historical conversation serving reads only platform SQLite storage. The explicit offline importer accepts staged database/history copies, checks ownership and bounded regular files, rejects unsafe links and source conflicts, and retains only public question/answer text with turn counts and provenance. It is not an application-startup migration and must not be pointed at a live production database. Deletion and disabled-account checks remain part of history access.

Credentials, `.env`, private keys, session tokens, SQLite/WAL/SHM, user content, logs, backups and old private Git history stay out of commits, candidate archives and public CI artifacts. Environment-specific operations source and configuration belong in private operations storage; secrets and runtime data stay outside Git even when private. Restrict host access, encrypt and test backups, and protect the configured data directory and secret key.

## Deployment boundary and limits

The administrator release/health pages observe state; they do not authorize or perform production changes. Local readiness and configured model keys do not prove live provider access, research quality, isolation or release completion.

Deployment owners must independently validate Native-only deployment and recovery contracts, exact artifact/dependency identities and database compatibility. Recovery must be separately accepted without CLI dependencies. Contract source and complete metadata do not authenticate receipts or authorize a deployment. Never restore the old CLI to satisfy a release gate. Keep instance-specific records restricted and follow [release-process.md](docs/release-process.md) for stop, drain, backup, recovery and verification requirements.

These controls do not protect against a compromised host, stolen valid credentials, malicious browser extensions or physical disk access. They do not establish exhaustive read auditing or encryption of SQLite at rest. Review deployed configuration and actual receipts for the specific revision being operated.
