"""Подержать замок эпизода заданное время — проверка замка руками.

Нужен там, где тест не дотягивается: второй настоящий процесс (пульт против
терминала) и сон ноутбука. Пока скрипт держит замок, пересборка из пульта
обязана отвечать 409, а `videoforge run` над тем же эпизодом — отказом словами.
Ctrl+C отпускает; убитый процесс отпускает тоже — блокировку снимает ядро.

    uv run python tools/hold_lock.py projects/demo/episodes/map 3600
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from videoforge.core.runner import Busy, episode_lock


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("использование: hold_lock.py <папка эпизода> <секунд>", file=sys.stderr)
        return 2
    episode, seconds = Path(argv[1]), float(argv[2])
    try:
        with episode_lock(episode, what="hold_lock: проверка замка руками"):
            print(f"держу замок {episode} {seconds:.0f} с, Ctrl+C отпускает", flush=True)
            try:
                time.sleep(seconds)
            except KeyboardInterrupt:
                pass
    except Busy as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("отпустил", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
