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

**New in 1.5.0**

- **Choose who answers** — Hermes (default), Garra, Pi, Claude, a local Ollama
  model, or any agent you add. The robot's name, the wake phrase and the agent
  are three independent settings: you can say "Fala Garra" and have Hermes
  think (see [AI agents](#ai-agents-150))
- **A robot that explains itself** — one status pill on every screen: waiting
  for the wake phrase, listening, transcribing, thinking, speaking, standby
  (with the reason), error
- **Voice settings in the panel** — several wake phrases, a beep and an antenna
  gesture when it hears you, microphone sensitivity, language, voice, speed and
  volume, and a listening test that shows what the robot understood (see
  [Voice activation](#voice-activation-150))
- **Self-healing** — the companion re-points the robot when your computer's
  address changes and starts the app again after the robot reboots

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
- **Vision on demand (1.4.1)** — "what do you see?" describes one fresh camera
  frame through a vision model on your computer; local by default, cloud only
  by explicit opt-in (see [Vision](#vision-141-off-by-default))
- **Home automation through Garra (1.4.1)** — connect Home Assistant to the
  gateway from the panel; the gateway's own device registry and risk gates do
  the work, and voice control of devices is opt-in
- **Spotify (1.4.1)** — "toca Hotel California": with spotatui on the
  computer, the robot plays, checks the player really started and confirms by
  voice; pause, resume, next, previous, volume, queue and playlists too. Voice
  control is an owner opt-in, off by default (Spotify's policy, stated in the
  panel); the panel works with or without it (see [Music](#music-spotify-141))
- **Family recognition (1.4.1)** — consent-based, local, encrypted, deletable;
  "who is there?" answers with enrolled names or "unknown", and never uses a
  face as authorization (see [Family](#family-recognition-141-off-by-default))
- **Five screens** — HOME · CHAT · CONTROL (Robot · Home · Music) · APPS ·
  SETTINGS, with a bottom bar on phones, the STOP button always in view, 320 px
  without horizontal scrolling and 44 px touch targets

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
  within three letters of the phrase **and the transcription itself was unsure
  of it**, the robot sends the same audio to the voice server once more,
  telling it which phrase to expect, and answers only if the phrase then comes
  out exact. On the robot: 22 of 26 calls answered, and 2 false activations in
  21 look-alike sentences. The phrase is never given as a hint to *every*
  transcription — that makes noise turn into a call — and not even every time
  the beginning looks alike: that made "Fala galera, beleza?" call the robot.
- **Double-check even when the transcription was sure** (off by default) trades
  the other way: you are heard more often, and look-alike greetings sometimes
  call the robot. The panel states the measured price.
- **Show what was heard instead of the phrase** (off by default). The panel
  lists the snippet that resembled the phrase — two or three words, never the
  rest of what was said — and what the double-check made of it. It is there to
  answer "why didn't it answer me?".

The speech detector also follows the room now. It used to take one noise
measurement at start-up and keep it; the audio board has automatic gain, so the
noise floor moves by itself, and a floor above that fixed threshold left the
robot listening to three minutes of nothing as if it were one sentence. The
status list in the panel shows what the microphone is measuring.

### Telling the time (1.4.1, off by default)

The model has no clock; the robot has one. With **Know what time it is** on,
the date and time are sent with every request, which is what makes "what time
is it?" answerable. The time zone is an app setting, not the system's — the
robot ships set to London — and the panel offers the browser's. The name of the
zone is never sent.

### Vision (1.4.1, off by default)

*Settings › Vision.* With **Let the robot look when asked** on, a request that
asks the robot to look ("what do you see?", "what am I holding?", "describe the
room") captures **one** camera frame — no older than 2 s, from the same frame
producer that feeds the panel, never in a loop — and sends it to the companion
on your computer, which describes it with a vision model. The description is
quoted to the model together with the capture time, so the answer is about
*now*; if the camera is stopped, the frame is stale, the model is down, does
not understand images, times out, or the cloud is off, the robot is told to say
it cannot see right now — it never describes from memory.

- **Provider:** *Automatic* tries the local model first; *Local* is Ollama on
  the companion (`qwen2.5vl:7b` by default, `ollama pull qwen2.5vl:7b`);
  *Cloud* is OpenRouter (`OPENROUTER_API_KEY` in
  `~/.config/garra-reachy/companion.env`).
- **Cloud is opt-in.** `vision_cloud_enabled` is the only switch that lets a
  picture of your home leave it, and the panel shows the warning whenever it is
  on. A local failure never falls back to the cloud silently.
- **Test connection** and **Analyze current frame** are in the same card.
- **Sharing the GPU.** Local vision never waits behind another model that is
  busy on the same Ollama: waiting there stalls Ollama's whole queue (other
  clients too). When a large model holds the GPU, the robot says right away
  that the computer is busy with another model — or uses the cloud, if you
  allowed it. On a free GPU the first question takes a few seconds (the model
  loads) and the next ones about two; the model stays loaded for 10 minutes.

### Home (Home Assistant, 1.4.1)

*Control › Home.* Enter the Home Assistant address and a long-lived access
token. The token crosses once to the companion, is written to the gateway's
environment and never comes back to the page or the model. The gateway (Garra)
then sees your devices through its own device registry and risk gates; the
robot does not keep a second registry and executes nothing from the panel. The
state is one of *not configured / pending / connected / broken*, each explained.
**Allow the robot to control devices by voice** is off by default; on, "turn
on the living room light" goes through the gateway's tools and gates (low-risk
actions run, riskier ones are refused by policy). When you ask about a device
while the home is not connected (or voice control is off), the robot tells the
model the real situation, so the answer is "not set up yet — connect Home
Assistant under Control › Home" rather than "I can't do that".

### Music (Spotify, 1.4.1)

*Control › Music.* Two providers answer through the same panel.

**spotatui on the computer (used by voice and chat).** If
[spotatui](https://github.com/LargeModGames/spotatui) is installed on the
computer that runs the companion, the companion drives it through its CLI —
with the Spotify session spotatui already keeps, so there is no new login and
no token handled by this app. Say, after the wake phrase:

- "toca Hotel California", "toca uma música", "coloca minha playlist X",
  "adiciona X na fila", "procura a música X" (then "toca essa");
- "pausa a música", "continua a música", "próxima música", "volta a música";
- "que música está tocando?", "abaixa o volume", "volume 30".

Music plays on the **target device** chosen in the panel (by default the
spotatui player on the computer, which must be open in a terminal); the robot
speaks the confirmation. The companion only says "playing" after reading the
player: the requested track on the target device with its progress moving.
Pause and resume only toggle after reading the opposite state; a command that
had no effect is reported as a failure and never repeated; the same request id
never runs twice. Music playing on another device (phone, TV) is not paused or
skipped from here; asking to *play* plays on the target, as any Spotify app
would. While the robot speaks, the music is lowered and restored afterwards
(only if nobody changed the volume meanwhile). If the search result does not
match what was heard, the robot asks instead of playing. If Spotify rate-limits
the app (HTTP 429), commands pause for the time Spotify asked and the robot says
so. Nothing from Spotify (titles, artists) is sent to the AI model.

**Voice control is an owner opt-in, off by default** (*Allow music commands by
voice and chat*). Spotify's Developer Policy (III.3) does not allow a voice
assistant to control Spotify in a distributed app; the switch exists for your
own robot and account, and the panel says this next to it.

**Spotify Web API (panel only).** Without spotatui, create an app at
developer.spotify.com with the redirect URI
`http://127.0.0.1:8125/spotify/callback`, save its Client ID in the panel, and
connect the account by opening `http://127.0.0.1:8125/spotify/conectar` in a
browser **on the computer that runs the companion** (loopback only; PKCE, no
secret; tokens in `~/.config/garra-reachy/spotify.json`, 0600). The panel then
shows what is playing and offers play, pause, next, previous, volume and
transfer. Playback control needs Spotify Premium. This provider is not used by
voice.

### Family recognition (1.4.1, off by default)

*Settings › Family.* Enrolling someone requires their explicit consent (or a
responsible adult's, for a minor); the panel records who consented and when.
Then capture 3–5 samples from the robot's camera — the panel says why a sample
is refused (no face, more than one, too small, blurry, too dark, too bright).
Only a face signature (a 128-number vector) is kept, encrypted, in
`~/.config/garra-reachy/rostos/` on the computer; no photo is stored, nothing
goes to the cloud, nothing is in the repository, and deleting a profile deletes
its signatures. With **Recognize enrolled family members when asked** on,
"who is there?" compares one fresh frame with the enrolled profiles on the
computer (a local YuNet + SFace service on 127.0.0.1:8127) and answers with
names or "unknown" — below the similarity threshold, or with two candidates
too close, the answer is *unknown* by rule. The vision model is never asked to
guess a name; the text the model receives states that recognizing a face does
**not** say who is speaking and does **not** authorize anything; events carry
counts, never names.

### Diagnostics (1.4.1)

*Settings › Diagnostics* reads the gateway's Capability Registry and lists each
capability with its name, origin, state, reason and next step, separating
*not configured* (a choice) from *unavailable* (a fault), plus this panel's own
integrations and the other gateway checks.

### AI agents (1.5.0)

*Settings › AI agent.* The robot does not know who it is talking to. It sends
what it heard to the companion on your computer, and the companion hands it to
the **active agent**. Agents are discovered on the computer:

| Agent | How it is reached | What it may do by voice |
|---|---|---|
| **Hermes** (default) | its command line (`hermes chat`) | conversation and web search |
| **Garra** | the local Garra gateway, through the locked bridge | the gateway's restricted voice mode |
| **Pi** | its command line (`pi -p`) | conversation only |
| **Claude** | its command line (`claude -p`) | conversation only |
| **Ollama** | `http://127.0.0.1:11434` | conversation only |

From the panel you can pick the agent, test it (a real round trip, with the
time it took), set its options, choose a **backup agent** that answers only
when the chosen one does not, look for agents again, and add any server on
your network that speaks the OpenAI chat API (LM Studio, llama.cpp…).

- **Hermes is the default** on a fresh install. Your own choice is remembered
  across restarts and updates.
- **Switching does not restart anything.** The next thing you say goes to the
  new agent; conversations are not mixed between agents.
- **An agent that is down is named**: the robot says "I could not reach Hermes
  right now", and the panel shows *the chosen AI agent is not answering* —
  which is not the same thing as *the robot cannot reach your computer*.
- **Nothing on your network can loosen an agent.** The robot's panel has no
  password, so from it you can only do what is safe. Adding a command-line
  agent, or letting an agent use all its tools by voice, is done on the
  computer itself:

```bash
python -m companion.agentes                        # list, with health
python -m companion.agentes usar pi                # choose the active agent
python -m companion.agentes testar hermes          # a real conversation
python -m companion.agentes opcao hermes tools completo
python -m companion.agentes adicionar recipe.json  # a command-line agent
```

The choice lives in `~/.config/garra-reachy/agentes.json` on the computer
(mode 0600); the companion re-reads it when it changes. How to describe a
command-line agent, how to write an adapter, and what each agent was measured
to do are in
[`docs/specs/2026-11-24-agentes-de-ia-e-ativacao.md`](docs/specs/2026-11-24-agentes-de-ia-e-ativacao.md).

Codex and Gemini are **not** offered: on the machine where this was built,
Codex's default model was refused by the account and Gemini's login had
expired. opencode is not offered either — it runs shell commands and has no
flag to stop it.

### Voice activation (1.5.0)

*Settings › Conversation.* The wake phrase is on by default on a fresh install
(`Fala Garra`), and everything here applies without restarting the app.

- **Listen** — the master switch. Off, the microphone is released: the robot
  hears nothing at all.
- **Wake phrase** and **other phrases** — up to four more, one per line. Any of
  them works; keep them distinct, since every extra phrase is one more chance
  of room talk sounding like a call.
- **Beep** and **antenna gesture** when the phrase is heard — the robot's "I
  heard you". Say the phrase alone, wait for the beep, then ask; or say it all
  in one breath.
- **Microphone sensitivity** (1–10) and **the pause that ends your sentence**.
- **Language**, **voice**, **speed** and **volume** of the conversation. Voices
  are samples of about ten seconds that you place on the computer in
  `~/.config/garra-reachy/vozes/` (one `.wav` per voice).
- **Test listening** hears your next sentence and shows what the robot
  understood, how long it took, and whether it counted as the wake phrase —
  without sending it to any agent. **Test speech** and **Test speaker** prove
  the way out.

Detection is by transcription, not by a dedicated keyword model: the robot's
speech detector opens a segment, the voice server on *your* computer transcribes
it, and only a segment that starts with the phrase goes any further. That is
what lets you change the phrase by typing it. Nothing said without the phrase
reaches the chat, the history or an agent.

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
- **With conversation enabled**, what you type or say is sent to the agent you
  chose, and from there to whatever that agent uses: Hermes, Pi, Claude and the
  Garra gateway talk to their own cloud providers with the accounts *you* set
  up for them; a local model (Ollama) keeps the conversation on your computer.
  Each agent also keeps its own history of the conversation, where it normally
  keeps histories. The companion itself holds the conversation only in memory.
- **The wake phrase is checked at home.** Speech segments go to the voice
  server you run; nothing is streamed continuously, and what is said without
  the phrase is not sent to any agent.
- **"Test listening" returns what it heard only to whoever pressed the
  button.** It is not written to the chat, the history or the log.
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
- **With "Know what time it is" on, the robot's local date and time go with
  every request.** The name of the time zone does not.
- **With "Notebook" on, what you ask the robot to write down is stored** in
  `~/.config/garra_reachy_mini/caderno.json` until you delete it, and is sent
  to the model with every request so it can answer questions about it.
- **Credentials are never returned by the API.** `GET /api/config` masks the
  gateway key; the panel can set it but never read it back.
- **Identity fields reach the model.** The assistant name, operator name and
  personality you configure are part of the agent's context and are sent to
  the AI provider with each conversation — that is what they are for. Do not
  put anything in them you would not send to the provider.
- **Camera frames leave the robot only for vision, and only when asked.** With
  vision on, one frame per "what do you see?" goes to the companion on your
  computer; it reaches a cloud provider **only** with the cloud switch on. The
  frame is never logged, stored, or kept in events.
- **Face recognition is off by default, consent-based and local.** Enrolment
  stores an encrypted face signature on the computer, never a photo; profiles
  can be deleted; recognition never identifies who is speaking and is never
  used as authorization. Without enrolment the camera still only *detects* a
  face for tracking, on the robot.
- **Home Assistant tokens and Spotify tokens live on the computer**, 0600,
  and never come back through the API or the panel; with spotatui, the
  session is spotatui's own and this app never reads it.
- **Nothing from Spotify reaches the model.** Music commands are understood by
  the robot without the model, and song names are data passed to the spotatui
  CLI as separate arguments (no shell), with a minimal environment.
- **Spotify voice control is off by default** and is the owner's decision
  (Spotify Developer Policy III.3), stated in the panel where it is turned on.
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

```
Your computer (the companion, `python -m companion`)
├── bridge :8126 .......... the only thing the robot talks to; token required
│    ├── agent registry ... discovery, choice, backup, options (agentes.json)
│    └── adapters ......... Hermes · Pi · Claude · HTTP · (Garra → locked proxy)
├── voice server :8123 .... Whisper + Chatterbox on the GPU
└── watcher ............... re-points the robot, restarts the app after a reboot
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
pytest tests -q                               # 2,270+ tests, no hardware needed
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
| Robot never reacts to anything you say | Look at the status pill. *Standby — the voice server is not answering* or *the robot cannot reach your computer* is almost always the network: same Wi-Fi? firewall? On Ubuntu with `ufw`: `sudo ufw allow from <your LAN>/24 to any port 8123,8126 proto tcp`. Both services still require the token. |
| Robot stopped answering after a reboot or a network change | The companion's watcher fixes both within a minute: it starts the app when the robot reappears and re-points it when your computer's address changes. `curl http://127.0.0.1:8125/api/reachy/watch` shows what it last saw. |
| "The chosen AI agent is not answering" | Your computer is reachable; the agent is not. `python -m companion.agentes testar <agent>` on the computer says why, or pick another agent in the panel. |
| Robot ignores the activation phrase | Turn on "Show what was heard instead of the phrase" and say it again: the panel shows what the transcription made of it, and how sure it was. Keep "Double-check" on, and restart the voice server after updating the app — an old one does not report confidence, and without it there is no double-check. If what you said shows up with high confidence, "Double-check even when the transcription was sure" catches it. |
| Robot says it has no clock | Turn on "Know what time it is" and set the time zone. |
| Robot deaf for minutes at a time | Fixed in 1.4.1: the speech detector used a noise threshold measured once at start-up. The status list now shows the noise floor it is following. |
| Beginning of sentences was cut | Fixed in 1.1.0 with an audio pre-roll. If it still happens, check microphone levels — a very high noise floor shrinks what the pre-roll can recover. |

**Upgrading from 1.0.x:** your stored settings are preserved. The holding
phrases ("deixa eu pensar") were unconditional code in 1.0.x, not a setting, so
after the upgrade the robot simply stops saying them; turn them back on in the
panel if you miss them.

## Licence

[Apache-2.0](LICENSE). Built on the [Reachy Mini
SDK](https://github.com/pollen-robotics/reachy_mini) by Pollen Robotics.
