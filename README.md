# Phase 1: Core Assistant

A terminal chat loop — the foundation everything else (tools, memory, voice)
gets built on top of in later phases.

## Setup (Groq mode — recommended, no download needed)

1. Install Python 3.9+ if you don't have it already (check with `python --version`).

2. Open a terminal in this folder and install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Get a free Groq API key:
   - Go to https://console.groq.com
   - Sign up (no credit card required)
   - Create an API key

4. Set the key. Two options:

   **Option A — .env file (persists across sessions, recommended):**
   Create a file named `.env` in this same folder with exactly this (no quotes):
   ```
   GROQ_API_KEY=your-key-here
   ```
   The script loads this automatically via `python-dotenv`. Never share this
   file or paste its contents anywhere — anyone with the key can use your
   free quota.

   **Option B — Set it directly in your terminal (session-only):**

   PowerShell:
   ```
   $env:GROQ_API_KEY="your-key-here"
   ```
   Command Prompt:
   ```
   set GROQ_API_KEY=your-key-here
   ```
   This only lasts until you close the terminal.

5. Run it:
   ```
   python main.py
   ```

6. Chat! Type `quit` or `exit` to stop.

## Setup (Ollama mode — fully offline alternative)

Only do this once you've freed up some disk space (you were at ~20GB free,
and the model + Ollama itself need a few GB).

1. Install Ollama from https://ollama.com (Windows installer available).
2. Pull the small model:
   ```
   ollama pull llama3.2:3b
   ```
3. In `main.py`, change `MODE = "groq"` to `MODE = "ollama"`.
4. Run it:
   ```
   python main.py
   ```

No API key or internet needed once the model is downloaded.

## What this does

- Keeps a running conversation (remembers the current session, not across runs — that's Phase 3)
- Trims history so it doesn't grow unbounded
- Handles errors gracefully if the API/model call fails

## Phase 2: Tools (to-do list, file reading, web search)

New files:
- `tools.py` — the actual tool functions and their schema
- `todos.json` — created automatically the first time you add a task

### Setup

1. Re-run `pip install -r requirements.txt` to get the new `tavily-python` package.
2. Get a free Tavily API key at https://tavily.com (free tier: 1,000 searches/month, no card required).
3. Add it to your `.env` file alongside your Groq key:
   ```
   GROQ_API_KEY=your-groq-key
   TAVILY_API_KEY=your-tavily-key
   ```
4. Run as before: `py main.py`

### Try it

- "Add 'buy groceries' to my to-do list"
- "What's on my to-do list?"
- "Mark task 0 as done"
- "Search the web for the latest news on X"
- "Read the file C:\\Users\\siyan\\Desktop\\notes.txt and summarize it"

You'll see a line like `[using tool: web_search({'query': '...'})]` printed
whenever the assistant decides to use a tool — that's it showing its work.

### Notes

- Tool calling currently only works in `"groq"` mode. Ollama mode still
  works for plain chat but won't use the tools yet.
- `todos.json` lives in this same folder and persists between runs.
- `read_file` only reads plain text files (not PDFs, Word docs, etc. — that'd
  need extra libraries, which we can add later if useful).

## Phase 3: Long-term memory

New file: `memory.py` — stores facts locally in `memories.json`, no external
dependencies at all. Relevance is matched by shared keywords between what
you ask and what's stored (not ML embeddings — see note below).

### Setup

No install needed — this uses only Python's standard library. (We initially
tried ChromaDB for smarter embedding-based search, but its native
dependencies require Microsoft's C++ Build Tools and a Rust toolchain on
Windows, and don't yet have prebuilt wheels for Python 3.14. Not worth the
hassle for this project — the keyword-based approach below is reliable and
good enough at this scale.)

### How it works

- **Automatic recall**: before every reply, the assistant quietly checks
  memory for anything relevant to what you just said and adds it as context.
  You won't see this happen — it just makes replies more informed.
- **Explicit saving**: the model calls `remember_fact` on its own when you
  share something worth keeping, or when you say "remember that...".
- **Managing memory**: ask it "what do you remember about me?" (uses
  `list_all_memories`) or "forget that I said X" (uses `forget_fact`).

### Try it

1. "Remember that I prefer dark roast coffee over light roast"
2. Ask something unrelated, then later: "what kind of coffee should I buy?"
   — it should recall the preference without you repeating it
