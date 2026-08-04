# Changelog

## 1.3.0 — não publicada

Validada em hardware até a etapa de ciclos start/stop; a publicação aguarda a
recuperação do daemon do robô. O código está completo e a suíte, verde.

### Added

- **Master voice-output switch (`speech_output_enabled`, default `true`).**
  Off, nothing reaches the speaker — not the final answer, not the greeting, not
  what the panel asks for — while text, chat, history, STT, commands, tools and
  movement keep working.

  It is a **new** key rather than a reinterpretation of
  `automatic_speech_enabled`, and that distinction is the whole point: the four
  speech switches that already ship are `false` in production and the robot
  still speaks its final answer. `automatic_speech_enabled` never governed
  output — it governs *automatic* speech. Reusing it would have silenced, on
  upgrade, every robot that answers today.

  Three paths reach the speaker and all three are covered: `falar()`, which
  pushes directly when there is no turn, and the audio coordinator's
  `tocar_ack` — the waiting phrase, which does not go through `falar()` — and
  `tocar_final`. The check in `falar()` runs before the lock, before synthesis,
  before the speaking state and before the mic drain, and reads config per call,
  so the panel switch applies to the next utterance without a restart. The
  coordinator refuses a turn whose policy says output is off, so a future route
  that forgets to ask fails in a test rather than on a robot.

  An utterance already playing finishes; the change applies from the next call.
  Cutting playback would add concurrency immediately after the 1.2.1 thread and
  socket hotfix, and `stop` already exists for that.

- **Wake phrase (`wake_phrase_*`, off by default).** With it on, the robot only
  listens after hearing the phrase; anything said without it is ignored and
  never reaches the chat, the history or the model.

  Matching is by initial token sequence, never `startswith`: `"fala garrafa
  agora"` begins with `"fala garra"` and is not an activation. Normalisation
  exists only to detect — what goes downstream is the **original** text with the
  phrase removed, so `"Fala Garra, qual é o preço do café em São Paulo?"`
  reaches the model with its accents and punctuation intact.

  The idle window renews at the end of the turn, after model, speech and mic
  drain — renewing on acceptance would close the session during the processing
  itself. The session cap counts from activation and never renews. `Stop` is the
  only bypass, recognised before the decision, and it neither opens nor renews a
  session. The echo margin is measured against the real capture timestamp,
  derived from the audio's own duration before STT runs.

  A rejected utterance produces one redacted event: reason, a duration bucket
  rather than milliseconds, session state, and a count of what the rate limit
  suppressed. No transcript, no hash, no fragment.

  The session is two timestamps compared at evaluation time — no thread, no
  timer, no task, created once per voice loop rather than once per turn.

### Fixed

- **One validation path for reading and writing configuration.** `normalizar()`
  and `perfil_atualizado()` each carried their own list of fields, so the wake
  phrase keys took effect through the file and were silently ignored through the
  panel — a custom phrase came back unchanged with a `200`. No unit test over
  `normalizar` could see it; the hardware validation did, on its second
  scenario. Both paths now share one helper, and a test asserts every writable
  key survives a write.

### Changed

- `POST /api/chat/falar` answers **409 `speech_output_disabled`** instead of a
  silent `200`, keeping four distinct outcomes (`invalid_text`,
  `tts_unavailable`, `speech_output_disabled`, `playback_failed`). The requested
  text never enters an error log or event.
- The tool-announcement switch is hidden. No code path announces a tool, so it
  was a visible control that changed nothing. The key stays, off.

## 1.2.1 — 2026-08-03

Hotfix release. Cut from the released `1.2.0`; no feature work.

### Fixed

