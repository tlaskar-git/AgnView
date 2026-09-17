# Architecture Decision Record: Bundled iroh Transport (ADR-IROH-TRANSPORT)

## Context & Problem Statement

AgnView reaches no further than the LAN. `--listen-lan` covers the same
Wi-Fi, `--listen-tailscale` covers a Tailnet, and both stop at the edge of
a network the user already administers. Anything beyond that has meant a
tunnel with a public URL, which `docs/PAIRING.md` rules out, or a hosted
service, which the product does not have.

The constraint is not reach. It is that reach must cost the user nothing:
no account, no key, no daemon, no port forwarding and no prompt.

## Decision

Embed [iroh](https://www.iroh.computer) in the hub process as a second
transport behind a small `Transport` contract in
`agent_relay/core/network.py`.

- The hub binds an iroh endpoint at startup, derives a node ticket and
  accepts connections on the `agnview/console/1` ALPN.
- iroh hole-punches a direct QUIC connection to the client. When that
  fails it falls back to n0's public relays.
- The ticket rides in the pairing QR code as the optional `iroh` field of
  the v2 payload. The pairing token still gates every request.
- Pin `iroh==1.1.0`, pinned on 17 September 2026.

### Startup is never on the network path

`Transport.start()` is required to return without waiting for the network.
`IrohTransport.start()` schedules the bind as a background task with a
30 second cap and returns. A hub with no route out starts, serves the
dashboard and reports `state: failed` at `GET /api/transport`.

### Configuration

`~/.agnview/config.yaml` carries `relay_url`, empty by default, meaning
the relays bundled with iroh. A non-empty value overrides them and is
validated when the config loads. A value the hub cannot act on stops the
iroh transport, is printed at startup and appears under `config.errors`.
It never degrades silently to the bundled relays.

---

## Invisibility audit

Everything the user would otherwise have had to do, and where it went:

| Would have needed | Where it went |
|---|---|
| An account with a relay provider | None exists. n0's public relays need no identity. |
| An endpoint key | Generated on first run at `~/.agnview/iroh_secret`, mode 0600, like the pairing token beside it. Never shown. |
| A node id to copy between devices | Encoded in the ticket, which rides in the QR pixels. |
| A daemon or a separate binary | iroh runs in the hub process. The wheels carry the compiled library, so no Rust toolchain is needed. |
| A port to forward | None. iroh hole-punches outbound. |
| A relay to choose | Empty `relay_url` means the bundled relays. |
| A transport to pick | The client walks the documented order and reports what it landed on. |

Nothing prompts. Nothing blocks. The hub starts the same way it did
before this change.

## What could not be hidden

Three things stay visible, recorded here rather than papered over:

1. **No macOS Intel wheel.** iroh 1.1.0 ships wheels for
   `win_amd64`, `manylinux_2_28_x86_64`, `manylinux_2_28_aarch64` and
   `macosx_11_0_arm64`. There is no `macosx_*_x86_64` wheel and no sdist.
   iroh is a hard dependency, so on an Intel Mac `pip install agnview`
   now fails outright rather than falling back to LAN only. The fix is
   either an upstream wheel or an environment marker on the dependency.
2. **Relay traffic crosses n0's infrastructure.** When hole-punching
   fails the bytes travel through a public relay. QUIC keeps them
   encrypted end to end and the relay cannot read them, but it does see
   that two endpoints are talking and how much. A user who will not
   accept that sets `relay_url` to their own relay, or
   `iroh_enabled: false`.
3. **A first-run firewall prompt is possible.** The endpoint binds a UDP
   socket, which a desktop firewall may ask about the first time, the
   same way it asks about the existing TCP listener. This is outside
   AgnView's control and was not reproduced on the headless machine this
   was built on.

## Consequences

- A client now has four outcomes to render: `lan`, `iroh-direct`,
  `iroh-relay`, `offline`. `GET /api/transport` and the `connected` frame
  of `GET /api/events` carry the value. No UI is built here.
- The pairing payload is versioned. v1 keeps working unchanged, which is
  a contract, not a courtesy. See `docs/PAIRING.md` section 3.
- `tools/iroh-client.py` is the reference client and the only way to
  exercise the transport without a phone.
