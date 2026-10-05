import io
import os
import re
import threading
import wave

import numpy as np
import sounddevice as sd
import speech_recognition as sr
import pyttsx3

TTS_TIMEOUT_SECONDS = 20  # hard cap so a bad TTS run can't freeze the program

CHUNK_SECONDS = 0.3          # granularity for checking mic energy
SILENCE_SECONDS = 1.3        # quiet duration (after speech) that ends a recording
MAX_RECORD_SECONDS = 15      # hard cap per recording regardless of silence
DEFAULT_ENERGY_THRESHOLD = 300  # fallback if calibration isn't run
FALLBACK_SAMPLE_RATE = 16000  # used only if device querying fails outright

_sample_rate = None
# Tracks the currently-playing MCI alias so it can be stopped from any thread.
_current_playback_alias = None
_playback_lock = threading.Lock()
_speak_gen_lock = threading.Lock()
_speak_gen = 0
# Cached Groq client for STT — creating a fresh client per transcription
# would redo TLS handshakes and connection setup on every voice call.
_groq_stt_client = None

# ---------- UI visualization hooks ----------
# Optional callbacks a UI (e.g. ui.py's desktop orb) can register to sync a
# visual to speech. on_start(envelope, duration_seconds) fires right before
# playback begins; envelope is a list of floats 0..1 (one per ~50ms window)
# for Piper's real waveform, or None when only an estimated duration is
# available (edge-tts/pyttsx3 fallback) — callers should fall back to a
# generic pulse animation in that case. on_end() fires once playback ends.
_on_speak_start = None
_on_speak_end = None


def set_speech_visual_callbacks(on_start, on_end):
    """Registers UI callbacks. Pass (None, None) to clear them."""
    global _on_speak_start, _on_speak_end
    _on_speak_start = on_start
    _on_speak_end = on_end


def _notify_speak_start(envelope, duration):
    if _on_speak_start is not None:
        try:
            _on_speak_start(envelope, duration)
        except Exception:
            pass  # a broken UI callback must never break speech itself


def _notify_speak_end():
    if _on_speak_end is not None:
        try:
            _on_speak_end()
        except Exception:
            pass


