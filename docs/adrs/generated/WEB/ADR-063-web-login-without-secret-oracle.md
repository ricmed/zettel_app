# ADR-063: Web Login Without a Secret Oracle

**Status:** Accepted
**Date:** 2026-10-20
**Issue:** #305 (epic #303)
**Depends on:** [ADR-022: FastAPI Server-Rendered Web Interface](./ADR-022-fastapi-server-rendered-jinja2.md)
**Related to:** [ADR-023: SQLite-Backed Job Queue](./ADR-023-sqlite-backed-job-queue-single-worker.md), [ADR-062: Web Handlers Run in the Threadpool](./ADR-062-web-handlers-run-in-threadpool.md)

## Context and Problem Statement

The web is a single-user instance protected by one shared secret, `SESSION_SECRET`, read from the process environment. The same value was the password and the key of every MAC the web produced:

* `GET /login` sent every visitor a login CSRF token equal to `HMAC-SHA256(SESSION_SECRET, "login")`. A known message and its tag is enough to test candidate secrets offline, at GPU speed, without a single request to the server; no rate limit can see that attack.
* Session cookies were `body.HMAC(SESSION_SECRET, body)`, so a captured cookie was a second oracle for the password.
* There was no limit on failed logins and no revocation: `POST /logout` only cleared the browser's cookie, and a copied cookie stayed valid for 24 hours.
* `hmac.compare_digest` on two `str` accepts ASCII only, so a secret typed with an accent made the login answer 500.

`docs/artigo/06-web.md` §4.2 reproduces all four.

## Decision Drivers

* Nothing a visitor or a cookie thief can see may be a function of the password.
* No user accounts, no new dependency, no external store: one process, one SQLite file.
* A restart must not log the user out (the job queue already survives restarts).

## Decision Outcome

1. **The password is only compared.** `security.password_ok` compares bytes in constant time; any mismatch, including non-ASCII input, is a 401.
2. **Login CSRF is a double submit.** `GET /login` sends a random `secrets.token_urlsafe(32)` in the form and the same value in a `zettel_login` cookie (`HttpOnly`, `SameSite=Strict`, `Path=/login`, 10 minutes). `POST /login` requires both to match. The token involves no key at all.
3. **Sessions are signed with a random key, not with the password.** The key is 256 random bits in the one-row table `web_auth`, created on first use (`StateDB.web_session_key`) and cached by `WebApplication.session_key`. Sessions survive a restart because the key is persisted.
4. **Logout revokes.** `POST /logout` replaces the key (`rotate_web_session_key`), so every cookie signed before it stops validating, on every device. For a single-user instance, "log out" means "log out everywhere".
5. **Failures are throttled.** `LoginThrottle` keeps, per client address and in memory, the count of consecutive failures; from the fifth on, the client is locked for `2^(n-5)` seconds up to 15 minutes and gets 429 with `Retry-After`. A success clears the count. Behind a proxy all clients share one address, which also locks the owner out during an attack; for one user that is the safe side.
6. **Weak secrets are reported.** `credentials.session_secret_warning` flags a secret shorter than 32 characters on `/settings` and in `zettel doctor`. The documentation recommends `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Consequences

* An attacker who reaches the login page learns nothing about the secret; guessing it costs one throttled request per guess.
* `web_auth` is a new table, created by `CREATE TABLE IF NOT EXISTS` on the next start; nothing to migrate.
* Deleting the `web_auth` row (or `zettel init --reset`) ends every session, the same as a logout.
* The `Secure` cookie flag still follows `X-Forwarded-Proto` without a trusted-proxy list; out of scope here.

## Alternatives

* **Derive separate keys from the secret with HKDF.** Rejected: a cookie signed with `HKDF(secret)` is still a tag computable from each candidate secret, so the offline attack survives one extra hash.
* **Sign the login token with a server key.** Unnecessary: the double submit already binds the form to the browser, and an unsigned random value carries no information.
* **A slow password hash (Argon2) of the secret.** Useful when a hash can leak; here the secret lives only in the environment and nothing derived from it is stored or sent.
* **Keep the key in memory only.** Rejected: every restart would log the user out and orphan a bibliographic review bound to the session.

## References

* `zettel/web/security.py` — `password_ok`, `login_token_ok`, `session_value`, `session`, `LoginThrottle`
* `zettel/web/auth.py` — `/login`, `/logout`
* `zettel/state/web.py` — `web_session_key`, `rotate_web_session_key`; `zettel/state/schema.py` — `web_auth`
* `zettel/web_app.py` — `WebApplication.session_key`, `rotate_session_key`
* `zettel/credentials.py` — `session_secret_warning`
* `tests/test_web.py` — login token, cookie key, non-ASCII secret, throttle, revocation, restart
* OWASP ASVS 2.20 (anti-automation): <https://owasp-aasvs4.readthedocs.io/en/latest/requirement-2.20.html>
