import os
import sys
import json
import time
import re
import threading
 
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()  


from tools import TOOLS_SCHEMA, execute_tool
from memory import recall_memories
from voice import (
    listen, speak, speak_async, stop_speaking, is_speaking,
    record_until_silence, transcribe_audio,
    calibrate_silence_threshold, contains_wake_word, strip_wake_word,
)
 
MODE = "groq"  # change to "ollama" if you'd rather run fully local

# Models tried in order. Each entry is (provider, model_id).
# Groq models use the Groq SDK. OpenRouter models use the OpenAI SDK
# pointed at openrouter.ai/api/v1. Separate quota pools mean a 429 from
# one provider immediately rotates to the next.
MODEL_CHAIN = [
    # --- Groq (primary — fastest, best tool calling) ---
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "openai/gpt-oss-20b"),
    ("groq", "qwen/qwen3.8-27b"),
    # --- OpenRouter free models (separate quota, broader pool) ---
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ("openrouter", "qwen/qwen3-coder:free"),
    ("openrouter", "openai/gpt-oss-120b:free"),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
    ("openrouter", "minimax/minimax-m2.5:free"),
]
 
SYSTEM_PROMPT = (
    "Your name is Mars. You are a helpful personal assistant. "
    "If asked your name, say Mars. "
    "Relevant memories are auto-injected as '[Relevant memories]' system notes — "
    "use them naturally without mentioning they were injected. "
    "Use remember_fact for durable facts about the user. "
    "Use tools whenever they'd give a better or more current answer than guessing. "
    "Keep replies concise. If nothing can help, say so honestly. "
    "Web search returns general content, not live structured data — for weather "
    "use get_weather; for time use get_datetime. Try a search at most twice. "
    "Never wrap local paths in file:// — pass plain paths. "
    "Browser tools: browser_open loads a page and returns its elements; "
    "browser_click/fill/press_key/go_back also return refreshed state — do NOT "
    "call browser_read_page after them. Only use browser tools when you actually "
    "need to interact; for reading, use web_search. Close with browser_close when done. "
    "Desktop tools: list_windows → get_window_controls → desktop_click/type. "
    "Use desktop_send_keys('%{F4}') to close a focused window, not a guess at the close button. "
    "Before consequential actions (submit, pay, upload), screenshot first for the user. "
)

MAX_HISTORY_TURNS = 10  # how many back-and-forth exchanges to remember in one session
MAX_TOOL_ROUNDS = 25  # safety cap on chained tool calls per user message
MAX_RETRIES = 4  # retries for transient API failures (rate limits, timeouts)
SUMMARY_PREFIX = "[Summary of earlier conversation]"

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _sanitize_messages_for_api(messages):
    """
    Removes or repairs any malformed tool-call exchanges in the message
    history. Guards against a bad entry (missing function name, incomplete
    tool-call exchange, orphaned tool result) poisoning every future
    request with 'Tools should have a name!' or similar harmony errors.
    """
    cleaned = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")

        if role == "assistant" and m.get("tool_calls"):
            valid_tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                name = fn.get("name")
                if not name or not _TOOL_NAME_RE.match(name):
                    print(f"  [sanitize: dropping tool_call with bad name: {name!r}]")
                    continue
                valid_tcs.append(tc)

            if not valid_tcs:
                cleaned.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                })
                i += 1
                continue

            needed_ids = {tc["id"] for tc in valid_tcs}
            lookahead = i + 1
            found_ids = set()
            while lookahead < len(messages) and messages[lookahead].get("role") == "tool":
                tid = messages[lookahead].get("tool_call_id")
                if tid in needed_ids:
                    found_ids.add(tid)
                lookahead += 1

            if found_ids != needed_ids:
                print(f"  [sanitize: dropping incomplete tool exchange "
                      f"({len(found_ids)}/{len(needed_ids)} results present)]")
                i = lookahead
                continue

            m = dict(m)
            m["tool_calls"] = valid_tcs
            cleaned.append(m)
            i += 1
            while i < lookahead:
                cleaned.append(messages[i])
                i += 1
            continue

        if role == "tool":
            print("  [sanitize: dropping orphaned tool message]")
            i += 1
            continue

        cleaned.append(m)
        i += 1

    return cleaned


