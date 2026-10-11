"""Сквозной прогон на заглушках: storyboard.json → final.mp4 без платных вызовов."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core.captions import parse_srt_words
from videoforge.core.ffmpeg import probe_duration
from videoforge.core.manifest import load_manifest
from videoforge.core.pauses import ScriptMarks, pause_after
from videoforge.core.project import approve_stage
from videoforge.core.runner import run_episode
from videoforge.core.stage import RunOptions
from videoforge.core.storyboard import load_storyboard
from videoforge.providers.base import ProviderSet
from videoforge.providers.mock import MockImage
from videoforge.stages.audio import MAX_PAD_SEC


@pytest.mark.render
def test_end_to_end(episode: Path, mock_providers: ProviderSet) -> None:
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    assert report.ok, report.reason

    sb = load_storyboard(episode)
    assert sb.final.video
    final = episode / sb.final.video
    assert final.exists()
    # s1 длится столько, сколько её речь (~2.2 с) плюс пауза до следующей (0.5 ± 0.1)
    # — остаток 6-секундного клипа отрезан; s2 без озвучки — конечная заставка, 5 с
    assert 7.0 <= probe_duration(final) <= 8.5

    # паузы речи меряются по словам на шкале финала: слова есть, пауз > 1 с нет
    assert sb.final.long_pauses == 0 and sb.final.long_pause_gaps == []

    s1, s2 = sb.ordered_scenes()
    assert s1.checks.image == "pass" and s1.checks.video == "pass" and s1.checks.audio == "pass"
    assert s2.checks.audio == "skipped" and s2.assets.audio is None
    assert (episode / s1.assets.clip).exists()

    m = load_manifest(episode)
    assert m.stages["director"].status == "skipped"
    assert m.stages["planner"].status == "skipped"
    assert m.stages["images"].status == "done"
    assert m.stages["assemble"].status == "done"
    # publish не грузит, но готовит: страница с заголовком и проверками площадки
    assert m.stages["publish"].status == "done"
    assert (episode / "06_publish" / "publish.md").exists()
    assert load_storyboard(episode).publish.title
    assert m.spent_usd == 0


@pytest.mark.render
def test_scene_duration_comes_from_the_voiceover(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Ритм задаёт голос: длительность сцены — речь + пауза до следующей реплики
    (срез вшитой тишины и пауза по классу — tests/test_speech_pauses.py)."""
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    s1, s2 = load_storyboard(episode).ordered_scenes()
    end = parse_srt_words((episode / s1.assets.captions).read_text(encoding="utf-8"))[-1].end
    pause = pause_after(s1, s2, ScriptMarks(), tail_sec=0.5)
    assert s1.duration_sec == pytest.approx(end + pause, abs=0.011)
    assert s1.duration_sec < 6.0  # планнерских 6 с в раскадровке больше нет
    assert s2.duration_sec == 5.0  # сцена без озвучки — заставка, не дольше 5 с
    # сборка режет 6-секундный клип по длине сцены, а не тянет его до конца
    assert s1.measured_sec["clip"] == pytest.approx(s1.duration_sec, abs=0.01)


