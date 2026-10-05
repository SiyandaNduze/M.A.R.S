import wave
from piper import PiperVoice

voice = PiperVoice.load("jarvis-high.onnx")

# Change these and re-run to hear the difference
voice.config.noise_scale = 0.4
voice.config.noise_w = 0.25
try:
    voice.config.noise_w_scale = 0.25
except Exception:
    pass
voice.config.length_scale = 1.12

SAMPLE = (
    "Good evening, sir. "
    "The weather in Durban is clear. "
    "Twenty-three degrees. "
    "Shall I open the workshop schematics?"
)

# Synthesize each sentence separately and stitch with pauses
import re, io

sentences = re.split(r"(?<=[.!?])\s+", SAMPLE.strip())

with wave.open("voice_test.wav", "wb") as out:
    first = True
    sr = None
    for s in sentences:
        if not s.strip():
            continue
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(s, wf)
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            if sr is None:
                sr = wf.getframerate()
                out.setnchannels(wf.getnchannels())
                out.setsampwidth(wf.getsampwidth())
                out.setframerate(sr)
            out.writeframes(wf.readframes(wf.getnframes()))
        # 420ms pause between sentences
        out.writeframes(b"\x00" * int(sr * 0.42 * 2))  # int16 mono

print("Wrote voice_test.wav — play it to compare.")