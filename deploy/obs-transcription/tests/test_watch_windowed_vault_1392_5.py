"""CPU-only checks for the isolated 60-second hard-seam candidate."""
import base64
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'transcribe_faster_whisper.py'
spec = importlib.util.spec_from_file_location('candidate_v19', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def context(tmp_path, monkeypatch):
    audio_path = tmp_path / 'input.wav'
    audio_path.write_bytes(b'fixture')
    dest = tmp_path / 'output'
    dest.mkdir()
    state = SimpleNamespace(audio=np.ones(16000, dtype=np.float32), detections=[], calls=[], probability=0.9,
                            result=lambda i: [SimpleNamespace(start=0.1, end=0.5, text='Test.')],
                            fail_detection=False, override_distribution=None, vad=lambda a: [{'start': 0, 'end': len(a)}])
    class Model:
        def __init__(self, *args, **kwargs): self.model = SimpleNamespace(is_multilingual=True)
        def detect_language(self, audio, **kwargs):
            if state.fail_detection: raise RuntimeError('detection failed')
            state.detections.append((audio, kwargs))
            if state.override_distribution is not None: return state.override_distribution
            language = 'en' if len(state.detections) <= 2 else 'pl'
            p = state.probability
            return language, p, [(k, p if k == language else (1-p)/3) for k in ('en', 'pl', 'ru', 'uk')]
        def transcribe(self, audio, **kwargs):
            state.calls.append((audio, kwargs))
            if isinstance(audio, str):
                return iter([SimpleNamespace(start=0.0, end=1.0, text='Legacy.')]), SimpleNamespace(language='en', language_probability=0.9, duration=1.0)
            return iter(state.result(len(state.calls) - 1)), SimpleNamespace()
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    monkeypatch.setitem(sys.modules, 'faster_whisper.audio', SimpleNamespace(decode_audio=lambda *args, **kwargs: state.audio))
    monkeypatch.setitem(sys.modules, 'faster_whisper.vad', SimpleNamespace(VadOptions=lambda **kwargs: kwargs,
                      get_speech_timestamps=lambda audio, options: state.vad(audio)))
    monkeypatch.setattr(mod, '_audio_activity', lambda path: {'active_seconds': 0.0, 'max_frame_dbfs': -120.0})
    monkeypatch.setenv('OBS_TRANSCRIPTION_DEVICE', 'cpu')
    monkeypatch.setenv('OBS_TRANSCRIPTION_GPU_LOCK', str(tmp_path / 'gpu.lock'))
    monkeypatch.setenv('OBS_TRANSCRIPTION_MODEL_DIR', str(tmp_path / 'models'))
    monkeypatch.setenv('OBS_TRANSCRIPTION_MODEL', 'tiny')
    monkeypatch.setenv('OBS_TRANSCRIPTION_CORRELATION_ID', 'a' * 32)
    monkeypatch.setenv('OBS_TRANSCRIPTION_WINDOWED', '1')
    monkeypatch.setenv('OBS_TRANSCRIPTION_VAD_FILTER', '1')
    monkeypatch.delenv('OBS_TRANSCRIPTION_LANGUAGE', raising=False)
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), str(audio_path), 'test', str(dest)])
    state.path, state.dest = audio_path, dest
    return state


def read_result(state):
    text = (state.dest / 'test.md').read_text(encoding='utf-8')
    fields = dict(line.split(': ', 1) for line in text.split('---')[1].strip().splitlines())
    return text, {key: json.loads(value) for key, value in fields.items()}


