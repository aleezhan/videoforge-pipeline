"""Удары трека: склейки эдита ставятся на них, а не на вычисленную сетку долей.

Проверяется чистая математика — ffmpeg здесь не нужен. Так же устроен
`shots.py` в анализаторе: замер отдельно, счёт отдельно, и счёт тестируется.
"""

from __future__ import annotations

from videoforge.core.beats import PER_SEC, Beat, flux, pick, snap, tempo


def _train(step: int, total: int, height: float = 1.0) -> list[float]:
    """Ровная очередь ударов: всплеск каждые `step` окон."""
    return [height if i and i % step == 0 else 0.02 for i in range(total)]


def test_flux_keeps_the_rise_and_drops_the_decay() -> None:
    """Удар — это скачок ВВЕРХ. Затухание есть у любого звука, и если считать
    его наравне с атакой, каждый удар превращается в два."""
    assert [round(v, 3) for v in flux([0.1, 0.9, 0.2, 0.25])] == [0.0, 0.8, 0.0, 0.05]


def test_beats_are_found_on_a_regular_train() -> None:
    ups = flux(_train(50, 1000))  # 120 BPM при окне 10 мс
    found = pick(ups)
    assert len(found) >= 15
    steps = [round(b.at - a.at, 2) for a, b in zip(found, found[1:], strict=False)]
    assert set(steps) == {0.5}


def test_two_hits_closer_than_the_gap_are_one_beat() -> None:
    """Атака удара длится несколько окон. Без минимального промежутка один
    удар распадается на очередь, и монтаж режет внутри одного звука."""
    ups = flux([0.02, 0.9, 0.85, 0.02, 0.02])
    assert len(pick(ups, per_sec=PER_SEC, min_gap_sec=0.12)) == 1


def test_cuts_are_pulled_onto_real_beats_and_misses_are_named() -> None:
    """Монтаж под музыку держится на том, что склейка стоит НА ударе.

    Промах в полкадра ухо прощает, в три — уже слышно «не в такт». Молчать о
    непопавших нельзя: именно они и портят ритм, а найденные притягиваются
    ровно на удар, чтобы монтажный лист не копил ошибку по чуть-чуть."""
    found = [Beat(1.0, 1.0), Beat(2.0, 0.8), Beat(3.5, 0.9)]

    (близко,) = snap([1.03], found)
    assert близко.hit and близко.at == 1.0  # притянуто на сам удар

    (мимо,) = snap([2.4], found)
    assert not мимо.hit and мимо.at == 2.4 and мимо.beat is None  # оставлено как было
    assert мимо.error == 0.4  # и сказано, на сколько промахнулись

    assert snap([1.0], [])[0].error is None  # ударов нет — не с чем сверять


def test_tempo_finds_the_period_of_a_regular_train() -> None:
    """Темп берётся из автокорреляции: очередь через 50 окон по 10 мс — 120 BPM.

    Согласие (`agreement`) при этом НЕ признак «живое или машинное»: на живых
    и машинных треках проверено, и живой концертник набрал 0.419 против 0.397 и
    0.161 у машинных. Здесь оно только сравнивает ровное с рваным на одном и
    том же сигнале — за пределы этого выводы не тянуть."""
    machine = tempo(flux(_train(50, 2000)))
    assert machine.bpm and abs(machine.bpm[0] - 120.0) < 1.0

    live = _train(50, 2000)
    for i, shift in enumerate(range(50, 2000, 50)):  # каждый удар едет по-своему
        jitter = (i % 7) - 3
        live[shift] = 0.02
        live[min(len(live) - 1, shift + jitter * 4)] = 1.0
    assert machine.agreement > tempo(live).agreement
