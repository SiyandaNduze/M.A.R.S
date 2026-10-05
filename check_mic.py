"""
Run this if voice input isn't working, to see what sounddevice can
actually detect on your system.
Usage: py check_mic.py
"""
import sounddevice as sd

print("All audio devices sounddevice can see:\n")
print(sd.query_devices())

print("\nDefault input device:")
try:
    info = sd.query_devices(kind="input")
    print(f"  Name: {info['name']}")
    print(f"  Default sample rate: {info['default_samplerate']}")
    print(f"  Max input channels: {info['max_input_channels']}")
except Exception as e:
    print(f"  Could not get default input device: {e}")

print("\nRecording 2 seconds as a test — speak now...")
try:
    rate = int(sd.query_devices(kind="input")["default_samplerate"])
    with sd.InputStream(samplerate=rate, channels=1, dtype="int16") as stream:
        data, _ = stream.read(int(2 * rate))
    peak = int(data.max())
    print(f"Recording finished successfully. Peak amplitude: {peak} (int16 range is -32768 to 32767)")
    if peak < 500:
        print("That's a very low peak — either it was quiet, or the wrong device is being used.")
    else:
        print("Looks like it picked up real audio. Mic capture itself is working.")
except Exception as e:
    print(f"Recording test failed: {e}")