def test_untimed_repeat_collapse_preserves_raw(context):
    original = 'before ' + 'alpha beta ' * 12 + 'after'
    context.result = lambda i: [SimpleNamespace(start=0.1, end=0.5, text='timed'),
                                SimpleNamespace(start=2.0, end=3.0, text=original)]
    assert mod.main() == 0
    text, fm = read_result(context)
    encoded = re.search(r'<!-- OBS_RAW_ASR_BASE64_V1:([^ ]+) -->', text).group(1)
    raw = base64.b64decode(encoded)
    assert json.loads(raw)['untimed_segments'][0]['text'] == original
    assert '> before alpha beta [repeat collapsed ×11] after' in text
    summary = fm['transcription_quality']['untimed_provisional_repeat_collapse']
    assert summary['raw_sha256'] == hashlib.sha256(raw).hexdigest()
    assert summary['items'][0]['removed_words'] == 22
    diagnostics = fm['transcription_quality']['decoder_diagnostics']
    assert diagnostics['strong_repeat_untimed_before_collapse_words'] == 24
    assert diagnostics['strong_repeat_untimed_after_collapse_words'] == 0
    assert fm['transcription_status'] == 'degraded'
    assert 'Untimed repetition was provisionally collapsed' in text


def test_untimed_items_collapse_in_emission_order_without_mutating_raw():
    original = [{'text': 'word ' * 8}, {'text': 'word ' * 8}]
    output, summary = mod._collapse_untimed_items(original)
    assert summary[0]['removed_words'] == 15
    assert mod._strong_periodic_run(' '.join(x['text'] for x in output)) == 0
    assert original == [{'text': 'word ' * 8}, {'text': 'word ' * 8}]


def test_untimed_multiple_repeats_and_unicode():
    original = [{'text': ('ŻÓŁĆ! ' * 16) + 'break ' + ('κόσμος ' * 16)}]
    output, summary = mod._collapse_untimed_items(original)
    assert summary[0]['runs_collapsed'] == 2
    assert summary[0]['remaining_periodic_words'] == 0
    assert output[0]['text'].count('[repeat collapsed ×15]') == 2
    assert original[0]['text'].count('ŻÓŁĆ') == 16


@pytest.mark.parametrize('start,end,reason', [(-3,-2,'entirely_before'),(-1,0,'at_or_before_start'),
                                              (0.4,0.4,'zero_length_within'),(1,1.5,'start_at_end'),
                                              (2,3,'entirely_after')])
def test_untimed_numeric_provenance(context, start, end, reason):
    context.result = lambda i: [SimpleNamespace(start=start,end=end,text='sample')]
    assert mod.main() == 0
    _, fields = read_result(context)
    diagnostics = fields['transcription_quality']['decoder_diagnostics']
    assert diagnostics['untimed_segment_count'] == 1
    bounds = diagnostics['untimed_segment_bounds'][0]
    assert bounds['reason'] == reason and bounds['window_duration'] == 1.0
    assert bounds['raw_start'] == start and bounds['raw_end'] == end
    assert 'text' not in bounds


def test_60_second_vad_reset_and_30_second_votes(context):
    context.audio = np.ones(125 * 16000, dtype=np.float32)
    assert mod.main() == 0
    text, fm = read_result(context)
    assert [len(a) for a, _ in context.detections] == [30*16000,30*16000,30*16000,30*16000,5*16000]
    assert all(k == {'vad_filter': False} for _, k in context.detections)
    assert [len(a) for a, _ in context.calls] == [60*16000,60*16000,5*16000]
    assert all(k['vad_filter'] is True and 'word_timestamps' not in k for _, k in context.calls)
    assert all(k['condition_on_previous_text'] is False and k['no_speech_threshold'] == 0.6 and
               k['log_prob_threshold'] == -1.0 and k['compression_ratio_threshold'] == 2.4 for _, k in context.calls)
    assert [k['language'] for _, k in context.calls] == ['en','pl','pl']
    assert [(w['start'],w['end'],w['language']) for w in fm['language_windows']] == [(0,60,'en'),(60,120,'pl'),(120,125,'pl')]
    assert fm['duration_seconds'] == 125 and fm['whisper_settings']['vad_filter'] is True
    assert fm['transcription_quality']['decoder_diagnostics']['hard_seams_seconds'] == [60.0,120.0]
    assert fm['transcription_quality']['decoder_diagnostics']['hard_seam_policy'] == 'no_overlap_coverage_unverified'
    assert '- `0.10-0.50` Test.' in text and '- `60.10-60.50` Test.' in text