3. "What do you remember about me?"
4. "Forget the coffee thing"

### Notes

- Memory persists across separate runs of the script now (unlike chat
  history, which still resets each time you restart).
- `memories.json` lives in this folder — back it up if you want to keep it
  when moving the project.
- Matching is keyword-based, not meaning-based: "I like dark roast coffee"
  will match a query containing "coffee" but won't automatically connect to
  "espresso" the way a real embedding model would. Good enough for a
  personal assistant's scale, but worth knowing the limit.

## Agent loop polish

Three robustness fixes to `main.py`, no new setup needed:

1. **Automatic retries** — if Groq returns a rate limit, timeout, or 5xx
   error, the call retries up to 3 times with increasing delay (2s, 4s, 8s)
   before giving up. Non-transient errors (bad request, auth failure) still
   fail immediately, since retrying those won't help.

2. **Malformed tool-call arguments no longer crash the loop** — if the
   model ever returns broken JSON for a tool call (rare, but it happens),
   that one call now returns an error message back to the model instead of
   throwing an exception that kills the whole turn.

3. **History is summarized, not deleted** — previously, once a
   conversation passed `MAX_HISTORY_TURNS`, the oldest exchanges were
   silently dropped. Now they're condensed into a running one-paragraph
   summary (one extra API call, only when trimming actually happens) that
   stays in context, so older details aren't completely lost — just
   compressed. You'll occasionally see a short pause and no "[using
   tool...]" line when this kicks in; that's the summarization call
   happening quietly in the background.

Ollama mode still hard-trims (no model handy locally to summarize with
cheaply, so it falls back to the old drop-the-oldest behavior).

### Known limitation

If an unrecoverable error hits in the middle of a multi-step tool-call
round (after retries are exhausted), the partially-built exchange for that
turn is discarded, but this is a coarse rollback — fine for a personal
project, but worth knowing if you ever see an odd follow-up error after a
failure.

## Phase 4: Voice in and out

New file: `voice.py`.

- **Speech-to-text**: records your mic locally, sends the audio to Google's
  free Web Speech API for transcription (no API key, but needs internet).
- **Text-to-speech**: uses Windows' own built-in voices via `pyttsx3` — no
  model download, fully offline.

We deliberately skipped Whisper/Torch and Piper here — both are heavy
installs likely to hit the same "no prebuilt wheel for your Python version
yet" wall that ChromaDB did in Phase 3. This trades a little polish for
something that reliably installs today.

### Setup

```
pip install -r requirements.txt
```
No new keys needed. Make sure your laptop's microphone permissions allow
desktop apps to use it (Windows Settings → Privacy & security → Microphone).

### Try it

- Type `talk`, then speak — it records for 5 seconds, transcribes, and
  treats it exactly like typed input (tools, memory, everything still works).
- Type `voice on` to have replies spoken aloud as well as printed. `voice off`
  to go back to text-only replies.
- You can mix typing and talking freely in the same session.

### Known limitations

- **Fixed 5-second recording window** (`RECORD_SECONDS` in `voice.py`) —
  it doesn't detect when you stop talking, so short answers get a pause
  afterward and longer ones might get cut off. Adjust the constant if 5s
  doesn't fit how you talk; proper silence detection is a nice next upgrade.
