"""Какие тесты затрагивает правка — по графу импортов, а не по совпадению имён.

Полный набор идёт 18 минут, и 94% этого времени дают 81 тест из 438 (замер
12.09.2026): те, что гоняют конвейер целиком и рендерят ffmpeg-ом в 1080×1920.
Гонять их из-за правки в `api/inbox.py` незачем — до сборки эта правка не
дотягивается; а правка в `core/` дотягивается до всех, поэтому они ещё и
помечены `render` и в `quick` не идут вовсе.

Считается так: у каждого `tests/test_*.py` собирается **транзитивное** множество
модулей `videoforge`, до которых он достаёт импортами; правка модуля запускает
те тесты, в чьём множестве он есть. Импорты — оценка СВЕРХУ (импортировал не
значит вызвал), и это правильная сторона ошибки: лишний тест стоит секунд,
пропущенный — сломанного релиза.

Импорты внутри функций считаются наравне с верхними: их пишут, чтобы разорвать
цикл, но зависимость от этого никуда не девается.

Чего способ НЕ видит — связей мимо импорта: шаблоны, workflow-файлы, шрифты,
`conftest.py`, сам `pyproject.toml`. Правка любого из них означает «гнать всё»,
и это записано явно в `RUN_ALL`, а не забыто.

Печатает два списка, по строке на файл, и читает это `check.ps1 quick`:

    edited tests/test_x.py     тест-файл правили сам — идёт целиком, с рендером:
                               тест, который только что написал, обязан
                               отработать хоть раз, даже если он дорогой
    reached tests/test_y.py    правка дотягивается до него по графу импортов —
                               идёт без рендера: тесты с пометкой `render`
                               ждут вечернего полного `check`
    ALL                        вместо строк `reached`: затронуто общее, весь
                               набор без рендера

Пусто — код не менялся, проверять нечего.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "videoforge"
SRC = ROOT / PKG
TESTS = ROOT / "tests"

# Правки, которые меняют поведение мимо графа импортов. Шаблоны читает человек и
# `create_resource`, workflow-файлы — раннер, шрифты — libass, conftest — все
# тесты сразу. Связь есть, импортом её не видно, поэтому здесь она названа руками.
RUN_ALL = (
    "videoforge/templates/",
    "videoforge/assets/",
    "workflows/",
    "tests/conftest.py",
    "pyproject.toml",
    "uv.lock",
    "check.ps1",
    "tools/affected_tests.py",
)


def module_name(path: Path) -> str:
    """Файл → имя модуля: videoforge/core/captions.py → videoforge.core.captions."""
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path) -> set[str]:
    """Имена модулей `videoforge`, на которые ссылается файл.

    Для `from videoforge.core import captions` кладём и `videoforge.core`, и
    `videoforge.core.captions`: по одному только `node.module` пакет-родитель
    выглядел бы как вся зависимость, и правка `captions.py` терялась бы.
    Лишние имена (`…captions.Word`) отсеются пересечением с реальными модулями.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()  # не наш файл — пусть решает RUN_ALL, а не молчаливый пропуск
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(PKG))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(PKG):
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def source_graph() -> dict[str, set[str]]:
    """Модуль → модули `videoforge`, которые он импортирует напрямую."""
    modules = {module_name(p): p for p in sorted(SRC.rglob("*.py"))}
    return {name: imports_of(path) & modules.keys() for name, path in modules.items()}


def reachable(start: set[str], graph: dict[str, set[str]]) -> set[str]:
    """Транзитивное замыкание: до чего файл достаёт через цепочку импортов."""
    seen: set[str] = set()
    stack = list(start)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(graph.get(name, ()))
    return seen


def changed_files(base: str | None) -> list[str]:
    """Что изменилось: против ветки/коммита, либо несохранённое в рабочем дереве."""
    if base:
        cmd = ["git", "diff", "--name-only", base]
    else:
        cmd = ["git", "status", "--porcelain", "--untracked-files=all"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    rows = [row.rstrip() for row in out.splitlines() if row.strip()]
    if not base:
        rows = [row[3:] for row in rows]  # снять колонку статуса ` M `
    return [row.strip().strip('"').replace("\\", "/") for row in rows]


def _rel(paths: set[Path]) -> list[str]:
    return sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in paths)


def select(files: list[str]) -> tuple[list[str], list[str] | None]:
    """Тест-файлы под правку: (правленные сами, достигнутые по графу).

    `None` во втором — «гнать всё». Правленный файл в достигнутые не попадает:
    он и так идёт целиком, второй раз его гонять незачем.
    """
    edited: set[Path] = set()
    for name in files:
        path = ROOT / name
        if name.startswith("tests/") and path.name.startswith("test_") and path.exists():
            edited.add(path)  # удалённый тест запускать не в чем

    if any(f.startswith(RUN_ALL) for f in files):
        return _rel(edited), None

    graph = source_graph()
    closure = {test: reachable(imports_of(test), graph) for test in sorted(TESTS.glob("test_*.py"))}

    reached: set[Path] = set()
    for name in files:
        path = ROOT / name
        if name.startswith(f"{PKG}/") and name.endswith(".py"):
            if not path.exists():  # модуль удалён — кто на него ссылался, неизвестно
                return _rel(edited), None
            module = module_name(path)
            reached.update(test for test, seen in closure.items() if module in seen)
    return _rel(edited), _rel(reached - edited)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else None
    edited, reached = select(changed_files(base))
    for path in edited:
        print(f"edited {path}")
    if reached is None:
        print("ALL")
    else:
        for path in reached:
            print(f"reached {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
