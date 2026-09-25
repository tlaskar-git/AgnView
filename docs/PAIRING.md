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

---

## 5. The iroh protocol

On the `lan` rung a client uses the HTTP API in `docs/mobile-api-spec.json`.
On an iroh rung it connects to the ticket's endpoint with the ALPN
`agnview/console/1` and speaks the protocol below. The reference client is
`tools/iroh-client.py`.

### Framing

- A connection carries one or more bidirectional streams. Each stream
  carries exactly one request.
- The client writes the request as one JSON object followed by a newline,
  then finishes its send side. The request may be at most 64 KiB and must
  arrive within 30 s.
- The hub answers with frames: one JSON object per line. A client must
  ignore keys it does not know.
- Frame types: `hello`, `log`, `ping`, `response` and `error`.

### The hello frame

```json
{"type": "hello", "app": "AgnView", "protocol": 1, "hostname": "<machine name>",
 "transport": "iroh-direct", "capabilities": ["console", "api", "uploads"]}
```

`capabilities` is new and optional. A hub from before API mode omits it,
and a client treats a missing value as `["console"]`. A hub with API mode
switched off sends `["console"]`. `uploads` is listed only when phone uploads
over iroh are on (see "Upload mode" below). A hub with them off sends
`["console", "api"]`, and a client must not try an upload over iroh.

### Console mode

The request has no `op` key:

```json
{"token": "<pairing key>", "agent": "all", "backlog": 200, "after_id": null}
```

The hub sends `hello`, then a `log` frame per console row, oldest first,
then keeps the stream open and sends new rows as they arrive, with a
`ping` frame after 15 s of silence. When the stream ends the hub closes
the connection. This mode is unchanged from the first iroh release.

```json
{"type": "log", "id": 42, "agent": "codex", "source": "stdout",
 "content": "...", "timestamp": "...", "session_id": null}
```

### API mode

The request carries `"op": "api"` and names one call to the mobile API:

```json
{"token": "<pairing key>", "op": "api", "method": "POST",
 "path": "/api/console/dispatch", "body": {"agent": "codex", "prompt": "run the tests"}}
```

- `method` is `GET`, `POST` or `DELETE`, in capitals.
- `path` is the API path, with a query string only where the allowlist
  permits one. `body` is any JSON value for `POST`, and `null` for `GET`.
- The hub sends `hello`, then exactly one `response` or `error` frame, then
  ends the stream. The connection stays open, so the client can open the
  next stream on it without reconnecting.

```json
{"type": "response", "status": 200, "body": {"status": "dispatched", "agent": "codex", "session_id": "..."}}
```

`status` and `body` are exactly what the same call returns over HTTP on the
LAN: the hub hands the request to its own API in-process, with the pairing
key in the `X-AgnView-Token` header, so the same validation, models and
handlers answer it. `body` is the parsed JSON, a string for a non-JSON
response, or `null` for an empty one. A validation failure comes back as a
`response` with status 422, not as an `error` frame.

Allowlist. Everything else is refused with `forbidden_path`:

| Method | Path | Query |
|---|---|---|
| GET | `/api/mobile/status` | none |
| GET | `/api/usage/accounts` | none |
| GET | `/api/jobs` | none |
| GET | `/api/jobs/{id}` | none |
| GET | `/api/console/live-sessions` | none |
| GET | `/api/console/logs` | `agent`, `limit`, `session_id`, `after_id` |
| POST | `/api/console/dispatch` | none |
| POST | `/api/tasks/{id}/request-revision` | none |
| POST | `/api/tasks/{id}/fail` | none |
| POST | `/api/uploads` | none |
| GET | `/api/uploads/{upload_id}` | none |
| POST | `/api/uploads/{upload_id}/finish` | none |
| DELETE | `/api/uploads/{upload_id}` | none |