@pytest.mark.render
def test_planned_floor_cannot_stretch_a_spoken_scene_into_silence(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Пол планнера для сцены с речью растягивает паузу не больше чем на
    MAX_PAD_SEC; сцена без речи живёт не весь план, а сколько законно молчать."""
    from videoforge.core.storyboard import save_storyboard

    sb = load_storyboard(episode)
    s1, s2 = sb.ordered_scenes()
    s1.planned_sec = 6.0  # планнер попросил 6 с при реплике на ~2.2 с
    s2.planned_sec = 6.0
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    s1, s2 = load_storyboard(episode).ordered_scenes()
    end = parse_srt_words((episode / s1.assets.captions).read_text(encoding="utf-8"))[-1].end
    pause = pause_after(s1, s2, ScriptMarks(), tail_sec=0.5)
    assert s1.duration_sec == pytest.approx(end + pause + MAX_PAD_SEC, abs=0.011)
    assert s1.duration_sec < 6.0
    assert s2.duration_sec == 5.0


@pytest.mark.render
def test_audio_runs_before_the_paid_stages(episode: Path, mock_providers: ProviderSet) -> None:
    """Озвучка должна отработать до картинок: на ней стоят длительности сцен."""
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    order = [name for name, _ in report.executed]
    assert order.index("audio") < order.index("images") < order.index("video")


def test_captions_are_written_next_to_the_voiceover(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Заглушка TTS кладёт .srt, как edge-tts, — конвейер на mock повторяет реальный."""
    run_episode(
        episode, mock_providers, RunOptions(only="audio", auto_approve=True, raise_errors=True)
    )
    s1, s2 = load_storyboard(episode).ordered_scenes()
    assert s1.assets.captions and (episode / s1.assets.captions).exists()
    assert "-->" in (episode / s1.assets.captions).read_text(encoding="utf-8")
    assert s2.assets.captions is None


@pytest.mark.render
def test_final_is_measured_not_assumed(episode: Path, mock_providers: ProviderSet) -> None:
    """Сборка меряет готовый файл: расхождение дорожек и длину заморозки.

    Расхождение здесь — не ожидаемый дефект, а сторож: склейка перекодирует, и
    пока это так, дорожки сходятся. Замена её на `-c copy` вернёт покадровый
    зазор контейнера на каждый стык, и тест сломается раньше, чем зритель."""
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    final = load_storyboard(episode).final

    assert final.av_drift_sec is not None, "дорожки не прочитались — сторож молчит"
    assert abs(final.av_drift_sec) <= 0.1, f"дорожки разъехались на {final.av_drift_sec}s"


@pytest.mark.render
def test_frozen_frame_is_counted(episode: Path, mock_providers: ProviderSet) -> None:
    """Речь длиннее клипа замораживает последний кадр. Это аварийный выход, и он
    обязан быть посчитан: незамеченным он тихо становится нормой."""
    from videoforge.core.storyboard import save_storyboard

    sb = load_storyboard(episode)
    # заглушка видео умеет 6 и 10 с, заглушка речи считает 14 символов в секунду:
    # 230 символов — это 16 с речи, заведомо больше самого длинного клипа. Раньше
    # тут стояло ×40 (65 с речи и столько же секунд рендера) — запас впятеро против
    # нужного, и он один стоил 50 секунд прогона всего набора
    sb.scenes[0].narration = "Очень длинная реплика. " * 10
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))

    sb = load_storyboard(episode)
    s1 = sb.ordered_scenes()[0]
    assert s1.measured_sec["frozen"] > 0, "заморозка случилась, но не посчитана"
    assert sb.final.frozen_sec >= s1.measured_sec["frozen"]


@pytest.mark.render
def test_nothing_frozen_when_speech_fits(episode: Path, mock_providers: ProviderSet) -> None:
    run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    sb = load_storyboard(episode)
    assert sb.final.frozen_sec == 0
    assert all(s.measured_sec["frozen"] == 0 for s in sb.scenes)


def test_only_the_called_providers_are_required(episode: Path) -> None:
    """Ключи спрашиваем у тех, кого позовут: бесплатная стадия не должна упираться
    в ключ картинок только потому, что тот есть в workflow."""
    from videoforge.core.runner import required_providers

    assert required_providers(episode, RunOptions(only="director")) == ["llm"]
    assert required_providers(episode, RunOptions(only="audio")) == ["tts"]
    assert required_providers(episode, RunOptions(only="assemble")) == []
    # кадры и клипы проверяет контролёр (`check`), а не llm: сценарист на
    # подписке и проверка на Gemini — разные ключи. Картинкам llm нужен тоже —
    # проверка по описанию пачкой и выбор референса (14.09.2026)
    assert required_providers(episode, RunOptions(only="images")) == ["image", "check", "llm"]
    # клипам — ещё и провайдер картинок-сцен (`render: still`, remotion без ключа)
    assert required_providers(episode, RunOptions(only="video")) == ["video", "still", "check"]
    everything = set(required_providers(episode, RunOptions()))
    assert everything == {"llm", "tts", "image", "video", "still", "check"}


