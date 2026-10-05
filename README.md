
# M.A.R.S. — Personal AI Assistant

<p align="center">
  <em>A JARVIS-inspired personal AI assistant for Windows — voice-controlled, tool-augmented, and always listening.</em>
</p>

---

## 📖 What is M.A.R.S.?

M.A.R.S. (Multi-Agent Reasoning System) is a **fully-featured, voice-first personal AI assistant** that runs on your Windows laptop. Unlike cloud-based assistants that lock you into a web interface, M.A.R.S. lives on your machine — it listens for a wake word, understands natural language, executes real actions through a modular tool system, and speaks back with a custom JARVIS-inspired neural voice.

The project started as a simple terminal chatbot and evolved into a desktop orb widget with:

- **Voice in/out** — wake-word detection ("Mars"), Groq Whisper STT, custom Piper TTS
- **50+ tools** — to-do lists, file operations, web search, browser automation, desktop automation, email, Spotify control, weather, notes, and long-term memory
- **Multi-model rotation** — automatic failover across Groq and OpenRouter free-tier models
- **Computer use** — Playwright for web automation, pywinauto for native Windows apps
- **Desktop orb UI** — a borderless, transparent, always-on-top widget with real-time waveform pulse

---

## 🎬 Demo

> **Screen recording** — coming soon. In the meantime, here's what a session looks like:

```
You (voice): "Hey Mars, what's the weather in Durban?"
[using tool: get_weather({'city': 'Durban'}) -> Durban: 🌦️ +22°C...]
Assistant: "Durban's currently cloudy with a temperature of about 22 degrees."

You (voice): "Open Spotify and play."
[using tool: open_spotify({}) -> Opened Spotify.]
[using tool: spotify_control({'action': 'play'}) -> Sent play to media player.]
Assistant: "Spotify is now playing."

You (voice): "Fill out the form at httpbin.org/forms/post with my name."
[using tool: browser_open({'url': 'https://httpbin.org/forms/post'}) -> ...]
[using tool: browser_fill({'index': 0, 'value': 'Siyanda'}) -> ...]
  [!] This click looks consequential: submit (target: 'Submit order')
  [confirm] browser_click(index=12) - allow? [y/N] y
[using tool: browser_click({'index': 12}) -> Clicked 12 (Submit order)...]
Assistant: "Done — form submitted."
```

---

## ✨ Features

### 🎙️ Voice Interface
- **Wake word detection** — say "Mars" (or a close homophone) to activate hands-free mode
- **Groq Whisper STT** — fast, accurate transcription with Google Speech as fallback
- **Custom JARVIS voice** — Piper neural TTS, fully offline, no API key required
- **Voice cascade** — Piper → edge-tts → pyttsx3 (three-tier fallback)
- **Barge-in support** — interrupt mid-reply with a new command (headphones recommended)
- **Voice confirmations** — consequential actions (send email, submit form) prompt for spoken yes/no confirmation

### 🧰 Tool System (50+ tools)
| Category | Tools |
|---|---|
| **To-do list** | add, list, complete, uncomplete, edit, delete |
| **Files** | read (text + PDF), open with default app, search by name |
| **Web** | Tavily search, open URLs, fetch weather |
| **System** | date/time, disk space, OS info |
| **Apps** | launch desktop apps, control Spotify via media keys |
| **Notes** | append to and list `notes.md` |
| **Memory** | remember, recall, list, forget facts |
| **Email** | send via SMTP (Gmail app password) |
| **Browser** | open pages, read elements, click, fill, select, upload, press keys, screenshots |
| **Desktop** | enumerate windows and controls, click, type, send hotkeys, screenshots |

### 🖥️ Desktop Orb UI
- **Borderless, transparent, always-on-top** circular widget
- **Live state feedback** — grey (idle), teal (listening), violet (thinking), cyan (speaking)
- **Real-time waveform pulse** — driven by actual Piper audio amplitude
- **Caption bubble** — shows what the mic heard and what the assistant replied
- **Drag anywhere** — click-and-drag to reposition; position persists across restarts
- **Right-click menu** — toggle voice, hide caption, exit
- **Click to type** — opens a quick-type box for when you don't want to speak

