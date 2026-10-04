import asyncio
import wave
from pathlib import Path

import httpx
import numpy as np

from memesearch.config import Settings
from memesearch.pipeline import MLClient, Pipeline
from memesearch.prompt import build_prompt, embedding_text
from memesearch.song import song_name
from memesearch.sounds import SAMPLE_RATE, aggregate, has_music, labels, read_wav, windows


def write_wav(path: Path, seconds: float, rate: int = 16000, channels: int = 1) -> None:
    t = np.arange(int(seconds * rate)) / rate
    tone = (np.sin(2 * np.pi * 440 * t) * 12000).astype("<i2")
    if channels == 2:
        tone = np.repeat(tone[:, None], 2, axis=1).reshape(-1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone.tobytes())


def test_read_wav_resamples_and_mixes_down(tmp_path: Path):
    p = tmp_path / "a.wav"
    write_wav(p, 2.0, rate=44100, channels=2)
    audio = read_wav(p, 60)
    assert audio.dtype == np.float32 and abs(len(audio) - 2 * SAMPLE_RATE) < 5
    assert 0.3 < float(np.abs(audio).max()) < 0.5


def test_windows_cover_long_audio_with_a_limit():
    audio = np.zeros(SAMPLE_RATE * 125, dtype=np.float32)
    w = windows(audio, seconds=10, limit=12)
    assert len(w) == 12 and all(len(x) == SAMPLE_RATE * 10 for x in w)
    assert len(windows(np.zeros(SAMPLE_RATE * 4, dtype=np.float32))) == 1
    assert windows(np.zeros(100, dtype=np.float32)) == []


def test_aggregate_takes_best_score_and_drops_noise():
    preds = [
        [{"label": "Music", "score": 0.4}, {"label": "Speech", "score": 0.9}, {"label": "Dog", "score": 0.05}],
        [{"label": "Music", "score": 0.8}, {"label": "Laughter", "score": 0.3}],
    ]
    tags = aggregate(preds)
    assert tags == [{"label": "Music", "score": 0.8}, {"label": "Laughter", "score": 0.3}]
    assert labels(tags) == ["music / музыка", "laughter / смех"]
    assert has_music(tags) and not has_music([{"label": "Laughter", "score": 0.9}])


def test_song_name():
    assert song_name({"track": {"title": "Never Gonna Give You Up", "subtitle": "Rick Astley"}}) == (
        "Rick Astley — Never Gonna Give You Up"
    )
    assert song_name({"matches": []}) == "" and song_name(None) == ""


def test_prompt_and_embedding_text_include_audio():
    prompt = build_prompt(
        kind="video",
        frame_count=3,
        frame_times=[0.5, 1.5, 2.5],
        duration_ms=3000,
        transcript="",
        caption="",
        filename="",
        extra="",
        sounds=["music / музыка", "laughter / смех"],
        song="Rick Astley — Never Gonna Give You Up",
    )
    assert "music / музыка; laughter / смех" in prompt and "Rick Astley — Never Gonna Give You Up" in prompt
    text = embedding_text({"title": "t", "song": "Rick Astley — Never Gonna Give You Up", "sounds": ["music / музыка"]})
    assert "Rick Astley" in text and "music / музыка" in text


def test_listen_recognizes_songs_only_when_music_is_heard(tmp_path: Path):
    class FakeML:
        def __init__(self, tags):
            self.tags = tags

        async def audio_tags(self, wav):
            return self.tags

    class FakeSong:
        calls = 0

        async def recognize(self, wav):
            FakeSong.calls += 1
            return "Artist — Title"

    async def listen(tags):
        p = Pipeline.__new__(Pipeline)
        p.ml, p.song = FakeML(tags), FakeSong()
        return await p._listen(tmp_path / "a.wav")

    music = asyncio.run(listen([{"label": "Music", "score": 0.9}]))
    assert music == {"sounds": ["music / музыка"], "song": "Artist — Title"}
    speech = asyncio.run(listen([{"label": "Laughter", "score": 0.9}]))
    assert speech == {"sounds": ["laughter / смех"], "song": ""} and FakeSong.calls == 1
    down = asyncio.run(listen(None))
    assert down == {"song": "Artist — Title"} and FakeSong.calls == 2


def test_audio_tags_failure_is_not_fatal(tmp_path: Path):
    async def run():
        ml = MLClient(Settings(database_url="postgresql://x", internal_token="x"))
        ml.http = httpx.AsyncClient(base_url="http://ml", transport=httpx.MockTransport(lambda r: httpx.Response(502)))
        return await ml.audio_tags(tmp_path / "a.wav")

    assert asyncio.run(run()) is None
