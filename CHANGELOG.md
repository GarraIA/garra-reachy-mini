# Changelog

## 1.0.0 — 2026-07-31

First public release, and the first that a stranger can install and use.

### Added

- **Control panel at `/reachy`** — status, live camera, head joystick, antennas,
  expression library, dances, quick commands, chat, robot apps and logs.
- **Action layer** — 23 actions behind an allowlist with validated schemas, a
  priority queue with real preemption, a formal state machine
  (`IDLE ⇄ RUNNING → STOPPING → ESTOPPED → RECOVERING`) and a physical envelope
  every angle passes through.
- **Emergency stop** that cuts the current move in ~90 ms and holds the pose. It
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
