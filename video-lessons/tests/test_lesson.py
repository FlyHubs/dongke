"""Offline regression tests for the independent Pillow/NumPy lesson renderer.

Run from the repository root:
    python -m unittest discover -s video-lessons/tests -v

No edge-tts installation, provider account, or network access is required.
Rendering tests skip with an explicit reason if Noto CJK is not installed;
real-media tests skip if FFmpeg/ffprobe (or their codecs) are unavailable.
"""
from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import wave

import numpy as np
from PIL import Image

REPOSITORY = Path(__file__).resolve().parents[2]
LESSON = REPOSITORY / "lessons" / "video" / "poem-coordinates"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def without_network_or_edge_tts():
    """Fail loudly if an offline operation attempts a network call or TTS import."""
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "edge_tts" or name.startswith("edge_tts."):
            raise AssertionError("Offline work must not import edge_tts")
        return original_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=guarded_import), \
            mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Network access forbidden")), \
            mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network access forbidden")), \
            mock.patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")):
        yield


render = load_script("poem_test_render", LESSON / "render_lesson.py")
with without_network_or_edge_tts():
    tts = load_script("poem_test_tts", LESSON / "generate_tts.py")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def require_fonts(test):
    try:
        render.font(24)
        render.font(24, True)
    except FileNotFoundError as error:
        test.skipTest(str(error))


def require_ffmpeg(test, *, probe=False, codecs=()):
    if not shutil.which("ffmpeg") or (probe and not shutil.which("ffprobe")):
        test.skipTest("FFmpeg/ffprobe is not installed")
    if codecs:
        encoders = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=True, capture_output=True, text=True,
        ).stdout
        for codec in codecs:
            if codec not in encoders:
                test.skipTest(f"FFmpeg encoder {codec} is not installed")