@pytest.mark.parametrize('language', ['en','pl','ru','uk','de'])
def test_forced_language_preserves_exact_legacy_call(context, monkeypatch, language):
    monkeypatch.setenv('OBS_TRANSCRIPTION_LANGUAGE', language)
    assert mod.main() == 0
    _, fm = read_result(context)
    assert len(context.calls) == 1 and context.calls[0][0] == str(context.path)
    assert context.calls[0][1]['language'] == language and context.calls[0][1]['vad_filter'] is True
    assert context.detections == [] and 'language_windows' not in fm


def test_non_watch_caller_keeps_legacy(context, monkeypatch):
    monkeypatch.delenv('OBS_TRANSCRIPTION_WINDOWED')
    assert mod.main() == 0 and context.calls[0][0] == str(context.path)


def test_silent_window_does_not_call_model(context):
    context.audio = np.zeros(2 * 16000, dtype=np.float32)
    assert mod.main() == 0
    _, fm = read_result(context)
    assert context.detections == [] and context.calls == [] and fm['segment_count'] == 0


def test_vad_empty_below_minus_80_dbfs_remains_degraded(context):
    context.audio = np.full(16000, 0.00001, dtype=np.float32)
    context.vad = lambda audio: []
    assert mod.main() == 0
    text, fm = read_result(context)
    assert len(context.calls) == 1 and len(context.detections) == 1
    assert fm['transcription_status'] == 'degraded'
    assert len(fm['transcription_quality']['decoder_diagnostics']['vad_empty_windows']) == 1
    assert 'No primary transcript was produced' in text
    assert 'No speech detected by the selected transcription model' not in text


def test_nonzero_vad_empty_is_degraded_and_skipped(context):
    context.vad = lambda audio: []
    assert mod.main() == 0
    text, fm = read_result(context)
    assert len(context.calls) == 1 and context.calls[0][1]['vad_filter'] is False
    assert 'No primary transcript was produced' in text
    assert 'No speech detected by the selected transcription model' not in text
    assert fm['transcription_status'] == 'degraded' and fm['segment_count'] == 0
    assert fm['transcription_quality']['degraded_reason'] == 'vad_empty_nonzero_window'
    diagnostics = fm['transcription_quality']['decoder_diagnostics']
    assert diagnostics['vad_empty_windows'] == [{'start': 0.0, 'end': 1.0}]
    assert diagnostics['vad_empty_alternatives'][0]['status'] == 'decoded_not_selected'
    assert diagnostics['vad_empty_alternatives'][0]['tokens'] == 1


def test_loud_vad_empty_does_not_claim_asr_produced_no_speech(context, monkeypatch):
    context.vad = lambda audio: []
    monkeypatch.setattr(mod, '_audio_activity', lambda path: {'active_seconds': 1.0, 'max_frame_dbfs': -10.0})
    assert mod.main() == 0
    text, fm = read_result(context)
    assert 'No primary transcript was produced' in text
    assert 'model produced no speech' not in text
    assert fm['transcription_status'] == 'degraded'
    assert fm['transcription_quality']['degraded_reason'] == 'vad_empty_nonzero_window'


def test_vad_empty_alternative_attempts_are_bounded(context):
    context.audio = np.ones(9 * 60 * 16000, dtype=np.float32)
    context.vad = lambda audio: []
    assert mod.main() == 0
    _, fm = read_result(context)
    diag = fm['transcription_quality']['decoder_diagnostics']
    assert len(diag['vad_empty_windows']) == 9 and len(context.calls) == 8
    assert all(x['status'] == 'decoded_not_selected' for x in diag['vad_empty_alternatives'][:8])
    assert diag['vad_empty_alternatives'][8]['status'] == 'limit_skipped'
    assert fm['transcription_status'] == 'degraded' and fm['segment_count'] == 0


