# Security policy

## The pairing token grants command execution on the host machine

Read this before you share a pairing QR code, a pairing token, or a screenshot
of either.

AgnView's purpose is to dispatch instructions to the agent CLIs installed on the
machine running the hub. Those agents read and write files and run commands.
The pairing token is the only thing standing between a client on your network
and that capability.

**Anyone who holds the pairing token can run commands on the host machine, as
the user account running the hub, with that account's full access to its files.**

This is not a flaw to be reported. It is what the product does. The token is a
credential of the same weight as an SSH key, and it deserves the same handling:

- Never post a pairing QR code, a screenshot containing one, or a token value
  into a chat, an issue, a pull request, or a support thread.
- Regenerate the pairing token immediately if one is exposed. Regenerating
  invalidates every device already paired.
- Do not run `agnview serve --listen-lan` on a network you do not trust, such as
  café, hotel or conference Wi-Fi. The default binding of `127.0.0.1` keeps the
  hub reachable only from the machine it runs on.
- Do not put the hub behind a public address or a tunnel. AgnView is designed
  for your own local network, and an ephemeral public URL carrying a token is
  not an acceptable transport for it.

## Reporting a vulnerability

Report privately. Do not open a public issue for a security problem.

Use GitHub's private vulnerability reporting on this repository: open the
**Security** tab and choose **Report a vulnerability**. That opens a channel
visible only to the maintainers.

Please include:

- What the problem is, and what an attacker gains from it.
- The steps to reproduce it, and the version or commit you saw it on.
- Your operating system and Python version.
- Anything you have already tried as a workaround.

What to expect:

- An acknowledgement within 7 days.
- An assessment, with whether it is accepted and a rough timeline, within 30
  days.
- Credit in the release notes when a fix ships, unless you would rather not be
  named.

Please give us a reasonable chance to ship a fix before describing the issue
publicly.

## What is in scope

In scope:

- Reaching the hub's API without a valid pairing token.
- Escaping the intended command dispatch boundary, or running commands the
  dashboard does not offer.
- Recovering stored subscription credentials from the database or the API in a
  form that is not masked.
- The hub binding or advertising an address beyond loopback and the private
  ranges, contrary to `docs/PAIRING.md`.
- Cross-site scripting or request forgery reachable from the dashboard.

Out of scope:

- Command execution by a client that legitimately holds the pairing token. That
  is the product's purpose, described above.
- Anything that requires an attacker to already control the host machine or the
  user account running the hub.
- Findings against a deployment that has been placed on a public address, which
  is explicitly unsupported.

## Where credentials live

Subscription credentials are stored in the local SQLite database, by default
under your home directory, and are masked whenever the API returns them. The
pairing token and pair id are generated on first run and stored with owner only
permissions. None of these values belong in the repository, and the commit
history has been checked to confirm none has ever been committed.