def probe_media(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


class LessonContentTests(unittest.TestCase):
    def setUp(self):
        render.configure(LESSON / "timeline.json")

    def tearDown(self):
        render.configure(LESSON / "timeline.json")

    def test_bundled_content_validates(self):
        render.validate_content()
        self.assertEqual((render.W, render.H, render.FPS), (1920, 1080, 24))
        self.assertEqual(len(render.SCENES), 14)
        self.assertAlmostEqual(render.DURATION, 126.476)

    def test_grid_and_published_answers_agree(self):
        grid, answers = render.STORY["grid"], render.STORY["answers"]
        self.assertEqual(set(grid), {str(row) for row in range(1, 7)})
        self.assertTrue(all(len(row) == 9 for row in grid.values()))
        for name in ("boat", "lookup"):
            answer = answers[name]
            self.assertEqual(grid[str(answer["row"])][answer["column"] - 1], answer["character"])
        self.assertEqual(answers["boat"], {"character": "舟", "column": 5, "row": 4})
        self.assertEqual(answers["lookup"], {"character": "千", "column": 7, "row": 2})
        shift = answers["shift"]
        self.assertEqual(grid[str(shift["before"][1])][shift["before"][0] - 1], "赠")
        self.assertEqual([a + b for a, b in zip(shift["before"], shift["delta"])], shift["after"])
        self.assertEqual(shift["after"], [3, 6])

    def test_board_coordinates_count_rows_upwards_and_shift_left(self):
        x, y = render.cell_center(4, 6)
        moved_x, moved_y = render.cell_center(4, 6, shift=-1)
        self.assertEqual((moved_x, moved_y), render.cell_center(3, 6))
        self.assertEqual(x - moved_x, render.CELL)
        self.assertEqual(y, moved_y)
        self.assertEqual(render.cell_center(1, 1)[1] - render.cell_center(1, 2)[1], render.CELL)
        self.assertEqual(render.cell_center(2, 1)[0] - render.cell_center(1, 1)[0], render.CELL)

    def test_reference_timing_is_sample_accurate_and_contiguous(self):
        sample_rate = render.TIME["audio_format"]["sample_rate"]
        self.assertEqual(render.TIME["total_samples"], round(render.DURATION * sample_rate))
        previous_scene_end = 0
        for scene in render.SCENES:
            with self.subTest(scene=scene["id"]):
                self.assertEqual(scene["start_sample"], previous_scene_end)
                self.assertAlmostEqual(scene["start"], scene["start_sample"] / sample_rate)
                self.assertAlmostEqual(scene["end"], scene["end_sample"] / sample_rate)
                self.assertAlmostEqual(scene["duration"], scene["end"] - scene["start"])
                previous_line_end = scene["start_sample"]
                for line in scene["lines"]:
                    self.assertGreaterEqual(line["start_sample"], previous_line_end)
                    self.assertGreater(line["end_sample"], line["start_sample"])
                    self.assertLessEqual(line["end_sample"], scene["end_sample"])
                    self.assertAlmostEqual(line["start"], line["start_sample"] / sample_rate)
                    self.assertAlmostEqual(line["end"], line["end_sample"] / sample_rate)
                    self.assertAlmostEqual(line["duration"], line["end"] - line["start"])
                    previous_line_end = line["end_sample"]
                previous_scene_end = scene["end_sample"]
        self.assertEqual(previous_scene_end, render.TIME["total_samples"])
        for index, line in enumerate(render.LINES):
            expected_end = render.LINES[index + 1]["start"] if index + 1 < len(render.LINES) else render.DURATION
            self.assertEqual(line["caption_start"], line["start"])
            self.assertEqual(line["caption_end"], expected_end)

    def test_bundled_subtitles_match_reference_timeline(self):
        blocks = (LESSON / "subtitles.srt").read_text(encoding="utf-8").strip().split("\n\n")
        self.assertEqual(len(blocks), len(render.LINES))
        for index, (block, line) in enumerate(zip(blocks, render.LINES), 1):
            number, interval, caption = block.split("\n", 2)
            self.assertEqual(number, str(index))
            self.assertEqual(interval, f'{tts.srt_time(line["caption_start"])} --> {tts.srt_time(line["caption_end"])}')
            self.assertEqual(caption, line["caption"])

    def test_validation_rejects_wrong_answer_grid_and_translation(self):
        for row, column in (("4", 4), ("2", 6), ("6", 3)):
            story = deepcopy(render.STORY)
            story["grid"][row][column] = "错"
            with self.subTest(row=row, column=column), mock.patch.object(render, "STORY", story):
                with self.assertRaises(ValueError):
                    render.validate_content()
        story = deepcopy(render.STORY)
        story["answers"]["shift"]["after"] = [5, 6]
        with mock.patch.object(render, "STORY", story), self.assertRaises(ValueError):
            render.validate_content()

    def test_validation_rejects_malformed_timeline(self):
        def bad_text(timeline):
            timeline["scenes"][0]["lines"][0]["text"] = "改过的讲解"

        def gap(timeline):
            timeline["scenes"][1]["start"] += 0.1

        def overlapping_line(timeline):
            scene = timeline["scenes"][0]
            scene["lines"][1]["start"] = scene["lines"][0]["start"]

        def bad_caption(timeline):
            line = timeline["scenes"][0]["lines"][0]
            line["caption_end"] = line["end"] - 0.1

        def wrong_scene(timeline):
            timeline["scenes"][0]["id"] = "incorrect"

        def bad_duration(timeline):
            timeline["total_duration"] = float("nan")

        for mutate in (bad_text, gap, overlapping_line, bad_caption, wrong_scene, bad_duration):
            with self.subTest(case=mutate.__name__), tempfile.TemporaryDirectory() as temp:
                timeline = deepcopy(render.TIME)
                mutate(timeline)
                path = Path(temp) / "timeline.json"
                write_json(path, timeline)
                render.configure(path)
                try:
                    with self.assertRaises(ValueError):
                        render.validate_content()
                finally:
                    render.configure(LESSON / "timeline.json")

    def test_scene_selection_is_stable_at_every_boundary(self):
        blank = Image.new("RGB", (render.W, render.H))
        panel = Image.new("RGBA", (1, 1))
        with mock.patch.object(render, "base_frame", return_value=blank) as base, \
                mock.patch.object(render, "draw_board", return_value=0), \
                mock.patch.object(render, "right_panel", return_value=panel), \
                mock.patch.object(render, "recap_frame"), \
                mock.patch.object(render, "subtitle_layer", return_value=panel):
            cases = [(0.0, render.SCENES[0]["id"]), (render.DURATION, render.SCENES[-1]["id"])]
            for previous, current in zip(render.SCENES, render.SCENES[1:]):
                boundary = current["start"]
                cases.extend(((boundary - 1e-7, previous["id"]),
                              (boundary, current["id"]),
                              (boundary + 1e-7, current["id"])))
            for instant, scene_id in cases:
                with self.subTest(time=instant, scene=scene_id):
                    render.frame_at(instant)
                    base.assert_called_with(scene_id)

    def test_configure_uses_requested_timeline_and_clears_caches(self):
        require_fonts(self)
        render.frame_at(0.0)
        self.assertGreater(render.base_frame.cache_info().currsize, 0)
        self.assertGreater(render.font.cache_info().currsize, 0)
        with tempfile.TemporaryDirectory() as temp:
            timeline = deepcopy(render.TIME)
            timeline["title"] = "Distinct test timeline"
            path = Path(temp) / "timeline.json"
            write_json(path, timeline)
            render.configure(path, font_dir=Path(temp))
            self.assertEqual(render.TIME["title"], "Distinct test timeline")
            self.assertEqual(render.FONT_DIR, Path(temp))
            for cached in (render.font, render.base_frame, render.poem_layer,
                           render.right_panel, render.mini_board, render.subtitle_layer):
                self.assertEqual(cached.cache_info().currsize, 0)

    def test_every_scene_renders_as_deterministic_independent_rgb_frame(self):
        require_fonts(self)
        for scene in render.SCENES:
            instant = scene["start"] + scene["duration"] * 0.72
            with self.subTest(scene=scene["id"]):
                first = render.frame_at(instant)
                expected = first.tobytes()
                self.assertEqual(first.mode, "RGB")
                self.assertEqual(first.size, (1920, 1080))
                self.assertGreater(np.asarray(first).std(), 10)
                first.paste((255, 0, 255), (0, 0, render.W, render.H))
                second = render.frame_at(instant)
                self.assertIsNot(first, second)
                self.assertEqual(second.tobytes(), expected)

    def test_translation_changes_the_board_between_before_and_after(self):
        require_fonts(self)
        scene = next(scene for scene in render.SCENES if scene["id"] == "shift_move")
        box = (render.GX, render.GY, render.GX + render.GW, render.GY + render.GH)
        before = render.frame_at(scene["start"] + 0.12 * scene["duration"]).crop(box)
        after = render.frame_at(scene["start"] + 0.98 * scene["duration"]).crop(box)
        self.assertNotEqual(before.tobytes(), after.tobytes())


class EncodeTests(unittest.TestCase):
    def setUp(self):
        render.configure(LESSON / "timeline.json")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "clip.mp4"

    def test_invalid_encode_options_fail_before_starting_ffmpeg(self):
        cases = [
            {"fps": 0}, {"fps": -1}, {"fps": 1.5}, {"fps": True},
            {"fps": float("nan")}, {"fps": float("inf")},
            {"start": -0.1}, {"start": float("nan")}, {"start": float("inf")},
            {"start": render.DURATION},
            {"duration": 0}, {"duration": -1}, {"duration": float("nan")},
            {"duration": float("inf")}, {"duration": render.DURATION + 1},
            {"start": render.DURATION - 0.1, "duration": 0.2},
        ]
        with mock.patch.object(render.shutil, "which", return_value="ffmpeg"), \
                mock.patch.object(render.subprocess, "Popen") as launch:
            for options in cases:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    render.encode(self.output, silent=True, **options)
            launch.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_ffmpeg_has_actionable_error(self):
        with mock.patch.object(render.shutil, "which", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "FFmpeg"):
            render.encode(self.output, duration=0.1, silent=True)

    def test_narration_is_required_unless_silent_is_explicit(self):
        with mock.patch.object(render.shutil, "which", return_value="ffmpeg"), \
                self.assertRaisesRegex(FileNotFoundError, "Narration|narration"):
            render.encode(self.output, duration=0.1, audio=Path(self.temp.name) / "missing.wav")
        self.assertFalse(self.output.exists())

    def test_short_narration_is_rejected(self):
        audio = Path(self.temp.name) / "too_short.wav"
        audio.touch()
        probe = subprocess.CompletedProcess([], 0, stdout="0.01\n")
        with mock.patch.object(render.shutil, "which", return_value="ffmpeg"), \
                mock.patch.object(render.subprocess, "run", return_value=probe), \
                mock.patch.object(render.subprocess, "Popen") as launch, \
                self.assertRaisesRegex(ValueError, "shorter"):
            render.encode(self.output, fps=5, start=0.1, duration=0.2, audio=audio)
        launch.assert_not_called()

    def test_real_silent_clip_has_expected_frames_and_no_audio(self):
        require_fonts(self)
        require_ffmpeg(self, probe=True, codecs=("libx264",))
        with without_network_or_edge_tts(), redirect_stdout(io.StringIO()):
            render.encode(self.output, fps=5, start=1.0, duration=0.21, silent=True)
        media = probe_media(self.output)
        video = [stream for stream in media["streams"] if stream["codec_type"] == "video"]
        self.assertEqual(len(video), 1)
        self.assertFalse(any(stream["codec_type"] == "audio" for stream in media["streams"]))
        self.assertEqual(video[0]["codec_name"], "h264")
        self.assertEqual(video[0]["pix_fmt"], "yuv420p")
        self.assertEqual((video[0]["width"], video[0]["height"]), (1920, 1080))
        self.assertEqual(int(video[0]["nb_read_frames"]), math.ceil(0.21 * 5))
        self.assertEqual(video[0]["avg_frame_rate"], "5/1")
        self.assertAlmostEqual(float(media["format"]["duration"]), 0.4, places=2)

    def test_real_narrated_clip_contains_aac_audio(self):
        require_fonts(self)
        require_ffmpeg(self, probe=True, codecs=("libx264", "aac"))
        audio = Path(self.temp.name) / "narration.wav"
        with wave.open(str(audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(bytes(24000 * 2 * 2))
        with without_network_or_edge_tts(), redirect_stdout(io.StringIO()):
            render.encode(self.output, fps=5, start=1.0, duration=0.4, audio=audio)
        media = probe_media(self.output)
        audio_streams = [stream for stream in media["streams"] if stream["codec_type"] == "audio"]
        self.assertEqual(len(audio_streams), 1)
        self.assertEqual(audio_streams[0]["codec_name"], "aac")
        self.assertEqual(audio_streams[0]["sample_rate"], "48000")
        self.assertAlmostEqual(float(media["format"]["duration"]), 0.4, places=2)


class TtsOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.story_path = self.root / "storyboard.json"
        self.out = self.root / "generated"
        self.story = {
            "title": "Offline test lesson", "voice": "zh-CN-XiaoxiaoNeural", "rate": "-8%",
            "lead_in": 0.05, "utterance_gap": 0.03, "scene_tail": 0.04,
            "scenes": [{"id": "test", "chapter": 0, "title": "Test", "lines": ["舟在第五列。"]}],
        }
        self.save_story()

    def save_story(self):
        write_json(self.story_path, self.story)

    def generate(self, **options):
        with without_network_or_edge_tts(), redirect_stdout(io.StringIO()):
            return asyncio.run(asyncio.wait_for(
                tts.generate(self.story_path, output_dir=self.out, offline=True, **options), timeout=20,
            ))

    def seed_cache(self, *, audio=b"cached-test-audio", request=None):
        directory = self.out / "audio"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "test_01.mp3").write_bytes(audio)
        (directory / "test_01.jsonl").write_text("", encoding="utf-8")
        if request is None:
            request = {"text": self.story["scenes"][0]["lines"][0],
                       "voice": self.story["voice"], "rate": self.story["rate"]}
        write_json(directory / "test_01.request.json", request)
        return directory

    def test_srt_time_rounding_clamping_and_hour_rollover(self):
        for value, expected in ((-1, "00:00:00,000"), (0, "00:00:00,000"),
                                (1.2344, "00:00:01,234"), (1.2346, "00:00:01,235"),
                                (59.9996, "00:01:00,000"), (3599.9996, "01:00:00,000"),
                                (3661.025, "01:01:01,025")):
            with self.subTest(value=value):
                self.assertEqual(tts.srt_time(value), expected)

    def test_captions_mark_the_three_isolated_characters(self):
        self.assertEqual(tts.caption_text("舟在第五列，千字在第二行，赠向左移。"),
                         "“舟”在第五列，“千”字在第二行，“赠”向左移。")
        self.assertEqual(tts.caption_text("七、二对应千。"), "七、二对应“千”。")
        self.assertEqual(tts.caption_text("桃花潭水深千尺"), "桃花潭水深千尺")

    def test_uncached_offline_request_fails_without_network(self):
        with mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), \
                mock.patch.object(tts, "decode_pcm") as decode, \
                self.assertRaisesRegex(RuntimeError, "[Oo]ffline"):
            self.generate()
        decode.assert_not_called()
        self.assertFalse((self.out / "timeline.json").exists())

    def test_text_voice_and_rate_changes_invalidate_cache(self):
        for key in ("text", "voice", "rate"):
            with self.subTest(field=key):
                request = {"text": self.story["scenes"][0]["lines"][0],
                           "voice": self.story["voice"], "rate": self.story["rate"]}
                request[key] += "-different"
                self.seed_cache(request=request)
                with mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), \
                        mock.patch.object(tts, "decode_pcm") as decode, \
                        self.assertRaisesRegex(RuntimeError, "[Oo]ffline"):
                    self.generate()
                decode.assert_not_called()
        self.assertFalse((self.out / "timeline.json").exists())

    def test_incomplete_or_empty_cache_is_not_reused(self):
        for missing in ("test_01.mp3", "test_01.jsonl", "test_01.request.json", "empty_audio"):
            with self.subTest(missing=missing):
                directory = self.seed_cache()
                if missing == "empty_audio":
                    (directory / "test_01.mp3").write_bytes(b"")
                else:
                    (directory / missing).unlink()
                with mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), \
                        mock.patch.object(tts, "decode_pcm") as decode, \
                        self.assertRaisesRegex(RuntimeError, "[Oo]ffline"):
                    self.generate()
                decode.assert_not_called()

    def test_failed_regeneration_preserves_audio_and_invalidates_request(self):
        directory = self.seed_cache()
        originals = {path.name: path.read_bytes() for path in directory.iterdir()}
        previous_request = json.loads(originals["test_01.request.json"])
        self.story["scenes"][0]["lines"][0] = "新讲解，尚未合成。"
        self.save_story()
        edge = types.ModuleType("edge_tts")
        edge.__path__ = []
        edge.communicate = types.ModuleType("edge_tts.communicate")

        for failure in ("provider", "decode"):
            self.seed_cache(request=previous_request)

            async def save_partial(audio_path, boundaries_path):
                Path(audio_path).write_bytes(b"incomplete-new-audio")
                Path(boundaries_path).write_text("", encoding="utf-8")
                if failure == "provider":
                    raise RuntimeError("Simulated provider failure")

            edge.Communicate = mock.Mock(return_value=types.SimpleNamespace(save=save_partial))
            with self.subTest(failure=failure), \
                    mock.patch.dict(sys.modules, {"edge_tts": edge, "edge_tts.communicate": edge.communicate}), \
                    mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Network access forbidden")), \
                    mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network access forbidden")), \
                    mock.patch("socket.create_connection", side_effect=AssertionError("Network access forbidden")), \
                    mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), \
                    mock.patch.object(tts.asyncio, "sleep", new_callable=mock.AsyncMock), \
                    mock.patch.object(tts, "decode_pcm", return_value=b""), \
                    redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                asyncio.run(tts.generate(self.story_path, output_dir=self.out))
            self.assertEqual(edge.Communicate.call_count, 3)
            for name, expected in originals.items():
                if name.endswith(".request.json"):
                    self.assertFalse((directory / name).exists(),
                                     "A failed request must not leave a valid cache sidecar")
                else:
                    self.assertEqual((directory / name).read_bytes(), expected,
                                     f"A {failure} failure corrupted cached {name}")

    def test_invalid_concurrency_is_rejected_without_hanging(self):
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "concurrency"):
                self.generate(concurrency=value)

    def test_scene_ids_cannot_escape_cache_directory_or_collide(self):
        for sid in ("../escape", "/absolute", "a/b", "a\\b", "", "UPPER", "a\x00b"):
            with self.subTest(id=sid):
                self.story["scenes"][0]["id"] = sid
                self.save_story()
                with mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), self.assertRaises(ValueError):
                    self.generate()
        self.story["scenes"][0]["id"] = "test"
        self.story["scenes"].append(deepcopy(self.story["scenes"][0]))
        self.save_story()
        with mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), self.assertRaises(ValueError):
            self.generate()
        self.assertFalse((self.root / "escape_01.mp3").exists())

    def test_empty_scenes_and_narration_are_rejected(self):
        original = deepcopy(self.story)
        for lines in ([], [""], ["   "], [{"text": ""}], [None]):
            self.story = deepcopy(original)
            self.story["scenes"][0]["lines"] = lines
            self.save_story()
            with self.subTest(lines=lines), mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), \
                    self.assertRaises(ValueError):
                self.generate()
        self.story["scenes"] = []
        self.save_story()
        with mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), self.assertRaises(ValueError):
            self.generate()

    def test_invalid_pause_values_are_rejected(self):
        for key in ("lead_in", "utterance_gap", "scene_tail"):
            for value in (-0.1, float("nan"), float("inf")):
                original = self.story.get(key)
                self.story[key] = value
                self.save_story()
                with self.subTest(key=key, value=value), mock.patch.object(tts.shutil, "which", return_value="ffmpeg"), \
                        self.assertRaisesRegex(ValueError, key):
                    self.generate()
                self.story[key] = original

    def test_real_cached_audio_rebuilds_sample_accurate_artifacts_offline(self):
        require_ffmpeg(self, codecs=("libmp3lame",))
        directory = self.seed_cache()
        cached = directory / "test_01.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", "0.12", "-c:a", "libmp3lame", str(cached)],
            check=True, capture_output=True,
        )
        pcm = tts.decode_pcm(cached)
        self.assertGreater(len(pcm), 0)
        with mock.patch.object(tts.importlib.metadata, "version", side_effect=tts.importlib.metadata.PackageNotFoundError):
            self.generate()
        timeline = json.loads((self.out / "timeline.json").read_text(encoding="utf-8"))
        line = timeline["scenes"][0]["lines"][0]
        expected_samples = (round(0.05 * tts.SAMPLE_RATE) + len(pcm) // 2
                            + round(0.04 * tts.SAMPLE_RATE) + round(1.5 * tts.SAMPLE_RATE))
        self.assertEqual(timeline["total_samples"], expected_samples)
        self.assertEqual(timeline["total_duration"], expected_samples / tts.SAMPLE_RATE)
        self.assertIsNone(timeline["voice"]["edge_tts_version"])
        self.assertTrue(timeline["voice"]["synthetic"])
        self.assertEqual(line["start_sample"], round(0.05 * tts.SAMPLE_RATE))
        self.assertEqual(line["end_sample"] - line["start_sample"], len(pcm) // 2)
        self.assertEqual(line["caption_end"], timeline["total_duration"])
        self.assertEqual(line["audio"], "audio/test_01.mp3")
        with wave.open(str(self.out / "narration.wav"), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, tts.SAMPLE_RATE))
            self.assertEqual(wav.getnframes(), expected_samples)
        self.assertGreater((self.out / "narration.mp3").stat().st_size, 0)
        self.assertIn(tts.caption_text(self.story["scenes"][0]["lines"][0]),
                      (self.out / "subtitles.srt").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "timeline.json").exists())
        self.assertFalse((self.root / "narration.wav").exists())


if __name__ == "__main__":
    unittest.main()