def test_repeat_retries_are_capped_across_recording(context):
    context.audio = np.ones(9 * 60 * 16000, dtype=np.float32)
    context.result = lambda i: [SimpleNamespace(start=0.1,end=0.5,text='word ' * 24)]
    assert mod.main() == 0
    _, fm = read_result(context)
    diag = fm['transcription_quality']['decoder_diagnostics']
    assert diag['extra_decode_attempts'] == 8 and len(context.calls) == 17
    assert len(diag['repeat_retry_windows']) == 9
    assert diag['repeat_retry_windows'][-1]['status'] == 'retry_limit_skipped'
    assert fm['transcription_status'] == 'degraded'


def test_vad_and_repeat_diagnostics_share_the_same_budget(context):
    context.audio = np.ones(10 * 60 * 16000, dtype=np.float32)
    def vad(chunk):
        offset = (chunk.__array_interface__['data'][0] - context.audio.__array_interface__['data'][0]) // context.audio.itemsize
        return [{'start':0,'end':len(chunk)}] if offset < 5 * 60 * 16000 else []
    context.vad = vad
    context.result = lambda i: [SimpleNamespace(start=0.1,end=0.5,text='word ' * 24)]
    assert mod.main() == 0
    _, fm = read_result(context)
    diag = fm['transcription_quality']['decoder_diagnostics']
    assert diag['extra_decode_attempts'] == 8 and diag['extra_decode_limit'] == 8
    assert len(diag['repeat_retry_windows']) == 5
    assert len(diag['vad_empty_alternatives']) == 5
    assert [item['status'] for item in diag['vad_empty_alternatives']].count('limit_skipped') == 2
    assert len(context.calls) == 13


def test_cross_boundary_segment_is_clamped_and_reported(context):
    context.audio = np.ones(65 * 16000, dtype=np.float32)
    context.result = lambda i: [SimpleNamespace(start=59.0,end=62.0,text='First.')] if i == 0 else [SimpleNamespace(start=0.1,end=0.4,text='Second.')]
    assert mod.main() == 0
    text, fm = read_result(context)
    assert '- `59.00-60.00` First.' in text and '- `60.10-60.40` Second.' in text
    assert fm['transcription_quality']['segments_end_overrun'] == [{'start':59.0,'end':62.0}]
    assert fm['transcription_status'] == 'degraded'
    assert fm['transcription_quality']['degraded_reason'] == 'segment_timing_outside_decoded_window'
    assert '[!warning] Segment timing required material clipping' in text
    assert '[!warning] Segment timing overran a decoded window' in text


def test_negative_segment_start_clipped_flagged_and_retained(context):
    context.result = lambda i: [SimpleNamespace(start=-0.6,end=0.5,text='First.')]
    assert mod.main() == 0
    text, fm = read_result(context)
    assert '- `0.00-0.50` First.' in text
    assert fm['transcription_status'] == 'degraded'
    assert fm['transcription_quality']['decoder_diagnostics']['segments_bounds_adjusted'] == [
        {'raw_start':-0.6,'raw_end':0.5,'start':0.0,'end':0.5}]
    assert '[!warning] Segment timing required material clipping' in text


@pytest.mark.parametrize('start,end', [(-0.5,0.5),(0.1,1.5)])
def test_up_to_half_second_clip_is_logged_without_degrading(context, start, end):
    context.result = lambda i: [SimpleNamespace(start=start,end=end,text='Test.')]
    assert mod.main() == 0
    text, fm = read_result(context)
    assert fm['transcription_status'] == 'ok'
    assert len(fm['transcription_quality']['decoder_diagnostics']['segments_bounds_adjusted']) == 1
    assert '[!warning] Segment timing required material clipping' not in text


