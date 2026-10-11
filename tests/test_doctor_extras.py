"""doctor знает про все extras пакета из его метаданных, а не из списка в коде."""

from __future__ import annotations

import tomllib
from pathlib import Path

from videoforge.cli import extras_status

ROOT = Path(__file__).resolve().parents[1]


def test_extras_come_from_package_metadata_not_a_hand_list() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    rows = {extra: (dists, missing) for extra, dists, missing in extras_status()}
    # каждый extra из pyproject виден doctor-у вместе со своими пакетами;
    # новый extra в pyproject не требует правки doctor
    assert set(rows) == set(declared)
    for extra, requirements in declared.items():
        dists, missing = rows[extra]
        assert dists, extra
        assert len(dists) == len(requirements)
        # отсутствующее — всегда подмножество входящего в extra
        assert set(missing) <= set(dists)
