"""Compare QAG's parallel and sequential modes through a running playground.

Streams a spoken sentence to the playground as microphone frames, the same one
in every turn, and measures what a person waits for: from the moment they stop
talking to the moment HuRI's voice comes back. Emotion is read from the sound
of the voice, so use your own:

    python scripts/compare_qag_modes.py --record 6     # record yourself, then run
    python scripts/compare_qag_modes.py --wav my_voice.wav --turns 5

Without either, a Piper voice reads a sentence: fine for timing, meaningless
for emotion.

    python scripts/compare_qag_modes.py "Can you tell me a joke?"

Columns:
  HuRI speaks after   you stop talking -> first sound of HuRI's answer. "You stop"
                      is the last 30 ms frame webrtcvad (HuRI's vad) counts as
                      speech, timed when it is sent; "first sound" is the first
                      huri.audio.out chunk, timed when it arrives.
  = done talking      vad's silence wait (1.5 s, modules.yaml) + the final
                      transcription of the whole sentence
  + emotion wait      QAG holding the question for its emotion (sequential only)
  + answer            LLM, then synthesizing the first spoken phrase
  emotion             eag's summary of the sentence, and whether RAG got it:
                      with the question (sequential), or after answering it, to
                      use in the next turn (parallel: QAG then attaches it to the
                      next question, flagged `emotion_earlier`)

Needs HuRI and the playground running (README); Piper needs HURI_PIPER_VOICE
(huri-modules' installer sets it in .huri-local/env.sh).
"""

import argparse
import asyncio
import json
import os
import statistics
import struct
import time

import numpy as np
import websockets

# Voice in, emotion, answer and voice out. No Gestures: EMAGE shares the GPU
# with Speech to text and slows every turn down on a small card.
MODULES = ["vad", "stt", "tag", "emo", "eag", "qag", "rag", "tts"]
# HuRI's voice keeps coming for a moment after the last word of text.
AUDIO_TAIL_S = 4.0
RATE = 16000
FRAME = 480  # 30 ms at 16 kHz, what the playground's vad is configured for
AUDIO_IN = b"huri.audio.in"
VAD_AGGRESSIVENESS = 3  # as in modules.yaml