@pytest.mark.parametrize('start,end', [(-2.0,-1.0),(2.0,3.0),(1.0,1.0)])
def test_wholly_outside_segment_is_untimed_and_preserved(context, start, end):
    context.result = lambda i: [SimpleNamespace(start=start,end=end,text='Not silently discarded.')]
    assert mod.main() == 0
    text, fm = read_result(context)
    assert fm['transcription_status'] == 'degraded'
    assert fm['transcription_quality']['degraded_reason'] == 'untimed_asr_segments'
    assert '> Not silently discarded.' in text and '## Segments' not in text
    raw, _ = raw_payload(text)
    assert raw['untimed_segments'][0]['text'] == 'Not silently discarded.'
    assert raw['untimed_segments'][0]['raw_start'] == start and raw['untimed_segments'][0]['raw_end'] == end


def raw_payload(text):
    marker = re.search(r'^<!-- OBS_RAW_ASR_BASE64_V1:([A-Za-z0-9+/=]+) -->$', text, re.M)
    assert marker is not None
    payload = base64.b64decode(marker.group(1), validate=True)
    return json.loads(payload), hashlib.sha256(payload).hexdigest()


def test_periodic_loop_is_labeled_degraded_and_raw_retained(context):
    loop = 'word ' * 24
    context.result = lambda i: [SimpleNamespace(start=0.1,end=0.5,text=loop)]
    assert mod.main() == 0
    text, fm = read_result(context)
    assert fm['transcription_status'] == 'degraded'
    assert fm['transcription_quality']['degraded_reason'] == 'provisional_repeat_collapse'
    diagnostics = fm['transcription_quality']['decoder_diagnostics']
    assert diagnostics['strong_repeat_segments'] == [{'start': 0.1, 'end': 0.5, 'periodic_words': 24}]
    assert diagnostics['strong_repeat_windows'] == [{'start': 0.0, 'end': 1.0, 'periodic_words': 24}]
    raw, digest = raw_payload(text)
    assert raw['segments'][0]['text'] == loop
    assert fm['transcription_quality']['provisional_repeat_collapse']['raw_sha256'] == digest
    assert '[repeat collapsed ×23]' in text
    assert fm['transcription_quality']['decoder_diagnostics']['strong_repeat_after_collapse_words'] == 0
    assert '[!warning] The transcript contains strong periodic repetition' in text


def test_one_bounded_retry_is_not_selected_and_remains_degraded(context):
    def result(i):
        if i == 0:
            return [SimpleNamespace(start=0.1,end=0.2,text='Keep'),
                    SimpleNamespace(start=0.2,end=0.5,text='loop ' * 24)]
        return [SimpleNamespace(start=0.1,end=0.5,text='Keep recovered')]
    context.result = result
    assert mod.main() == 0
    text, fm = read_result(context)
    assert len(context.calls) == 2
    assert context.calls[1][1]['temperature'] == (0.2,0.4,0.6)
    assert context.calls[1][1]['condition_on_previous_text'] is False
    assert 'Keep recovered' not in text and 'Keep' in text and '[repeat collapsed ×23]' in text
    assert fm['transcription_status'] == 'degraded'
    retry = fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]
    assert retry['status'] == 'alternative_not_selected' and retry['attempts'] == 1
    assert retry['retry_tokens'] == 2


def test_all_flagged_original_is_never_replaced_by_unrelated_retry(context):
    def result(i):
        if i == 0: return [SimpleNamespace(start=0.1,end=0.5,text='loop ' * 24)]
        return [SimpleNamespace(start=0.1,end=0.5,text='unrelated')]
    context.result = result
    assert mod.main() == 0
    text, fm = read_result(context)
    assert '[repeat collapsed ×23]' in text and 'unrelated' not in text
    assert fm['transcription_status'] == 'degraded'
    assert fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]['status'] == 'alternative_not_selected'