- **Needs internet** for speech-to-text (it calls Google's free endpoint).
  Text-to-speech still works offline.
- **Rate limits**: Google's free speech endpoint isn't meant for heavy use.
  Fine for a personal assistant, not for constant/production use.

## Phase 5: Silence detection + wake word ("Jev")

Changes to `voice.py` and `main.py`, no new installs.

### Silence detection

Recording no longer uses a fixed 5-second window. It now measures mic
energy in small chunks and stops automatically once you've paused for
about a second after speaking (or after 15 seconds, as a hard cap). The
first time you use `talk` or `wake` in a session, it does a quick ~1-second
mic calibration ("stay quiet for a second...") to set a sensible energy
threshold for your room/mic, then reuses that for the rest of the session.

### Wake word: "Jev"

Type `wake` to enter hands-free mode. It listens continuously and responds
once it hears "Jev" (also recognizes "Jeff", "Jef", or "Jevv" — speech
recognition is inconsistent about how it spells a short word like this, so
all four count).

**You only need to say "Jev" once per conversation, not before every
request.** Hearing the wake word switches it into an "active" state where
everything you say afterward is treated as a direct command — no repeating
"Jev" each time. Two ways to kick it off:

- **One-shot**: "Jev, what's on my to-do list?" — wake word and the request
  together in one breath. You're now active for follow-ups too.
- **Two-step**: say "Jev" alone, wait for "Heard 'Jev' — go ahead" (or hear
  it say "Yes?" if `voice on` is set), then say your request.

Once active, just keep talking — "what's the weather?", then "open
Spotify", then "play something" — no wake word needed in between. Active
mode automatically lapses back to requiring "Jev" after about 30 seconds of
silence (`ACTIVE_IDLE_TIMEOUT` in `main.py` if you want to change that), or
immediately if you say "go to sleep" or "never mind".

Say "stop listening" (or "exit wake mode") to leave wake mode entirely,
back to the typed prompt. Ctrl+C also works.

### How wake detection actually works (and its real limitation)

There's no dedicated low-latency wake-word engine running here — that would
mean Porcupine (needs a signup/access key) or Vosk (heavier native install,
same Python-3.14 wheel risk we hit with ChromaDB). Instead, wake mode is a
loop of short recordings: each one records until silence, and *only if
something was actually said* does it get sent to Google's free speech API
and checked for "Jev". Sitting in silence costs nothing — no API calls, no
rate-limit risk — but there's a real response lag (roughly half a second to
a couple seconds per cycle) rather than the instant on-device response a
dedicated wake-word model gives you. Good enough for a personal project,
not something you'd ship as a product.

### Troubleshooting a frozen/silent mic

If `talk` or `wake` seems to turn the mic on but never responds (you have
to Ctrl+C to get control back), that's a `sounddevice` quirk on Windows:
when a recording doesn't properly start (wrong sample rate, wrong default
device), the old `rec()`/`wait()` approach could hang forever instead of
erroring out. This was fixed by switching to a direct `InputStream` and
using your mic's own default sample rate instead of forcing 16kHz — but if
you still hit it, run:
```
py check_mic.py
```
This lists every audio device Windows exposes to Python, shows which one
is the default input, and does a 2-second test recording so you can see
whether real audio is actually being captured.

### A second mic-related fix: TTS could also hang

A long reply with a markdown table tripped up Windows' SAPI5 voice driver
and froze the program the same way the mic issue did — `pyttsx3`'s
`runAndWait()` has a known failure mode where a problematic run hangs
forever instead of erroring. Two fixes in `voice.py`:
- Replies are cleaned before being spoken (markdown tables become plain
  comma-separated text, emphasis symbols and smart quotes/dashes are
  stripped) — most of what trips up the driver was never meant to be heard
  anyway.
- `speak()` now runs in a background thread with a 20-second timeout, so
  even if a particular reply does hang the driver, the program itself won't
  freeze — it just moves on and the speech may still finish quietly later.

### Fixed: silent failures with no feedback

