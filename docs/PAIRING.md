# AgnView Pairing Protocol

Status: contract. Both the hub and the iOS app build against this file.
Do not deviate from it.

Current payload version: **v2**. v1 stays valid forever, see section 3.

---

## 1. Connectivity policy

AgnView pairs by QR code. The user creates no account and installs
nothing beyond AgnView itself. AgnView runs on the user's own local
network and nothing else.

Rules that must hold in every build:

1. The hub binds 127.0.0.1 by default. --listen-lan is explicit
   opt-in. If the only available interface is public, the hub refuses
   to start and prints the reason.
2. The hub never advertises a public address. Endpoint enumeration
   keeps loopback, 10.0.0.0/8, 172.16.0.0/12 and 192.168.0.0/16.
   Everything else is dropped.
3. The Cloudflare quick tunnel path is removed. An ephemeral public
   URL carrying a token is not an acceptable transport.
4. The hub also accepts over iroh, a peer-to-peer QUIC endpoint
   embedded in the hub process. It publishes no URL and opens no
   port. A client reaches it only with the node ticket carried in the
   QR pixels, and the pairing token still gates every request. iroh
   needs no account, no daemon and no user setup.

---

## 2. QR payload v1

`
agnview://pair?v=1
  &name=<machine name, url-encoded>
  &lan=<rfc1918 address>:<port>
  &fp=<sha256 of the hub self-signed certificate, hex>
  &id=<pair_id, base64url>
  &k=<key, base64url>
`

- pair_id is 16 bytes from a CSPRNG.
- k is 32 bytes from a CSPRNG. It never leaves the QR pixels and the
  two paired devices.
- fp is the SHA-256 fingerprint (hex) of the hub self-signed HTTPS certificate.
- lan is `127.0.0.1:<port>` when the hub accepts no connections from the local
  network, which is the default. A client treats that value as "no LAN rung"
  and goes straight to iroh. It is never a reachable address from a phone.

Both values are generated on the hub at first run and stored at mode
0600. A Regenerate action invalidates every paired device.

---

## 3. QR payload v2

v2 adds one optional field to v1 and changes nothing else. Every v1
field keeps its name, its meaning and its position in the query string.

`
agnview://pair?v=2
  &name=<machine name, url-encoded>
  &lan=<rfc1918 address>:<port>
  &fp=<sha256 of the hub self-signed certificate, hex>
  &id=<pair_id, base64url>
  &k=<key, base64url>
  &iroh=<iroh node ticket, url-encoded>      # optional, v2 only
`

- `iroh` carries the hub's iroh node ticket. The ticket encodes the
  hub's public endpoint id, its relay hint and its candidate direct
  addresses. It is not a secret in the sense the key is, but it is
  useless on its own: the pairing token still gates every request.
- The ticket is stable across hub restarts. The hub stores its endpoint
  secret at `~/.agnview/iroh_secret`, mode 0600, created on first run.

### Version rules

1. The hub emits `v=1` whenever it has no iroh ticket, byte for byte
   what it emitted before iroh existed. It emits `v=2` only when a
   ticket is present.
2. A client must accept both `v=1` and `v=2`.
3. A client must ignore query fields it does not know. That is what
   makes the next optional field cheap to add.
4. A v1 payload from a device paired before iroh existed keeps working
   unchanged. It pairs over LAN exactly as before and never attempts
   iroh, because it carries no ticket.
5. A v2 payload read by a client that only understands v1 loses the
   iroh rung and keeps the LAN rung. Nothing else changes.

---

## 4. Connection order

Every client, whatever platform, tries the rungs in this order and
stops at the first that answers:

| Order | Rung | Budget |
|---|---|---|
| 1 | `lan` - the `lan` field | 800 ms, then move on. Skip it when the field is `127.0.0.1:<port>` |
| 2 | `iroh-direct` - hole-punched peer to peer | iroh's own timeout |
| 3 | `iroh-relay` - via a relay | iroh's own timeout |

The resolved rung is one of `lan`, `iroh-direct`, `iroh-relay` or
`offline`. The hub reports its own view at `GET /api/transport` and in
the `connected` frame of `GET /api/events`.