`{upload_id}` is exactly 32 lowercase hexadecimal characters, the form the hub
mints. Anything else on these routes is refused with `forbidden_path`, and so
is `PUT /api/uploads/{upload_id}`: chunks go in the `upload_chunk` op below.
`GET` and `DELETE` take no body.

`{id}` is one path segment of letters, digits, `_`, `-` and `.`, up to 128
characters, not starting with a dot. A path with `..`, `//`, a backslash,
any percent-encoding, a scheme or a host is refused. So is a query on any
other route, an unknown or repeated query key, and a method the route does
not serve. Pairing, settings and the event stream stay LAN only.

### Upload mode

A phone can send a file to the hub, in ordered chunks, to attach to a prompt
or a pipeline task. Only a hub whose `hello` lists `uploads` accepts them.
The calls that are not chunks use API mode, so the same validation answers
on both transports:

1. `POST /api/uploads` with `{"name", "size", "mime"?}` answers 201 with
   `{"upload_id", "chunk_size", "max_size", "name", "size"}`. `name` is the
   name the hub will store, reduced to a safe base name. A Windows device
   name such as `NUL` is refused with 422 and detail `invalid_name`. `size`
   above `max_size` is refused with 413.
2. Send the bytes with `upload_chunk` requests, described next. `chunk_size`
   is the most one chunk may carry, 1 MiB.
3. `GET /api/uploads/{upload_id}` answers `{"upload_id", "received", "size",
   "state", "name"}`, with `state` `receiving` or `finished`. Use it to
   resume after a dropped connection.
4. `POST /api/uploads/{upload_id}/finish` with an optional `{"sha256"}`
   answers `{"upload_id", "path", "name", "size", "sha256"}`. `path` is the
   absolute path on the hub that `files` in dispatch and in pipeline tasks
   accept. It answers 409 `incomplete` when `received` is below `size`, and
   422 `checksum_mismatch` when the digest differs, and in both cases the
   upload stays open. Calling it again on a finished upload gives the same
   answer.
5. `DELETE /api/uploads/{upload_id}` cancels an upload and deletes what was
   stored.

The `upload_chunk` request is one JSON line, then exactly `length` raw bytes,
then the client finishes its send side:

```
{"token": "<pairing key>", "op": "upload_chunk", "upload_id": "<32 hex>", "offset": 1048576, "length": 1048576}\n
<length raw bytes>
```

- The line is at most 64 KiB and ends at the first newline. `length` is 1 to
  1048576. `offset` is a whole number from 0. No other keys are allowed.
- The hub checks the key from the line alone, before it reads any body byte.
  A wrong key gets `unauthorised` and the body is never read.
- The body must be exactly `length` bytes. A shorter body, a longer body, or a
  body that does not end at the end of the stream is `bad_request`, and
  nothing is stored. The body must arrive within 60 s of the line
  (`timeout`). Nothing is stored until every byte has arrived, so a dropped
  connection leaves the upload as it was.
- The hub answers `hello`, then one `response` frame with `status` 200 and
  `body` `{"upload_id", "received", "size", "state", "name"}`, or one `error`
  frame, and ends the stream. The connection stays open for the next chunk.
- `offset` must equal `received`. Sending the last chunk again (a lost reply)
  is answered 200 and stored once. Any other offset is a `response` with
  status 409 and `body` `{"detail": "offset_mismatch", "received": <n>}`: a
  gap or an overlap. Bytes past `size` are 413 `beyond_declared_size`.
- Other refusals arrive as `response` frames with the status the LAN route
  gives: 404 `not_found`, 409 `not_receiving`, 507 `insufficient_storage`.
  An upload chunk counts as one API call for the per-connection limit of 4
  and the hub-wide limit of 16.

On the LAN the same upload uses `PUT /api/uploads/{upload_id}?offset=N` with
the chunk as the raw request body, and every other call as above.

### Errors

Every error is the existing frame `{"type": "error", "detail": "<code>"}`,
and it ends the request. An error before `hello` also closes the connection.