Previously, if the mic never detected speech above the threshold, or heard
something Google couldn't transcribe, or heard speech without "Jev" in it,
wake mode just went quiet and listened again — with no indication of what
happened. That's indistinguishable from "broken." Now every one of those
cases prints something:
- Pure silence: a `.` printed per cycle (so you know it's alive, not frozen)
- Heard something, couldn't transcribe it: an explicit message to speak up/clearer
- Heard clear speech, no wake word in it: shows you exactly what it heard,
  e.g. `(heard: "go to documentary turn all the" — no wake word, ignoring)`
  — handy for noticing when recognition is mangling your speech
- The calibrated threshold is printed right after calibration, so you can
  see if it looks sane (very low = might trigger on background noise; very
  high = might never register your voice at all)

If you keep seeing it transcribe garbled, unrelated-sounding text even when
you spoke clearly, that's Google's free recognizer struggling with short,
isolated utterances (it does better with more natural, longer phrases) —
worth knowing as a real limitation of the no-cost speech API, not something
more tuning will fully fix.

Also fixed: `search_files` was checking your whole query as one literal
substring, so "siyanda CV" never matched "Siyanda's CV.pdf" (the words
are there, just not in that exact combined phrase). It now matches each
word separately.

### Notes

- `wake` mode blocks typing while active — it's a dedicated hands-free loop,
  not a background thread running alongside the typed prompt. Exit it to
  type again.
- The energy threshold is calibrated once per session on first use, not
  per-`wake`-entry. If your room gets noisier partway through (TV turns on,
  etc.), restarting the script will force a fresh calibration.
- All the existing tools, memory, and history summarization work exactly
  the same whether a request comes from typing, `talk`, or `wake` — they
  all funnel through the same turn-processing logic now.

## Follow-up conversation mode (no repeated wake word)

Changes to `main.py` only, no new installs.

Previously you had to say "Jev" before every single request, which got old
fast. Now hearing "Jev" once switches wake mode into an **active** state —
everything you say afterward is treated as a direct command, no wake word
needed, until:
- about 30 seconds pass with no speech (`ACTIVE_IDLE_TIMEOUT` in `main.py`),
  which quietly drops back to requiring "Jev" again, or
- you say "go to sleep" or "never mind" to drop back immediately, or
- you say "stop listening" / "exit wake mode" to leave wake mode entirely

### Voice commands for spoken replies

While active, you can say any of these instead of typing them:
- **Turn on**: "voice on", "speak", "talk", "talk to me", "start talking"
- **Turn off**: "voice off", "stop talking", "be quiet", "mute"

These are caught before being sent to the model, so they're instant and
free (no API call), and it always says a short confirmation aloud so you
know it registered. The choice persists after you leave wake mode and
return to typing, too.

## Neural voice upgrade (free, optional)

New in `voice.py`: `speak()` now tries a much more natural-sounding neural
voice first via `edge-tts` — a free, keyless wrapper around the same voices
behind Microsoft Edge's "Read Aloud" feature — and only falls back to the
original offline Windows voice if that's unavailable (not installed, no
internet, or a service hiccup). Nothing else changes either way; this is a
drop-in upgrade.

### Setup

```
pip install -r requirements.txt
```
No signup, no key. Playback uses Windows' own `winmm.dll` via `ctypes`
(built directly into `voice.py`) rather than a separate audio-playback
package — we initially tried the `playsound` package for this, but its
installer script is broken on Python 3.14, so we cut it out entirely rather
than fight it. If `edge-tts` itself ever fails to install, just delete that
line from `requirements.txt` and reinstall — voice output keeps working
exactly as before using the offline voice, with zero code changes needed.

### Picking a voice

The default is `en-US-GuyNeural`, set at the top of `voice.py`
(`EDGE_VOICE`). To hear your options and pick a favorite:
```
edge-tts --list-voices
```
A few worth trying for a more "sophisticated" sound:
- `en-GB-RyanNeural` — British, deeper, formal
- `en-US-DavisNeural` — warm, confident American male
- `en-AU-WilliamNeural` — Australian, calm
- `en-US-AriaNeural` — American female, expressive

Just change the `EDGE_VOICE` constant in `voice.py` to whichever voice name
you like and restart the script — no other changes needed.

## Voice-aware confirmation + tool call logging

Changes to `main.py` and `tools.py`.

### The problem this fixes

Confirmation prompts (`delete_todo`, `send_email`, `browser_upload_file`,
and any browser/desktop click that looks consequential — submit, pay,
delete) used Python's `input()`, which blocks waiting for someone to type
`y` at the keyboard. In hands-free wake mode, that meant the assistant
could silently freeze mid-conversation waiting for a keystroke that was
never coming — exactly at the moment something risky was about to happen.

### The fix

`execute_tool` now accepts an optional `confirm_fn`. Typed turns still use
the original keyboard prompt (`_default_confirm` in `tools.py`) — no
change there. Voice turns (`talk` or wake mode) instead route through
`_voice_confirm` in `main.py`, which:
1. Speaks the question aloud regardless of whether `voice on` is set —
   a safety prompt needs to be heard even in a normally-silent session
2. Listens for a spoken answer (yes/no and common variants —
   "sure", "go ahead", "cancel", "nope", etc.)
3. **Defaults to NO on any ambiguity** — silence, an unclear answer, a
   mic error — since a missed confirmation should block the action, not
   wave it through

Try it: in `wake` mode, ask it to delete a to-do item or do something that
contains a word like "delete" or "submit" on a browser/desktop click. It
should ask the question out loud and wait for a spoken yes/no instead of
hanging.

### Tool call logging