def test_retry_dropping_clean_content_keeps_original_degraded(context):
    def result(i):
        if i == 0:
            return [SimpleNamespace(start=0.1,end=0.2,text='Keep'),
                    SimpleNamespace(start=0.2,end=0.5,text='loop ' * 24)]
        return [SimpleNamespace(start=0.1,end=0.5,text='unrelated')]
    context.result = result
    assert mod.main() == 0
    text, fm = read_result(context)
    assert len(context.calls) == 2 and 'Keep' in text and '[repeat collapsed ×23]' in text
    assert fm['transcription_status'] == 'degraded'
    retry = fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]
    assert retry['status'] == 'alternative_not_selected' and retry['retry_tokens'] == 1


def test_retry_with_malformed_or_outside_times_keeps_original(context):
    def result(i):
        if i == 0: return [SimpleNamespace(start=0.1,end=0.5,text='loop ' * 24)]
        return [SimpleNamespace(start=4.0,end=5.0,text='wrong')]
    context.result = result
    assert mod.main() == 0
    text, fm = read_result(context)
    retry = fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]
    assert retry['status'] == 'unresolved_preserve_original'
    assert len(context.calls) == 2 and '[repeat collapsed ×23]' in text and 'wrong' not in text


@pytest.mark.parametrize('start,end', [(-0.1,0.5),(0.5,1.1)])
def test_partially_outside_retry_is_never_labeled_valid(context, start, end):
    def result(i):
        if i == 0: return [SimpleNamespace(start=0.1,end=0.5,text='loop ' * 24)]
        return [SimpleNamespace(start=start,end=end,text='alternative')]
    context.result = result
    assert mod.main() == 0
    text, fm = read_result(context)
    retry = fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]
    assert retry['status'] == 'unresolved_preserve_original' and '[repeat collapsed ×23]' in text


def test_vad_alternative_partial_overrun_is_flagged(context):
    context.vad = lambda audio: []
    context.result = lambda i: [SimpleNamespace(start=-0.1,end=0.5,text='alternative')]
    assert mod.main() == 0
    _, fm = read_result(context)
    alt = fm['transcription_quality']['decoder_diagnostics']['vad_empty_alternatives'][0]
    assert alt['status'] == 'invalid_timestamps' and fm['transcription_status'] == 'degraded'


def test_compression_alone_triggers_retry(context):
    original = SimpleNamespace(start=0.1,end=0.5,text='Legitimate phrase',compression_ratio=3.0)
    alternative = SimpleNamespace(start=0.1,end=0.5,text='Legitimate phrase',compression_ratio=1.0)
    context.result = lambda i: [original] if i == 0 else [alternative]
    assert mod.main() == 0
    _, fm = read_result(context)
    retry = fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]
    assert len(context.calls) == 2 and retry['original_high_compression_segments'] == 1
    assert retry['status'] == 'alternative_not_selected' and fm['transcription_status'] == 'degraded'


def test_retry_exception_keeps_original_and_reports_only_type(context):
    def result(i):
        if i == 0: return [SimpleNamespace(start=0.1,end=0.5,text='loop ' * 24)]
        raise RuntimeError('private detail')
    context.result = result
    assert mod.main() == 0
    text, fm = read_result(context)
    retry = fm['transcription_quality']['decoder_diagnostics']['repeat_retry_windows'][0]
    assert retry['status'] == 'retry_error_preserve_original' and retry['error_type'] == 'RuntimeError'
    assert 'private detail' not in text and '[repeat collapsed ×23]' in text
    assert len(context.calls) == 2 and fm['transcription_status'] == 'degraded'


