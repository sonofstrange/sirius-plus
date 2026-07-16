"""Generate the four bundled alarm melodies for web and Android."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path


SAMPLE_RATE = 22_050
DURATION = 4.0
ROOT = Path(__file__).resolve().parents[1]
TARGETS = (ROOT / "static" / "audio", ROOT / "android" / "app" / "src" / "main" / "res" / "raw")


def envelope(position: float, length: float) -> float:
    return min(1.0, position / 0.025, (length - position) / 0.08)


def sample(name: str, time_s: float) -> float:
    if name == "siren":
        frequency = 660 + 300 * (0.5 + 0.5 * math.sin(time_s * math.pi * 1.5))
        return math.sin(2 * math.pi * frequency * time_s) * 0.55
    if name == "pulse":
        position = time_s % 0.72
        if position > 0.18:
            return 0.0
        return math.sin(2 * math.pi * 900 * time_s) * 0.65 * envelope(position, 0.18)
    if name == "chime":
        notes = (523.25, 659.25, 783.99, 1046.5)
        index = min(int(time_s / 0.75), len(notes) - 1)
        position = time_s - index * 0.75
        decay = math.exp(-position * 2.3)
        return (math.sin(2 * math.pi * notes[index] * time_s) + 0.35 * math.sin(2 * math.pi * notes[index] * 2 * time_s)) * 0.45 * decay
    position = time_s % 0.5
    frequency = 430 if position < 0.25 else 690
    return math.sin(2 * math.pi * frequency * time_s) * 0.6


def write_sound(name: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for index in range(int(SAMPLE_RATE * DURATION)):
        value = max(-1.0, min(1.0, sample(name, index / SAMPLE_RATE)))
        frames.extend(struct.pack("<h", int(value * 32767)))
    with wave.open(str(target), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(frames)


for sound in ("siren", "pulse", "chime", "signal"):
    for directory in TARGETS:
        write_sound(sound, directory / f"alarm_{sound}.wav")