def to_frames(audio: np.ndarray, rate: int) -> list[bytes]:
    """Float mono audio at any rate -> 30 ms int16 frames at 16 kHz."""
    times = np.arange(0, len(audio) / rate, 1 / RATE)
    pcm = np.interp(times, np.arange(len(audio)) / rate, audio)
    pcm = (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
    # A little silence before, and 2 s after so the VAD ends the turn.
    pcm = np.concatenate([np.zeros(4800, np.int16), pcm, np.zeros(2 * RATE, np.int16)])
    return [pcm[i : i + FRAME].tobytes() for i in range(0, len(pcm) - FRAME, FRAME)]


def last_speech_frame(frames: list[bytes]) -> int:
    """Index of the last frame HuRI's vad will hear as speech."""
    import webrtcvad

    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    speech = [i for i, f in enumerate(frames) if vad.is_speech(f, RATE)]
    if not speech:
        raise SystemExit("no speech found in the recording")
    return speech[-1]


def piper(text: str, voice_path: str) -> tuple[np.ndarray, int]:
    from piper import PiperVoice

    chunks = list(PiperVoice.load(voice_path).synthesize(text))
    return np.concatenate([c.audio_float_array for c in chunks]), chunks[0].sample_rate


def record(seconds: float, path: str) -> None:
    import sounddevice as sd
    import soundfile as sf

    for n in (3, 2, 1):
        print(f"recording in {n}…", end="\r", flush=True)
        time.sleep(1)
    print(f"speak now, {seconds:.0f} s: one sentence, the way you would say it")
    audio = sd.rec(int(seconds * RATE), samplerate=RATE, channels=1, dtype="float32")
    sd.wait()
    sf.write(path, audio, RATE)
    print(f"saved {path} (replay it later with --wav {path})\n")


def load(path: str) -> tuple[np.ndarray, int]:
    import soundfile as sf

    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    return audio.mean(axis=1), rate


def audio_out(msg: bytes) -> tuple[int, np.ndarray] | None:
    """Binary ``huri.audio.out``: [u16 len][topic][u32 rate][u8 end][f64 pts][f32…]."""
    (n,) = struct.unpack(">H", msg[:2])
    if msg[2 : 2 + n] != b"huri.audio.out":
        return None
    body = msg[2 + n :]
    (rate,) = struct.unpack(">I", body[:4])
    return rate, np.frombuffer(body[13:], dtype=np.float32)


def play(chunks: list[tuple[int, np.ndarray]]) -> None:
    import sounddevice as sd

    if not chunks:
        print(f"{'':11} (no voice came back)")
        return
    sd.play(np.concatenate([c for _, c in chunks]), chunks[0][0])
    sd.wait()


def summarize(windows: list[dict]) -> str | None:
    """The sentence's emotion as eag computes it: the mean of emo's windows."""
    if not windows:
        return None
    labels = {label for w in windows for label in w["scores"]}
    mean = {
        lb: sum(w["scores"].get(lb, 0.0) for w in windows) / len(windows)
        for lb in labels
    }
    best = max(mean, key=lambda lb: mean[lb])
    return f"{best} {mean[best]:.0%}"


async def turn(url: str, mode: str, frames: list[bytes], speech_end: int) -> dict:
    voice: list[tuple[int, np.ndarray]] = []
    windows: list[dict] = []  # emo's readings, window by window
    stopped = heard = sent = spoke = emotion_done = None
    text, emotion, earlier, answer = "", None, False, ""

    def on_message(raw: str | bytes, now: float) -> bool:
        """Record one message from HuRI; True once its text answer is complete."""
        nonlocal heard, sent, spoke, emotion_done, text, emotion, earlier, answer
        if isinstance(raw, bytes):
            chunk = audio_out(raw)
            if chunk is not None and len(chunk[1]) > 0:
                spoke = spoke or now
                voice.append(chunk)
            return False
        msg = json.loads(raw)
        topic, data = msg.get("topic"), msg.get("data")
        if topic == "huri.transcript" and data["end"]:
            heard = now
        elif topic == "huri.question":
            sent, text = now, data["transcript"]["text"]
            emotion = (data.get("emotion") or {}).get("label")
            # Parallel mode: the emotion of the previous turn, read after its
            # own question had left, used for this one.
            earlier = bool(data.get("emotion_earlier"))
        elif topic == "huri.emotion":
            windows.append(data)
            if data.get("end"):
                emotion_done = now
        elif topic == "huri.token":
            answer += data["text"]
            return bool(data["end"])
        return False

    async with websockets.connect(url, max_size=None) as ws:
        start = {
            "type": "start",
            "modules": MODULES,
            "options": {"qag": {"mode": mode}},
        }
        await ws.send(json.dumps(start))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 120))
        if ready.get("type") != "ready":
            raise RuntimeError(f"playground refused the session: {ready}")

        async def speak() -> None:
            nonlocal stopped
            header = struct.pack(">H", len(AUDIO_IN)) + AUDIO_IN
            for i, frame in enumerate(frames):
                await ws.send(header + frame)
                if i == speech_end:
                    stopped = time.monotonic()
                await asyncio.sleep(0.03)

        speaking = asyncio.create_task(speak())
        while not on_message(await asyncio.wait_for(ws.recv(), 180), time.monotonic()):
            pass
        await speaking
        # The voice of the last words is still being synthesized, and in parallel
        # mode emo may still be on its last window.
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), AUDIO_TAIL_S)
            except asyncio.TimeoutError:
                break
            on_message(raw, time.monotonic())
        await ws.send(json.dumps({"type": "finish"}))

    if None in (stopped, heard, sent, spoke):
        raise RuntimeError("the turn did not go through: no transcript or no voice")
    assert stopped and heard and sent and spoke  # for mypy
    # Parallel mode: the sentence's emotion was read after its question left;
    # QAG keeps it for the next question.
    late = emotion is None and emotion_done is not None and emotion_done > sent
    return {
        "mode": mode,
        "speaks": spoke - stopped,
        "done": heard - stopped,
        "emotion_wait": sent - heard,
        "answer_time": spoke - sent,
        "emotion": emotion,
        "earlier": earlier,
        "late": late,
        "sentence": summarize(windows),
        "text": text,
        "answer": answer.strip(),
        "voice": voice,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "sentence",
        nargs="?",
        default="I am really happy to meet you today. "
        "Can you tell me what the weather is like in Paris?",
    )
    parser.add_argument("--turns", type=int, default=3, help="turns per mode")
    parser.add_argument("--wav", help="replay this recording instead of Piper")
    parser.add_argument(
        "--record",
        type=float,
        metavar="SECONDS",
        help="record yourself first (saved to --wav, default my_voice.wav)",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="do not play HuRI's spoken answers"
    )
    parser.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    parser.add_argument("--voice", default=os.environ.get("HURI_PIPER_VOICE"))
    args = parser.parse_args()

    if args.record:
        args.wav = args.wav or "my_voice.wav"
        record(args.record, args.wav)
    if args.wav:
        frames = to_frames(*load(args.wav))
        source = args.wav
    else:
        if not args.voice:
            parser.error("no Piper voice: source .huri-local/env.sh or pass --voice")
        frames = to_frames(*piper(args.sentence, args.voice))
        source = f"Piper reading {args.sentence!r} (emotion will not mean much)"
    speech_end = last_speech_frame(frames)
    print(f"voice: {source}, you stop talking at {speech_end * 0.03:.2f} s\n")

    print(
        f"{'mode':11} {'HuRI speaks after':>17} {'= done talking':>15} "
        f"{'+ emotion wait':>15} {'+ answer':>9}   emotion (RAG got it?)"
    )
    results: dict[str, list[dict]] = {"parallel": [], "sequential": []}
    for mode in ["parallel", "sequential"] * args.turns:
        r = await turn(args.url, mode, frames, speech_end)
        results[mode].append(r)
        got = (
            "yes, from previous turn"
            if r["emotion"] and r["earlier"]
            else "yes" if r["emotion"] else "next turn" if r["late"] else "no"
        )
        print(
            f"{mode:11} {r['speaks']:16.2f}s {r['done']:14.2f}s "
            f"{r['emotion_wait']:14.2f}s {r['answer_time']:8.2f}s   "
            f"{r['sentence'] or '-'} ({got})"
        )
        if len(results["parallel"]) + len(results["sequential"]) == 1:
            print(f"{'':11} HuRI heard: {r['text']!r}")
        print(f"{'':11} HuRI: {r['answer']}")
        if not args.quiet:
            play(r["voice"])  # before the next turn, so they never overlap

    print("\nmedians, from the moment you stop talking:")
    for mode, rs in results.items():
        speaks = statistics.median(r["speaks"] for r in rs)
        wait = statistics.median(r["emotion_wait"] for r in rs)
        print(
            f"  {mode:11} HuRI speaks after {speaks:5.2f}s (emotion wait {wait:.2f}s)"
        )


if __name__ == "__main__":
    asyncio.run(main())