Every tool call — allowed, cancelled, or errored — now appends one JSON
line to `tool_log.jsonl` in the project folder:
```json
{"timestamp": "2026-10-03T14:22:07", "tool": "get_weather", "args": {"city": "Durban"}, "result_preview": "Durban: ☀️ +22°C", "elapsed_ms": 412, "error": null}
```
This is independent of terminal scrollback, so you can review what the
assistant actually did after the fact, search it, or diff sessions.
Logging failures are swallowed on purpose — a broken log must never break
the tool call it's trying to record.

To browse it, either open `tool_log.jsonl` directly (each line is valid
JSON) or ask the assistant to read it as a file: `"read tool_log.jsonl and
summarize today's tool calls"` works today since `read_file` handles plain
text.

## Desktop orb UI — replacing the terminal

New file: `ui.py`. Significant rewrite of `main()` in `main.py`. No new
installs — `tkinter` is part of Python's standard library.

### What it is

A borderless, always-on-top, transparent circular widget that sits on your
desktop. **This is an original design**, not a reproduction of Raphael
from *Tensura* — that's copyrighted character/UI art owned by
Kodansha/8-bit, and recreating it (even as a desktop widget) would be
reproducing someone else's IP. What's here instead is an original
"glowing reactive HUD circle" — a concentric ring with rotating tick marks
and a pulsing core — in the same general sci-fi-assistant spirit, built
from scratch.

- **Grey and gently breathing** when idle
- **Teal** while actively listening after the wake word
- **Violet** while waiting on the model/tools ("thinking")
- **Cyan, pulsing with the actual waveform** while speaking — when Piper
  is doing the talking, the pulse is driven by real audio amplitude
  (computed from the raw PCM before playback), not just a generic
  animation. The edge-tts/pyttsx3 fallback paths don't expose raw
  amplitude, so those get a plausible generic speech-like pulse instead.

### How to use it

```
py main.py
```
No more typed commands (`talk`, `wake`, `voice on`) — those are gone along
with the terminal prompt:

- **It's always listening** for "Jev" — no need to type `wake` first.
- **Click the orb** to pop up a small transparent text box; type and press
  Enter to send, or Escape to cancel. Typing skips the wake-word
  requirement entirely, since typing is already a deliberate act.
- **Drag the orb** anywhere on screen (click-and-move, as opposed to a
  plain click which opens the type box).
- **Right-click** for a small menu: toggle voice output, or exit.

### What changed under the hood

- `main()` no longer runs a blocking terminal loop. `tkinter` owns the
  main thread (required on Windows); the entire assistant loop (API calls,
  mic recording, the wake-word state machine) now runs in a background
  thread, started once and left running until the orb is closed.
- **All tool confirmations now go through voice**, always — there's no
  terminal left for a keyboard `[y/N]` prompt to attach to. This applies
  even to requests that came in by typing in the quick-type box: if what
  you type triggers something like `delete_todo`, the orb will speak the
  confirmation question aloud and listen for your spoken answer.
- **Voice output defaults to on** (previously off by default) — with no
  console to read text replies from, hearing them is the only way to know
  what happened unless you watch the terminal window running the script.
- `voice.py` gained `set_speech_visual_callbacks(on_start, on_end)` — a
  small hook any UI can register to sync a visual to speech. Piper's path
  computes a real amplitude envelope from the synthesized audio before
  playback; the edge-tts/pyttsx3 paths pass `None` (triggering ui.py's
  generic pulse) along with an estimated duration from word count.

### Known rough edges (first pass — tell me what you hit)

- The underlying terminal window is still there and still prints debug
  output (tool calls, STT errors, model rotation messages) — useful while
  you're getting this dialed in, but there's no in-UI equivalent yet. A
  console-free launch (pythonw.exe, or a .pyw file) is a natural next step
  once you're happy with how it behaves.
- The quick-type box doesn't show the assistant's reply as text — replies
  are voice-only right now. A transient caption bubble near the orb is a
  reasonable next addition if you want to glance at replies instead of
  always listening.
- Confirmation always being voice-based means a typed request can still
  trigger a spoken question + mic listen — a little inconsistent-feeling
  if you typed specifically to stay quiet. Worth discussing if that
  friction shows up in practice.
- `ALLOW_BARGE_IN` and `TTS_COOLDOWN_SECONDS` (top of `main.py`) still
  govern the mic-mute-while-speaking behavior exactly as before — nothing
  about the orb changes that.

## What's next

From here, natural next steps are scheduled automations (a morning
briefing, reminders that fire on their own), a console-free launch, or a
text caption near the orb for reading replies instead of only hearing them.