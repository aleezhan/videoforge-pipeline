"""Атомарная запись на Windows: подмена файла, который держит открытым другой
процесс (пульт читает раскадровку, плеер держит final.mp4), — повтор, а не
падение стадии. ep003 (18.09.2026): сборка упала на PermissionError."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoforge.core import io


def test_a_busy_target_is_retried_until_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = io.os.replace
    busy = {"left": 3}

    def flaky(src: Path, dst: Path) -> None:
        if busy["left"]:
            busy["left"] -= 1
            raise PermissionError("[WinError 5] Отказано в доступе")
        real(src, dst)

    monkeypatch.setattr(io.os, "replace", flaky)
    monkeypatch.setattr(io, "_sleep", lambda _: None)
    tmp, path = tmp_path / "a.tmp", tmp_path / "a.json"
    tmp.write_text("{}", encoding="utf-8")
    io.replace_file(tmp, path)
    assert path.read_text(encoding="utf-8") == "{}" and not tmp.exists()


def test_a_file_held_forever_still_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(src: Path, dst: Path) -> None:
        raise PermissionError("[WinError 5] Отказано в доступе")

    monkeypatch.setattr(io.os, "replace", never)
    monkeypatch.setattr(io, "_sleep", lambda _: None)
    (tmp_path / "a.tmp").write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError):
        io.replace_file(tmp_path / "a.tmp", tmp_path / "a.json")