def _call_with_retry(func):
    """
    Retries transient failures with exponential backoff. 429s are handled
    specially — they get a longer backoff (and honor Retry-After when
    Groq provides it), since a per-minute rate limit won't clear in 2s.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return func()
        except Exception as e:
            last_error = e
            msg = str(e).lower()

            is_429 = "429" in msg or "rate limit" in msg

            transient = is_429 or any(k in msg for k in (
                "timeout", "timed out",
                "connection reset", "connection aborted",
                "503", "502", "overloaded",
            ))
            if not transient or attempt == MAX_RETRIES - 1:
                raise

            delay = None
            try:
                resp = getattr(e, "response", None)
                if resp is not None:
                    ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                    if ra:
                        delay = int(float(ra)) + 1
            except Exception:
                pass

            if delay is None:
                if is_429:
                    delay = [5, 15, 30][min(attempt, 2)]
                else:
                    delay = 2 ** (attempt + 1)

            print(f"  [temporary issue ({e}), retrying in {delay}s...]")
            time.sleep(delay)
    raise last_error


def _chat_create(client, messages, use_tools=True, temperature=0.7,
                 openrouter_client=None):
    """
    Tries each (provider, model) in MODEL_CHAIN in order. Uses the Groq
    SDK for groq models and the OpenAI SDK (pointed at OpenRouter) for
    openrouter models. On 429 or "model unavailable" from either, rotates
    immediately to the next entry.

    If openrouter_client is None (no key configured), OpenRouter entries
    are skipped silently.
    """
    safe_messages = _sanitize_messages_for_api(messages)

    kwargs = dict(messages=safe_messages, temperature=temperature)
    if use_tools:
        kwargs["tools"] = TOOLS_SCHEMA

    last_error = None

    for provider, model_id in MODEL_CHAIN:
        if provider == "openrouter" and openrouter_client is None:
            continue

        active_client = client if provider == "groq" else openrouter_client
        if active_client is None:
            continue

        try:
            return active_client.chat.completions.create(
                model=model_id, **kwargs
            )
        except Exception as e:
            msg = str(e).lower()

            if "429" in msg or "rate limit" in msg:
                print(f"  [{provider}/{model_id} rate-limited, trying next...]")
                last_error = e
                continue

            if "503" in msg or "overloaded" in msg or "over capacity" in msg:
                print(f"  [{provider}/{model_id} over capacity, trying next...]")
                last_error = e
                continue

            if "402" in msg or "insufficient" in msg:
                print(f"  [{provider}/{model_id} out of credits, trying next...]")
                last_error = e
                continue

            if "model" in msg and (
                "not found" in msg
                or "does not exist" in msg
                or "invalid" in msg
                or "decommissioned" in msg
            ):
                print(f"  [{provider}/{model_id} unavailable, skipping...]")
                last_error = e
                continue

            raise

    first_groq = next((m for p, m in MODEL_CHAIN if p == "groq"), None)
    if first_groq and client is not None:
        print(f"  [all models exhausted, waiting on groq/{first_groq}...]")
        return _call_with_retry(lambda: client.chat.completions.create(
            model=first_groq, **kwargs
        ))

    raise last_error or RuntimeError("No usable model in MODEL_CHAIN")


def get_groq_client():
    from groq import Groq
 
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("ERROR: Set the GROQ_API_KEY environment variable first.")
        print("Get a free key at https://console.groq.com")
        sys.exit(1)
    print(f"[debug] Using key starting with: {api_key[:8]}... (length {len(api_key)})")
    return Groq(api_key=api_key, max_retries=0)


def get_openrouter_client():
    """Returns an OpenAI-SDK client pointed at OpenRouter, or None if the
    key isn't configured."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("  [openrouter skipped: 'openai' package not installed]")
        return None
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://github.com/your-repo/mars",
            "X-OpenRouter-Title": "M.A.R.S Assistant",
        },
    )


