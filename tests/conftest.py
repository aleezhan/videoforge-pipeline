from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from videoforge.config import ProviderNames, load_env
from videoforge.core.ffmpeg import ffmpeg_exe
from videoforge.core.ffmpeg import run as ffmpeg_run
from videoforge.core.project import ProjectSpec, create_episode, create_project
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import PublishInfo, Scene, load_storyboard, save_storyboard
from videoforge.paths import ROOT
from videoforge.providers.base import ProviderSet
from videoforge.providers.registry import build_providers

load_env(ROOT)

# Пометка `render`: тест рендерит видео ffmpeg-ом и идёт секунды. Замер
# 12.09.2026: 81 такой тест из 438 съедает 94% набора (1019 с из 1090), остальные
# 357 идут за полторы минуты — поэтому `check.ps1 quick` откладывает помеченные
# до вечернего полного `check`. Пометка — утверждение о времени, и оно сверяется
# с фактом: тест дольше RENDER_SEC без пометки и помеченный короче FAST_SEC
# попадают в итоговую строку прогона. Строка, а не провал: время шумит от чужой
# нагрузки на машине, и красный от соседнего ffmpeg никого бы ничему не научил.
# Помечено всё дольше 4 с, страж срабатывает с 5 с: секунда зазора, чтобы тест
# на границе не мигал от шума. Считается сумма setup + call + teardown: у
# нарезок исходник рендерится в фикстуре, и по одному call такой тест выглядел
# бы быстрым. Исключение — общие на прогон фикстуры `final` и `packed`:
# session-фикстура выполняется внутри setup того теста, который попросил её
# первым, и её секунды легли бы на него, хотя это цена прогона, а не теста (и
# какой тест окажется первым, решает отбор — `-k`, `quick`). Такие фикстуры
# сообщают свою цену через `_shared`, и с ближайшего setup она списывается
RENDER_SEC = 5.0
FAST_SEC = 1.0

_spent: dict[str, float] = {}
_render: set[str] = set()
_shared: list[float] = []  # цена общих фикстур прогона, ещё не списанная с теста


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    spent = report.duration
    if report.when == "setup" and _shared:
        spent -= sum(_shared)  # общие фикстуры строились в этом setup — не тесту в счёт
        _shared.clear()
    _spent[report.nodeid] = _spent.get(report.nodeid, 0.0) + spent
    if "render" in report.keywords:
        _render.add(report.nodeid)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    unmarked = [(s, n) for n, s in _spent.items() if n not in _render and s > RENDER_SEC]
    stale = [(s, n) for n, s in _spent.items() if n in _render and s < FAST_SEC]
    if not unmarked and not stale:
        return
    terminalreporter.section("пометка render", sep="-")
    for sec, nodeid in sorted(unmarked, reverse=True):
        terminalreporter.line(f"{sec:6.1f}s без пометки render: {nodeid}")
    for sec, nodeid in sorted(stale):
        terminalreporter.line(f"{sec:6.1f}s помечен render, но быстрый: {nodeid}")


def add_music(episode: Path, projects_root: Path, *, license_text: str = "test") -> None:
    """Трек в ресурсах проекта t: синус через ffmpeg, короче эпизода — проверяет
    зацикливание. Включает музыку в project.yaml и ссылается на трек из раскадровки."""
    mdir = projects_root / "t" / "resources" / "music"
    mdir.mkdir(parents=True, exist_ok=True)
    ffmpeg_run(["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100", "-t", "4",
                "-ac", "2", "-c:a", "pcm_s16le", str(mdir / "tone.wav")])  # fmt: skip
    (mdir / "tone.yaml").write_text(
        f'id: tone\nfile: tone.wav\nlicense: "{license_text}"\nsource: "ffmpeg lavfi"\n'
        "gain_db: -18\nduck_db: 12\n",
        encoding="utf-8",
    )
    cfg = projects_root / "t" / "project.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("music: false", "music: true"), encoding="utf-8"
    )
    sb = load_storyboard(episode)
    sb.resources.music = "tone"
    save_storyboard(episode, sb)


SOURCE_SEC = 60.0  # длина «подкаста» для тестов нарезок


def make_test_video(
    path: Path, *, seconds: float = SOURCE_SEC, size: str = "640x360", rate: int = 24
) -> Path:
    """Видео со звуком: цветные полосы и синус. Нужно нарезкам — им, в отличие
    от ai_video, звук приходит из самого исходника, и тишина ничего не проверит.
    `rate` — частота кадров «подкаста»: нарезка обязана её сохранить."""
    ffmpeg_run(
        [
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds:g}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds:g}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ]
    )  # fmt: skip
    return path


