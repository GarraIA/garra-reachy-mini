---
title: Garra Reachy Mini
emoji: 🦾
colorFrom: red
colorTo: blue
sdk: static
pinned: false
short_description: Control panel, expressions, dances and face tracking
tags:
 - reachy_mini
 - reachy_mini_python_app
---

# Garra Reachy Mini

A complete control panel for Reachy Mini — head control, 85 emotions, 19 dances,
native face tracking, live camera and an emergency stop — plus an **optional** AI
layer that lets the robot listen, talk and move on its own.

**Works the moment you install it.** Everything that only needs the robot works
with no configuration at all. Conversation and voice are opt-in extras that
require services you provide; the panel tells you exactly which ones are missing
and what to do about each. Nothing is sent anywhere until you configure a
provider yourself.

![The control panel running on a Reachy Mini](assets/screenshots/panel.png)

That is a real screenshot of a fresh install with nothing configured: the robot,
its 85 emotions, 19 dances and camera are all there, and the banner names the
two optional pieces that are not, with what to do about each.

## Features

**Available immediately, no setup**

- **Manual control** — joystick for the head, antennas, speed and intensity
  sliders, recentre
- **Expressions** — 9 canonical ones (happy, sad, curious, surprised, confused,
  excited, sleepy, attentive, neutral) resolved against the robot's own emotion
  library, plus all 85 raw moves
- **Dances** — the full 19-move library, played through the daemon so they can
  be interrupted mid-move
- **Native face tracking** — the SDK's own YuNet/ONNX tracker running inside the
  daemon. No `mediapipe`, no extra dependency, no separate venv.
- **Live camera** — MJPEG stream, snapshot, fullscreen, single frame producer
  shared by every consumer
- **Emergency stop** — a fixed button, the `Esc` key and `POST /api/robot/stop`.
  It costs exactly one round-trip to the robot daemon, which is local when the
  app runs on the robot. It holds the pose, never authenticates and never
  auto-recovers.
- **Robot apps** — list, start, stop and restart the other apps installed on your
  robot
- **REST + WebSocket API** — every panel action is a documented endpoint, and
  every state change is a real-time event

**Optional, once you configure them**

- **Conversation** — text chat from the panel, and voice if you run a speech
  server. The model can move the robot through a controlled allowlist of 20
  tools; it can never send an arbitrary pose.
