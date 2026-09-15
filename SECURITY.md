# Security Policy

## Supported versions

Fixes land on `main` and ship in the next release; there are no backports.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
private vulnerability reporting on this repository (repo → **Security** tab
→ **Report a vulnerability**).

This is a spare-time project — please allow up to two weeks for a first
response. Once a fix is out, the advisory is published and you'll be
credited unless you'd rather not be.

## Scope notes

GramVault Atlas is local-first: no accounts, no telemetry, no hosted
component. By default it binds `127.0.0.1` and makes no network calls
except to a localhost Ollama. The most security-relevant surfaces:

- **ZIP import** (`backend/gramvault/ingestion/`) — parsing untrusted
  archive contents (path traversal / zip-slip, decompression bombs).
- **Obsidian export** (`backend/gramvault/export/`) — writing files to a
  user-supplied vault path.
- **Markdown / HTML rendering** in the frontend — caption/transcript text
  from an import is attacker-influenced if a malicious ZIP is imported.
- **The optional Instagram pull** (`backend/gramvault/ingestion/instagram.py`,
  `api/routes_pull.py`) — cookie parsing, the on-disk session file
  (`chmod 600`, never in the DB), and the fact that enabling it makes the
  server talk to a third party. The `sessionid` must never be logged or
  persisted anywhere but the session file.
- **Secrets handling** — `secrets.yaml` (`chmod 600`, gitignored); the
  Models API is write-only and must never echo a key back.
- **The bearer-token auth middleware** (`auth.token`, auto-generated on
  first run) and the cross-site request / Host-allowlist protections —
  bypasses of any of these, or of the localhost-only bind, are in scope.
  "Another app on the same machine can reach the port" is inherent to a
  localhost web app and not a vulnerability by itself.

## Deferred / out of scope

A 2026-09 post-publish audit's findings have all been fixed except the
following, deliberately deferred:

- A DB-backed keyword table for the classifier (needs verification against
  a live LLM eval harness before it can safely replace the current one).
- Configurable Obsidian note templates (risk of breaking the exporter's
  managed-region round-trip logic without full verification).
- An in-app scheduled-pull run summary — the documented cron/systemd-timer
  path already covers scheduling.
- A browser-extension hand-off to replace server-side Firefox cookie
  reading for the Instagram pull — a separate project, not a code change
  in this repo.
- Signed releases / pipx packaging — open-ended release-process work.

Reports in those areas are especially welcome.
