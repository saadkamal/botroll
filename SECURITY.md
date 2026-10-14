# Security policy

## Supported versions

BotRoll is pre-1.0. Security fixes go into the latest release and `main`.

## Reporting a vulnerability

Please don't open a public issue. Instead, either:

- use GitHub's **Report a vulnerability** button on the [Security tab](https://github.com/saadkamal/botroll/security), or
- email **hello@botroll.co** with "security" in the subject.

Include what you found, how to reproduce it, and the version or commit. You'll get a reply within 5 working days. Once a fix ships, you're credited in the changelog unless you'd rather not be.

## Scope

In scope: `botroll serve` (sign-in, sessions, CSRF, the push, health and action APIs), the board's HTML escaping, email rendering, action delivery (URL validation, signing) and the Box health collector's redaction and Close/Stop controls.

Out of scope: problems that need an attacker who can already write your bots' roll files *and* sign in to your board, denial of service by volume against a self-hosted server, and the marketing site at botroll.co.

## Hardening your deployment

- Use `BOTROLL_PASSWORD_HASH`, not `BOTROLL_PASSWORD`, and a long random `BOTROLL_SECRET` and `BOTROLL_PUSH_TOKEN`.
- Keep `botroll.env` out of the Docker build folder and out of git.
- Only set `BOTROLL_CLIENT_IP_HEADER` behind a proxy that overwrites that header.
- Leave `BOTROLL_ACTIONS_ALLOW_PRIVATE`, `BOTROLL_ACTIONS_ALLOW_HTTP` and `BOTROLL_HEALTH_CONTROL` off unless you need them.
