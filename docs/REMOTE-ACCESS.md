# Remote Access Guide

AgnView is a local-first coordination hub and live multi-agent console. It runs on your own hardware, with no hosted service standing between your agents and you.

---

## 1. LAN, the default

By default the server binds to `127.0.0.1` only, and nothing outside the machine can reach it:

```bash
agnview serve --port 8765
```

To reach it from another device on your own network, opt in explicitly:

```bash
agnview serve --listen-lan --port 8765
```

This binds to `0.0.0.0` and accepts connections from your private LAN (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). Nothing here touches the internet. Nothing external is involved at any point.

---

## 2. Bundled iroh

Remote access beyond your LAN works out of the box, using [iroh](https://www.iroh.computer) embedded in the hub process. There is nothing to install and no account to create.

Check what a client is actually connected over at any time:

```bash
curl http://127.0.0.1:8765/api/transport
```

`resolved_transport` is one of `lan`, `iroh-direct`, `iroh-relay` or `offline`. Turn iroh off entirely with `AGNVIEW_IROH=0`, or with `iroh_enabled: false` in `~/.agnview/config.yaml`.

Over iroh a paired phone gets the live console and the parts of the mobile API it needs: status, Usage, Pipelines, Sessions and prompt dispatch. Every call passes the same pairing key check and rate limit as on the LAN, and only an allowlist of routes answers. `docs/PAIRING.md` section 5 has the protocol. Anyone who holds the pairing key can run the enabled agents on this computer, which amounts to remote code execution. Keep the key private and regenerate it if it leaks. To confine phone dispatches to certain folders, set `iroh_dispatch_roots` in `~/.agnview/config.yaml`. Keep iroh on but serve only the console with `AGNVIEW_IROH_API=0`, or with `iroh_api_enabled: false` in `~/.agnview/config.yaml`.

---

## 3. What this depends on

Remote access depends on n0's public iroh relays, a third-party service AgnView does not operate.

- Public relays are rate limited on throughput and carry no uptime guarantee.
- n0 document the public relays as suitable for development and hobby use.
- n0 support only the latest stable iroh release on the public relays, and can drop older versions at any time, so AgnView needs occasional updates to keep connecting.
- Most traffic is direct and never touches a relay: iroh hole-punches a peer-to-peer connection first, and only falls back to a relay when that fails.
- A relay outage affects remote access only. LAN access keeps working regardless, because it never depends on iroh or on any third party.

---

## 4. Using your own relay

A relay is a server that forwards traffic between two iroh endpoints when they cannot reach each other directly. It never sees the plaintext: QUIC keeps every byte encrypted end to end, and the relay only sees that two endpoints are talking and how much.

You might run your own relay to avoid n0's rate limits, to get an uptime guarantee the public relays do not carry, or to keep relayed traffic on infrastructure you control.

Set `relay_url` in `~/.agnview/config.yaml`, or in the app's relay setting, to your relay's URL. It must be an `http` or `https` URL with a host and no query string. A value AgnView cannot act on stops the iroh transport and says why in the log and at `GET /api/transport`; it never falls back to the bundled relays silently.

```yaml
# Leave empty to use the relays bundled with iroh. They need no account,
# no key and no setup. Set this only to point the hub at a relay you run
# yourself.
relay_url: ""
```

For running your own relay, see [iroh's relay documentation](https://www.iroh.computer/docs).

---

## 5. Overlay network alternatives

If you would rather not depend on iroh's public relays at all, put the hub on an overlay network and bind to it with `--listen-overlay`:

```bash
agnview serve --listen-overlay --port 8765
```

This binds to your machine's address inside `100.64.0.0/10` and advertises only that address. AgnView does not care which of these you use, they are four equal options:

1. **Tailscale.** A Tailnet address falls inside `100.64.0.0/10` and `--listen-overlay` picks it up automatically.
2. **NetBird.** Same range, same flag, same result.
3. **An SSH local port forward.** `ssh -L 8765:localhost:8765 user@host` reaches a hub bound to loopback on the remote machine, no `--listen-overlay` needed.
4. **An existing WireGuard tunnel.** If your WireGuard setup hands out a `100.64.0.0/10` address, `--listen-overlay` works the same as it does for Tailscale or NetBird. A tunnel using a different address range works too; bind AgnView to that address with `--host`.

None of these touch n0's relays. All of them keep traffic on infrastructure you already trust.

---

## Mobile Companion Pairing (iOS / iPadOS)

1. Start the server with `--listen-lan` or `--listen-overlay`, or leave it on LAN only and pair over iroh.
2. Open the web UI at `http://localhost:8765`.
3. Click the companion device pairing button (iPhone/iPad icon in the top header).
4. Scan the rendered QR code with the AgnView companion app on iOS or iPadOS.
5. The companion app connects over LAN, an overlay network, or iroh, in that order, using end-to-end token verification.

---

## Security Policies

- **No Public Bindings**: AgnView refuses to start on unauthenticated public IPs.
- **Token Protection**: Remote API requests require the `X-AgnView-Token` or `Authorization: Bearer <token>` header.
- **Regeneration**: Invalidate all active mobile sessions at any time using the "Regenerate Token" action.