### 🔄 Reliability
- **Multi-provider rotation** — Groq and OpenRouter models tried in order; rate-limited or unavailable models skipped instantly
- **Retry with backoff** — 429s, timeouts, 5xx errors retried with provider-supplied `Retry-After`
- **Message sanitization** — malformed tool-call entries never poison subsequent requests
- **Hallucination filter** — Whisper's short-utterance garbage ("Thank you.", "?") rejected before reaching the model
- **Tool logging** — every call logged to `tool_log.jsonl` for post-session review

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         M.A.R.S. Core                           │
├─────────────────────────────────────────────────────────────────┤
│  main.py                                                        │
│  ├── _chat_create()        — multi-provider model rotation       │
│  ├── chat_groq()           — tool-calling agent loop            │
│  ├── _sanitize_messages()  — malformed tool-call repair         │
│  ├── _voice_confirm()      — spoken yes/no confirmation         │
│  └── run_worker()          — mic loop + orb event bridge        │
├─────────────────────────────────────────────────────────────────┤
│  tools.py        — 50+ tool implementations + schemas           │
│  voice.py        — STT (Whisper/Google) + TTS (Piper/edge/pyttsx3)│
│  memory.py       — persistent fact storage (memories.json)      │
│  browser.py      — Playwright web automation                    │
│  desktop.py      — pywinauto UIA desktop automation             │
│  ui.py           — layered-window desktop orb widget            │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
User speaks "Mars, what's the weather?"
        │
        ▼
┌───────────────┐     ┌──────────────┐     ┌──────────────┐
│  Mic capture  │────▶│  Groq Whisper│────▶│ Wake-word    │
│  (sounddevice)│     │  STT         │     │ check ("Mars")│
└───────────────┘     └──────────────┘     └──────┬───────┘
                                                  │
                    ┌─────────────────────────────┘
                    ▼
            ┌───────────────┐
            │  Agent loop   │◀──── Tool results
            │  (chat_groq)  │
            └───────┬───────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │  Groq   │ │OpenRoute│ │ Ollama  │
   │  120B   │ │  free   │ │ (local) │
   └─────────┘ └─────────┘ └─────────┘
        │
        ▼
┌───────────────┐     ┌──────────────┐     ┌──────────────┐
│ Piper TTS     │────▶│  MCI playback│────▶│  Orb waveform│
│ (jarvis-high) │     │  (winmm.dll) │     │  pulse       │
└───────────────┘     └──────────────┘     └──────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

- **Windows 10/11** (the desktop automation and layered-window UI are Windows-specific)
- **Python 3.10+** (3.14 is known to work)
- **A microphone** (for voice input)
- **~500MB free disk space** (Piper voice model + dependencies)

### 1. Clone and install

```powershell
git clone https://github.com/SiyandaNduze/M.A.R.S.git
cd M.A.R.S
py -m pip install -r requirements.txt
py -m playwright install chromium
```

### 2. Get API keys

Create a `.env` file in the project folder:

