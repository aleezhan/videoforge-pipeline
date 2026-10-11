"""Дочерний процесс не открывает своего окна консоли.

Пульт живёт задачей планировщика под `pythonw`, у которого консоли нет вовсе, и
Windows выдаёт КАЖДОМУ его дочернему ffmpeg свою собственную: чёрное окно на
долю секунды поверх всего, включая игру. Пачкой это заметно: страница «Тесты»
снимает обложку каждому ролику, и шесть обложек подряд — шесть окон (20.09.2026).

Проверяется не поведение (окна на тестовой машине не видно — процесс и так в
консоли), а сам вызов: у каждого `subprocess.run`/`Popen` в модулях, которые
пульт может запустить, стоит `**NO_WINDOW`. Это страж от возврата: следующий
вызов ffmpeg пишется копипастой соседнего, и без него окно вернётся молча.

Чего здесь нет. `bench/skeleton.py` и `providers/runpod_pod.py` зовутся только
руками из терминала, а у скелета вывод ffmpeg не перехвачен — с этим флагом
текст ошибки ушёл бы в невидимую консоль, то есть в никуда. `agent_tasks/` уже
ставит `CREATE_NO_WINDOW` сам (и на linux — `start_new_session`), там свой
запуск фонового процесса, а не разовый вызов.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from videoforge.core.proc import NO_WINDOW

# модуль → его зовёт пульт: ffmpeg обложек и дорожек, волна и удары монтажного
# листа, рендер картинки-сцены, LLM стадии, запущенной кнопкой
GUARDED = [
    "core/ffmpeg.py",
    "core/waveform.py",
    "core/beats.py",
    "providers/remotion_video.py",
    "providers/claude_cli.py",
]

PACKAGE = Path(__file__).resolve().parent.parent / "videoforge"


def _spawns(tree: ast.AST) -> list[ast.Call]:
    """Вызовы subprocess.run / subprocess.Popen в модуле."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("run", "Popen"):
            if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                out.append(node)
    return out


@pytest.mark.parametrize("relative", GUARDED)
def test_every_spawn_hides_its_console(relative: str) -> None:
    source = (PACKAGE / relative).read_text(encoding="utf-8")
    calls = _spawns(ast.parse(source))
    assert calls, f"{relative}: вызовов subprocess не нашлось — страж смотрит не туда"
    for call in calls:
        starred = [
            kw
            for kw in call.keywords
            if kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == "NO_WINDOW"
        ]
        assert starred, (
            f"{relative}:{call.lineno}: запуск без **NO_WINDOW — "
            "под pythonw он откроет своё окно консоли"
        )


def test_flag_is_real_on_windows() -> None:
    if os.name == "nt":
        assert NO_WINDOW == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert NO_WINDOW == {}