- **Voice** — speech-to-text and text-to-speech through a companion server you
  run yourself (see [Voice](#voice-optional))
- **Ambient behaviour** — subtle head movement while listening, antennas while
  thinking, audio-reactive wobbling while speaking

## Requirements

| | |
|---|---|
| **Required** | A Reachy Mini (wireless or Lite) with daemon 1.9 or newer |
| **Optional — conversation** | An OpenRouter API key, or a [Garra](https://github.com/michelbr84/GarraRUST) gateway on your network |
| **Optional — voice** | A machine with a GPU running `tools/servidor_voz.py` (Whisper + Chatterbox) |

The app itself installs nothing heavy: no GPU libraries, no models, no
`mediapipe`. It runs on the robot's ARM CPU alongside the other apps.

## Install from your Reachy Mini

1. Open the Reachy Mini dashboard and go to **Applications → Discover apps**
2. Find **garra_reachy_mini** and press **Install**
3. Press **Start**

That is the whole installation. The panel comes up immediately and the robot is
controllable straight away.

### Opening the panel

The app serves its panel on port **8042**. From any machine on your network:

```
http://reachy-mini.local:8042/reachy
```

No token, no setup — see [Security](#security) for why, and for how to require
one if you want.

> The **Settings** link the dashboard shows for any app points at
> `http://0.0.0.0:8042`, which in a browser on your laptop means *your laptop*,
> not the robot. That is a platform-wide limitation — the daemon passes the URL
> through without rewriting it — so use the address above instead.

### Start and stop

**Start** launches the app and hands it exclusive ownership of the robot.
**Stop** sends `SIGINT`; the app shuts down its threads, camera and audio and
releases the robot, normally in under two seconds. The daemon then returns the
robot to its neutral pose by itself.

### Uninstall

**Applications → Installed → garra_reachy_mini → Remove.** That runs
`pip uninstall` in the shared apps venv and deletes the app's metadata.

Two things the daemon does *not* clean up, for any app:

```bash
rm -rf ~/.config/garra_reachy_mini          # settings, session state, token, captures
rm -rf ~/.cache/huggingface/hub/spaces--*   # downloaded app snapshots
```

## Configuration

Everything is optional. Set it on the settings page (`http://<robot>:8042/`), or
as an environment variable, or in `~/.config/garra_reachy_mini/.env`. The
settings page wins over the defaults; environment variables win over both.

### Conversation (optional)

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Enables conversation through OpenRouter (fallback brain) |
| `GARRA_MODEL` | `anthropic/claude-haiku-4.5` | Model for the OpenRouter fallback only |
| `GARRA_GATEWAY_URL` | `http://127.0.0.1:3888` | Garra gateway, if you run one |
| `GARRA_GATEWAY_KEY` | — | Bearer token for the gateway |
| `GARRA_USUARIO` | — | Your name, used only by the fallback brains |

With none of these set, the robot still listens and obeys the local shortcuts
("stop", "dance"), but says once that conversation is not configured.

**Which model answers?** When the robot talks through a Garra gateway, the
model is configured **on the gateway**, not in this app — per agent, in the
gateway's own config. If the gateway routes through a meta-router (such as
OpenRouter's `openrouter/auto`), the specific model varies per request and the
gateway does not currently expose which one was chosen, so this app never
claims to know it.

### Conversation pace and automatic speech

Since 1.1.0 a fresh install is **silent while processing**: the robot thinks
visually (antennas, panel state) and speaks only the final answer, however
long the answer takes. Four switches on the panel — master, waiting phrases,
progress updates, tool announcements — plus a startup-greeting switch bring
the old behaviour back per taste. Two pace profiles (`fast`, `informative`)
control *when* a phrase may start once enabled; the switches control *if*.
Settings persist in the robot's own config with optimistic revisions, so two
panels never silently overwrite each other.

### Context listening and notebook (optional, off by default)

Since 1.4.0, with the activation phrase on, two more switches live in the
Conversation card.

**Listen for context** lets the robot sit in a room and make sense of "write
*that* down". What is said *without* the activation phrase is normally dropped;
with this switch on, its **text** is kept in memory for a short window (120 s by
default, 30–300 s) and sent to the model together with the next request. It is
never written to disk, never logged and never put in an event, and it is
forgotten as soon as it falls out of the window or the switch is turned off.
Read the Privacy section before turning it on: this is the switch that lets
room conversation leave the house.

**Notebook** gives the robot lists that survive restarts and new conversations
("put butter on the shopping list", "what is on the shopping list?"). They live
in `caderno.json` next to the app's settings, show up in the panel's Notebook
card, and can be deleted there item by item. The panel is where you check what
was actually written down.

### When the robot does not hear its name (1.4.1)

Speech recognition through the robot's microphone gets the activation phrase
wrong more often than it gets it right — "Fala Garra" comes back as "fala
gabi", "fala garne", "fala gara". Measured with phrases played through a PC's
speakers into the robot: 6 right out of 30. Three things deal with it:

- **Doubled letters are the same word.** "Gara" and "Garra" both activate;
  "fala cara" and "falta garra" still do not.
- **Double-check** (on by default). When what was heard *begins* with something
  within three letters of the phrase, the robot sends the same audio to the
  voice server once more, telling it which phrase to expect, and answers only
  if the phrase then comes out exact. Same 30 phrases: 30 right. The phrase is
  never given as a hint to *every* transcription — that makes noise turn into
  a call.
- **Show what was heard instead of the phrase** (off by default). The panel
  lists the snippet that resembled the phrase — two or three words, never the
  rest of what was said — and what the double-check made of it. It is there to
  answer "why didn't it answer me?".

The speech detector also follows the room now. It used to take one noise
measurement at start-up and keep it; the audio board has automatic gain, so the
noise floor moves by itself, and a floor above that fixed threshold left the
robot listening to three minutes of nothing as if it were one sentence. The
status list in the panel shows what the microphone is measuring.

### Assistant identity (requires a Garra gateway)

The assistant's display name and personality live in the **gateway's**
configuration — the robot panel and the Garra console both edit the same
values through an authenticated bridge, and the robot never stores a second
copy. Three identities are kept deliberately separate:

- **Assistant name** — what it answers when asked who it is (default `Garra`);
- **Configured operator** — who the system was set up *for* (empty by default);
- **Current speaker** — always `unknown` until reliable identity context
  exists. The robot will say who it was configured for, but will not assume
  the person in front of it is that person, and will not address them by the
  operator's name. Someone stating a name is not identification.

The protected core of the agent's prompt (tool rules, safety, privacy) is not
editable from any panel. Identity changes apply to new conversations.

### Voice (optional)

Speech needs a companion server, because speech-to-text and text-to-speech do
not fit on the robot's CPU. `tools/servidor_voz.py` in this repository runs
Whisper and Chatterbox on a GPU machine:

```bash
pip install torch torchaudio faster-whisper chatterbox-tts fastapi uvicorn
python tools/servidor_voz.py --host 0.0.0.0     # bind 0.0.0.0 so the robot reaches it
```

Then point the app at it with `GARRA_VOZ_URL=http://<your-machine>:8123`.

### Network and behaviour

| Variable | Default | Purpose |
|---|---|---|
| `GARRA_REACHY_BIND` | auto | `127.0.0.1` to keep the panel local-only |
| `GARRA_REACHY_TOKEN` | — | Require this token on the API (off by default on the robot) |
| `GARRA_REACHY_PORTA` | `8042`, else 8043–8046 | Panel port |
| `GARRA_ROBO_API` | auto-detected | Robot daemon REST URL |
| `GARRA_COMPORTAMENTO_AMBIENTE` | `true` | Ambient movement during conversation |
| `GARRA_TRACKING_AMBIENTE` | `false` | Look at the user while talking |
| `GARRA_CAMERA_FPS` | `12` | Camera capture rate |

The full list is in [`.env.example`](.env.example).

## Security

**On a wireless Reachy Mini this app is exactly as reachable as your robot
already is.** The robot's own daemon listens on all interfaces on port 8000 with
no authentication at all, so anyone on your network can already move the head
and read the camera through it. Requiring a token on our panel would not change
that, and it would make the panel unusable: the daemon has no endpoint that
shows an app's log output, so there would be nowhere to read the token from.

Two things follow, and both are deliberate:

- **On the robot**, the panel and API are open on the LAN. Set
  `GARRA_REACHY_TOKEN=<something>` to require a token anyway, or
  `GARRA_REACHY_BIND=127.0.0.1` to keep the panel strictly local.
- **Anywhere else** — a desktop driving the robot over the network, or a Lite —
  the default is loopback only, because there the daemon is loopback too and
  opening our API would create exposure that did not exist. Network access there
  needs `GARRA_REACHY_ALLOW_REMOTE=1`, and a token is generated automatically.

Always on, in every mode: `Origin` is validated on anything that changes state,
requests are rate-limited per IP, the gateway key is write-only and masked on
read, and `POST /api/robot/stop` never asks for a token — a panic button that
answers `401` is a worse hazard than the access it would deny.

If your network is not one you trust, put the robot on a segment you do. That
advice applies to the robot as a whole, not just to this app.

## Safety

**This app moves a physical robot.**

- Keep hands, hair and cables clear of the head and antennas while it runs.
- Every movement passes through a fixed envelope — ±45° yaw, ±28° pitch, ±22°
  roll, ±1.5 rad antennas, 0.15–6 s duration — enforced in `robo/limites.py`.
  Requests outside it are clamped, not rejected, and the clamp is reported in
  the response.
- The AI model cannot send arbitrary poses. It picks from an allowlist of named
  actions with validated parameters, and each one is clamped again before it
  reaches the robot.
- The emergency stop cuts the current move and **holds the pose** — it does not
  return to neutral, because returning to neutral is itself a movement.
  Recovering is two deliberate steps: clear the stop, then recentre.
- Motors are re-enabled and the robot returns to zero by the daemon when the app
  stops. Expect a short movement right after you press Stop.

## Privacy

- **Nothing leaves your network unless you configure it to.** With no API key
  and no voice server, the app makes no outbound connections beyond your robot's
  own daemon.
- **With conversation enabled**, what you type or say is sent to the provider you
  chose (OpenRouter, or your own Garra gateway) so it can reply.
- **With voice enabled**, microphone audio is sent to the speech server *you* run
  and pointed the app at.
- **Camera frames stay on the robot.** They are served on your local network for
  the panel and are never uploaded anywhere.
- **No transcripts are written to disk.** Only settings, a session id and the
  API token live in `~/.config/garra_reachy_mini/`. Photos you take with the
  capture button are saved there too, and nowhere else.
- **With "Listen for context" on, room conversation reaches the provider.**
  Every time the robot is called, the text of what it heard in the last window
  (120 s by default) is sent along with the request — to your gateway, which
  keeps it in the session history, and to the model behind it. On the robot
  that text exists only in memory. With the switch off, which is the default,
  nothing said without the activation phrase leaves the robot.
- **With "Show what was heard instead of the phrase" on, a snippet of what was
  said appears in the panel.** Only the two or three words that resembled the
  activation phrase, only in events on your local network, never on disk. It is
  the one place where something said *without* the phrase is shown. Off by
  default.
- **The double-check sends the same audio twice** to the speech server you run.
  Nothing new leaves the house.
- **With "Notebook" on, what you ask the robot to write down is stored** in
  `~/.config/garra_reachy_mini/caderno.json` until you delete it, and is sent
  to the model with every request so it can answer questions about it.
- **Credentials are never returned by the API.** `GET /api/config` masks the
  gateway key; the panel can set it but never read it back.
- **Identity fields reach the model.** The assistant name, operator name and
  personality you configure are part of the agent's context and are sent to
  the AI provider with each conversation — that is what they are for. Do not
  put anything in them you would not send to the provider.
- **No face recognition.** The camera can *detect* a face for tracking, on the
  robot; nothing identifies who it is, and no biometric data is stored.
- **On a wireless robot the panel is reachable by anyone on your network, with
  no token** — the same posture the robot itself already has. See below.

## Architecture

```
Reachy Mini (this app is the single owner of the robot)
├── ControladorRobo ....... priority queue, preemption, e-stop, physical limits
│    └── daemon REST ...... /api/move/goto (cancellable), tracking, apps
├── FrameHub .............. one camera producer, fanned out to MJPEG/snapshot
├── FastAPI :8042 ......... /reachy panel · REST · WebSocket events
└── voice loop (optional) . VAD → speech-to-text → model → speech → speaker
```

One process owns the robot. The panel, the voice shortcuts and the AI tools all
go through the same single-threaded executor, which is what stops two commands
from fighting over the head. Movement goes through the daemon's REST API rather
than the SDK's blocking `goto_target`, because that is the only path that can be
cancelled mid-move — which is what makes the emergency stop real.

Detailed documentation, in Portuguese, is in [`DOCS_REACHY.md`](DOCS_REACHY.md):
endpoints, real-time events, the tool catalogue, how to add new movements, and
troubleshooting.

## Running from source

```bash
git clone https://github.com/GarraIA/garra-reachy-mini
cd garra-reachy-mini
pip install -e ".[dev]"

python -m garra_reachy_mini.main --simulado   # panel with no robot at all
pytest tests -q                               # 191 tests, no hardware needed
```

`--simulado` accepts every action and reports `executed: false` for all of them,
so nothing can quietly pretend a movement happened.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Panel says "limited" | Normal with no AI or voice configured. The banner lists what is missing. |
| Cannot open the panel from another machine | Use `http://<robot>:8042/reachy?token=…`, not the dashboard's Settings link. Check the app log for the URL. |
| `401` on every action | The token is missing from the URL. Open the tokenized URL again. |
| Dances and expressions are empty | The move library is still loading from Hugging Face, or the daemon is unreachable. `catalog_ready` in `/api/robot/status` says which. |
| Robot deaf or mute | Another app is holding the media. Stop it from the dashboard. |
| Port is 8043 instead of 8042 | Something already holds 8042 — on a desktop, usually Pollen's `reachy-mini-control`. |
| App does not stop | It gets `SIGINT` and 20 seconds before `SIGKILL`. If it is stuck, the daemon kills the process tree. |
| Robot no longer says "let me think" | That is the 1.1.0 default: silent while processing. Re-enable the phrases in the panel's conversation card. |
| Identity card says it needs Garra | Assistant name and personality live in the gateway's config; without a reachable gateway they cannot be read or written — nothing is cached or queued. |
| Robot hears but never answers aloud | "Speak responses" is unticked in the Conversation card — since 1.4.1 the card opens with a warning when it is. |
| Robot ignores the activation phrase | Turn on "Show what was heard instead of the phrase" and say it again: the panel shows what the transcription made of it. Keep "Double-check" on. If it is heard as something else entirely, pick a phrase made of common words. |
| Robot deaf for minutes at a time | Fixed in 1.4.1: the speech detector used a noise threshold measured once at start-up. The status list now shows the noise floor it is following. |
| Beginning of sentences was cut | Fixed in 1.1.0 with an audio pre-roll. If it still happens, check microphone levels — a very high noise floor shrinks what the pre-roll can recover. |

**Upgrading from 1.0.x:** your stored settings are preserved. The holding
phrases ("deixa eu pensar") were unconditional code in 1.0.x, not a setting, so
after the upgrade the robot simply stops saying them; turn them back on in the
panel if you miss them.

## Licence

[Apache-2.0](LICENSE). Built on the [Reachy Mini
SDK](https://github.com/pollen-robotics/reachy_mini) by Pollen Robotics.
