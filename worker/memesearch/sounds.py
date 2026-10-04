from __future__ import annotations

import wave
from collections.abc import Iterable
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000

RU = {
    "Music": "музыка",
    "Speech": "речь",
    "Singing": "пение",
    "Song": "песня",
    "Choir": "хор",
    "Rapping": "рэп",
    "Laughter": "смех",
    "Giggle": "хихиканье",
    "Chuckle, chortle": "смешок",
    "Belly laugh": "хохот",
    "Baby laughter": "детский смех",
    "Crying, sobbing": "плач",
    "Baby cry, infant cry": "детский плач",
    "Screaming": "крик",
    "Shout": "крик",
    "Yell": "вопль",
    "Whispering": "шёпот",
    "Sigh": "вздох",
    "Groan": "стон",
    "Burping, eructation": "отрыжка",
    "Fart": "пердёж",
    "Cough": "кашель",
    "Sneeze": "чих",
    "Snoring": "храп",
    "Whistling": "свист",
    "Applause": "аплодисменты",
    "Clapping": "хлопки",
    "Cheering": "ликование",
    "Crowd": "толпа",
    "Children shouting": "дети кричат",
    "Dog": "собака",
    "Bark": "лай",
    "Cat": "кошка",
    "Meow": "мяу",
    "Purr": "мурлыканье",
    "Hiss": "шипение",
    "Bird": "птица",
    "Chicken, rooster": "курица, петух",
    "Crowing, cock-a-doodle-doo": "кукареку",
    "Duck": "утка",
    "Goat": "коза",
    "Cattle, bovinae": "корова",
    "Moo": "мычание",
    "Pig": "свинья",
    "Horse": "лошадь",
    "Frog": "лягушка",
    "Insect": "насекомое",
    "Animal": "животное",
    "Guitar": "гитара",
    "Electric guitar": "электрогитара",
    "Bass guitar": "бас-гитара",
    "Piano": "пианино",
    "Keyboard (musical)": "клавишные",
    "Synthesizer": "синтезатор",
    "Drum": "барабан",
    "Drum kit": "ударная установка",
    "Drum machine": "драм-машина",
    "Violin, fiddle": "скрипка",
    "Trumpet": "труба",
    "Saxophone": "саксофон",
    "Flute": "флейта",
    "Accordion": "аккордеон",
    "Harmonica": "губная гармошка",
    "Organ": "орган",
    "Bell": "колокол",
    "Orchestra": "оркестр",
    "Bass drum": "бочка",
    "Techno": "техно",
    "Electronic music": "электронная музыка",
    "Electronic dance music": "EDM",
    "Dubstep": "дабстеп",
    "House music": "хаус",
    "Trance music": "транс",
    "Drum and bass": "драм-н-бэйс",
    "Hip hop music": "хип-хоп",
    "Trap music": "трэп",
    "Rock music": "рок",
    "Heavy metal": "хэви-метал",
    "Punk rock": "панк-рок",
    "Pop music": "поп",
    "Disco": "диско",
    "Jazz": "джаз",
    "Blues": "блюз",
    "Classical music": "классическая музыка",
    "Opera": "опера",
    "Country": "кантри",
    "Reggae": "регги",
    "Funk": "фанк",
    "Soul music": "соул",
    "Folk music": "фолк",
    "Ambient music": "эмбиент",
    "Video game music": "музыка из видеоигры",
    "Theme music": "музыкальная тема",
    "Soundtrack music": "саундтрек",
    "Background music": "фоновая музыка",
    "Lullaby": "колыбельная",
    "Christmas music": "новогодняя музыка",
    "Dance music": "танцевальная музыка",
    "Wedding music": "свадебная музыка",
    "Happy music": "весёлая музыка",
    "Sad music": "грустная музыка",
    "Tender music": "нежная музыка",
    "Exciting music": "энергичная музыка",
    "Angry music": "агрессивная музыка",
    "Scary music": "страшная музыка",
    "Funny music": "смешная музыка",
    "Explosion": "взрыв",
    "Gunshot, gunfire": "выстрел",
    "Machine gun": "пулемёт",
    "Fireworks": "фейерверк",
    "Boom": "бум",
    "Siren": "сирена",
    "Police car (siren)": "полицейская сирена",
    "Ambulance (siren)": "сирена скорой",
    "Alarm": "тревога",
    "Car": "машина",
    "Vehicle horn, car horn, honking": "гудок",
    "Engine": "двигатель",
    "Motorcycle": "мотоцикл",
    "Train": "поезд",
    "Aircraft": "самолёт",
    "Helicopter": "вертолёт",
    "Skidding": "занос",
    "Tire squeal": "визг шин",
    "Glass": "стекло",
    "Shatter": "звон разбитого стекла",
    "Slam": "хлопок двери",
    "Knock": "стук",
    "Door": "дверь",
    "Footsteps": "шаги",
    "Telephone": "телефон",
    "Ringtone": "рингтон",
    "Beep, bleep": "писк",
    "Censorship bleep": "запикивание",
    "Typing": "печать на клавиатуре",
    "Water": "вода",
    "Rain": "дождь",
    "Thunder": "гром",
    "Wind": "ветер",
    "Fire": "огонь",
    "Silence": "тишина",
    "Static": "помехи",
    "Noise": "шум",
    "Television": "телевизор",
    "Radio": "радио",
    "Sound effect": "звуковой эффект",
    "Cartoon": "мультяшный звук",
    "Boing": "боинг",
    "Whoosh, swoosh, swish": "вжух",
    "Zing": "дзынь",
    "Echo": "эхо",
}

MUSIC = {
    "Music",
    "Singing",
    "Song",
    "Rapping",
    "Choir",
    "Musical instrument",
    "Pop music",
    "Rock music",
    "Hip hop music",
    "Electronic music",
    "Techno",
    "Dance music",
    "Video game music",
    "Theme music",
    "Soundtrack music",
    "Background music",
}

SKIP = {
    "Speech",
    "Silence",
    "Inside, small room",
    "Inside, large room or hall",
    "Outside, urban or manmade",
    "Narration, monologue",
}


def read_wav(path: Path, max_seconds: float) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM wav")
        rate, channels = w.getframerate(), w.getnchannels()
        frames = w.readframes(int(rate * max_seconds))
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(audio):
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def windows(audio: np.ndarray, seconds: float = 10.0, limit: int = 12) -> list[np.ndarray]:
    size = int(seconds * SAMPLE_RATE)
    if len(audio) <= size:
        return [audio] if len(audio) >= SAMPLE_RATE // 2 else []
    starts = list(range(0, len(audio) - size // 2, size))
    if len(starts) > limit:
        step = len(starts) / limit
        starts = [starts[int(i * step)] for i in range(limit)]
    return [audio[s : s + size] for s in starts]


def aggregate(per_window: Iterable[Iterable[dict]], min_score: float = 0.15, top: int = 10) -> list[dict]:
    best: dict[str, float] = {}
    for preds in per_window:
        for p in preds:
            label, score = p["label"], float(p["score"])
            if score > best.get(label, 0.0):
                best[label] = score
    out = [{"label": k, "score": round(v, 3)} for k, v in best.items() if v >= min_score and k not in SKIP]
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:top]


def labels(tags: list[dict]) -> list[str]:
    out = []
    for t in tags:
        name = t["label"]
        ru = RU.get(name)
        out.append(f"{name.lower()} / {ru}" if ru else name.lower())
    return out


def has_music(tags: list[dict], threshold: float = 0.3) -> bool:
    return any(t["label"] in MUSIC and t["score"] >= threshold for t in tags)
