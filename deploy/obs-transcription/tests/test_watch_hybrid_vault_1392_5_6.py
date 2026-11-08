"""CPU-only routing and retention checks for the Watch hybrid."""
import base64
import hashlib
import json
import re
from types import SimpleNamespace

import numpy as np
import pytest

from test_watch_windowed_vault_1392_5 import context, mod, read_result


@pytest.mark.parametrize('amplitude,boosted,threshold', [(1., False, .5), (.001, True, .3)])
def test_hybrid_gain_is_scoped_and_original_samples_unchanged(context, monkeypatch, amplitude, boosted, threshold):
    monkeypatch.setenv('OBS_TRANSCRIPTION_HYBRID_WATCH', '1')
    context.audio = np.full(16000, amplitude, dtype=np.float32)
    original = context.audio.copy()
    assert mod.main() == 0
    _, fields = read_result(context)
    window = fields['language_windows'][0]
    assert window['loudness_adaptation']['boosted'] is boosted
    assert window['loudness_adaptation']['vad_threshold'] == threshold
    assert context.calls[0][1]['vad_parameters']['threshold'] == threshold
    assert np.array_equal(context.audio, original)
    assert fields['whisper_settings']['watch_loudness_hybrid'] is True


def test_nonwatch_caller_preserves_unfiltered_text(context, monkeypatch):
    monkeypatch.delenv('OBS_TRANSCRIPTION_HYBRID_WATCH', raising=False)
    context.result = lambda i: [SimpleNamespace(start=.1, end=.5, text='Thanks for watching!',
        no_speech_prob=.8, avg_logprob=-.9, compression_ratio=1.)]
    assert mod.main() == 0
    text, fields = read_result(context)
    assert 'Thanks for watching!' in text
    assert 'experimental_guard' not in fields['transcription_quality']


def test_hybrid_filter_preserves_original_raw(context, monkeypatch):
    monkeypatch.setenv('OBS_TRANSCRIPTION_HYBRID_WATCH', '1')
    context.audio = np.full(16000, .001, dtype=np.float32)
    context.result = lambda i: [SimpleNamespace(start=.1, end=.5, text='Thanks for watching!',
        no_speech_prob=.8, avg_logprob=-.9, compression_ratio=1.)]
    assert mod.main() == 0
    text, fields = read_result(context)
    encoded = re.search(r'<!-- OBS_RAW_ASR_BASE64_V1:([^ ]+) -->', text).group(1)
    raw = base64.b64decode(encoded)
    assert json.loads(raw)['segments'][0]['text'] == 'Thanks for watching!'
    assert fields['transcription_quality']['raw_asr_capture']['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert fields['transcription_quality']['experimental_guard']['events']
    assert fields['transcription_status'] == 'degraded'


def test_explicit_vad_override_disables_hybrid(context, monkeypatch):
    monkeypatch.setenv('OBS_TRANSCRIPTION_HYBRID_WATCH', '1')
    monkeypatch.setenv('OBS_TRANSCRIPTION_VAD_FILTER', '0')
    context.audio = np.full(16000, .001, dtype=np.float32)
    assert mod.main() == 0
    _, fields = read_result(context)
    assert context.calls[0][1]['vad_filter'] is False
    assert np.array_equal(context.calls[0][0], context.audio)
    assert 'watch_loudness_hybrid' not in fields['whisper_settings']
