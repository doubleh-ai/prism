# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.10.x  | :white_check_mark: |
| 0.9.x   | :white_check_mark: |
| < 0.9   | :x:                |

We follow semver. Security fixes are backported to the current minor
release for at least 6 months.

## Reporting a vulnerability

**Please do not open a public GitHub issue for suspected security
problems.**

Email the maintainers directly at the address listed on the GitHub
profile of the primary maintainer. Include:

- A clear description of the issue and its impact
- Steps to reproduce (or a proof-of-concept)
- The commit / version where you observed it

We aim to acknowledge within 2 business days and ship a fix within
30 days for high-severity issues.

## Security model

Prism is a single-tenant web app: one user database, one SQLite file.
Each user has a bcrypt-hashed password and a session cookie. There is
no concept of "organization" or "team"; if you need multi-user
isolation, deploy one instance per user (or contact us about an
enterprise tier).

### What we protect against

| Threat | Defense |
| --- | --- |
| Password brute force | bcrypt (cost 12) + login rate limit (10/min/IP) |
| Session hijacking | `HttpOnly` + `SameSite=strict` cookies, `secure` flag when over HTTPS |
| CSRF | Double-submit cookie + `X-Requested-With` header check on all state-changing requests |
| Reflected XSS | HTML escaping on server; CSP `default-src 'self'` on responses |
| Stored XSS in AI output | `sanitizeHtml()` strips `<script>`, `on*` handlers, and `javascript:` URLs from marked output |
| Path traversal in report viewer | `Path.resolve()` + `relative_to(reports_dir)` check |
| Large-file DoS | 50 MB cap on report downloads, response size limits |
| Dependency confusion | Pinned versions in `requirements.txt`; CI runs `pip-audit` |
| Information leak via error pages | Global exception handler returns generic 500 with request ID; details only in server logs |

### Operational guidance

- Always deploy behind HTTPS. Set `PRISM_COOKIE_SECURE=1` in production.
- Configure `PRISM_ALLOWED_ORIGINS` to your actual domain(s) for
  Origin-pinned CSRF protection.
- Mount `data/` and `logs/` on persistent volumes (the SQLite DB and
  rotated logs need to survive container restarts).
- Back up `data/secret_key` separately — losing it invalidates all
  sessions and forces re-login.
- Restrict outbound network access for the Claude Code subprocess
  using a process-level firewall or container network policy.

### What we do NOT protect against

- A compromised client (XSS-by-design features or a malicious
  browser extension).
- A malicious user with valid credentials. There is no audit log of
  read access; we log writes only.
- Side-channel attacks on the host OS (Spectre, Rowhammer, etc.).
- Physical access to the server's disk (the SQLite DB and secret key
  are stored in cleartext at rest).
