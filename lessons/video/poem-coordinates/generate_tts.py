#!/usr/bin/env python3
"""Generate sentence-level Chinese TTS and a sample-accurate lesson timeline.

Usage:
    python -m pip install edge-tts
    python generate_tts.py [storyboard.json]

Requires Python 3.11+, edge-tts and ffmpeg. The narration uses the text and
voice in storyboard.json. Network access to Microsoft Edge's public read-
aloud service is required to regenerate uncached speech. Audio is an AI
synthesized voice, not a recording or imitation of a real speaker.

Output files in the storyboard's output/ directory (or --output-dir):
    audio/<scene>_<line>.mp3: cached, individual sentence audio
    audio/<scene>_<line>.jsonl: provider sentence-boundary timing metadata
    narration.wav: exact 24 kHz mono PCM concatenation, including pauses
    narration.mp3: standalone narration for listening
    timeline.json: absolute scene, utterance and subtitle times in seconds
    subtitles.srt: subtitle intervals extended through short pauses
"""
from __future__ import annotations
import argparse
import asyncio
import importlib.metadata
import json
import math
import re
import shutil
from pathlib import Path
import ssl
import subprocess
import wave

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_RATE = 24000


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None  # A complete audio cache can be rebuilt without edge-tts installed.


def json_write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def decode_pcm(path: Path) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
         "-acodec", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
        stdout=subprocess.PIPE, check=True,
    ).stdout


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def caption_text(text: str) -> str:
    # These are the three isolated characters discussed in the lesson.
    return text.replace("舟", "“舟”").replace("千字", "“千”字").replace("对应千", "对应“千”").replace("赠", "“赠”")