def chat_groq(client, messages, openrouter_client=None, confirm_fn=None, on_tool=None):
    """
    Runs one full turn, including any tool calls the model makes along the
    way. on_tool, if given, is called with the tool name right before each
    tool executes — used to update the orb's status text.
    """
    for _ in range(MAX_TOOL_ROUNDS):
        response = _chat_create(
            client,
            messages,
            use_tools=True,
            temperature=0.7,
            openrouter_client=openrouter_client,
        )
        msg = response.choices[0].message
 
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            return msg.content, messages

        valid_tcs = []
        for tc in msg.tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn is not None else None
            if not name:
                print(f"  [dropping tool call with missing name: id={getattr(tc,'id',None)}]")
                continue
            valid_tcs.append(tc)

        if not valid_tcs:
            fallback = msg.content or "(model returned an invalid tool call)"
            messages.append({"role": "assistant", "content": fallback})
            return fallback, messages

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in valid_tcs
            ],
        })
 
        for tc in valid_tcs:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                result = (
                    f"Error: the arguments for {tc.function.name} were not valid JSON "
                    f"({tc.function.arguments!r}). Try calling it again with corrected arguments."
                )
                print(f"  [tool call had malformed arguments: {tc.function.name}]")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                continue

            # Notify UI before the tool runs
            if on_tool is not None:
                try:
                    on_tool(tc.function.name)
                except Exception:
                    pass

            if confirm_fn is not None:
                result = execute_tool(tc.function.name, args, confirm_fn=confirm_fn)
            else:
                result = execute_tool(tc.function.name, args)
            preview = str(result)[:100].replace("\n", " ")
            print(f"  [using tool: {tc.function.name}({args}) -> {preview}...]")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })
 
    messages.append({
        "role": "user",
        "content": "Please answer now using only what you've already found — don't call any more tools.",
    })
    response = _chat_create(
        client, messages,
        use_tools=False,
        temperature=0.7,
        openrouter_client=openrouter_client,
    )
    final = response.choices[0].message.content
    messages.append({"role": "assistant", "content": final})
    return final, messages
 
 
def chat_ollama(messages):
    import ollama
 
    response = ollama.chat(model="llama3.2:3b", messages=messages)
    return response["message"]["content"]
 
 
def _has_summary(messages):
    return len(messages) > 1 and messages[1]["role"] == "system" \
        and (messages[1]["content"] or "").startswith(SUMMARY_PREFIX)