def test_repetition_across_segments_flags_window(context):
    context.result = lambda i: [SimpleNamespace(start=0.1,end=0.3,text='a b ' * 4),
                                SimpleNamespace(start=0.3,end=0.5,text='a b ' * 4)]
    assert mod.main() == 0
    text, fm = read_result(context)
    diagnostics = fm['transcription_quality']['decoder_diagnostics']
    assert not diagnostics['strong_repeat_segments']
    assert diagnostics['strong_repeat_windows'][0]['periodic_words'] == 16
    assert diagnostics['strong_repeat_after_collapse_words'] == 0
    assert '[repeat collapsed ×7]' in text
    raw, _ = raw_payload(text)
    assert len(raw['segments']) == 2
    assert fm['transcription_status'] == 'degraded'


def test_repetition_far_apart_across_windows_is_not_collapsed(context):
    context.audio = np.ones(120 * 16000, dtype=np.float32)
    context.result = lambda i: [SimpleNamespace(start=0.1,end=0.5,text='no ' * 8)]
    assert mod.main() == 0
    text, fm = read_result(context)
    diag = fm['transcription_quality']['decoder_diagnostics']
    assert not diag['strong_repeat_windows'] and not diag['repeat_retry_windows']
    assert diag['strong_repeat_full_transcript_words'] == 0
    assert fm['transcription_status'] == 'ok' and '[repeat collapsed' not in text
    assert text.split('# Transcript',1)[1].split('## Segments',1)[0].split().count('no') == 16


def test_repetition_contiguous_at_60_second_seam_is_collapsed(context):
    context.audio = np.ones(120 * 16000, dtype=np.float32)
    context.result = lambda i: [SimpleNamespace(start=59.5,end=60.0,text='no ' * 8)] if i == 0 else [SimpleNamespace(start=0.0,end=0.5,text='no ' * 8)]
    assert mod.main() == 0
    text, fm = read_result(context)
    assert fm['transcription_status'] == 'degraded' and '[repeat collapsed ×15]' in text
    assert fm['transcription_quality']['decoder_diagnostics']['strong_repeat_full_transcript_words'] == 16
    raw, _ = raw_payload(text)
    assert [seg['text'] for seg in raw['segments']] == ['no ' * 8, 'no ' * 8]


def test_short_genuine_repetitions_are_not_flagged(context):
    context.result = lambda i: [SimpleNamespace(start=0.1,end=0.5,text='no no no yes yes yes')]
    assert mod.main() == 0
    _, fm = read_result(context)
    assert fm['transcription_status'] == 'ok'
    assert not fm['transcription_quality']['decoder_diagnostics']['strong_repeat_windows']


def test_qualifying_short_loop_not_masked_by_longer_nonqualifying_pattern():
    text = 'token ' * 16 + 'alpha beta gamma delta epsilon zeta eta theta ' * 7
    assert mod._strong_periodic_run(text) >= 16










































































































































































































































def test_cross_segment_collapse_keeps_nonloop_text_and_original_segments():
    segments = [SimpleNamespace(start=0.0,end=1.0,text='Before ' + 'a b ' * 4),
                SimpleNamespace(start=1.0,end=2.0,text='a b ' * 4 + 'After')]
    original = [seg.text for seg in segments]
    collapsed, receipt = mod._collapse_periodic_segments(segments)
    assert [seg.text for seg in segments] == original
    output = ' '.join(seg.text for seg in collapsed)
    assert 'Before' in output and 'After' in output and '[repeat collapsed ×7]' in output
    assert receipt['runs_collapsed'] == 1 and receipt['removed_words'] == 14
    assert receipt['remaining_periodic_words'] == 0


@pytest.mark.parametrize('gap,expected_runs', [(0.0,1),(2.0,1),(2.01,0),(3599.0,0)])
def test_collapse_requires_temporal_continuity(gap, expected_runs):
    segments = [SimpleNamespace(start=0.0,end=1.0,text='no ' * 8),
                SimpleNamespace(start=1.0 + gap,end=2.0 + gap,text='no ' * 8)]
    collapsed, receipt = mod._collapse_periodic_segments(segments)
    assert receipt['runs_collapsed'] == expected_runs
    assert sum(seg.text.count('[repeat collapsed') for seg in collapsed) == expected_runs
    assert mod._strong_periodic_segments(segments) == (16 if expected_runs else 0)