```env
# Required — free at https://console.groq.com
GROQ_API_KEY=gsk_...

# Optional — free at https://openrouter.ai (50 free requests/day)
OPENROUTER_API_KEY=sk-or-v1-...

# Optional — free at https://tavily.com (1000 searches/month)
TAVILY_API_KEY=tvly-...

# Optional — for email tool
EMAIL_ADDRESS=you@gmail.com
EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

### 3. Download the JARVIS voice

```powershell
curl.exe -L "https://huggingface.co/jgkawell/jarvis/resolve/main/en/en_GB/jarvis/high/jarvis-high.onnx" -o jarvis-high.onnx
curl.exe -L "https://huggingface.co/jgkawell/jarvis/resolve/main/en/en_GB/jarvis/high/jarvis-high.onnx.json" -o jarvis-high.onnx.json
```

> **Note:** Use `curl.exe`, not `curl`. PowerShell aliases plain `curl` to `Invoke-WebRequest`, which doesn't understand the `-L` flag.

### 4. Run

```powershell
py main.py
```

The orb appears in the top-right corner of your screen. Say **"Hey Mars"** to get started.

---

## 🎮 Usage

### Voice Commands

| Command | Effect |
|---|---|
| `"Mars, [request]"` | Wake word + request in one breath |
| `"Mars"` *(pause)* `"[request]"` | Two-step activation |
| `"Stop listening"` | Suspend mic entirely (orb shows **Asleep**) |
| `"Go to sleep"` | End current conversation; mic stays active for "Mars" |
| `"Voice on"` / `"Voice off"` | Toggle spoken replies |
| `"Never mind"` | Same as "Go to sleep" |

### Type-Box Commands

Click the orb to open a quick-type box. Type any of these to control the assistant without speaking:

| Typed | Effect |
|---|---|
| `wake` / `resume` | Resume mic after suspend |
| `stop listening` / `sleep` | Suspend mic |
| `voice on` / `voice off` | Toggle spoken replies |
| *(anything else)* | Sent to the model as a normal request |

### Example Prompts

```
"Add 'buy groceries' to my to-do list"
"What's on my to-do list?"
"Remember that my resume is at C:\Users\siyan\Documents\cv.pdf"
"Search for Siyanda's CV.pdf and open it"
"Open Spotify and play"
"Fill out the form at httpbin.org/forms/post with my name"
"List my open windows and type hello into notepad"
"What's the weather in Durban?"
"Search the web for today's news"
```

---

## 🧠 Long-Term Memory

M.A.R.S. remembers facts about you across sessions. Memory is stored locally in `memories.json`.

```python
# The model calls this automatically when you share something worth keeping
remember_fact("User prefers dark roast coffee over light roast")