def _summarize_dropped(client, dropped, prior_summary, openrouter_client=None):
    """
    Condenses messages about to be trimmed into a short running summary.
    Falls back to the prior summary unchanged if the summarization call fails.
    """
    lines = []
    for m in dropped:
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and content:
            lines.append(f"{role.capitalize()}: {content}")
    transcript = "\n".join(lines)
    if not transcript.strip():
        return prior_summary

    prompt = (
        "Summarize the key facts, decisions, and context from this conversation "
        "excerpt in 2-4 short sentences — only what would matter for continuing "
        "the conversation later. Be concise, plain prose, no preamble.\n\n"
    )
    if prior_summary:
        prompt += f"Existing summary so far:\n{prior_summary}\n\n"
    prompt += f"New excerpt to fold in:\n{transcript}"

    try:
        response = _chat_create(
            client,
            [{"role": "user", "content": prompt}],
            use_tools=False,
            temperature=0.3,
            openrouter_client=openrouter_client,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return prior_summary or "(summary unavailable due to an API error)"


def trim_history(client, messages, openrouter_client=None):
    """
    Keeps the most recent MAX_HISTORY_TURNS exchanges in full. Older ones
    are condensed into a running summary in messages[1].
    """
    system = messages[0]
    has_summary = _has_summary(messages)
    prior_summary = messages[1]["content"][len(SUMMARY_PREFIX):].strip() if has_summary else ""
    rest = messages[2:] if has_summary else messages[1:]

    max_messages = MAX_HISTORY_TURNS * 2
    if len(rest) <= max_messages:
        return [system, messages[1]] + rest if has_summary else [system] + rest

    cut = len(rest) - max_messages
    while cut < len(rest) and rest[cut].get("role") == "tool":
        cut += 1
    if cut < len(rest) and rest[cut].get("role") == "assistant" and rest[cut].get("tool_calls"):
        cut += 1
    cut = min(cut, len(rest))

    dropped, kept = rest[:cut], rest[cut:]
    if not dropped:
        return [system, messages[1]] + rest if has_summary else [system] + rest

    if client is None:
        return [system] + kept

    new_summary = _summarize_dropped(client, dropped, prior_summary,
                                     openrouter_client=openrouter_client)
    summary_msg = {"role": "system", "content": f"{SUMMARY_PREFIX}\n{new_summary}"}
    return [system, summary_msg] + kept
 
 
_AFFIRM_WORDS = {"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed",
                  "go ahead", "do it", "proceed", "okay", "ok", "affirmative"}
_DENY_WORDS = {"no", "nope", "cancel", "stop", "dont", "negative", "abort", "wait"}


def _voice_confirm(name, args, submit_kw, threshold, voice_output):
    """
    Replaces the keyboard confirmation prompt when a tool call needing
    confirmation happens during a voice turn. Speaks the question aloud
    regardless of voice_output — a safety prompt mid hands-free use needs
    to be heard. Any ambiguity defaults to NO.
    """
    label = name.replace("_", " ")
    detail = f" — {submit_kw}" if submit_kw else ""
    question = f"I'm about to {label}{detail}. Should I go ahead?"
    print(f"\n  [voice confirm] {question} (say yes or no)")
    speak(question)

    try:
        audio, spoke = record_until_silence(threshold=threshold, max_seconds=8)
    except Exception as e:
        print(f"  [voice confirm] Mic error ({e}) — defaulting to NO for safety.")
        return False

    if not spoke:
        print("  [voice confirm] No response heard — defaulting to NO for safety.")
        speak("I didn't hear a response, so I'll hold off on that.")
        return False

    answer = transcribe_audio(audio)
    if not answer or answer.startswith("["):
        print("  [voice confirm] Couldn't understand the response — defaulting to NO for safety.")
        speak("I couldn't make that out, so I'll hold off on that.")
        return False

    print(f"  [voice confirm] Heard: \"{answer}\"")
    words = set(_normalize_voice_command(answer).split())

    if words & _AFFIRM_WORDS and not (words & _DENY_WORDS):
        speak("Okay, doing it.")
        return True
    if words & _DENY_WORDS:
        speak("Okay, cancelling that.")
        return False

    print("  [voice confirm] Unclear response — defaulting to NO for safety.")
    speak("That wasn't clear, so I'll hold off on that for now.")
    return False


def process_turn(client, messages, user_input, voice_output,
                 openrouter_client=None, is_voice=False, threshold=None,
                 on_reply=None, on_tool=None):
    """
    Runs one full turn — memory recall, the model/tool-calling exchange,
    printing (and optionally speaking) the reply.

    on_reply: called with the final reply string right before speaking —
              used to push text to the orb's caption.
    on_tool:  passed to chat_groq, called with the tool name before each
              tool runs — used to update the orb's status.
    """
    messages = _strip_memory_notes(messages)
    rollback_at = len(messages)
    relevant = recall_memories(user_input, n_results=3)
    if relevant:
        memory_note = "[Relevant memories]\n" + "\n".join(f"- {m}" for m in relevant)
        messages.append({"role": "system", "content": memory_note})

    messages.append({"role": "user", "content": user_input})
    messages = trim_history(client, messages, openrouter_client=openrouter_client)

    if is_voice and threshold is not None:
        def confirm_fn(name, args, submit_kw):
            return _voice_confirm(name, args, submit_kw, threshold, voice_output)
    else:
        confirm_fn = None

    try:
        if MODE == "groq":
            reply, messages = chat_groq(
                client, messages,
                openrouter_client=openrouter_client,
                confirm_fn=confirm_fn,
                on_tool=on_tool,
            )
        else:
            reply = chat_ollama(messages)
            messages.append({"role": "assistant", "content": reply})
    except Exception as e:
        print(f"\n[Error talking to the model: {e}]")
        del messages[rollback_at:]
        return messages

    print(f"\nAssistant: {reply}")
    if on_reply is not None:
        try:
            on_reply(reply)
        except Exception:
            pass
    if voice_output and reply:
        speak_async(reply)
    return messages


# Voice-path phrases (spoken while the mic is live)
_SUSPEND_PHRASES = {"stop listening", "exit wake mode", "that's all", "quit", "exit"}
_SLEEP_PHRASES = {"go to sleep", "never mind", "that's all for now", "stop listening for now", "standby", "sleep", "pause listening"}
_VOICE_ON_PHRASES = {"voice on", "speak", "talk", "talk to me", "start talking", "turn on voice", "enable voice"}
_VOICE_OFF_PHRASES = {"voice off", "stop talking", "be quiet", "stay quiet", "turn off voice", "disable voice", "mute"}
ACTIVE_IDLE_TIMEOUT = 30

# Type-box command phrases. These are matched exactly (after normalizing
# case/punctuation) and short-circuit before reaching the model, so typing
# "wake" doesn't become a chat message.
_TYPE_RESUME = {"wake", "wake up", "start listening", "resume", "resume listening"}
_TYPE_SUSPEND = {"stop listening", "sleep", "suspend", "go to sleep"}
_TYPE_VOICE_ON = {"voice on"}
_TYPE_VOICE_OFF = {"voice off"}


def _strip_memory_notes(messages):
    return [m for m in messages if not (
        m.get("role") == "system"
        and (m.get("content") or "").startswith("[Relevant memories]")
    )]


def _normalize_voice_command(text):
    """Lowercase, strip punctuation, collapse whitespace."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


def _matches_voice_phrase(text, phrases):
    """True if the normalized text equals one of `phrases`, or the same
    phrase repeated with only whitespace between occurrences."""
    t = _normalize_voice_command(text)
    if not t:
        return False
    if t in phrases:
        return True
    for phrase in phrases:
        if re.fullmatch(r"(?:" + re.escape(phrase) + r"\s*)+", t):
            return True
    return False

# --- Mic listening behavior ---
# ALLOW_BARGE_IN: if True, you can interrupt Mars mid-reply by speaking.
#   Only reliable with HEADPHONES. With speakers, the mic hears Mars's own
#   voice and self-triggers — leave this False if you're on speakers.
ALLOW_BARGE_IN = False

# After Mars finishes speaking, hold an elevated threshold for this many
# seconds so the tail of the TTS playback doesn't get picked up as a command.
TTS_COOLDOWN_SECONDS = 2

class _SharedState:
    """Tiny mutable container so the UI thread's 'toggle voice' menu item
    can flip a flag the worker thread reads, without needing a lock for a
    single bool (GIL makes this safe enough for our purposes)."""
    def __init__(self):
        self.voice_output = True  # on by default — no terminal to read replies from


def run_worker(orb, client, openrouter_client, shared):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    energy_threshold = calibrate_silence_threshold()

    active = False
    last_activity = 0.0
    last_speak_end = 0.0
    prev_speaking = False
    listening_suspended = False  # set by "stop listening"; cleared by typing "wake"

    orb.set_state("idle")

    # Closure used by every process_turn call — updates the orb's status
    # strip with the current tool name while a tool runs.
    def _on_tool(name):
        orb.set_state("thinking", f"Tool: {name}")

    while not orb.exit_flag.is_set():
        # --- 1) Typed input from the orb's click-to-type box ---
        try:
            typed_text = orb.typed_queue.get_nowait()
        except Exception:
            typed_text = None

        if typed_text is not None:
            # Intercept control phrases before they reach the model, so
            # typing "wake" or "voice off" acts as a command, not a chat.
            lowered = _normalize_voice_command(typed_text)

            if lowered in _TYPE_RESUME:
                listening_suspended = False
                active = False
                orb.set_state("idle")
                orb.set_reply("Listening for 'Mars'.")
                if shared.voice_output:
                    speak("Back online.")
                continue

            if lowered in _TYPE_SUSPEND:
                listening_suspended = True
                active = False
                stop_speaking()
                orb.set_state("idle", "Asleep")
                orb.set_reply("Microphone paused. Type 'wake' to resume.")
                if shared.voice_output:
                    speak("Going to sleep.")
                continue

            if lowered in _TYPE_VOICE_ON:
                shared.voice_output = True
                orb.set_reply("Voice output enabled.")
                speak("Voice on.")
                continue

            if lowered in _TYPE_VOICE_OFF:
                shared.voice_output = False
                orb.set_reply("Voice output disabled.")
                continue

            # Normal typed request
            orb.set_transcript(typed_text)
            orb.set_state("thinking", "Thinking...")
            messages = process_turn(
                client, messages, typed_text, shared.voice_output,
                openrouter_client=openrouter_client,
                is_voice=True, threshold=energy_threshold,
                on_reply=orb.set_reply,
                on_tool=_on_tool,
            )
            orb.set_state("listening" if active else "idle")
            continue

        # --- 2) Mic suspended: skip polling entirely. Only the type box
        #        can bring us back. Keeps the CPU quiet and the mic off. ---
        if listening_suspended:
            time.sleep(0.2)
            continue

        # --- 3) Always-on wake-word listening ---
        speaking = is_speaking()
        if prev_speaking and not speaking:
            last_speak_end = time.time()
        prev_speaking = speaking

        if speaking and not ALLOW_BARGE_IN:
            time.sleep(0.1)
            continue

        time_since_speak = time.time() - last_speak_end
        if time_since_speak < TTS_COOLDOWN_SECONDS:
            time.sleep(0.1)
            continue

        effective_threshold = energy_threshold
        if speaking:
            effective_threshold = energy_threshold * 8.0
        elif time_since_speak < TTS_COOLDOWN_SECONDS:
            effective_threshold = energy_threshold * 3.0

        orb.set_state("listening" if active else "idle")

        try:
            audio, spoke = record_until_silence(
                threshold=effective_threshold, max_seconds=4
            )
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  [microphone error: {e}]")
            time.sleep(0.5)
            continue

        if not spoke:
            if active and (time.time() - last_activity) > ACTIVE_IDLE_TIMEOUT:
                active = False
                orb.set_state("idle")
            continue

        orb.set_state("thinking", "Transcribing...")
        text = transcribe_audio(audio)
        if not text or text.startswith("["):
            orb.set_state("listening" if active else "idle")
            continue

        # Show what the mic heard
        orb.set_transcript(text)

        if active:
            command = strip_wake_word(text) if contains_wake_word(text) else text
        elif contains_wake_word(text):
            command = strip_wake_word(text)
            active = True
            last_activity = time.time()
        else:
            orb.set_state("idle")
            continue

        if not command:
            orb.set_state("listening")
            if shared.voice_output:
                speak("Yes?")
            last_activity = time.time()
            continue

        last_activity = time.time()

        if _matches_voice_phrase(command, _SUSPEND_PHRASES):
            # Full suspend: mic goes dark, only the type box can wake us.
            listening_suspended = True
            active = False
            stop_speaking()
            orb.set_state("idle", "Asleep")
            if shared.voice_output:
                speak("Okay, I'll stop listening.")
            continue

        if _matches_voice_phrase(command, _SLEEP_PHRASES):
            active = False
            orb.set_state("idle")
            continue

        if _matches_voice_phrase(command, _VOICE_ON_PHRASES):
            shared.voice_output = True
            speak("Voice on.")
            active = True
            orb.set_state("idle")
            continue

        if _matches_voice_phrase(command, _VOICE_OFF_PHRASES):
            shared.voice_output = False
            speak("Okay, going quiet.")
            active = True
            orb.set_state("idle")
            continue

        active = True
        orb.set_state("thinking", "Thinking...")
        messages = process_turn(
            client, messages, command, shared.voice_output,
            openrouter_client=openrouter_client,
            is_voice=True, threshold=energy_threshold,
            on_reply=orb.set_reply,
            on_tool=_on_tool,
        )
        orb.set_state("listening" if active else "idle")


def main():
    from ui import AssistantOrb
    from voice import set_speech_visual_callbacks

    client = get_groq_client() if MODE == "groq" else None
    openrouter_client = get_openrouter_client() if MODE == "groq" else None
    if openrouter_client:
        print("[openrouter] Client ready — using free models as fallback")
    else:
        print("[openrouter] Not configured (set OPENROUTER_API_KEY in .env)")

    shared = _SharedState()
    orb = AssistantOrb(on_toggle_voice=lambda: setattr(
        shared, "voice_output", not shared.voice_output
    ))

    # Wire voice.py's speech-start/end events straight to the orb, so Piper's
    # real waveform (or the estimated-duration fallback) drives the pulse.
    set_speech_visual_callbacks(orb.speak_started, orb.speak_ended)

    worker = threading.Thread(
        target=run_worker, args=(orb, client, openrouter_client, shared),
        daemon=True,
    )
    worker.start()

    try:
        orb.run()  # blocks on the Tkinter mainloop (must be the main thread)
    finally:
        orb.exit_flag.set()
        try:
            stop_speaking()
        except Exception:
            pass
        try:
            import browser as _browser
            _browser.browser_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()