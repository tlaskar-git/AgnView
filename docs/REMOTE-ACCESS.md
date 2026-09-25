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

Over iroh a paired phone gets the live console and the parts of the mobile API it needs: status, Usage, Pipelines, Sessions, prompt dispatch and file uploads (section 6). Every call passes the same pairing key check and rate limit as on the LAN, and only an allowlist of routes answers. `docs/PAIRING.md` section 5 has the protocol. Anyone who holds the pairing key can run the enabled agents on this computer, which amounts to remote code execution. Keep the key private and regenerate it if it leaks. To confine phone dispatches to certain folders, set `iroh_dispatch_roots` in `~/.agnview/config.yaml`. Keep iroh on but serve only the console with `AGNVIEW_IROH_API=0`, or with `iroh_api_enabled: false` in `~/.agnview/config.yaml`.

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

## 6. Phone uploads

A paired phone can send files to the hub, so it can attach them to a chat prompt or to a pipeline task. This adds a write path onto this computer's disk. Anyone who holds the pairing key can use it, on the LAN and, with the default settings, over iroh from anywhere. The pairing key already lets that person run the enabled agents here, so uploads widen what the key can do only by letting it store files, within the limits below.

**What a phone can write, and where.** Each upload gets its own folder, `<uploads folder>/<random 128-bit id>/<file name>`. The uploads folder is `uploads` beside the hub database (`~/.agent_relay/uploads` by default), or the folder named by `uploads_dir`. The hub creates its own folders private to the hub's user. It never writes uploads into a project folder or an agent's working folder.

- The name comes from the phone and is reduced to a safe base name. Path parts, drive letters, control and invisible characters, characters Windows refuses, square brackets, leading dots and spaces and trailing dots and spaces are removed. A Windows device name (`CON`, `NUL`, `COM1` and so on) is refused. The name is capped at 180 bytes and keeps its extension.
- The stored file is plain data with no execute permission. The hub never runs it and never reads it, except to hash it. An agent that is given the path can read it, and its content is untrusted like any other file. A file can carry text that tries to instruct the agent.
- Bytes arrive in ordered chunks of at most 1 MiB at an exact offset, and a chunk is stored only when all of it has arrived. A gap, an overlap or bytes past the declared size are refused.
- `files` in a dispatch or a pipeline task that arrives over iroh must be an upload the hub stored, or a project file that `GET /api/system/files` would list, inside the folder the agent runs in. Anything else gets 422 `forbidden_file`. The LAN keeps its old behaviour.

**Limits.** All are set in `~/.agnview/config.yaml`, and a value that is not a whole number above zero stops uploads until it is fixed.

| Setting | Default | Meaning |
|---|---|---|
| `uploads_max_file_bytes` | 2147483648 (2 GiB) | Largest single file |
| `uploads_max_total_bytes` | 10737418240 (10 GiB) | All stored uploads together, counting an unfinished upload at its declared size |
| `uploads_min_free_bytes` | 2147483648 (2 GiB) | Free disk that must remain. An upload that would cross it is refused, and every chunk is checked again |
| `uploads_max_per_peer` | 2 | Unfinished uploads per iroh peer, or per LAN address |
| `uploads_max_concurrent` | 4 | Unfinished uploads across the hub |
| `uploads_idle_expiry_seconds` | 3600 | An unfinished upload with no chunk for this long is deleted |
| `uploads_retention_days` | 14 | A finished upload is deleted after this many days |

Starting uploads is also rate limited: 20 a minute per peer and 60 a minute across the hub. Wrong pairing keys are limited per peer as for every other iroh call, and a valid key is never blocked by another peer's wrong ones. Up to 16 chunks are in memory across the hub at once, 1 MiB each.

**Cleanup.** The hub removes expired and finished-past-retention uploads every five minutes and at start. It deletes only folders named like an upload id directly under the uploads folder. To delete one now, call `DELETE /api/uploads/{upload_id}`, or remove its folder while the hub is stopped.

**Switching off.**

- `uploads_enabled: false` in `~/.agnview/config.yaml`, or `AGNVIEW_UPLOADS=0`, refuses every upload on both transports. A path that an earlier upload returned is no longer accepted in `files`.
- `iroh_uploads_enabled: false`, or `AGNVIEW_IROH_UPLOADS=0`, keeps uploads on the LAN and refuses them over iroh. The hello frame then omits `uploads` from `capabilities`. Uploads over iroh also need iroh API mode, so `iroh_api_enabled: false` turns them off too.

**The trade-off.** Uploads over iroh let a phone attach photos and files to prompts away from home, which the LAN alone cannot do. The cost is that a leaked pairing key can also fill up to `uploads_max_total_bytes` of disk from anywhere until you regenerate the key or switch iroh uploads off. Set `iroh_uploads_enabled: false` if you never need that. Lower `uploads_max_total_bytes` and `uploads_max_file_bytes` to fit the disk. A LAN-only hub with `--listen-lan` off reaches uploads only from this computer.

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