- **The voice loop no longer leaks a thread and a socket per cycle.** This is
  the root cause of the intermittent `Process exited with code -5`: descriptors
  ran out, GLib could not create a pipe for GWakeup, and `G_BREAKPOINT()`
  raised SIGTRAP. There were **two** leaks, and the second was the larger one.

  - The notification poller was started on every entry to `_laco_voz` but waited
    on the app-wide stop event, so it only ended when the whole app ended. Every
    re-entry left another one running, publishing into an event queue nobody
    read any more and holding its brain's keep-alive session. It now ends with
    the cycle that started it, and the brain's session is returned with it.
  - `_laco_voz` also created a `ThreadPoolExecutor` on every entry and dropped
    it with `shutdown(wait=False, cancel_futures=True)`, which cancels only what
    is still queued — a query already running keeps its thread and its socket.
    A single shared pool replaced it.

  Measured on the robot, with everything idle and one status request per minute:
  before the second fix, `+1` thread and `+1` socket per minute, perfectly
  linear — about sixteen hours from a fresh start to the 1024-descriptor limit.
  A routine gateway restart was enough to start the countdown.
- The retry loop now backs off with jitter instead of re-entering with no wait,
  and the voice cycle is considered stable after 30 s.
- The companion fails loudly at startup when the gateway requires a token and
  none is configured, instead of degrading silently to unauthenticated calls.

### Added

- `resources` in `GET /api/robot/status`: open descriptors, sockets, pipes,
  files, threads and usage against the soft limit, classified `ok` / `warning`
  / `critical`. The app runs on the robot and its `/proc` is not reachable from
  the desktop, so before this the only evidence available from outside was the
  exit code — which is exactly what was not enough last time. Counts only; no
  path, address, peer or inode is exposed.

  This is what found the second leak: the first fix passed every test and every
  degradation scenario on hardware, and the series still climbed by one per
  minute with the robot doing nothing.

## 1.2.0 — 2026-08-03

Validated as `1.2.0-rc.1` on the private Space and on real hardware at `:8047`
(five controlled start/stop cycles, no crash and no stuck state) before
being promoted here.

### Added

- **Agents section (read-only).** The panel shows the factual agent registry
  of the Garra gateway: Garra with its global default, override, provider and
  provider-resolved model as separate facts; Forja as a command adapter that
  is not integrated; Hera as a remote agent with no bridge. Cards are rendered
  entirely from `GET /api/robot/agents`; nothing is cached and a failure
  clears them, so an offline gateway never leaves stale data on screen.
  Capability key: `agent_registry_read_only`.
- Narrow, authenticated companion route (`GET /api/agents`) with a fixed
  upstream, a field allowlist, its own rate limit and `Cache-Control:
  no-store`. The bridge token never reaches the browser.

### Not included

No administration, no toggles, no messaging, no adapters, no voice or
wake-phrase changes. The registry is facts only.

## 1.1.0 — 2026-08-01

Everything here was validated on real hardware in an isolated staging build
before being promoted.

### Added

- **Fast conversation.** The model is consulted immediately; the holding
  phrase became a scheduled, cancellable event with absolute deadlines. A
  question answered in under 4 s gets no phrase at all. Two profiles (`fast`,
  `informative`), a single audio owner with `clear_player()` barge-in,
  per-turn invalidation so a late answer from a superseded turn never speaks,
  and `voice.turn.*` events with metrics.
- **Automatic-speech switches.** A master switch plus waiting-phrases,
  progress-updates, tool-announcement and startup-greeting switches — on both
  panels, persisted with optimistic revisions (`409` on conflict). Off is the
  new default for fresh installs; the acknowledgement decision is reported as
  `disabled`, distinct from `cancelled`.
- **Assistant identity.** Editable assistant name and personality, plus a
  configured-operator field, all stored in the Garra gateway (single source of
  truth; the robot panel edits through an authenticated bridge). The current
  speaker is always `unknown` until reliable identity exists — the robot knows
  who it was configured for without assuming that is who is talking. The
  protected core prompt (tools, safety, privacy) is not panel-editable.
- **Runtime diagnostics.** `GET /api/robot/diagnostics/runtime` (authenticated)
  reports dependency health in the shared venv and whether the panel assets
  made it into the installed build.