# Or ask directly
"What do you remember about me?"
"Forget the coffee thing"
```

Relevant memories are auto-injected as `[Relevant memories]` system notes before each reply — the model uses them naturally without mentioning the injection.

---

## 🔧 Configuration

Key constants in `main.py`:

| Constant | Default | Description |
|---|---|---|
| `MODEL_CHAIN` | Groq + OpenRouter | Ordered list of `(provider, model_id)` to try |
| `MAX_HISTORY_TURNS` | 10 | How many exchanges to keep in full before summarizing |
| `MAX_TOOL_ROUNDS` | 25 | Safety cap on chained tool calls per user message |
| `MAX_RETRIES` | 4 | Retries for transient API failures |
| `ALLOW_BARGE_IN` | `False` | Enable mid-reply interruption (headphones required) |
| `TTS_COOLDOWN_SECONDS` | 1.7 | Mic mute duration after speech ends |

Key constants in `voice.py`:

| Constant | Default | Description |
|---|---|---|
| `WAKE_WORDS` | `{"mars", "marz", ...}` | Accepted transcriptions of the wake word |
| `PIPER_VOICE` | `"jarvis-high"` | Piper voice model filename (without `.onnx`) |
| `GROQ_STT_MODEL` | `"whisper-large-v3-turbo"` | Fast Whisper model for STT |
| `EDGE_VOICE` | `"en-US-GuyNeural"` | Fallback neural voice (edge-tts) |
| `SILENCE_SECONDS` | 1.0 | Quiet duration that ends a recording |

Key constants in `ui.py`:

| Constant | Default | Description |
|---|---|---|
| `ORB_SIZE` | 210 | Orb diameter in pixels |
| `SUPERSAMPLE` | 3 | Anti-aliasing factor (lower = faster, higher = smoother) |
| `CAPTION_FADE_START` | 9.0 | Seconds before caption starts fading |
| `CAPTION_FADE_DURATION` | 1.5 | Fade duration once started |

---

## 📁 Project Structure

```
M.A.R.S/
├── main.py                  # Agent loop, model rotation, worker thread
├── tools.py                 # Tool implementations + schemas
├── memory.py                # Long-term fact storage (memories.json)
├── voice.py                 # STT + TTS cascade
├── browser.py               # Playwright web automation
├── desktop.py               # pywinauto UIA desktop automation
├── ui.py                    # Layered-window desktop orb widget
├── requirements.txt
├── .env                     # API keys (not committed)
├── jarvis-high.onnx         # Piper JARVIS voice model
├── jarvis-high.onnx.json    # Voice config
├── memories.json            # Auto-created
├── todos.json               # Auto-created
├── notes.md                 # Auto-created
├── tool_log.jsonl           # Auto-created — one line per tool call
├── orb_position.json        # Auto-created — remembers orb position
├── screenshots/             # Auto-created by browser/desktop tools
└── .browser_profile/        # Auto-created by Playwright (persistent login)
```

---

## 🛠️ Tech Stack

| Component | Technology |
|---|---|
| **LLM providers** | Groq (primary), OpenRouter (fallback), Ollama (local) |
| **Speech-to-text** | Groq Whisper (`whisper-large-v3-turbo`), Google Web Speech (fallback) |
| **Text-to-speech** | Piper (offline, custom JARVIS voice), edge-tts, pyttsx3 |
| **Web automation** | Playwright (Chromium) |
| **Desktop automation** | pywinauto (UIA backend) |
| **UI** | Tkinter + Pillow + Win32 layered windows (`UpdateLayeredWindow`) |
| **Audio** | sounddevice, numpy, winmm.dll via ctypes |
| **Search** | Tavily API |
| **PDF** | pypdf |
| **HTTP** | Groq SDK, OpenAI SDK (for OpenRouter), requests |

---

## ⚠️ Known Limitations

### Voice
- **Wake word accuracy** — "Mars" is a real English word (the planet, the candy bar), so it transcribes consistently but will occasionally false-trigger on unrelated speech. The wake-word set includes several plausible transcriptions (`mars`, `marz`, `marc`, `marks`, etc.). If false positives become annoying, try requiring "Hey Mars" instead.
- **Speaker self-triggering** — with barge-in disabled, the mic is fully muted while TTS plays. With speakers on loud volume, the 2s cooldown may still leak. Headphones eliminate it entirely.
- **Custom wake-word training** — out of scope for this README, but possible with openWakeWord (~1 hour Colab session).

### Browser automation
- **Login walls** require manual setup — log in once in the automation browser (`.browser_profile/`), future runs reuse the session
- **CAPTCHAs and MFA** cannot be solved
- **Anti-bot detection** blocks some sites (LinkedIn, banks, Google account flows)
- **Element indices** are unstable across reads — the model must call `browser_read_page` after every DOM change

### Desktop automation
- Works only with apps that expose a Windows accessibility tree
- Custom-drawn UIs (games, some Electron apps) return "no accessible controls"
- Notepad's Document control is one text region — you can't address individual lines

### Rate limits
- **Groq free tier:** 8,000 tokens per minute on `gpt-oss-120b`. A full tool-call round is ~2,000 tokens, so roughly 4 calls per minute.
- **OpenRouter free tier:** 20 requests/minute, 50/day (or 1000/day with a one-time $10 credit purchase).
- Rotation is silent — you'll see `[model X rate-limited, trying next...]` in the terminal, but the user experience is uninterrupted.

---

## 🤝 Contributing

This is a personal project, but suggestions and pull requests are welcome. If you build something interesting with M.A.R.S., open an issue and tell me about it.

---

## 🙏 Acknowledgements

- **Piper TTS** — for making custom offline neural voices accessible
- **jgkawell/jarvis** — the community voice model that makes M.A.R.S. sound like JARVIS
- **Groq** — for the absurdly fast inference that makes the agent loop feel instant
- **OpenRouter** — for the free-tier model pool that keeps the assistant running when Groq is rate-limited
- **The JARVIS character** — for the inspiration, and for setting an unreasonably high bar for what a personal assistant should feel like

---

<p align="center">
  <em>"Sometimes you gotta run before you can walk."</em><br>
  — Tony Stark
</p>

---
