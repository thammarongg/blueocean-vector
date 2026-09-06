# What an MCP server is allowed to initiate

Research note for [issue #3](https://github.com/thammarongg/blueocean-vector/issues/3), a
ticket on the map in [issue #1](https://github.com/thammarongg/blueocean-vector/issues/1)
("End-of-session memory reliability"). Planning only — nothing here was implemented.

**Question.** Within the MCP protocol itself, what can a *server* initiate toward a
client, and does any of it give a server a way to act at the moment a session ends?

**Sources.** The `modelcontextprotocol.io` specification revisions (each claim is
attributed to a specific revision), the Python SDK actually installed in this repo's
virtualenv (`mcp` / `mcp_types` 2.0.0, at
`/Users/thammarongg/Projects/Repo/blueocean-vector/.venv/lib/python3.12/site-packages/`),
and the TypeScript SDK source on `main`. Where the specification permits something the
SDK does not surface, that is called out separately.

**Date of research:** 2026-09-06.

---

## 0. The headline: a spec revision landed after the map's premises were written

The map's notes date from August 2026, and the deferral they correct is dated 2026-08-18.
The protocol has since shipped the revision labelled **2026-07-28** (the label is a
spec-freeze date; the publication date was not verified here), and it is not an
incremental change: it removes sessions from the protocol. Nothing in the map's notes
reflects it, so it is worth stating first.

The five released revisions, from the registry the installed SDK ships
(`.venv/lib/python3.12/site-packages/mcp_types/version.py:25-43`):

| Revision | Era |
| --- | --- |
| `2024-11-05` | handshake |
| `2025-03-26` | handshake |
| `2025-06-18` | handshake |
| `2025-11-25` | handshake |
| `2026-07-28` | **modern** — "stateless per-request envelope" |

`HANDSHAKE_PROTOCOL_VERSIONS` covers the first four; `MODERN_PROTOCOL_VERSIONS` is
`("2026-07-28",)` (`mcp_types/version.py:32-42`).

The changelog states the three changes that matter most here
([2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
"Major changes" 1, 2 and 7), quoted verbatim:

> 1. Remove protocol-level sessions and the `Mcp-Session-Id` header from the Streamable
>    HTTP transport. List endpoints (`tools/list`, `resources/list`, `prompts/list`) no
>    longer vary per-connection. Servers that need cross-call state use explicit,
>    server-minted handles passed as ordinary tool arguments (SEP-2567).

> 2. Make MCP stateless: remove the `initialize`/`notifications/initialized` handshake.
>    Every request now carries its protocol version and client capabilities in `_meta`
>    (`io.modelcontextprotocol/protocolVersion`,
>    `io.modelcontextprotocol/clientCapabilities`). [...] (SEP-2575)

> 7. Multi Round-Trip Requests (MRTR) pattern introduced which replaces the previous
>    approach of sending server-initiated requests, such as `roots/list`,
>    `sampling/createMessage`, or `elicitation/create`. Servers return an
>    `InputRequiredResult` (`resultType: "input_required"`) whose `inputRequests` field
>    carries the requests for the additional information needed to process the request.
>    Clients respond with `inputResponses` on a retry of the original request providing
>    the requested information. (SEP-2322)

The same changelog deprecates Roots, Sampling and Logging outright:

> 1. Deprecate the Roots, Sampling, and Logging features (SEP-2577). These features
>    remain fully functional during the deprecation window but new implementations should
>    not add support for them.

So the protocol has moved *away* from server-initiated anything, not toward it.

**Which era this repo is actually in — the important qualifier.** The SDK pinned here
(`mcp>=2.0.0,<3.0.0`, `pyproject.toml:26`) supports `2026-07-28` *alongside* the four
handshake revisions and negotiates per client, so different clients get different eras
from the same running server. Today's live traffic is still handshake-era: the map's
telemetry populates `session_id` from the `mcp-session-id` header
(`src/blueocean_mcp/telemetry/instrument.py:105-128`), and a `2026-07-28` client never
sends that header, so a populated column proves the four measured clients are all
pre-modern. `2026-07-28` therefore matters here as three things — the direction of
travel, the ceiling any protocol-level design must respect, and a silent break in the
session dimension the moment any one client upgrades — and not as a description of what
is on the wire today.

---

## 1. What server-to-client messages exist, and which a client must support

### Handshake era (`2024-11-05` … `2025-11-25`)

Server-to-client **requests** — three, all gated on a client-declared capability:

| Method | Capability the client must declare |
| --- | --- |
| `sampling/createMessage` | `sampling` |
| `elicitation/create` | `elicitation` |
| `roots/list` | `roots` |

Plus `ping` (either direction). The capability table in
[2025-11-25 lifecycle → Capability Negotiation](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#capability-negotiation)
lists `roots`, `sampling`, `elicitation`, `tasks`, `experimental` as *client* capabilities
and introduces them with:

> Client and server capabilities establish which **optional** protocol features will be
> available during the session.

The sampling page is explicit that support is opt-in and that the client stays in
control ([2025-11-25 client/sampling](https://modelcontextprotocol.io/specification/2025-11-25/client/sampling)):

> Clients that support sampling **MUST** declare the `sampling` capability during
> initialization

> For trust & safety and security, there **SHOULD** always be a human in the loop with
> the ability to deny sampling requests.

and the defined error for refusal is `-1`, "User rejected sampling request". There is no
MUST anywhere requiring a client to implement sampling, elicitation or roots. Both parties
**MUST** "Only use capabilities that were successfully negotiated"
([2025-11-25 lifecycle → Operation](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#operation)).

Server-to-client **notifications** in `2025-11-25`, taken from the installed schema
(`mcp_types/_v2025_11_25/__init__.py`): `notifications/message` (logging),
`notifications/progress`, `notifications/cancelled`, `notifications/resources/updated`,
`notifications/{tools,prompts,resources}/list_changed`, `notifications/tasks/status`,
`notifications/elicitation/complete`. None of them is a general-purpose "do this now"
channel: `list_changed` and `resources/updated` say a catalogue changed, `message` is a log
line the client asked for via `logging/setLevel`, `progress` is scoped to an in-flight
request.

**There is no notification that carries prose to the agent.** That is the shape of the
gap: the only prose channel to the model is `instructions`, and it is delivered exactly
once (§2).

### Modern era (`2026-07-28`)

Server-to-client **requests: none** — `ping` was removed too (changelog, major change 5:
"Remove `ping`, `logging/setLevel`, and `notifications/roots/list_changed`"), so the
category is empty rather than merely reduced. The streamable-HTTP transport page
([2026-07-28 → Receiving Messages](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#receiving-messages)):

> The server **MUST NOT** send independent JSON-RPC *requests* on this stream.
> Server-to-client interactions (sampling, elicitation, list-roots) are embedded as input
> requests inside an `InputRequiredResult` per MRTR (SEP-2322), not delivered as separate
> requests on this or any other stream. This is a change from Streamable HTTP in protocol
> versions `2025-03-26` through `2025-11-25`, where servers could send such requests on
> SSE streams.

The stdio page for the same revision
([2026-07-28 → stdio → Receiving Messages](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)):

> The server **MUST NOT** write JSON-RPC *requests* to `stdout`. Server-to-client
> interactions are carried in `InputRequiredResult` replies.

And the payload of an `InputRequiredResult` is a *closed set of three*
(`mcp_types/_v2026_07_28/__init__.py:3628`):

```python
class InputRequest(RootModel[CreateMessageRequest | ListRootsRequest | ElicitRequest]):
```

Server-to-client **notifications**: still exist, but now strictly opt-in per type.
`resources/subscribe`/`unsubscribe` and the standalone HTTP GET stream are gone,
replaced by `subscriptions/listen`, whose filter object carries this rule
(`mcp_types/_v2026_07_28/__init__.py:862-869`):

> Each notification type is **opt-in**; the server **MUST NOT** send notification types
> the client has not explicitly requested here.

The opt-in set is exactly `promptsListChanged`, `resourcesListChanged`,
`toolsListChanged`, `resourceSubscriptions` (same file, lines 874-890). `logging/setLevel`
was removed; logging is now per-request and opt-in
(`mcp_types/_v2026_07_28/__init__.py:3501-3510`):

> If absent, the server **MUST NOT** send any `notifications/message` notifications for
> this request. The client opts in to log messages by explicitly setting a level.
> Replaces the former `logging/setLevel` RPC.

**Implementation check.** The installed Python SDK models the absence of a back channel
as a first-class error (`mcp/shared/exceptions.py:55-62`):

> `NoBackChannelError` — Raised when a server-initiated request has no channel that can
> deliver it.

raised from the modern HTTP path at `mcp/server/_streamable_http_modern.py:102` and from
`mcp/server/runner.py:593`. Server code that tries to initiate under `2026-07-28` gets an
exception, not a message on the wire.

---

## 2. Can `instructions` be updated after the handshake?

**Handshake era: no.** `instructions` appears in exactly one place in the whole
`2025-11-25` schema — `InitializeResult`
(`mcp_types/_v2025_11_25/__init__.py:2002` is the only hit for the string `instructions`
in that file). It is shown in the `initialize` response example in
[2025-11-25 lifecycle → Initialization](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization):

```json
"instructions": "Optional instructions for the client"
```

There is no `notifications/instructions_changed`, no `instructions/get`, and no way to
re-run `initialize` without starting a new session (the spec only mandates re-initialising
after an HTTP 404 — §3). So: **fixed for the life of the session, by omission rather than
by an explicit prohibition.** Nothing in the spec text says "servers MUST NOT change
instructions"; there simply is no message that would carry a change.

**Modern era: it becomes re-fetchable.** In `2026-07-28`, `instructions` moves to
`DiscoverResult`, the reply to `server/discover`
(`mcp_types/_v2026_07_28/__init__.py:3139-3199`). `DiscoverRequest` says:

> Servers **MUST** implement `server/discover`. Clients **MAY** call it but are not
> required to — version negotiation can also happen inline via per-request `_meta`.

`DiscoverResult` carries a `ttlMs` freshness hint and a `cacheScope`
(same file, lines 3186-3199):

> A hint from the server indicating how long (in milliseconds) the client **MAY** cache
> this response before re-fetching. [...] If 0, the response **SHOULD** be considered
> immediately stale; the client **MAY** re-fetch every time the result is needed.

So under `2026-07-28` a server *can* return different `instructions` on successive
`server/discover` calls, and a low `ttlMs` invites the client to re-fetch. But: calling it
is `MAY`, re-fetching on expiry is `MAY`, and there is still no way for the server to
*cause* a re-fetch. It is a pull with a hint, not a push.

**Implementation check.** In the installed Python SDK, `instructions` is a plain mutable
attribute (`mcp/server/lowlevel/server.py:420`) read at response-construction time in both
`create_initialization_options` (line 550) and `_handle_discover` (line 674). So mutating
`server.instructions` at runtime *would* affect later `server/discover` replies — but it
cannot affect a handshake-era client that already initialised. This repo sets it once at
construction (`src/blueocean_mcp/server.py:27-45`).

---

## 3. Does a server learn that a session ended?

### Streamable HTTP, handshake era (`2025-03-26` … `2025-11-25`)

The relevant clauses are identical in `2025-06-18` and `2025-11-25`
([2025-11-25 transports → Session Management](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#session-management)):

> 3. The server **MAY** terminate the session at any time, after which it **MUST**
>    respond to requests containing that session ID with HTTP 404 Not Found.
> 4. When a client receives HTTP 404 in response to a request containing an
>    `MCP-Session-Id`, it **MUST** start a new session by sending a new
>    `InitializeRequest` without a session ID attached.
> 5. Clients that no longer need a particular session (e.g., because the user is leaving
>    the client application) **SHOULD** send an HTTP DELETE to the MCP endpoint with the
>    `MCP-Session-Id` header, to explicitly terminate the session.
>    * The server **MAY** respond to this request with HTTP 405 Method Not Allowed,
>      indicating that the server does not allow clients to terminate sessions.

So the server *can* learn a session ended — but only if the client politely sends DELETE,
which is **SHOULD**, not MUST. Nothing detects a client that simply stops talking. The
only server-side signal that always works is the server's own timeout, which the spec
sanctions ("the server **MAY** terminate the session at any time") but does not define.

### Streamable HTTP, modern era (`2026-07-28`)

There is no session to end. From
[2026-07-28 → Streamable HTTP → Earlier Streamable HTTP Revisions](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#earlier-streamable-http-revisions):

> Protocol versions `2025-03-26` through `2025-11-25` also used the Streamable HTTP
> transport, but in a different shape: servers could assign a session via the
> `Mcp-Session-Id` header (terminated with HTTP DELETE) [...] **None of these mechanisms
> are part of this revision.**

and a server speaking only this revision is told to reject the old signals:

> * HTTP GET or DELETE to the MCP endpoint: respond with `405 Method Not Allowed`.
> * An `Mcp-Session-Id` header on a request: ignore it, and do not mint or echo session IDs.

Per-request capabilities are explicitly non-inheritable
(`mcp_types/_v2026_07_28/__init__.py:3481-3483`):

> Capabilities are declared per-request rather than once at initialization; an empty
> object means the client supports no optional capabilities. **Servers MUST NOT infer
> capabilities from prior requests.**

The only long-lived object left is a `subscriptions/listen` stream, and it is a
notification pipe, not a session: closing it tells the server a *stream* ended, not that
the agent finished working.

### stdio

Both eras: the server learns only by EOF on stdin, and only if the client shuts down
cleanly. `2025-11-25`
([lifecycle → Shutdown](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#shutdown)):

> During the shutdown phase, one side (usually the client) cleanly terminates the protocol
> connection. **No specific shutdown messages are defined** — instead, the underlying
> transport mechanism should be used to signal connection termination.
>
> For the stdio transport, the client **SHOULD** initiate shutdown by:
> 1. First, closing the input stream to the child process (the server)
> 2. Waiting for the server to exit, or sending `SIGTERM` if the server does not exit
>    within a reasonable time
> 3. Sending `SIGKILL` if the server does not exit within a reasonable time after `SIGTERM`

`2026-07-28` keeps this and adds a note that makes the ceiling plain
([2026-07-28 → stdio → Shutdown / Unexpected Termination](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)):

> Servers **SHOULD** exit promptly when their standard input is closed or reads return
> end-of-file. This is the primary graceful-shutdown signal and the only portable one

> If the server process exits unexpectedly, the client **SHOULD** restart it. Because the
> protocol is stateless, any in-flight requests are simply lost

So on stdio a server gets one notification of the end — EOF — and by the time it arrives
the client has already stopped reading its stdout. Anything the server wants to *say* at
that point has nowhere to go. A `SIGKILL` (step 3) delivers nothing at all, and neither
does a crashed or force-quit client, which is exactly the "abnormal endings" bucket the
map lists as unspecified.

### What the SDKs actually implement — the three findings are different

1. **Spec permits the server to notice DELETE / expiry** (handshake era only).
2. **Python SDK does not surface it to server code.** `StreamableHTTPSessionManager` has
   a `session_idle_timeout` parameter
   (`mcp/server/streamable_http_manager.py:68-72, 85`) — "sessions that receive no HTTP
   requests for this duration will be automatically terminated and removed [...] A value
   of 1800 (30 minutes) is recommended" — but when it fires, the handler simply logs
   `Session {id} idle timeout`, pops the transport from a dict and calls
   `http_transport.terminate()` (lines 316-337). There is **no callback, hook or event**
   for application code. The same is true of the DELETE path
   (`mcp/server/streamable_http.py:785-813`). A grep for `on_disconnect|on_session_end|
   session_end|on_terminate` across `mcp/server/` returns nothing. The per-session task is
   driven by `serve_loop` rather than `Server.run()` specifically so that "the manager's
   already-entered lifespan is reused rather than re-entered per session"
   (`mcp/server/streamable_http_manager.py:319-321`) — so there is no per-session lifespan
   teardown to hang work off either.
3. **TypeScript SDK does surface it, for DELETE only.**
   `packages/server/src/server/streamableHttp.ts:101-111` on `main`:

   > A callback for session close events. This is called when the server closes a session
   > due to a `DELETE` request.

   fired at line 1009 inside `handleDeleteRequest`. There is no equivalent for idle
   expiry: a grep of that file for `idle`, `timeout`, `expir` and `reap` returns nothing,
   so the TS SDK has no session reaper at all — the reverse asymmetry from the Python SDK,
   which has the reaper but no callback. A fourth hook, `transport.onclose`
   (declared line 272, fired at line 1087 in `close()`), fires on *any* transport close
   including the `finally` of the DELETE path — but a transport close is not a session
   end; the file's own comment on `onsessionclosed` notes you may want to "close each
   transport after a request is completed while still keeping the session open/running".

4. **This repo would not receive either signal today.** `session_idle_timeout` is not a
   parameter of `Server.streamable_http_app()`
   (`mcp/server/lowlevel/server.py:720-754` — the manager is constructed there without it),
   and this repo calls `mcp.streamable_http_app(host=host)`
   (`src/blueocean_mcp/__main__.py`). So HTTP sessions here are never reaped by the SDK's
   timer, and nothing reports DELETE.

One consequence for the telemetry the map relies on: `_agent_identity` derives
`session_id` from the `mcp-session-id` header, falling back to a per-process id
(`src/blueocean_mcp/telemetry/instrument.py:105-128`). Under `2026-07-28` that header no
longer exists, so every HTTP call from every client would collapse into the single
process-id bucket. The measured 74-calls/19-manifests/9-summaries denominator is a
handshake-era measurement, and its session dimension does not survive the transition.

---

## 4. Can a server prompt a client to act unprompted?

**No, in every revision — and progressively less so.**

- Handshake era: the three server-initiated requests
  (`sampling/createMessage`, `elicitation/create`, `roots/list`) are the closest thing,
  and none of them is "call this tool". Sampling asks the client's *model* for a
  completion; elicitation asks the *user* for structured input; roots asks for a list of
  directories. The reply comes back to the server; nothing is injected into the agent's
  own turn, and each requires a capability the client chose to declare, with a human
  **SHOULD** be in the loop able to deny it. Delivery also depends on the client having
  opened the standalone SSE stream: "The client **MAY** issue an HTTP GET to the MCP
  endpoint"
  ([2025-11-25 transports → Listening for Messages from the Server](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#listening-for-messages-from-the-server))
  — MAY, not MUST, and a server that offers no such stream returns 405.
- Modern era: those three still exist but can only be *returned as part of an answer* to a
  request the client already made (MRTR), and the union is closed to those three
  (`mcp_types/_v2026_07_28/__init__.py:3628`). Notifications are opt-in per type and
  limited to catalogue changes. Attempting a server-initiated request raises
  `NoBackChannelError`.

There has never been, in any revision, a message that means "agent, please call tool X".
The nearest thing to an imperative the protocol offers is `instructions`, and that is
prose delivered once, before the agent has done anything.

---

## What this means for the map

**A protocol-level mechanism for acting at session end does not exist.** Not as an
oversight to be worked around, but as a direction the protocol is actively moving away
from. Concretely, on the map's three alternatives:

- **Nudge** (what exists today). The `instructions` field really is the cross-tool channel
  the map's corrected premise says it is — but it is a single delivery at the *start* of
  the session, cannot be re-sent (handshake era), and has no companion message that could
  reinforce it later. Under `2026-07-28` it becomes re-fetchable via `server/discover`
  with a `ttlMs` hint, which is a genuine improvement, but the server still cannot trigger
  the re-fetch and the client is only ever `MAY`-bound to call it. If the existing
  instruction fails for reasons of *structure* rather than wording, the protocol offers
  nothing better to replace it with. Rewriting the text is the only lever `instructions`
  gives.

- **Client guarantee** (a per-tool lifecycle hook). The protocol will not supply this and
  is removing the pieces that would have approximated it. A mechanism built on
  sampling/elicitation would be building on features the `2026-07-28` changelog deprecates
  by name, with a twelve-month removal window already running. This is worth stating
  plainly on the map: any design that depends on the server initiating anything has a
  known expiry date.

- **Server synthesis** (the server writes the summary itself). This is the only one of the
  three that survives contact with the protocol, precisely because it asks nothing of the
  agent. But note what it loses: the map's provisional definition of session end —
  server-side idle timeout — is a *server implementation* choice, not a protocol event.
  The spec sanctions it ("The server **MAY** terminate the session at any time"), the
  Python SDK implements it (`session_idle_timeout`), and this repo does not currently
  enable it and could not act on it if it did, because the SDK exposes no hook. Under
  `2026-07-28` there is no session to time out at all, so an idle timer would have to be
  keyed to something the server mints itself — which is exactly what the changelog tells
  servers to do: "Servers that need cross-call state use explicit, server-minted handles
  passed as ordinary tool arguments."

**The honest ceiling.** A server can *observe* an ending, imperfectly and only in some
transports and revisions: DELETE (client's choice, handshake era only), stdin EOF (only on
a clean stdio shutdown), or its own idle timer (always available, but a guess). A server
can **never act through the agent** at that moment — by the time any of those signals
arrives there is no channel back to the model, and in the modern revision there was never
one to begin with. Observation exists; the channel to the agent does not survive past the
moment it would be needed.

That makes the choice on the map a real one between "improve the nudge, accept it is
best-effort" and "synthesise server-side from what was observed", with no third option
where a guarantee is obtainable at the protocol level. It also means the map's
"no cross-tool primitive exists" premise from 2026-08-18 was closer to right than the
correction allowed: `instructions` is a cross-tool channel, but it is a *start-of-session*
channel, and the thing being asked for is an end-of-session one.

**Two things the map should absorb regardless of which branch it takes.**

1. The installed SDK already supports `2026-07-28`, and that revision removes sessions.
   Live traffic is still handshake-era (§0), so nothing is broken today — but any
   decision framed around `mcp-session-id` is scoped to the handshake era and will need a
   server-minted equivalent the first time a client upgrades. That upgrade will be
   silent: no error, just a `session_id` column that stops distinguishing anything.
2. The telemetry denominator behind "74 calls, 19 manifests, 9 summaries" depends on the
   `mcp-session-id` header (`src/blueocean_mcp/telemetry/instrument.py:105-128`). That
   dimension does not exist under `2026-07-28`. This belongs to the sibling denominator
   ticket, but it originates here.

---

## Source index

**Specification**
- [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [2026-07-28 Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [2026-07-28 stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
- [2025-11-25 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
- [2025-11-25 transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [2025-11-25 client/sampling](https://modelcontextprotocol.io/specification/2025-11-25/client/sampling)
- [2025-06-18 transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

**Installed Python SDK** (`mcp` / `mcp_types` 2.0.0), all under
`/Users/thammarongg/Projects/Repo/blueocean-vector/.venv/lib/python3.12/site-packages/`
- `mcp_types/version.py`
- `mcp_types/_v2025_11_25/__init__.py`
- `mcp_types/_v2026_07_28/__init__.py`
- `mcp/server/lowlevel/server.py`
- `mcp/server/streamable_http_manager.py`
- `mcp/server/streamable_http.py`
- `mcp/server/_streamable_http_modern.py`
- `mcp/shared/exceptions.py`

**TypeScript SDK** (`main`)
- [`packages/server/src/server/streamableHttp.ts`](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/packages/server/src/server/streamableHttp.ts)

**This repo**
- `src/blueocean_mcp/server.py`
- `src/blueocean_mcp/__main__.py`
- `src/blueocean_mcp/telemetry/instrument.py`
- `pyproject.toml`
