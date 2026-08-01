# Roadmap

What is planned, what is being considered, and — just as important — what this
app does **not** do yet. Nothing here is a promise with a date.

## Next

- **Speaker identity sources.** The `speaker_identity` contract
  (`status/person_id/display_name/source/confidence`) already ships, always
  `unknown`. Planned fillers, each explicit and opt-in: panel selection,
  authenticated session profile, confirmed verbal identification. A stated
  name alone will never count as identification.
- **Resolved-model display.** The gateway captures which model a meta-router
  picked but does not expose it; once it does, the panels will show
  `requested → resolved` instead of staying silent about it.
- **Expression while thinking.** An optional subtle idle animation during
  long turns — visual, never spoken, and off by default like all automatic
  behaviour.

## Considered

- **Local, opt-in face recognition** — *not implemented today*. If it ever
  ships: processing on the robot only, nothing uploaded, explicit enrolment,
  one switch to erase everything. Face *detection* for tracking already exists
  and identifies no one.
- **Multi-robot support** in the desktop console.
- A public marketing site for the app.

## Non-goals

- Cloud storage of camera frames, audio or transcripts.
- Autonomous movement beyond the validated action envelope.
- Identity inference from voice alone.
