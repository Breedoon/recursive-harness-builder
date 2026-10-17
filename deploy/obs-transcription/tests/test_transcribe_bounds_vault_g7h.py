"""vault-g7h regression tests: no segment past the audio end; empty-with-energy is degraded.

Run inside obs-transcription (real decode needs the sidecar venv):
  /workspace/runtime/transcription/venv/bin/python -m unittest tests.test_transcribe_bounds_vault_g7h
Set TRANSCRIBE_HELPER to test a candidate copy instead of the deployed helper.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(os.environ.get("TRANSCRIBE_HELPER", "/workspace/runtime/transcription/transcribe_faster_whisper.py"))
spec = importlib.util.spec_from_file_location("transcribe_faster_whisper_g7h", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def seg(start, end, text):
    return SimpleNamespace(start=start, end=end, text=text)


class FakeModel:
    segments: list = []
    duration = 80.4135

    def __init__(self, *a, **k):
        pass

    def transcribe(self, path, **kwargs):
        return iter(list(FakeModel.segments)), SimpleNamespace(
            language="en", language_probability=0.99, duration=FakeModel.duration
        )


def frontmatter(text):
    fm = {}
    for line in text.split("---")[1].splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            fm[k] = json.loads(v)
    return fm


class HelperRun(unittest.TestCase):
    def run_helper(self, segments, duration=80.4135, activity=None):
        FakeModel.segments = segments
        FakeModel.duration = duration
        tmp = Path(tempfile.mkdtemp())
        audio = tmp / "a.wav"
        audio.write_bytes(b"x")
        dest = tmp / "out"
        dest.mkdir()
        env = {
            "OBS_TRANSCRIPTION_DEVICE": "cpu",
            "OBS_TRANSCRIPTION_GPU_LOCK": str(tmp / "gpu.lock"),
            "OBS_TRANSCRIPTION_MODEL_DIR": str(tmp / "models"),
            "OBS_TRANSCRIPTION_MODEL": "tiny",
            "OBS_TRANSCRIPTION_CORRELATION_ID": "b" * 32,
        }
        fake_fw = SimpleNamespace(WhisperModel=FakeModel)
        act = activity or {"active_seconds": 0.0, "max_frame_dbfs": -90.0}
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"faster_whisper": fake_fw}), \
                mock.patch.object(mod, "_audio_activity", return_value=act), \
                mock.patch.object(sys, "argv", [str(SCRIPT), str(audio), "t", str(dest)]):
            rc = mod.main()
        self.assertEqual(rc, 0)
        text = (dest / "t.md").read_text(encoding="utf-8")
        return frontmatter(text), text

    def test_segment_starting_after_end_is_dropped(self):
        fm, text = self.run_helper([
            seg(0.14, 5.0, "Real start."),
            seg(70.0, 79.07, "Real ending."),
            seg(80.5, 109.05, "Toronto18 Brazil 1922"),
            seg(95.0, 109.0, "Alright, let me know if that's worth checking."),
        ])
        self.assertEqual(fm["transcription_status"], "ok")
        self.assertEqual(fm["segment_count"], 2)
        self.assertEqual(fm["transcription_quality"]["segments_dropped_past_end"], 2)
        self.assertNotIn("Toronto18", text)
        self.assertNotIn("worth checking", text)
        self.assertIn("Real ending.", text)

    def test_overrunning_segment_is_kept_clamped_and_flagged(self):
        fm, text = self.run_helper([
            seg(0.0, 40.0, "Real."),
            seg(79.07, 109.01, "Alright, let me know if that's worth checking."),
        ])
        self.assertEqual(fm["transcription_quality"]["segments_end_overrun"], [{"start": 79.07, "end": 109.01}])
        self.assertIn("`79.07-80.41`", text)
        self.assertIn("[!warning]", text)
        self.assertIn("worth checking", text)

    def test_legitimate_final_speech_kept_without_warning(self):
        # Final word ends a few hundred ms past decoded duration (codec rounding): keep, clamp, no warning.
        fm, text = self.run_helper([seg(76.0, 80.9, "Bye.")])
        self.assertEqual(fm["transcription_status"], "ok")
        self.assertEqual(fm["transcription_quality"]["segments_end_overrun"], [])
        self.assertEqual(fm["transcription_quality"]["segments_dropped_past_end"], 0)
        self.assertIn("`76.00-80.41` Bye.", text)
        self.assertNotIn("[!warning]", text)

    def test_empty_with_audio_energy_is_degraded(self):
        fm, text = self.run_helper([], duration=75.77, activity={"active_seconds": 52.3, "max_frame_dbfs": -6.0})
        self.assertEqual(fm["transcription_status"], "degraded")
        self.assertEqual(fm["transcription_quality"]["degraded_reason"], "no_segments_despite_audio_energy")
        self.assertIn("Transcription degraded", text)

    def test_empty_silence_is_ok_no_speech(self):
        fm, text = self.run_helper([], duration=10.0, activity={"active_seconds": 0.0, "max_frame_dbfs": -91.0})
        self.assertEqual(fm["transcription_status"], "ok")
        self.assertIn("No speech detected", text)

    def test_short_speech_kept(self):
        fm, text = self.run_helper([seg(0.2, 1.1, "Tak.")], duration=1.3)
        self.assertEqual(fm["transcription_status"], "ok")
        self.assertIn("`0.20-1.10` Tak.", text)


class RealDecodeActivity(unittest.TestCase):
    """Uses ffmpeg + the real faster_whisper decoder; skipped where unavailable."""

    def make(self, tmp, lavfi):
        path = Path(tmp) / "f.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", lavfi, "-ac", "1", "-ar", "16000", str(path)], check=True)
        return path

    def setUp(self):
        try:
            import faster_whisper.audio  # noqa: F401
        except Exception:
            self.skipTest("faster_whisper not importable")

    def test_digital_silence_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            act = mod._audio_activity(self.make(tmp, "anullsrc=r=16000:cl=mono:d=5"))
        self.assertLess(act["active_seconds"], mod.MIN_ACTIVE_AUDIO_SECONDS)

    def test_low_noise_floor_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            act = mod._audio_activity(self.make(tmp, "anoisesrc=d=5:c=pink:a=0.002"))
        self.assertLess(act["active_seconds"], mod.MIN_ACTIVE_AUDIO_SECONDS)

    def test_loud_tone_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            act = mod._audio_activity(self.make(tmp, "sine=f=440:d=3"))
        self.assertGreaterEqual(act["active_seconds"], 2.5)


if __name__ == "__main__":
    unittest.main()