def _compute_envelope(frames, sample_rate, sample_width, channels, chunk_ms=50):
    """
    Converts raw PCM frames into a list of normalized 0..1 loudness values,
    one per chunk_ms window — a cheap amplitude envelope for driving a
    pulse animation in sync with real speech, no extra dependencies.
    """
    if sample_width != 2:
        return []  # only handling int16 PCM, which is what Piper/wave give us
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    chunk_samples = max(1, int(sample_rate * chunk_ms / 1000))
    n_chunks = max(1, len(audio) // chunk_samples)
    envelope = []
    for i in range(n_chunks):
        seg = audio[i * chunk_samples:(i + 1) * chunk_samples]
        if seg.size == 0:
            envelope.append(0.0)
            continue
        envelope.append(float(np.sqrt(np.mean(seg ** 2))))
    peak = max(envelope) if envelope else 1.0
    if peak <= 0:
        return [0.0] * len(envelope)
    return [min(1.0, v / peak) for v in envelope]


def _get_sample_rate():
    """
    Uses the input device's own default sample rate instead of forcing
    16kHz. Forcing an unsupported rate is a common cause of sounddevice
    silently hanging on Windows (the recording never actually starts, so
    sd.wait() blocks forever waiting for a completion that never comes).
    """
    global _sample_rate
    if _sample_rate is None:
        try:
            info = sd.query_devices(kind="input")
            _sample_rate = int(info["default_samplerate"])
        except Exception:
            _sample_rate = FALLBACK_SAMPLE_RATE
    return _sample_rate

# Wake word: "Mars". Unlike "Jev" (a nonsense syllable that needed a long
# homophone list to catch Whisper/Google mangling it), "Mars" is a real,
# unambiguous word — so it transcribes reliably and needs far fewer
# variants here. The flip side: it's a common word, so expect MORE false
# wake-triggers than "Jev" ever had — "Bruno Mars," "Mars bar," the planet,
# etc. will all now trip the wake word in normal conversation. If that
# becomes annoying, the fix is a less common wake phrase, not more entries
# here (see the "check"/"dev" regression note from the previous rename —
# don't add real dictionary words to this set).
WAKE_WORDS = {
    "mars", "marz", "marrs",
}
_WAKE_PATTERN = re.compile(r"\b(" + "|".join(WAKE_WORDS) + r")\b", re.IGNORECASE)

_MARKDOWN_EMPHASIS = re.compile(r"[*_`#]+")
_SMART_PUNCTUATION = {
    "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
}


def _clean_for_speech(text):
    """
    Strips markdown formatting that was written to be read, not heard —
    table pipes, emphasis symbols, smart quotes/dashes. A raw markdown
    table in particular seems to be what tripped up the Windows TTS driver
    in testing, so table rows get turned into plain comma-separated text.
    """
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Skip table separator rows like |---|---|
        if stripped and re.fullmatch(r"[\s\-:|]+", stripped) and "|" in stripped:
            continue
        # Turn a table row into plain comma-separated text
        if stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            lines.append(", ".join(c for c in cells if c))
        else:
            lines.append(line)
    cleaned = "\n".join(lines)
    cleaned = _MARKDOWN_EMPHASIS.sub("", cleaned)
    for bad, good in _SMART_PUNCTUATION.items():
        cleaned = cleaned.replace(bad, good)
    return cleaned.strip()

# ---------- Piper (primary — custom voice, fully offline) ----------

# Name of the Piper voice model to use. Must match a downloaded model.
# Download more with: py -m piper.download_voices <name>
# Browse all: https://huggingface.co/rhasspy/piper-voices
PIPER_VOICE = "jarvis-high"

_piper_voice = None  # cached PiperVoice instance


def _get_piper_voice():
    """Lazily loads the Piper voice model. Returns None if piper-tts is
    not installed, the model isn't downloaded, or loading fails."""
    global _piper_voice
    if _piper_voice is not None:
        return _piper_voice
    try:
        from piper import PiperVoice
        from pathlib import Path
        import os

        # Piper downloads voices to the user data dir. Locate the .onnx file.
        candidates = [
            Path(os.environ.get("APPDATA", "")) / "piper-tts" / f"{PIPER_VOICE}.onnx",
            Path.home() / ".local" / "share" / "piper" / f"{PIPER_VOICE}.onnx",
            Path.cwd() / f"{PIPER_VOICE}.onnx",
        ]
        model_path = next((p for p in candidates if p.exists()), None)
        if model_path is None:
            print(f"  [piper voice {PIPER_VOICE!r} not found. "
                  f"Run: py -m piper.download_voices {PIPER_VOICE}]")
            return None

        _piper_voice = PiperVoice.load(str(model_path))
        return _piper_voice
    except ImportError:
        return None  # piper-tts not installed — silent fallback
    except Exception as e:
        print(f"  [piper load failed: {e}]")
        return None
def _synth_part_to_pcm(voice, text):
    """Synthesizes a single fragment to raw PCM bytes. Returns
    (frames_bytes, sample_rate, sample_width, channels)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)
    buf.seek(0)
    with wave.open(buf, "rb") as wf:
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        nch = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    return frames, sr, sw, nch

def _synthesize_with_pauses(voice, text, sentence_pause_ms=420):
    """
    Synthesizes text with an explicit pause after each sentence. Piper
    itself doesn't insert silence at full stops — it reads straight
    through — so we split at sentence boundaries, synthesize each
    sentence, and concatenate with silence between them. Commas are left
    intact so Piper's own prosody handles them naturally.
    """
    import re

    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return None

    # Split after . ! ? when followed by whitespace or end of string.
    # The punctuation stays attached to the preceding sentence.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return None

    all_frames = b""
    sample_rate = None
    sample_width = 2
    channels = 1

    for i, sentence in enumerate(sentences):
        frames, sr, sw, nch = _synth_part_to_pcm(voice, sentence)
        if sample_rate is None:
            sample_rate = sr
            sample_width = sw
            channels = nch
        all_frames += frames

        if i < len(sentences) - 1:
            # Silence: (ms / 1000) * samples_per_sec * bytes_per_sample * channels
            silence = b"\x00" * int(sr * sentence_pause_ms / 1000 * sw * nch)
            all_frames += silence

    return all_frames, sample_rate, sample_width, channels

def _speak_piper(text, generation=None):
    voice = _get_piper_voice()
    if voice is None:
        return False

    import os
    import tempfile
    import wave

    tmp_path = None
    try:
        for attr, val in (
            ("noise_scale", 0.35),
            ("noise_w", 0.5),
            ("noise_w_scale", 0.5),
            ("length_scale", 1.18),
        ):
            try:
                setattr(voice.config, attr, val)
            except Exception:
                pass

        result = _synthesize_with_pauses(voice, text, sentence_pause_ms=420)
        if result is None:
            return False
        frames, sr, sw, nch = result

        # If a newer speak request came in during synthesis, discard this one
        if generation is not None:
            with _speak_gen_lock:
                if generation != _speak_gen:
                    return True  # claimed success so cascade doesn't retry

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(nch)
            wf.setsampwidth(sw)
            wf.setframerate(sr)
            wf.writeframes(frames)

        duration = len(frames) / (sr * sw * nch)
        envelope = _compute_envelope(frames, sr, sw, nch)
        _notify_speak_start(envelope, duration)
        try:
            _play_wav_windows(tmp_path, generation=generation)
        finally:
            _notify_speak_end()
        return True
    except Exception as e:
        print(f"  [piper synthesis failed: {e}]")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def _play_wav_windows(path, generation=None):
    _play_audio_windows(path, "waveaudio", generation)

# ---------- edge-tts (secondary — fallback neural voice, online only) ----------

EDGE_VOICE = "en-US-GuyNeural"

def _play_mp3_windows(path, generation=None):
    _play_audio_windows(path, "mpegvideo", generation)
    
def _play_audio_windows(path, mci_type, generation=None):
    """Plays audio via MCI. If `generation` is provided, aborts playback
    if a newer speak request has already superseded this one — this is
    what prevents two TTS threads from playing over each other."""
    global _current_playback_alias
    import ctypes
    import time as _time
    alias = f"mars_play_{int(_time.time() * 1000) % 100000000}"

    with _playback_lock:
        if generation is not None and generation != _speak_gen:
            return  # superseded before we got the lock — bail silently
        prev = _current_playback_alias
        _current_playback_alias = alias

    mci = ctypes.windll.winmm.mciSendStringW
    if prev:
        try:
            mci(f"stop {prev}", None, 0, None)
        except Exception:
            pass

    mci(f'open "{path}" type {mci_type} alias {alias}', None, 0, None)
    try:
        mci(f"play {alias} wait", None, 0, None)
    finally:
        try:
            mci(f"close {alias}", None, 0, None)
        except Exception:
            pass
        with _playback_lock:
            if _current_playback_alias == alias:
                _current_playback_alias = None

def _speak_edge(text, generation=None):
    try:
        import asyncio
        import os
        import platform
        import tempfile
        import edge_tts
    except ImportError:
        return False

    if platform.system() != "Windows":
        return False

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        async def _synthesize():
            communicate = edge_tts.Communicate(text, EDGE_VOICE)
            await communicate.save(tmp_path)

        asyncio.run(_synthesize())

        if generation is not None:
            with _speak_gen_lock:
                if generation != _speak_gen:
                    return True

        # No cheap way to get real mp3 amplitude without decoding it (would
        # need ffmpeg/pydub) — estimate duration from word count instead and
        # let the UI fall back to a generic speech-like pulse (envelope=None).
        estimated_duration = max(1.0, len(text.split()) / 2.5)
        _notify_speak_start(None, estimated_duration)
        try:
            _play_mp3_windows(tmp_path, generation=generation)
        finally:
            _notify_speak_end()
        return True
    except Exception as e:
        print(f"  [edge-tts unavailable ({e})]")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ---------- pyttsx3 (last resort — offline SAPI5) ----------

def _speak_pyttsx3(text):
    """Fresh engine instance per call rather than a cached one."""
    estimated_duration = max(1.0, len(text.split()) / 2.5)
    _notify_speak_start(None, estimated_duration)
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception as e:
        print(f"  [voice output failed: {e}]")
    finally:
        _notify_speak_end()


# ---------- Cascade ----------

def _speak_now(text, generation=None):
    if _speak_piper(text, generation=generation):
        return
    if generation is not None:
        with _speak_gen_lock:
            if generation != _speak_gen:
                return
    if _speak_edge(text, generation=generation):
        return
    _speak_pyttsx3(text)

def is_speaking():
    with _playback_lock:
        return _current_playback_alias is not None


def stop_speaking():
    global _current_playback_alias
    with _playback_lock:
        alias = _current_playback_alias
        _current_playback_alias = None
    if not alias:
        return False
    import ctypes
    try:
        ctypes.windll.winmm.mciSendStringW(f"stop {alias}", None, 0, None)
        return True
    except Exception:
        return False

def speak(text, timeout=TTS_TIMEOUT_SECONDS):
    """Blocking version of speak_async. Uses the same generation system,
    so it cancels any in-flight playback before it starts."""
    thread = speak_async(text)
    if thread is None:
        return
    thread.join(timeout=timeout)
    if thread.is_alive():
        print("  [voice output is taking unusually long — continuing without waiting]")
        
def speak_async(text):
    global _speak_gen
    cleaned = _clean_for_speech(text)
    if not cleaned:
        return None
    with _speak_gen_lock:
        _speak_gen += 1
        my_gen = _speak_gen
    stop_speaking()
    t = threading.Thread(target=_speak_now, args=(cleaned, my_gen), daemon=True)
    t.start()
    return t

def _rms(chunk):
    if chunk.size == 0:
        return 0.0
    data = chunk.astype(np.float64)
    return float(np.sqrt(np.mean(data ** 2)))


def calibrate_silence_threshold(duration=1.0):
    """
    Records a short ambient sample (assumes the room is quiet right now)
    and returns a threshold scaled to that noise floor. More reliable than
    a hardcoded guess across different mics and rooms. Falls back to the
    default threshold if the mic isn't available for some reason.
    """
    try:
        print("  [calibrating mic — stay quiet for a second...]")
        rate = _get_sample_rate()
        with sd.InputStream(samplerate=rate, channels=1, dtype="int16") as stream:
            chunk, _ = stream.read(int(duration * rate))
        ambient = _rms(chunk)
        result = max(ambient * 3, DEFAULT_ENERGY_THRESHOLD)
        print(f"  [ambient level: {ambient:.0f}, threshold set to: {result:.0f}]")
        return result
    except Exception as e:
        print(f"  [calibration failed ({e}), using default threshold]")
        return DEFAULT_ENERGY_THRESHOLD


def record_until_silence(threshold=DEFAULT_ENERGY_THRESHOLD, max_seconds=MAX_RECORD_SECONDS,
                          silence_seconds=SILENCE_SECONDS, chunk_seconds=CHUNK_SECONDS):
    """
    Records from the mic, stopping once there's been `silence_seconds` of
    quiet following actual speech, or after `max_seconds` as a hard cap
    (also the cap when nothing is said at all — used by wake mode to poll
    in bounded sweeps rather than blocking forever).

    Returns (audio_array, spoke_at_all). spoke_at_all is False if the whole
    recording stayed under the energy threshold (pure silence/background
    noise) — callers can skip transcription entirely in that case.
    """
    rate = _get_sample_rate()
    chunk_samples = int(chunk_seconds * rate)
    chunks = []
    silence_run = 0.0
    elapsed = 0.0
    speech_started = False

    # A single open stream with blocking reads — more reliable on Windows
    # than repeatedly calling sd.rec()/sd.wait(), which can hang
    # indefinitely if a recording never properly starts.
    with sd.InputStream(samplerate=rate, channels=1, dtype="int16") as stream:
        while elapsed < max_seconds:
            chunk, _ = stream.read(chunk_samples)
            chunks.append(chunk.copy())
            elapsed += chunk_seconds

            if _rms(chunk) > threshold:
                speech_started = True
                silence_run = 0.0
            else:
                silence_run += chunk_seconds

            if speech_started and silence_run >= silence_seconds:
                break

    if not chunks:
        return np.zeros((0, 1), dtype="int16"), False

    return np.concatenate(chunks, axis=0), speech_started


def _audio_to_wav_bytes(audio_array, sample_rate=None):
    if sample_rate is None:
        sample_rate = _get_sample_rate()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes/sample
        wf.setframerate(sample_rate)
        wf.writeframes(audio_array.tobytes())
    buf.seek(0)
    return buf

# Model used for Groq Whisper transcription. "whisper-large-v3-turbo" is
# ~8x faster than "whisper-large-v3" and only marginally less accurate —
# the right tradeoff for short voice commands.
GROQ_STT_MODEL = "whisper-large-v3-turbo"

_HALLUCINATION_PHRASES = {
    "", ".", "?", "!", "...",
    "thank you", "thanks", "thank you.", "thanks.",
    "thank you for watching", "thanks for watching",
    "thank you for watching.", "thanks for watching.",
    "you", "you.", "bye", "bye.", "okay", "okay.",
    "[music]", "[applause]", "[silence]", "[blank_audio]",
    "please subscribe", "subtitles by", "subs by",
    "amara.org", "www.zeoflix.com",
}

_MIN_TRANSCRIPT_CHARS = 2

_SHORT_VALID = {
    "no", "ok", "go", "yeah", "yep", "yes", "nah", "yo",
    "up", "down", "on", "off",
}


def _is_probably_hallucination(text):
    if not text:
        return True
    stripped = text.strip().lower().rstrip(".!?,;:")
    if stripped in _SHORT_VALID:
        return False
    if len(text.strip()) < _MIN_TRANSCRIPT_CHARS:
        return True
    if stripped in _HALLUCINATION_PHRASES:
        return True
    if not any(c.isalnum() for c in text):
        return True
    return False

def _get_groq_stt_client():
    """Returns a cached Groq client for STT, or None if it can't be built."""
    global _groq_stt_client
    if _groq_stt_client is not None:
        return _groq_stt_client
    try:
        from groq import Groq
    except ImportError:
        return None
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    _groq_stt_client = Groq(api_key=api_key, max_retries=0)
    return _groq_stt_client


def _transcribe_groq(audio_array):
    """
    Transcribes via Groq Whisper. Returns (text, None) on success (text may
    be None if nothing intelligible was heard), or (None, error_string) if
    the call itself failed for a structural reason — missing SDK, missing
    key, network error, rate limit. Callers use the error string to decide
    whether to fall back to another provider.
    """
    client = _get_groq_stt_client()
    if client is None:
        return None, "Groq SDK or API key unavailable"

    try:
        wav_buf = _audio_to_wav_bytes(audio_array)
        wav_buf.name = "audio.wav"  # SDK needs a filename on the file object
        response = client.audio.transcriptions.create(
            file=wav_buf,
            model=GROQ_STT_MODEL,
            response_format="text",
            language="en",
        )
        text = response if isinstance(response, str) else getattr(response, "text", "")
        text = (text or "").strip()
        if _is_probably_hallucination(text):
            return None, None
        return text, None
    except Exception as e:
        return None, f"Groq STT failed: {e}"


def _transcribe_google(audio_array):
    """Google's free Web Speech endpoint. Original fallback path."""
    recognizer = sr.Recognizer()
    wav_buf = _audio_to_wav_bytes(audio_array)
    try:
        with sr.AudioFile(wav_buf) as source:
            audio_data = recognizer.record(source)
        result = recognizer.recognize_google(audio_data)
        if _is_probably_hallucination(result):
            return None, None
        return result, None
    except sr.UnknownValueError:
        return None, None
    except sr.RequestError as e:
        return None, f"[Google STT failed: {e}]"


def transcribe_audio(audio_array):
    """
    Returns transcribed text, None if nothing understandable was heard, or
    an error string (prefixed "[") if every STT provider failed.

    Primary path is Groq Whisper (faster, more accurate, uses the same key
    you already have). Google's free endpoint is the fallback for when
    Groq is unreachable, rate-limited, or the SDK is missing.
    """
    if audio_array.size == 0:
        return None

    text, err = _transcribe_groq(audio_array)
    if err is None:
        # Groq either succeeded (text) or heard nothing (None) — trust its
        # answer. Falling back to Google here would just re-hear the same
        # audio and reach the same conclusion, wasting a call.
        return text

    # Groq failed for a structural reason — try Google before giving up.
    print(f"  [{err} — falling back to Google STT]")
    return _transcribe_google(audio_array)

def listen(threshold=DEFAULT_ENERGY_THRESHOLD):
    """
    Records one utterance — stopping automatically on silence, not a fixed
    duration — and transcribes it. Used by the manual 'talk' command.
    """
    print("  [listening — speak now, pause when you're done...]")
    try:
        audio, spoke = record_until_silence(threshold=threshold)
    except Exception as e:
        return f"[Microphone error: {e}]"
    if not spoke:
        return None
    return transcribe_audio(audio)


def contains_wake_word(text):
    return bool(text) and bool(_WAKE_PATTERN.search(text))


def strip_wake_word(text):
    """Removes the first wake-word occurrence; returns whatever's left,
    trimmed of leftover punctuation (e.g. 'Mars, open Spotify' -> 'open Spotify')."""
    if not text:
        return ""
    match = _WAKE_PATTERN.search(text)
    if not match:
        return text.strip()
    remainder = (text[:match.start()] + text[match.end():]).strip()
    return remainder.strip(" ,.-")