@pytest.mark.render
def test_rerun_is_idempotent(episode: Path, mock_providers: ProviderSet) -> None:
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    first = load_storyboard(episode).scenes[0].attempts.copy()
    report = run_episode(episode, mock_providers, opts)
    assert report.ok
    assert load_storyboard(episode).scenes[0].attempts == first  # ничего не перегенерировано


@pytest.mark.render
def test_stage_time_is_measured_by_steps(episode: Path, mock_providers: ProviderSet) -> None:
    """Сколько заняла стадия и на что ушло время внутри неё — из прогона, а не
    из догадок: `videoforge timings` читает эти замеры, не запуская ничего.

    Пропуск времени не занимает и прошлые замеры не затирает — иначе после
    второго прогона в манифесте не осталось бы следов того, где работа
    действительно делалась."""
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    record = load_manifest(episode).stages["assemble"]
    assert record.elapsed_sec is not None and record.elapsed_sec > 0
    names = [s.name for s in record.steps]
    assert "финальный проход" in names and "склейка сцен" in names
    assert sum(s.sec for s in record.steps) <= record.elapsed_sec + 0.5
    assert all(s.sec >= 0 for s in record.steps)

    before = [(s.name, s.sec) for s in record.steps]
    run_episode(episode, mock_providers, opts)  # второй прогон: собирать нечего
    again = load_manifest(episode).stages["assemble"]
    assert [(s.name, s.sec) for s in again.steps] == before


