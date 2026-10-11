"""Монтажный вид: одна шкала времени, на ней всё, что зритель видит и слышит.

Смысл проверки — не форма ответа, а три обещания, на которых держится польза
вкладки. Реплика на дорожке — та, что НАРИСОВАНА в ролике (а анимированный
стиль пишет одну реплику десятком строк ASS, и наивный счёт врёт в разы).
Слово речи стоит там, где оно звучит в ГОТОВОМ файле, а не там, где оно
лежало в своей сцене. Волна измерена из файла, а не нарисована по длительности.
"""

from __future__ import annotations

from pathlib import Path

from conftest import add_music, make_test_video
from fastapi.testclient import TestClient

from videoforge.api.app import create_app
from videoforge.core.captions import Word, words_to_srt
from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.storyboard import SceneOverlay, load_storyboard, save_storyboard

# Одна реплика анимированного стиля: осевшая строка на слое 0 и две строки
# подъёма на слое 1 — ровно так `reveal` раскладывает КАЖДОЕ слово. Плюс плашка,
# которую от субтитров отличает только имя стиля: слой у неё тот же, что у
# анимации, и по слою их не разделить
ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname
Style: Default,PT Sans
Style: OverlayLabel,PT Sans

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.10,0:00:00.60,Default,,0,0,0,,{\\pos(1,2)}ПЕРВАЯ РЕПЛИКА
Dialogue: 1,0:00:00.10,0:00:00.20,Default,,0,0,0,,{\\move(1,2,3,4)}ПЕРВАЯ РЕПЛИКА
Dialogue: 1,0:00:00.20,0:00:00.60,Default,,0,0,0,,{\\move(3,4,5,6)}ПЕРВАЯ РЕПЛИКА
Dialogue: 0,0:00:00.60,0:00:01.90,Default,,0,0,0,,{\\k40}ВТОРАЯ {\\k90}РЕПЛИКА
Dialogue: 0,0:00:02.00,0:00:02.05,Default,,0,0,0,,МИГ
Dialogue: 1,0:00:00.00,0:00:03.00,OverlayLabel,,0,0,0,,ПУНКТ 1
"""


def _assembled(edir: Path) -> None:
    """Эпизод, который уже собран: готовый файл, измеренный таймлайн, субтитры."""
    out = edir / "05_assemble"
    out.mkdir(exist_ok=True)
    make_test_video(out / "final.mp4", seconds=6.0, size="1080x1920")
    (out / "timeline.json").write_text(
        '[{"scene_id": "s1", "start": 0.0, "end": 3.0, "lead": 0.0},'
        ' {"scene_id": "s2", "start": 3.0, "end": 6.0, "lead": 0.0}]',
        encoding="utf-8",
    )
    (out / "captions.ass").write_text(ASS, encoding="utf-8")
    audio = edir / "04_audio"
    audio.mkdir(exist_ok=True)
    (audio / "s2.srt").write_text(
        words_to_srt([Word("тише", 0.5, 1.0), Word("едешь", 1.0, 1.6)]), encoding="utf-8"
    )
    sb = load_storyboard(edir)
    sb.scenes[1].assets.captions = "04_audio/s2.srt"
    sb.scenes[0].overlay = SceneOverlay(text="ПУНКТ 1", kind="label")
    # чем сделан кадр и почём — это уже записано конвейером, выдумывать нечего
    sb.scenes[0].provenance = {"image": "comfy:klein4b", "audio": "edge"}
    sb.scenes[0].cost_usd = {"image": 0.009}
    sb.scenes[1].provenance = {"image": "comfy:klein4b"}
    sb.final.video = "05_assemble/final.mp4"
    sb.final.captions = "05_assemble/captions.ass"
    sb.final.duration_sec = 6.0
    save_storyboard(edir, sb)
    # готовый файл — самый молодой: сборка пишет его последним, и фикстура не
    # вправе врать про этот порядок, иначе «финал устарел» горит с рождения
    (out / "final.mp4").touch()


def _tracks(client: TestClient) -> dict[str, dict]:
    body = client.get("/api/projects/t/episodes/e1/montage").json()
    return {t["id"]: t for t in body["tracks"]}


def test_animated_caption_is_one_line_not_ten(episode: Path) -> None:
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    captions = _tracks(client)["captions"]["items"]
    # три строки ASS одной реплики — одна реплика на дорожке, от 0.10 до 0.60
    assert [i["text"] for i in captions] == ["ПЕРВАЯ РЕПЛИКА", "ВТОРАЯ РЕПЛИКА", "МИГ"]
    assert (captions[0]["start"], captions[0]["end"]) == (0.1, 0.6)
    # караоке-теги дают слова на секундах финала — их не надо ниоткуда выводить
    assert [w["text"] for w in captions[1]["words"]] == ["ВТОРАЯ", "РЕПЛИКА"]
    assert captions[1]["words"][1]["start"] == 1.0


def test_overlay_is_told_from_captions_by_style_not_layer(episode: Path) -> None:
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    tracks = _tracks(client)
    assert "ПУНКТ 1" not in [i["text"] for i in tracks["captions"]["items"]]
    overlay = tracks["overlays"]["items"][0]
    assert overlay["text"] == "ПУНКТ 1" and overlay["kind"] == "label"


def test_overlay_window_reaches_the_track(episode: Path) -> None:
    """Плашка с окном (хук на 0–1.5 с) на дорожке занимает окно, а не сцену:
    пульт показывает то, что нарисовано, и «плашка на весь ролик» на дорожке
    была бы ложью про две секунды текста."""
    _assembled(episode)
    sb = load_storyboard(episode)
    sb.scenes[0].overlay = SceneOverlay(text="Почему?", start_sec=0, end_sec=1.5)
    sb.scenes[1].overlay = SceneOverlay(text="Коран 2:30", kind="source", start_sec=1)
    save_storyboard(episode, sb)
    (episode / "05_assemble" / "final.mp4").touch()
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    items = _tracks(client)["overlays"]["items"]
    assert [(i["text"], i["start"], i["end"]) for i in items] == [
        ("Почему?", 0.0, 1.5),
        ("Коран 2:30", 4.0, 6.0),  # сцена s2 с 3.0: окно едет вместе с ней
    ]


def test_speech_words_stand_where_they_sound_in_the_final(episode: Path) -> None:
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    speech = _tracks(client)["speech"]["items"]
    # слова лежат в .srt от нуля СЦЕНЫ (0.5 и 1.0), а сцена s2 начинается на 3.0
    assert [i["text"] for i in speech] == ["тише", "едешь"]
    assert speech[0]["start"] == 3.5 and speech[1]["start"] == 4.0


def test_wave_is_measured_from_the_file(episode: Path) -> None:
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    sound = _tracks(client)["sound"]
    # 25 точек на секунду × 6 секунд, и в них есть звук: синус из make_test_video
    # даёт ровную полку около 0.09 — ровно то, что и должно быть у синуса
    assert sound["per_sec"] == 25 and 145 <= len(sound["values"]) <= 155
    assert max(sound["values"]) > 0.05
    # кэш ложится рядом с файлом и второй запрос берёт его
    assert (episode / "05_assemble" / "final.mp4.peaks.json").exists()


def test_short_caption_is_a_measured_note(episode: Path) -> None:
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.get("/api/projects/t/episodes/e1/montage").json()
    assert any("МИГ" in n["text"] for n in body["notes"])  # реплика в 0.05 с


def test_materials_say_what_it_is_made_of_and_where_it_came_from(
    episode: Path, projects_root: Path
) -> None:
    """«Из чего собран ролик» не выдумывается и не заводит своего файла: чем
    сделан кадр — в provenance сцены, права на трек — в его ресурсе. Права тут
    главное: лицензия, о которой знает только YAML, — это нарушение, о котором
    человек узнаёт от площадки."""
    add_music(episode, projects_root, license_text="CC0")
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.get("/api/projects/t/episodes/e1/montage").json()
    materials = {m["title"]: m for m in body["materials"]}
    assert materials["Кадры"]["value"] == "comfy:klein4b × 2"
    assert materials["Кадры"]["origin"] == "$0.01"
    assert materials["Музыка"]["license"] == "CC0"
    assert materials["Музыка"]["origin"] == "ffmpeg lavfi"
    assert materials["Музыка"]["resource"] == "music/tone"


def test_passport_speaks_for_videos_the_pipeline_did_not_make(episode: Path) -> None:
    """Ролик, собранный скриптом, конвейер описать не может — за него говорит
    паспорт. И наоборот: у ролика без сцен раскадровка всё равно несёт ресурсы
    по умолчанию, и вывести из них строку «голос такой-то» — соврать."""
    from videoforge.core.materials import Material, Materials, save_materials

    _assembled(episode)
    sb = load_storyboard(episode)
    sb.scenes = []  # так выглядит ролик, собранный мимо конвейера
    save_storyboard(episode, sb)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    assert client.get("/api/projects/t/episodes/e1/montage").json()["materials"] == []

    save_materials(
        episode,
        Materials(
            made_by="build.py рядом",
            rebuild="uv run python build.py",
            items=[
                Material(
                    role="clip",
                    title="Дождливая улица",
                    path="src/rain.mp4",
                    link="https://pexels.com/1",
                ),
                Material(role="script", title="Сборка", path="build.py"),
            ],
        ),
    )
    body = client.get("/api/projects/t/episodes/e1/montage").json()
    rows = {m["value"]: m for m in body["materials"]}
    assert body["passport"]["made_by"] == "build.py рядом"
    assert rows["Дождливая улица"]["link"] == "https://pexels.com/1"
    # файла нет на диске — и это видно, а не выглядит как «всё на месте»
    assert rows["Дождливая улица"]["state"] == "gone"
    assert rows["Дождливая улица"]["from"] == "паспорт"


def test_stale_names_inputs_newer_than_the_final(episode: Path) -> None:
    """Между прогонами человек правит клип или план, а смотрит на вчерашний
    файл. Отпечаток входов считается только в момент сборки, поэтому здесь
    сравнение по времени правки: грубее, зато доступно всегда."""
    _assembled(episode)
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    assert client.get("/api/projects/t/episodes/e1/montage").json()["stale"] == []

    srt = episode / "04_audio" / "s2.srt"
    srt.write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")  # правка = новое время
    assert client.get("/api/projects/t/episodes/e1/montage").json()["stale"] == ["04_audio/s2.srt"]


def test_same_source_text_tracks_are_called_out_as_one_source() -> None:
    """«На экране» и «Слова песни» лежат рядом, чтобы поймать «написано ≠ спето».
    Но когда текст на экране СДЕЛАН из того же файла слов, они совпадают всегда —
    в том числе когда обе дорожки врут. На t04 это выдали за проверку: слова
    сошлись секунда в секунду, а вокал в тот момент пел другое. Совпадение
    убедительнее расхождения, поэтому молчать о его причине нельзя.

    Считается по МЕНЬШЕЙ дорожке: на экран попадает часть слов (интро эдита
    нарисовано маской, а не субтитрами), и симметричное сходство это пропускало."""
    from videoforge.api.montage import _circular_note

    def track(tid: str, words: list[str]) -> dict[str, object]:
        return {"id": tid, "items": [{"text": w} for w in words]}

    song = "go deeper than I have ever known feel like on rodeo".split()
    same = {"captions": track("captions", song[:6]), "lyrics": track("lyrics", song)}
    assert _circular_note(same) is not None

    other = {
        "captions": track("captions", "совсем другие слова на экране прямо тут".split()),
        "lyrics": track("lyrics", song),
    }
    assert _circular_note(other) is None
    assert _circular_note({"lyrics": track("lyrics", song)}) is None  # одной дорожки мало


def test_no_final_file_no_montage(episode: Path) -> None:
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    assert client.get("/api/projects/t/episodes/e1/montage").status_code == 404
    assert client.get("/api/projects/t/episodes/zzz/montage").status_code == 404


def test_shots_are_taken_from_the_file_when_there_is_no_storyboard(episode: Path) -> None:
    """У ролика, собранного мимо конвейера, раскадровки нет — и «из чего он
    состоит» неоткуда взять, кроме как посмотреть сам файл: границы планов даёт
    детектор смен, кадр на план снимается ОДНИМ проходом ffmpeg (по кадру за
    вызов было бы 20.9 с против 4.5 на живом ролике)."""
    from videoforge.core.ffmpeg import concat

    _assembled(episode)
    out = episode / "05_assemble"
    # ролик с НАСТОЯЩЕЙ склейкой: одна заставка сменяется другой на 3-й секунде.
    # На непрерывной картинке детектор смен честно не находит ничего, и дорожки
    # планов не будет — это не ошибка, а отсутствие планов
    first = make_test_video(out / "a.mp4", seconds=3.0, size="480x270")
    second = out / "b.mp4"
    ffmpeg_run(
        ["-f", "lavfi", "-i", "smptebars=size=480x270:rate=24:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(second)]
    )  # fmt: skip
    concat([first, second], out / "final.mp4")
    sb = load_storyboard(episode)
    sb.scenes = []
    sb.final.duration_sec = 6.0
    save_storyboard(episode, sb)

    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    tracks = _tracks(client)
    assert "scenes" not in tracks
    shots = tracks["shots"]["items"]
    assert len(shots) >= 2  # склейку на 3-й секунде детектор обязан увидеть
    assert shots[0]["start"] == 0.0 and shots[-1]["end"] > 5.0
    assert all(a["end"] <= b["start"] + 0.001 for a, b in zip(shots, shots[1:], strict=False))
    assert any(i["thumb_url"] for i in shots)
    assert (episode / "05_assemble" / "shots").is_dir()


def test_copy_of_another_episode_shows_the_original_tracks(episode: Path) -> None:
    """Копия готового файла чужого эпизода (так живёт галерея тестов) сама по
    себе несёт одну волну звука, а эффекты и звуки — в плане оригинала. Паспорт
    знает, чей это файл, и дорожки берутся оттуда — пока файлы совпадают.
    Пересобранный оригинал описывает другой ролик: его дорожки под этим файлом
    были бы совпадением, выданным за доказательство."""
    import shutil

    from videoforge.core.editplan import EditPlan, EffectCue, SfxCue, save_plan
    from videoforge.core.materials import Material, Materials, save_materials
    from videoforge.core.project import create_episode

    _assembled(episode)
    save_plan(
        episode,
        EditPlan(
            author="manual",
            effects=[EffectCue(at_sec=2.5, kind="zoom_punch", why="удар на склейке")],
            sfx=[SfxCue(at_sec=2.5, id="impact", why="тот же удар")],
        ),
    )
    copy = create_episode("t", "e2", title="копия")
    sb = load_storyboard(copy)
    sb.scenes = []
    sb.final.video = "05_assemble/final.mp4"
    sb.final.duration_sec = 6.0
    save_storyboard(copy, sb)
    (copy / "05_assemble").mkdir(exist_ok=True)
    shutil.copyfile(episode / "05_assemble" / "final.mp4", copy / "05_assemble" / "final.mp4")
    save_materials(
        copy,
        Materials(
            made_by="копия готового файла другого эпизода",
            items=[Material(role="copy_of", title="t/e1", path="../e1/05_assemble/final.mp4")],
        ),
    )
    client = TestClient(create_app(ui_dist=Path("nonexistent")))
    body = client.get("/api/projects/t/episodes/e2/montage").json()
    tracks = {t["id"]: t for t in body["tracks"]}
    assert body["copy_of"] == "t/e1"
    assert body["ref"] == "t/e2"
    assert body["video_url"].startswith("/files/projects/t/episodes/e2/")
    assert [i["label"] for i in tracks["effects"]["items"]] == ["zoom_punch"]
    assert [i["label"] for i in tracks["sfx"]["items"]] == ["impact"]
    assert "scenes" in tracks and "captions" in tracks
    assert body["notes"][0]["severity"] == "info"
    assert "t/e1" in body["notes"][0]["text"]
    # своя строка паспорта — первой, дальше то, из чего собран оригинал
    assert body["materials"][0]["kind"] == "copy_of"
    assert any(m["title"] == "Кадры" for m in body["materials"])

    # оригинал пересобран — файл другой длины: чужие дорожки не показываются
    make_test_video(episode / "05_assemble" / "final.mp4", seconds=4.0, size="1080x1920")
    body = client.get("/api/projects/t/episodes/e2/montage").json()
    assert body["copy_of"] is None
    assert "effects" not in {t["id"] for t in body["tracks"]}
    assert body["notes"][0]["severity"] == "warn"
    assert "пересобран" in body["notes"][0]["text"]