def count_frames(path: Path) -> int:
    """Сколько кадров в видеодорожке файла на самом деле.

    Тем же способом, что `probe_stream_duration` читает длительность: дорожка
    в null-муксер, число из `-progress`; `passthrough` — чтобы ffmpeg не
    подкладывал и не выбрасывал кадры по дороге. Считаются кадры, которые в
    файле есть, а не длительность × fps: у VFR-файла эти числа расходятся, и
    в этом расхождении весь смысл замера."""
    proc = subprocess.run(
        [
            ffmpeg_exe(), "-hide_banner", "-nostats", "-loglevel", "error",
            "-i", str(path), "-map", "0:v:0", "-fps_mode", "passthrough",
            "-f", "null", "-progress", "-", "-",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    marks = re.findall(r"frame=(\d+)", proc.stdout)
    return int(marks[-1]) if marks else 0


FINAL_SEC = 30.0  # длина общего готового ролика; `final.duration_sec` эпизодов берётся отсюда
PACKAGING = PublishInfo(title="Проба", description="Описание.", hashtags=["тег"])


@pytest.fixture(scope="session")
def final(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Готовый вертикальный ролик 30 с 1080×1920 — один на весь прогон.

    Тестам публикации, очереди и упаковки нужен у эпизода final.mp4, и файл у
    всех один и тот же. Замер 12.09.2026: 28 тестов test_publish_upload.py шли
    по ~6 с, и ffmpeg в каждом кодировал этот файл заново (3–8 с в зависимости
    от загрузки машины). Теперь он кодируется один раз, а в эпизод теста
    КОПИРУЕТСЯ (`shutil.copy2`, 4 мс). Копировать, а не отдавать путь: тест,
    который подменит общий файл, сломает соседей. Цена рендера ложится в setup
    первого просившего теста и списывается с него стражем пометки render (см.
    `_shared` выше)."""
    started = time.perf_counter()
    path = make_test_video(
        tmp_path_factory.mktemp("final") / "final.mp4", seconds=FINAL_SEC, size="1080x1920"
    )
    _shared.append(time.perf_counter() - started)
    return path


def pack(edir: Path, providers: ProviderSet, final: Path) -> Path:
    """Эпизод с готовым файлом и упаковкой: копия ролика `final`, упаковка в
    раскадровке, стадия publish на моке — обложка, 06_publish/, запись в манифесте.

    Так один раз собирается шаблон `packed`; напрямую это зовут только для
    эпизода, которому шаблон t/e1 не подходит (нарезка: другой проект, workflow
    и манифест) — и такой тест платит за стадию сам."""
    out = edir / "05_assemble"
    out.mkdir(exist_ok=True)
    shutil.copy2(final, out / "final.mp4")
    sb = load_storyboard(edir)
    sb.final.video = "05_assemble/final.mp4"
    sb.final.duration_sec = FINAL_SEC
    sb.publish = PACKAGING.model_copy()
    save_storyboard(edir, sb)
    run_episode(edir, providers, RunOptions(only="publish", raise_errors=True))
    return edir


@pytest.fixture(scope="session")
def packed(final: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Эпизод t/e1 с готовым роликом, прошедший стадию publish, — шаблон на прогон.

    Общий файл снял рендер, но сама стадия publish на 1080×1920 стоит ещё 2 с на
    свободной машине и до 7 с под чужой нагрузкой: обложка выбирается из 8
    кадров, и на каждом YuNet ищет лицо в полный размер (замер 14.09.2026:
    detect 0.15–0.47 с на кадр; сик по файлу и сборка детектора — сотые, GOP
    роли не играет). Тестам публикации и очереди нужен
    СЛЕД стадии, а не её ход, поэтому она гонится один раз здесь, а `unpack`
    копирует след в эпизод теста. Шаблон живёт в своей папке проектов: папку
    теста session-фикстура не видит и трогать не должна. Шаблон обязан
    совпадать с фикстурой `episode` — оба строит `make_episode`."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("VF_PROJECTS_DIR", str(tmp_path_factory.mktemp("packed")))
        started = time.perf_counter()
        edir = pack(make_episode(), build_providers(ProviderNames.mock()), final)
        _shared.append(time.perf_counter() - started)
    return edir


PACKED = ("05_assemble", "06_publish", "manifest.json", "storyboard.json")


def unpack(packed: Path, edir: Path) -> Path:
    """След стадии publish из шаблона `packed` — в эпизод теста: ролик, обложка,
    упаковка, манифест и раскадровка, те же файлы, что оставил бы честный
    прогон, только за миллисекунды. Копия, а не ссылка: тест правит свой
    эпизод и соседей не видит."""
    for name in PACKED:
        src = packed / name
        if src.is_dir():
            shutil.copytree(src, edir / name, dirs_exist_ok=True)
        else:
            shutil.copy2(src, edir / name)
    return edir


@pytest.fixture
def mock_providers() -> ProviderSet:
    return build_providers(ProviderNames.mock())


@pytest.fixture
def projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VF_PROJECTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def accounts_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный accounts/: аккаунты репозитория в тесты не тянутся."""
    root = tmp_path / "accounts"
    monkeypatch.setenv("VF_ACCOUNTS_DIR", str(root))
    return root


@pytest.fixture
def features_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный features/: планы репозитория в тесты не тянутся."""
    root = tmp_path / "features"
    monkeypatch.setenv("VF_FEATURES_DIR", str(root))
    return root


@pytest.fixture
def links_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный links/: закладки репозитория в тесты не тянутся."""
    root = tmp_path / "links"
    monkeypatch.setenv("VF_LINKS_DIR", str(root))
    return root


@pytest.fixture
def research_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный research/: анализы и playbook не тянутся из репозитория."""
    root = tmp_path / "research"
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("VF_RESEARCH_DIR", str(root))
    return root


@pytest.fixture
def analysis(research_root: Path) -> Path:
    """Анализ канала на mock-коллекторе в изолированной папке."""
    from videoforge.intel.analysis import AnalysisSpec, create_analysis

    return create_analysis(
        AnalysisSpec(
            id="chan",
            urls=["https://www.youtube.com/@demo"],
            collector="mock",
            budget_usd=3.0,
        )
    )


@pytest.fixture
def clip_episode(projects_root: Path, tmp_path: Path) -> Path:
    """Проект нарезок с локальным источником и эпизодом на отрезок 20–45 с.

    Источник локальный (`SourceInfo.file`) — сеть в тестах не трогаем: докачка
    отрезка с YouTube проверяется живьём, руками."""
    from videoforge.clips.highlights import Candidate, episode_from_candidate
    from videoforge.clips.source import WORDS_FILE, SourceInfo, save_source
    from videoforge.core.captions import Word, words_to_srt
    from videoforge.paths import source_dir

    create_project(ProjectSpec(id="c", name="Нарезки", workflow="clips_from_long"))
    video = make_test_video(tmp_path / "podcast.mp4")
    sdir = source_dir("c", "vid1")
    save_source(
        sdir, SourceInfo(id="vid1", file=str(video), title="Подкаст", duration_sec=SOURCE_SEC)
    )
    # слово в секунду на всю запись: у любого отрезка гарантированно есть свои слова
    words = [Word(f"слово{i}", float(i), float(i) + 0.6) for i in range(int(SOURCE_SEC))]
    (sdir / WORDS_FILE).write_text(words_to_srt(words), encoding="utf-8")
    return episode_from_candidate(
        "c",
        "vid1",
        Candidate(id="h01", start_sec=20.0, end_sec=45.0, title="Момент", hook="крючок"),
    )


@pytest.fixture
def episode(projects_root: Path) -> Path:
    """Проект t + эпизод e1 с двумя сценами в изолированной папке."""
    return make_episode()


def make_episode() -> Path:
    """Проект t + эпизод e1 в текущей папке проектов: фикстура `episode` и
    шаблон `packed` строят одно и то же, иначе след стадии лёг бы в чужой эпизод."""
    create_project(
        ProjectSpec(
            id="t",
            name="Тест",
            platforms=["youtube_shorts"],
            character="timur",
            style="pixar3d",
            voice="ru_male",
            budget_per_episode_usd=5.0,
        )
    )
    edir = create_episode("t", "e1", title="проба")
    sb = load_storyboard(edir)
    sb.scenes = [
        Scene(
            id="s1",
            order=1,
            narration="Первая сцена, короткая реплика.",
            visual_prompt="boy at the forest edge",
            motion_prompt="slow push in",
        ),
        Scene(
            id="s2", order=2, narration="", visual_prompt="map close-up", motion_prompt="tilt up"
        ),
    ]
    save_storyboard(edir, sb)
    return edir