@pytest.mark.render
def test_provider_switch_invalidates_assets(episode: Path, mock_providers: ProviderSet) -> None:
    """Сменился провайдер кадров — перерисован кадр И переснят клип из него.

    До 11.09.2026 клип оставался прежним: провайдер видео не менялся, а входы
    никто не сверял. Выходило вот что: человек переключился с mock на gemini,
    заплатил за новые кадры — и получил ровно прежний ролик, потому что в
    финал идут КЛИПЫ, а они анимированы со старых картинок. Новые кадры не
    попадали в результат вообще.

    Клип теперь держится на кадре (`_inputs` видео включает отпечаток кадра),
    и это осознанно дорого: смена провайдера картинок тянет за собой самую
    дорогую стадию. От неожиданного счёта защищают ворота после картинок и
    сверка бюджета перед каждым платным вызовом."""
    opts = RunOptions(auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    first_clip = sb.scenes[0].assets.video

    mock_providers.image.name = "other"  # как будто переключились с mock на gemini
    report = run_episode(episode, mock_providers, opts)
    assert report.ok
    s1 = load_storyboard(episode).scenes[0]
    assert s1.attempts["image"] == 2 and s1.provenance["image"] == "other"
    assert s1.attempts["video"] == 2, "клип остался снят со старого кадра"
    assert s1.assets.video != first_clip


@pytest.mark.render
def test_gate_stops_and_resumes(episode: Path, mock_providers: ProviderSet) -> None:
    opts = RunOptions(raise_errors=True)
    report = run_episode(episode, mock_providers, opts)
    assert report.awaiting_approval == "images"
    m = load_manifest(episode)
    assert m.awaiting_approval == "images"
    assert "video" not in m.stages  # до дорогой стадии не дошли

    approve_stage(episode, "images")
    report = run_episode(episode, mock_providers, opts)
    assert report.awaiting_approval == "qa"  # итог и замечания QA — до публикации

    approve_stage(episode, "qa")
    report = run_episode(episode, mock_providers, opts)
    assert report.ok, report.reason


def test_budget_guard_blocks_before_spending(episode: Path, mock_providers: ProviderSet) -> None:
    mock_providers.image.price_usd = 10.0  # дороже бюджета эпизода ($5)
    report = run_episode(episode, mock_providers, RunOptions(auto_approve=True, raise_errors=True))
    assert report.stopped_at == "images"
    assert report.reason and "бюджет" in report.reason
    m = load_manifest(episode)
    assert m.stages["images"].status == "blocked"
    assert m.spent_usd == 0
    images_dir = episode / "02_images"
    assert not images_dir.exists() or not list(images_dir.glob("*.png"))


def test_stage_selection(episode: Path, mock_providers: ProviderSet) -> None:
    report = run_episode(
        episode, mock_providers, RunOptions(only="audio", auto_approve=True, raise_errors=True)
    )
    assert report.ok
    assert [name for name, _ in report.executed] == ["audio"]
    assert load_storyboard(episode).scenes[0].assets.audio


def test_budget_stops_before_each_paid_call_not_just_before_the_stage(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Прогноз стадии — ожидание, а не потолок.

    Он считает одну генерацию на сцену, а непройденная проверка кадра
    запускает перегенерацию до `max_attempts` раз. Сверка один раз перед
    стадией пропускала кратный перерасход: бюджет $1 при цене $1 за кадр
    выпускал три вызова и списывал $3. Сверять надо перед каждой тратой."""
    from videoforge.core.manifest import save_manifest
    from videoforge.core.storyboard import save_storyboard
    from videoforge.providers.base import CheckResult, Reference
    from videoforge.providers.mock import MockLlm

    class Picky(MockLlm):
        """Кадр не нравится всегда — значит стадия будет перегенерировать."""

        def check_images(
            self,
            images: list[Path],
            *,
            references: list[Reference],
            checklist: list[str],
            context: str,
        ) -> CheckResult:
            return CheckResult(passed=False, issues=["не похож"], raw="mock")

    sb = load_storyboard(episode)
    sb.scenes = sb.scenes[:1]  # одна сцена: перерасход считается глазами
    sb.scenes[0].cast = ["characters/timur"]  # чеклист непустой → проверка работает
    save_storyboard(episode, sb)
    m = load_manifest(episode)
    m.budget_usd = 1.0
    save_manifest(episode, m)

    mock_providers.image.price_usd = 1.0  # ровно один кадр по карману
    mock_providers.llm = Picky()

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=3)
    )

    assert report.stopped_at == "images"
    assert report.reason and "бюджет" in report.reason
    m = load_manifest(episode)
    assert m.stages["images"].status == "blocked"
    # заплатили за один кадр и остановились, а не за три
    assert m.spent_usd <= 1.0, f"перерасход: ${m.spent_usd}"
    assert len(list((episode / "02_images").glob("*.png"))) == 1


def test_a_paid_check_does_not_slip_through_an_exhausted_budget(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Ноль значит «бесплатно», а не «цена неизвестна».

    Настоящая цена проверки кадров приходит в ответе, и на её месте до
    12.09.2026 стоял 0. При исчерпанном бюджете такой вызов проходил, тогда как
    вызов с известной ценой блокировался: неизвестность была безопаснее знания
    (нашёл внешний аудит). Теперь оценку сверху называет провайдер."""
    from videoforge.core.manifest import save_manifest
    from videoforge.core.storyboard import save_storyboard
    from videoforge.providers.base import CheckResult, Reference
    from videoforge.providers.mock import MockLlm

    class Paid(MockLlm):
        """Платный контролёр: называет цену до вызова и считает вызовы."""

        check_price_usd = 0.05

        def __init__(self) -> None:
            super().__init__()
            self.checks = 0

        def check_images(
            self,
            images: list[Path],
            *,
            references: list[Reference],
            checklist: list[str],
            context: str,
        ) -> CheckResult:
            self.checks += 1
            return CheckResult(passed=True, issues=[], raw="mock")

    sb = load_storyboard(episode)
    sb.scenes = sb.scenes[:1]
    sb.scenes[0].cast = ["characters/timur"]  # чеклист непустой → проверка нужна
    save_storyboard(episode, sb)
    m = load_manifest(episode)
    m.budget_usd = 1.0
    save_manifest(episode, m)
    mock_providers.image.price_usd = 1.0  # кадр съедает бюджет ровно целиком
    llm = Paid()
    mock_providers.llm = llm

    report = run_episode(
        episode, mock_providers, RunOptions(only="images", auto_approve=True, max_attempts=1)
    )

    assert report.reason and "бюджет" in report.reason
    assert llm.checks == 0, "платная проверка ушла при кончившемся бюджете"
    assert load_manifest(episode).spent_usd <= 1.0


def test_a_leftover_lock_from_a_dead_process_does_not_hold_the_episode(tmp_path: Path) -> None:
    """Машину перезагрузили посреди прогона — файл замка остался.

    Живость держится блокировкой уровня ОС: её снимает ядро в момент смерти
    процесса. Поэтому брошенный файл ничего не значит и эпизод не держит."""
    import json

    from videoforge.core.runner import episode_lock, lock_path, running

    lock_path(tmp_path).write_text(json.dumps({"pid": 1, "what": "мёртвый прогон"}), "utf-8")
    assert running(tmp_path) is None  # файл есть, хозяина нет
    with episode_lock(tmp_path, what="новый прогон"):
        assert (running(tmp_path) or {}).get("what") == "новый прогон"


def test_a_long_run_does_not_lose_the_lock_to_its_own_age(tmp_path: Path) -> None:
    """Возраст файла врал в обе стороны.

    Прогон на своей карте живёт дольше любого порога и терял защиту на ровном
    месте; убитый процесс, наоборот, держал эпизод до конца порога. Возраст
    больше ни на что не влияет."""
    import os
    import time

    from videoforge.core.runner import episode_lock, lock_path, running

    with episode_lock(tmp_path, what="долгий прогон"):
        old = time.time() - 30 * 24 * 3600  # «начат месяц назад»
        os.utime(lock_path(tmp_path), (old, old))
        assert (running(tmp_path) or {}).get("what") == "долгий прогон"


def test_the_lock_holds_against_another_real_process(tmp_path: Path) -> None:
    """Пульт и терминал — разные процессы, общей памяти у них нет.

    Тест в одном процессе доказывал только, что второй захват в том же
    процессе не проходит; Codex такую проверку живой не считал (T-20). Здесь
    замок держит настоящий второй интерпретатор: снаружи видно, кто держит,
    второй захват получает отказ, а после `kill` эпизод свободен сразу —
    блокировку снимает ядро, а не наш `finally`, и оставшийся файл ничего не
    значит. Тот же сценарий 12.09.2026 прогнан пультом и терминалом вживую."""
    import os
    import signal
    import subprocess
    import sys

    from videoforge.core.runner import Busy, episode_lock, lock_path, running

    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from videoforge.core.runner import episode_lock\n"
        "with episode_lock(Path(sys.argv[1]), what='чужой процесс'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        seen = running(tmp_path) or {}
        assert seen.get("what") == "чужой процесс"
        # pid в замке — того, кто его держит. Под uv `.venv/Scripts/python.exe`
        # — трамплин: настоящий интерпретатор у него дочерний, и `holder.pid`
        # это не он. Авария — смерть именно держателя, без `finally`.
        holder_pid = int(seen["pid"])
        with pytest.raises(Busy, match="чужой процесс"):
            with episode_lock(tmp_path, what="свой"):
                pass
        os.kill(holder_pid, signal.SIGTERM)  # на Windows это TerminateProcess
        holder.wait(timeout=10)
        assert lock_path(tmp_path).exists()  # файл остался, но доказательство — не он
        assert running(tmp_path) is None
        with episode_lock(tmp_path, what="после аварии"):
            assert (running(tmp_path) or {}).get("what") == "после аварии"
        assert not lock_path(tmp_path).exists()
    finally:
        if holder.poll() is None:
            holder.kill()


def test_run_refuses_a_busy_episode_in_words(
    episode: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`videoforge run` над занятым эпизодом отвечает отказом, а не стеком.

    Живой опыт 12.09.2026: пульт получал 409 с именем держателя, а терминал
    печатал трассировку Python — команда ловила только ValueError."""
    import typer

    from videoforge import cli
    from videoforge.core.runner import episode_lock

    with episode_lock(episode, what="пересборка из пульта"):
        with pytest.raises(typer.Exit) as exc:
            cli.run(
                "t/e1",
                mock=True,
                yes=False,
                ignore_budget=False,
                only="assemble",
                start=None,
                stop=None,
                max_attempts=3,
                force=False,
            )
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "уже идёт работа" in err and "пересборка из пульта" in err
    assert "Traceback" not in err


def test_editing_the_text_by_hand_makes_the_voice_over_stale(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """«Поправил JSON — перезапустил» должно работать.

    Готовность актива держалась на пути, статусе и имени провайдера. Всё три
    остаются прежними, когда человек правит `narration` руками, — и повтор
    считал старую озвучку годной. Звучал в ролике старый текст."""
    from videoforge.core.storyboard import save_storyboard

    opts = RunOptions(only="audio", auto_approve=True, raise_errors=True)
    sb = load_storyboard(episode)
    sb.scenes[1].narration = "У соседней сцены своя реплика, и её никто не трогал."
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    made = (episode / (sb.scenes[0].assets.audio or "")).read_bytes()
    neighbour = (episode / (sb.scenes[1].assets.audio or "")).stat().st_mtime_ns
    assert sb.scenes[0].inputs_fingerprint.get("audio")

    run_episode(episode, mock_providers, opts)  # без правок ничего не трогаем
    sb = load_storyboard(episode)
    assert (episode / (sb.scenes[0].assets.audio or "")).read_bytes() == made

    sb.scenes[0].narration = "Совсем другой текст, которого раньше в сцене не было."
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, opts)

    sb = load_storyboard(episode)
    assert (episode / (sb.scenes[0].assets.audio or "")).read_bytes() != made
    # соседняя сцена не тронута: пересоздаём изменённое, а не всё подряд
    assert (episode / (sb.scenes[1].assets.audio or "")).stat().st_mtime_ns == neighbour


@pytest.mark.render
def test_editing_the_prompt_by_hand_makes_the_frame_and_its_clip_stale(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Правка `visual_prompt` устаревляет кадр, а устаревший кадр — клип из него.

    Цепочка держится тем, что отпечаток кадра входит в отпечаток клипа: иначе
    картинка бы перерисовалась, а анимировали бы по-прежнему старую."""
    from videoforge.core.storyboard import save_storyboard

    opts = RunOptions(start="images", stop="video", auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    first_image, first_clip = sb.scenes[0].assets.image, sb.scenes[0].assets.video
    neighbour = (sb.scenes[1].assets.image, sb.scenes[1].assets.video)
    assert first_image and first_clip

    run_episode(episode, mock_providers, opts)  # без правок — те же файлы
    sb = load_storyboard(episode)
    assert (sb.scenes[0].assets.image, sb.scenes[0].assets.video) == (first_image, first_clip)

    sb.scenes[0].visual_prompt = "a completely different frame: an empty red room"
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, opts)

    sb = load_storyboard(episode)
    assert sb.scenes[0].assets.image != first_image, "кадр не перерисовали"
    assert sb.scenes[0].assets.video != first_clip, "клип анимировали по старому кадру"
    # соседняя сцена не переделана: правка одной реплики не оплачивается всем эпизодом
    assert (sb.scenes[1].assets.image, sb.scenes[1].assets.video) == neighbour


def test_assets_made_before_fingerprints_are_not_thrown_away(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Отпечатка нет — это не «устарел».

    Активы, сделанные до появления отпечатков, честно сделаны из тогдашних
    входов, и переделывать их не за что: первый же запуск после обновления
    иначе переплатил бы за всё готовое. Такой актив помечается на месте, и
    следующая правка уже ловится."""
    from videoforge.core.storyboard import save_storyboard

    opts = RunOptions(only="audio", auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    made = (episode / (sb.scenes[0].assets.audio or "")).read_bytes()

    sb.scenes[0].inputs_fingerprint.pop("audio")  # эпизод из прошлой версии
    save_storyboard(episode, sb)
    run_episode(episode, mock_providers, opts)

    sb = load_storyboard(episode)
    assert (episode / (sb.scenes[0].assets.audio or "")).read_bytes() == made  # не переделали
    assert sb.scenes[0].inputs_fingerprint.get("audio")  # но отпечаток теперь есть
    # и он помечен принятым: «сделан из этих входов» о старом активе не
    # утверждается, потому что этого никто не проверял
    assert sb.scenes[0].adopted("audio")


def test_the_readiness_predicate_does_not_write() -> None:
    """`asset_ready` — чистый предикат: его читают и `estimate`, и HTML-отчёт, и
    страница пульта, а отметку о принятии старого актива ставит стадия.

    Раньше запись жила внутри предиката, то есть модель менялась от чтения.
    Проверяется ровно это: спросили готовность — в раскадровке ничего не
    появилось; принятое помечено приставкой; после принятия правка входов
    ловится как обычно, а повторное принятие ничего не переписывает."""
    from videoforge.core.storyboard import Scene

    scene = Scene(id="s01", order=1, narration="текст", visual_prompt="кадр")
    scene.assets.audio = "04_audio/s01.mp3"
    scene.checks.audio = "pass"
    scene.provenance["audio"] = "edge"

    assert scene.asset_ready("audio", "edge", "отпечаток-1") is True
    assert scene.inputs_fingerprint == {}, "предикат записал отметку сам"

    assert scene.adopt_inputs("audio", "отпечаток-1") is True
    assert scene.adopted("audio") is True  # «принят как есть», а не «сделан из этого»
    assert scene.asset_ready("audio", "edge", "отпечаток-1") is True
    assert scene.asset_ready("audio", "edge", "отпечаток-2") is False  # правку ловим
    assert scene.adopt_inputs("audio", "отпечаток-2") is False  # поверх не пишем
    assert scene.asset_ready("audio", "edge", "отпечаток-1") is True


class WeightedPainter(MockImage):
    """Заглушка художника с настраиваемыми весами — как comfy с LoRA на поде.
    `current = None` изображает опущенную карту: весов назвать нечем."""

    current: str | None = ""

    def weights(self) -> str | None:
        return self.current


@pytest.mark.render
def test_changing_the_painters_weights_makes_the_frame_and_its_clip_stale(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Веса художника (LoRA персонажа на поде) — вход кадра, как и промпт.

    На своей карте лицо держит LoRA, залитая на под, а не промпт: смени её или
    её силу — тот же провайдер с тем же промптом нарисует другое лицо. До
    12.09.2026 отпечаток про веса не знал, и ролик собирался из кадров, сделанных
    разными весами (`features/lora_in_fingerprint`). Клип держится на кадре, так
    что переснимается и он."""
    painter = WeightedPainter()
    painter.current = "amir_v2.safetensors@1"
    mock_providers.image = painter
    opts = RunOptions(start="images", stop="video", auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    first = (sb.scenes[0].assets.image, sb.scenes[0].assets.video)
    assert all(first)

    run_episode(episode, mock_providers, opts)  # те же веса — те же файлы
    sb = load_storyboard(episode)
    assert (sb.scenes[0].assets.image, sb.scenes[0].assets.video) == first

    painter.current = "amir_v3.safetensors@1"
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    assert sb.scenes[0].assets.image != first[0], "кадр от другой LoRA сочли свежим"
    assert sb.scenes[0].assets.video != first[1], "клип анимировали по кадру от старой LoRA"


def test_frames_are_kept_when_the_painter_cannot_name_its_weights(
    episode: Path, mock_providers: ProviderSet
) -> None:
    """Карта не поднята — художник не знает, чем рендерил бы. Это не «устарел»:
    готовые кадры остаются (и клипы из них — отпечаток кадра не тронут), а
    стадия говорит, что входы не сверяла. Иначе `run` после `gpu down`
    (пересборка, публикация) падал бы на стадии, которой делать нечего. Сверка
    случается, когда карта поднимется: та же сцена с другой LoRA
    перерисовывается."""
    painter = WeightedPainter()
    painter.current = "amir_v2.safetensors@1"
    mock_providers.image = painter
    opts = RunOptions(only="images", auto_approve=True, raise_errors=True)
    run_episode(episode, mock_providers, opts)
    sb = load_storyboard(episode)
    first = sb.scenes[0].assets.image
    assert first

    painter.current = None
    report = run_episode(episode, mock_providers, opts)
    assert report.ok
    sb = load_storyboard(episode)
    assert sb.scenes[0].assets.image == first
    images = dict(report.executed)["images"]
    assert "не сверялись" in images.message, images.message
    # отпечаток остался посчитанным при создании, а не переписан «как есть»
    assert sb.scenes[0].inputs_fingerprint["image"] and not sb.scenes[0].adopted("image")

    painter.current = "amir_v3.safetensors@1"  # карта поднялась с другой LoRA
    run_episode(episode, mock_providers, opts)
    assert load_storyboard(episode).scenes[0].assets.image != first
