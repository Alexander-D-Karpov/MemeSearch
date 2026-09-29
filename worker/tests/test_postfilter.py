import pytest

from memesearch.postfilter import PostFilter, analysis_verdict

F = PostFilter(max_text_chars=700)


@pytest.mark.parametrize(
    ("text", "links", "buttons"),
    [
        ("Лучший VPN со скидкой 50%! Промокод MEME50 #реклама", [], 0),
        ("Реклама. ООО «Ромашка», ИНН 7701234567, erid: 2VtzqvXyz", [], 0),
        ("Как заработать 100к в месяц? Бесплатный курс по ссылке, жми 👇", ["https://t.me/+AbCdEf"], 1),
        ("Подписывайся на лучший канал @crypto_signals_pro и @invest_daily, переходи", ["https://t.me/crypto_signals_pro"], 0),
    ],
)
def test_ads_are_filtered(text, links, buttons):
    assert F.check(text, links, buttons, "memstash32").startswith("advertisement")


@pytest.mark.parametrize(
    ("text", "links"),
    [
        ("", []),
        ("когда курс доллара снова вырос", []),
        ("😂😂😂\n\n@memstash32", ["https://t.me/memstash32"]),
        ("Узнал тут новое слово — кубернетес", []),
        ("бесплатно отдам кота", []),
    ],
)
def test_memes_pass(text, links):
    assert F.check(text, links, 0, "memstash32") == ""


def test_long_text_post_is_not_a_meme():
    assert F.check("новости " * 200, [], 0, "news") == "not a meme (long text post)"
    assert PostFilter(max_text_chars=0).check("новости " * 200, [], 0, "news") == ""


def test_extra_words():
    f = PostFilter(extra_words=["спонсор выпуска", ""])
    assert f.check("Спонсор выпуска — магазин носков", [], 0, "x") == "advertisement: спонсор выпуска"


def test_analysis_verdict():
    assert analysis_verdict({"is_ad": True, "is_meme": True}) == "advertisement (analysis)"
    assert analysis_verdict({"is_ad": False, "is_meme": False}) == "not a meme (analysis)"
    assert analysis_verdict({"is_ad": False, "is_meme": True}) == ""
    assert analysis_verdict({}) == ""