async def generate(storyboard_path: Path, concurrency: int = 3, output_dir=None, offline=False):
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required to decode and concatenate narration.")
    story = read_json(storyboard_path)
    seen = set()
    if not story.get("scenes"):
        raise ValueError("The storyboard must have at least one scene.")
    for scene in story["scenes"]:
        sid = scene["id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", sid) or sid in seen:
            raise ValueError("Scene IDs must be unique safe lowercase identifiers.")
        seen.add(sid)
        if not scene.get("lines"):
            raise ValueError(f"Scene {sid} must have narration lines.")
        for line in scene["lines"]:
            text = line.get("text") if isinstance(line, dict) else line
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Scene {sid} contains an empty narration line.")
    for key, default in [("lead_in", 0.3), ("utterance_gap", 0.25), ("scene_tail", 0.7)]:
        value = float(story.get(key, default))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a finite nonnegative number.")
    out = Path(output_dir).resolve() if output_dir is not None else storyboard_path.resolve().parent / "output"
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    voice = story.get("voice", "zh-CN-XiaoxiaoNeural")
    rate = story.get("rate", "-8%")
    sem = asyncio.Semaphore(concurrency)
    utterances = []
    for scene in story["scenes"]:
        for index, line in enumerate(scene["lines"], 1):
            text = line["text"] if isinstance(line, dict) else line
            utterances.append({"id": f"{scene['id']}_{index:02}",
                               "scene": scene["id"], "text": text})

    async def synthesize(item):
        stem = item["id"]
        path = audio_dir / f"{stem}.mp3"
        boundaries_path = audio_dir / f"{stem}.jsonl"
        request_path = audio_dir / f"{stem}.request.json"
        request = {"text": item["text"], "voice": voice, "rate": rate}
        cached = path.exists() and boundaries_path.exists() and request_path.exists()
        cached = bool(cached and path.stat().st_size > 0 and read_json(request_path) == request)
        if not cached:
            if offline:
                raise RuntimeError(f"Missing or stale cached speech for {stem}; offline mode cannot synthesize it.")
            try:
                import edge_tts
                import edge_tts.communicate
            except ImportError as error:
                raise RuntimeError("Install video-lessons/requirements-tts.txt to synthesize uncached narration.") from error
            # Preserve certificate verification while respecting system CA configuration.
            edge_tts.communicate._SSL_CTX = ssl.create_default_context()
            async with sem:
                # A failed replacement must never keep an old request marked valid.
                # Audio and boundaries are first written to temporary files below;
                # the request marker is committed only after both are promoted.
                request_path.unlink(missing_ok=True)
                for attempt in range(3):
                    try:
                        speech = edge_tts.Communicate(
                            item["text"], voice=voice, rate=rate,
                            boundary="SentenceBoundary", connect_timeout=20,
                            receive_timeout=60,
                        )
                        pending_audio = path.with_suffix(".pending.mp3")
                        pending_boundaries = boundaries_path.with_suffix(".pending.jsonl")
                        await speech.save(str(pending_audio), str(pending_boundaries))
                        if not decode_pcm(pending_audio):
                            raise RuntimeError(f"No decoded audio for {stem}")
                        pending_audio.replace(path)
                        pending_boundaries.replace(boundaries_path)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(attempt + 1)
                json_write(request_path, request)
        pcm = decode_pcm(path)
        if not pcm:
            raise RuntimeError(f"No decoded audio for {stem}")
        metadata = [json.loads(line) for line in boundaries_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        duration = len(pcm) // 2 / SAMPLE_RATE
        print(f"{stem}: {duration:.3f}s ({'cached' if cached else 'synthesized'})", flush=True)
        return {**item, "pcm": pcm, "samples": len(pcm) // 2,
                "provider_boundaries": metadata,
                "audio": str(path.relative_to(out))}

    rendered = await asyncio.gather(*[synthesize(item) for item in utterances])
    rendered_by_id = {item["id"]: item for item in rendered}
    pcm_parts = []
    total_samples = 0
    scenes = []
    all_lines = []

    def silence(seconds):
        nonlocal total_samples
        count = round(float(seconds) * SAMPLE_RATE)
        pcm_parts.append(bytes(count * 2))
        total_samples += count

    def time_now():
        return total_samples / SAMPLE_RATE

    for scene_index, original_scene in enumerate(story["scenes"]):
        scene = {key: value for key, value in original_scene.items() if key != "lines"}
        scene["start"] = time_now()
        scene["start_sample"] = total_samples
        scene["lines"] = []
        silence(story.get("lead_in", 0.3))
        for line_index, original_line in enumerate(original_scene["lines"], 1):
            utterance_id = f"{scene['id']}_{line_index:02}"
            utterance = rendered_by_id[utterance_id]
            start = time_now()
            start_sample = total_samples
            pcm_parts.append(utterance["pcm"])
            total_samples += utterance["samples"]
            end = time_now()
            boundaries = []
            for boundary in utterance["provider_boundaries"]:
                if boundary.get("type") not in ("SentenceBoundary", "WordBoundary"):
                    continue
                relative_start = float(boundary.get("offset", 0)) / 10_000_000
                relative_end = relative_start + float(boundary.get("duration", 0)) / 10_000_000
                boundaries.append({
                    "text": boundary["text"],
                    "start": min(end, start + max(0, relative_start)),
                    "end": min(end, start + max(relative_start, relative_end)),
                    "offset_100ns": boundary.get("offset"),
                    "duration_100ns": boundary.get("duration"),
                })
            line = {"id": utterance_id,
                    "text": utterance["text"], "caption": caption_text(utterance["text"]),
                    "start": start, "end": end, "duration": end - start,
                    "start_sample": start_sample, "end_sample": total_samples,
                    "audio": utterance["audio"], "sentences": boundaries}
            scene["lines"].append(line)
            all_lines.append(line)
            if line_index < len(original_scene["lines"]):
                silence(story.get("utterance_gap", 0.25))
        silence(story.get("scene_tail", 0.7))
        if scene_index == len(story["scenes"]) - 1:
            silence(1.5)
        scene["end"] = time_now()
        scene["end_sample"] = total_samples
        scene["duration"] = scene["end"] - scene["start"]
        scenes.append(scene)

    total_duration = time_now()
    for index, line in enumerate(all_lines):
        line["caption_start"] = line["start"]
        line["caption_end"] = (all_lines[index + 1]["start"]
                               if index + 1 < len(all_lines) else total_duration)

    wav_path = out / "narration.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        for pcm in pcm_parts:
            wav.writeframesraw(pcm)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav_path),
                    "-c:a", "libmp3lame", "-b:a", "128k", str(out / "narration.mp3")], check=True)
    manifest = {"title": story["title"], "total_duration": total_duration,
                "total_samples": total_samples,
                "voice": {"provider": "Microsoft Edge read-aloud via edge-tts",
                          "name": voice, "locale": "zh-CN", "rate": rate,
                          "synthetic": True,
                          "edge_tts_version": package_version("edge-tts")},
                "audio_format": {"sample_rate": SAMPLE_RATE, "channels": 1,
                                 "sample_width_bytes": 2, "encoding": "signed 16-bit PCM little-endian"},
                "pauses": {"scene_lead_in": story.get("lead_in", 0.3),
                           "utterance_gap": story.get("utterance_gap", 0.25),
                           "scene_tail": story.get("scene_tail", 0.7),
                           "final_review": 1.5},
                "timing_convention": "All times are absolute seconds. Line start/end/duration describe decoded audio; caption_start/caption_end describe subtitle display. Scene spans include silence. Timing is accumulated at exact PCM sample boundaries.",
                "scenes": scenes}
    json_write(out / "timeline.json", manifest)
    subtitle_blocks = []
    for index, line in enumerate(all_lines, 1):
        subtitle_blocks.append(f"{index}\n{srt_time(line['caption_start'])} --> {srt_time(line['caption_end'])}\n{line['caption']}\n")
    (out / "subtitles.srt").write_text("\n".join(subtitle_blocks), encoding="utf-8")
    # This verification catches concatenation errors and audio truncation.
    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getnframes() == total_samples
        assert wav.getframerate() == SAMPLE_RATE and wav.getnchannels() == 1
    assert len(all_lines) == len(utterances)
    for index, scene in enumerate(scenes):
        assert scene["end"] > scene["start"]
        if index:
            assert scene["start_sample"] == scenes[index - 1]["end_sample"]
        for line in scene["lines"]:
            assert scene["start"] <= line["start"] < line["end"] <= scene["end"]
            assert line["caption_end"] >= line["end"]
    print(json.dumps({"status": "complete", "duration": total_duration,
                      "scene_count": len(scenes), "utterance_count": len(all_lines),
                      "samples": total_samples, "output": str(out)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storyboard", nargs="?", type=Path, default=BASE_DIR / "storyboard.json")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, help="Defaults to the storyboard's output/ directory.")
    parser.add_argument("--offline", action="store_true", help="Only reuse exact cached utterances; never contact TTS.")
    args = parser.parse_args()
    try:
        asyncio.run(generate(args.storyboard, args.concurrency, args.output_dir, args.offline))
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Error: {error}\n")