def test_unicode_combining_marks_collapse_with_original_offsets():
    raw = ' '.join('é' if i % 3 else 'e\u0301' for i in range(16))
    segments = [SimpleNamespace(start=0.0,end=1.0,text=raw)]
    assert mod._strong_periodic_run(raw) == 16
    collapsed, receipt = mod._collapse_periodic_segments(segments)
    assert '[repeat collapsed ×15]' in collapsed[0].text
    assert receipt['runs_collapsed'] == 1 and receipt['remaining_periodic_words'] == 0
    assert segments[0].text == raw


def test_collapse_attempt_limit_is_distinct_from_residual_loop():
    segments = [SimpleNamespace(start=0.0,end=1.0,text='word ' * 16)]
    _, receipt = mod._collapse_periodic_segments(segments, limit=1)
    assert receipt['attempt_limit_hit'] is True
    assert receipt['remaining_periodic_words'] == 0


def test_long_genuine_repetition_is_explicitly_provisional_not_lossless():
    segments = [SimpleNamespace(start=0.0,end=1.0,text='no ' * 16)]
    collapsed, receipt = mod._collapse_periodic_segments(segments)
    assert '[repeat collapsed ×15]' in collapsed[0].text
    assert receipt['removed_words'] == 15
    assert segments[0].text == 'no ' * 16


def test_out_of_set_language_is_flagged(context):
    context.override_distribution = ('es',0.4,[('es',0.4),('pl',0.35),('en',0.15),('ru',0.05),('uk',0.05)])
    assert mod.main() == 0
    _, fm = read_result(context)
    assert fm['language'] == 'pl' and fm['transcription_status'] == 'degraded'
    assert fm['language_windows'][0]['unrestricted_language'] == 'es'


def test_fp16_rounded_mass_and_raw_metadata(context):
    scores = [('en',1.0),('pl',3.159046173095703e-06),('ru',1.8894672393798828e-05),('uk',2.980232238769531e-07)]
    context.override_distribution = ('en',1.0,scores)
    assert mod.main() == 0
    _, fm = read_result(context)
    window = fm['language_windows'][0]
    assert window['language_set_mass_raw'] == 1.0000223517417908
    assert window['language_set_mass'] == 1.0 and window['language_set_mass_rounding_corrected'] is True


@pytest.mark.parametrize('scores', [
    [('en',.8),('en',.1),('pl',.1),('ru',0),('uk',0)],
    [('en',.8),('pl',.1),('ru',.1)],
    [('en',.8),('pl',.3),('ru',0),('uk',0)],
    [('en',.8),('pl',float('nan')),('ru',0),('uk',0)],
    [('en',0),('pl',0),('ru',0),('uk',0)],
])
def test_invalid_probabilities_fail_before_transcribe(context, scores):
    context.override_distribution = ('en',.8,scores)
    assert mod.main() == 80 and context.calls == []


def test_language_detection_failure_preserves_failure_status(context):
    context.fail_detection = True
    assert mod.main() == 80 and context.calls == []
    _, fm = read_result(context)
    assert fm['transcription_status'] == 'failed'


@pytest.mark.parametrize('field,value', [('start',float('nan')),('end',float('nan')),
                                         ('start',float('inf')),('end',float('inf')),
                                         ('start',float('-inf')),('end',float('-inf')),('start',2.0)])
def test_invalid_segment_timing_fails(context, field, value):
    segment = SimpleNamespace(start=0.1,end=0.5,text='Test.')
    setattr(segment, field, value)
    context.result = lambda i: [segment]
    assert mod.main() == 80