- **Identifiable build.** `/api/robot/status` now reports `version`,
  `channel: production` and the exact `commit` stamped at publish time.
- **Clean-session escape.** `POST /api/robot/conversation/session` starts the
  brain on a fresh gateway session — identity changes apply to new
  conversations.

### Fixed

- **Speech onset was being cut.** The voice detector only opened its buffer on
  the first block above the energy threshold, discarding the attack of the
  first word — "Qual é a capital da França?" reached the transcriber as "Ó a
  capital da França." A 0.5 s pre-roll ring now feeds the buffer.
- **Panel assets missing from renamed builds.** `package-data` was keyed on a
  hard-coded package name; the wheel shipped without `static/` and the
  embedded panel showed a bare `Not Found`. The key is now a wildcard, the app
  answers `503` with the cause when assets are missing, and the wheel content
  is guarded by tests.
- **Camera status stuck on "waiting for the first frame".** It was marked once
  at start-up with a 5 s window; it is now re-evaluated in the periodic loop,
  in both directions.
- **Gateway probe unauthenticated.** The readiness probe now sends the Bearer
  token, so it works against any compliant gateway instead of relying on an
  unauthenticated `/ping`.
- **Old-app compatibility.** The desktop console distinguishes "robot offline"
  from "installed app too old" via capabilities and stable error codes, and
  never tries to write against a route that does not exist.

### Security

- Gateway credentials are redacted from the configure response; the bridge
  health endpoint reflects the real gateway, not itself.


## 1.0.0 — 2026-07-31

First public release, and the first that a stranger can install and use.

### Added

- **Control panel at `/reachy`** — status, live camera, head joystick, antennas,
  expression library, dances, quick commands, chat, robot apps and logs.
- **Action layer** — 23 actions behind an allowlist with validated schemas, a
  priority queue with real preemption, a formal state machine
  (`IDLE ⇄ RUNNING → STOPPING → ESTOPPED → RECOVERING`) and a physical envelope
  every angle passes through.
- **Emergency stop** that costs one round-trip to the robot daemon — local, and
  therefore near-instant, when the app runs on the robot; measured 190–640 ms
  from a desktop over Wi-Fi, where the network dominates. It holds the pose,
  never authenticates and never auto-recovers; clearing it and recentring are
  two deliberate steps.
- **Native face tracking** using the SDK's YuNet/ONNX tracker inside the daemon.
- **REST + WebSocket API** with structured real-time events.
- **20 AI tools** over MCP, so a model can drive the robot without ever sending
  an arbitrary pose.
- **Optional voice** — companion speech server in `tools/servidor_voz.py`.
- **`services` block** in `/api/robot/status`, naming every subsystem that is
  down and what to do about it.
- Automatic API token, generated on first run and stored at mode `600`.

### Notes for people arriving from the third-party Face Tracker app

That app is broken in two independent ways: it depends on `mediapipe`, which
fails to load in the robot's `apps_venv`, and it calls `reachy_mini.camera`, an
attribute that does not exist in SDK 1.9 (the camera lives under `.media`). The
second failure is swallowed by a bare `except`, so the robot just sweeps slowly
and says nothing. This app uses the SDK's own tracker instead, which needs no
extra dependency at all.

### Known limitations

- **Chat is not streamed.** The Garra gateway's message endpoint is synchronous
  and its history carries no tool calls. Executed robot actions are shown from
  our own event bus instead, which is more faithful — it is what actually ran.
- **`capture_image` returns text, not an image**, because the gateway's tool
  bridge cannot pass images back to the model.
- **No lip sync** — the robot has no mouth. Audio-reactive head wobbling is the
  substitute.
- **The dashboard's Settings link does not work from another machine.** The
  daemon passes `custom_app_url` through verbatim, and the platform convention
  is `http://0.0.0.0:8042`, which a remote browser resolves to itself. Use
  `http://<robot>:8042/reachy` instead; the app logs the exact URL.