| Code | When | Before hello |
|---|---|---|
| `unauthorised` | The pairing key is missing or wrong. API mode always needs a key | yes |
| `rate_limited` | Too many wrong keys (see below), or the per-connection or hub-wide API limit is full | yes for keys, no for the API limit |
| `too_large` | The request is over 64 KiB, or the response body is over 1 MiB | yes for the request, no for the response |
| `timeout` | The request did not arrive within 30 s, or the call took over 30 s | yes for the request, no for the call |
| `malformed request` | The request is not a JSON object (unchanged from console mode) | yes |
| `bad_request` | An unknown `op`, a method or path that is not a string, a `GET` or `DELETE` with a body, or an `upload_chunk` line or body that does not match (see Upload mode) | no |
| `forbidden_path` | The call is not on the allowlist, API mode is switched off, or an upload call arrives while uploads over iroh are off | no |
| `upstream_error` | The hub's API failed without answering | no |

### Limits and security

- The key is compared in constant time (`secrets.compare_digest`) and is
  never logged. The hub logs the method, the route template, the status or
  error code and the duration of each API call, and nothing else.
- Anyone who holds the pairing key can run the enabled agents on this
  computer, which amounts to remote code execution. Keep the key private and
  regenerate it if it leaks.
- A dispatch over iroh reaches only the named built-in agents and the enabled
  adapters. Any other `agent` gets status 422 with detail `unknown_agent`, and
  the generic shell runner is never reached. `working_directory` must be an
  existing local folder: a UNC path (`\\host\share`), a device path (`\\?\` or
  `\\.\`) and a NUL character are refused with 422 and detail
  `invalid_working_directory`. Set `iroh_dispatch_roots` in
  `~/.agnview/config.yaml` to a list of folders to confine phone dispatches to
  them. It is empty by default, which allows any existing folder. The LAN is
  not affected.
- The key is checked first, so a valid key is never refused. Wrong keys are
  counted per peer under the peer's iroh endpoint id, in the limiter the LAN
  uses: 10 wrong keys in 60 s from one peer and that peer's further wrong keys
  get `rate_limited` until the window passes. A second cap of 60 wrong keys in
  60 s across all peers covers the fact that a peer id costs nothing to make.
  Neither counts a valid key, and neither touches the LAN's own counters.
- `limit` on the logs route is a whole number from 1 to 1000, `after_id` is a
  whole number, and the console stream backlog is capped at 1000 rows.
- A connection may hold 8 streams at once. At most 4 API calls run per
  connection and 16 across the hub. A call over either limit gets
  `rate_limited` at once rather than waiting. The console stream is not
  counted, so API calls cannot starve it.
- API mode is on whenever iroh is on. The Allow phones on my network switch
  does not govern it: that switch only chooses whether the hub also listens
  on the LAN, and phones reach a hub with it off over iroh. Turn API mode off
  with `iroh_api_enabled: false` in `~/.agnview/config.yaml` or
  `AGNVIEW_IROH_API=0`. The console stays available.
- `files` in a dispatch or a pipeline task that arrives over iroh must be
  either a path returned by `POST /api/uploads/{upload_id}/finish`, or a
  file that `GET /api/system/files` would list, inside the folder the agent
  runs in (or inside an `iroh_dispatch_roots` folder for a task, which has no
  folder). Hidden files and folders, `.git`, `node_modules` and the like, and
  `.db`, `.log`, `.png`, `.jpg` and `.pyc` files are never listed and so are
  refused. Anything else gets status 422 with detail `forbidden_file`. Paths
  are resolved first, so a `..` or a symlink cannot climb out. The LAN is not
  affected.
- Uploads over iroh are on by default. Turn them off with
  `iroh_uploads_enabled: false` in `~/.agnview/config.yaml` or
  `AGNVIEW_IROH_UPLOADS=0`. Turn every upload off with `uploads_enabled:
  false` or `AGNVIEW_UPLOADS=0`. `docs/REMOTE-ACCESS.md` lists the limits.
