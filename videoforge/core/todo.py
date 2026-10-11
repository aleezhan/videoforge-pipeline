"""Задачи человека: короткий список того, что надо сделать руками.

`todo.md` в корне — ОДИН файл на всё. Не по проектам намеренно: забывается не
то, что лежит в проекте, куда заходишь, а то, что лежит там, куда не заходишь.
Пульт показывает его первым разделом на странице «Планы» — туда смотрят с
телефона, и туда же попадает всё, что всплыло в разговоре как «надо сделать,
когда буду за компом».

**Чем это НЕ является.** Не `features/`: там возможности инструмента, у них
жизнь от идеи до сделанного. Здесь — действия с концом: войти в кабинет,
нажать кнопку на площадке, посмотреть ролик, показать паспорт. Задача, которая
на поверку оказалась возможностью, переезжает в `features/` и остаётся здесь
строкой со ссылкой.

**Код задачи — чтобы её можно было назвать вслух.** `T-07` живёт с задачей до
конца и после закрытия НЕ переиспользуется: «делаем T-07» через неделю значит
ровно ту же задачу, а не новую с тем же номером. Поэтому следующий номер — на
единицу больше самого большого, который когда-либо встречался в файле, включая
сделанные.

Формат — обычный markdown, человек правит его руками:

    - [ ] T-07 · открыть первый ролик · podcast_clips
    - [x] T-06 · завести OAuth Google · podcast_clips · сделано 2026-09-11
    - [ ] T-08 · войти в TikTok · podcast_clips · features/tiktok_upload

**Связь с записью в `features/`** — отдельная часть строки, целиком равная
`features/<id>` (или `feature:<id>`): задача — последний ручной шаг этой
возможности, и `todo done` называет запись, которая его ждала. Упоминание
внутри текста («делаем features/x») связью НЕ считается: так пишут «см. там»,
а не «этот шаг закрывает ту запись». Появилось после 12.09.2026: три записи из
четырёх «в работе» оказались давно сделанными — их последние шаги человек
закрыл через todo, а запись никто не перечитал.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from videoforge.core.features import load_feature
from videoforge.paths import ROOT

TODO_FILE = "todo.md"
CODE_PREFIX = "T"
# `- [ ] T-07 · текст · метка · сделано 2026-09-11` — всё после кода необязательно
LINE = re.compile(
    r"^(?P<indent>\s*)[-*]\s*\[(?P<mark>[ xX])\]\s*"
    r"(?:(?P<code>[A-Z]+-\d+)\s*[·:.-]\s*)?"
    r"(?P<rest>.+?)\s*$"
)
DONE_MARK = "сделано"
# часть строки, которая ЦЕЛИКОМ есть ссылка на запись: `features/<id>` или
# `feature:<id>`; id — как у файла записи (core.features.create_feature)
FEATURE_PART = re.compile(r"^(?:features/|feature:)\s*(?P<id>[a-z0-9][a-z0-9_]*)$")
HEADER = """# Задачи

Что надо сделать руками — одним списком на всё. Правится и командой
(`videoforge todo`), и руками: это обычный markdown.

Код (`T-07`) живёт с задачей до конца и после закрытия не переиспользуется —
по нему задачу называют вслух: «делаем T-07».
"""


@dataclass
class Task:
    code: str
    text: str
    done: bool = False
    tag: str = ""  # проект или область, к которой задача относится
    done_at: str = ""
    feature: str = ""  # id записи features/, чей последний шаг эта задача
    extras: list[str] = field(default_factory=list)  # прочие куски строки, не наши

    @property
    def line(self) -> str:
        mark = "x" if self.done else " "
        parts = [self.text]
        if self.tag:
            parts.append(self.tag)
        if self.feature:
            parts.append(f"features/{self.feature}")
        parts.extend(self.extras)
        if self.done and self.done_at:
            parts.append(f"{DONE_MARK} {self.done_at}")
        return f"- [{mark}] {self.code} · " + " · ".join(parts)


def todo_path(root: Path | None = None) -> Path:
    return (root or ROOT) / TODO_FILE


def _parse_line(line: str) -> Task | None:
    m = LINE.match(line)
    if not m or not m.group("code"):
        return None  # строка без кода — не задача, а текст вокруг списка
    chunks = [c.strip() for c in re.split(r"\s+·\s+", m.group("rest")) if c.strip()]
    text = chunks[0] if chunks else ""
    tag, done_at, feature, extras = "", "", "", []
    for chunk in chunks[1:]:
        if chunk.lower().startswith(DONE_MARK):
            done_at = chunk[len(DONE_MARK) :].strip()
        elif (fm := FEATURE_PART.match(chunk)) and not feature:
            feature = fm.group("id")  # только целая часть строки; в тексте — не связь
        elif not tag:
            tag = chunk
        else:
            extras.append(chunk)
    return Task(
        code=m.group("code"),
        text=text,
        done=m.group("mark").lower() == "x",
        tag=tag,
        done_at=done_at,
        feature=feature,
        extras=extras,
    )


def load_tasks(root: Path | None = None) -> list[Task]:
    path = todo_path(root)
    if not path.exists():
        return []
    return [
        t for t in (_parse_line(ln) for ln in path.read_text(encoding="utf-8").splitlines()) if t
    ]


def next_code(root: Path | None = None) -> str:
    """Следующий номер — больше любого, что встречался, включая закрытые."""
    used = [
        int(m.group(1))
        for t in load_tasks(root)
        if (m := re.fullmatch(rf"{CODE_PREFIX}-(\d+)", t.code))
    ]
    return f"{CODE_PREFIX}-{max(used, default=0) + 1:02d}"


def save_tasks(tasks: list[Task], root: Path | None = None) -> Path:
    """Переписать файл: сначала открытые, потом закрытые.

    Текст вокруг списка (шапка) не сохраняется как есть — он один и тот же и
    пишется заново. Всё, что человек дописал между строками, при машинной
    записи пропадёт, поэтому руками правят сами строки задач."""
    path = todo_path(root)
    open_rows = [t for t in tasks if not t.done]
    done_rows = [t for t in tasks if t.done]
    body = [HEADER, "## Ждут\n"]
    body.append("\n".join(t.line for t in open_rows) if open_rows else "_пусто_")
    if done_rows:
        body.append("\n## Сделано\n")
        body.append("\n".join(t.line for t in done_rows))
    path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
    return path


def add_task(text: str, *, tag: str = "", feature: str = "", root: Path | None = None) -> Task:
    """Записать задачу. `feature` — id записи features/, которую этот шаг закрывает:
    проверяется сразу, потому что опечатка в id обнаружилась бы только на `done`,
    когда подсказка указала бы на несуществующую запись."""
    feature = feature.strip().removeprefix("features/").removeprefix("feature:")
    if feature:
        try:
            load_feature(feature)
        except FileNotFoundError as exc:
            raise ValueError(
                f"записи features/{feature} нет ({exc}); список — videoforge feature list"
            ) from exc
    tasks = load_tasks(root)
    task = Task(code=next_code(root), text=text.strip(), tag=tag.strip(), feature=feature)
    tasks.append(task)
    save_tasks(tasks, root)
    return task


def feature_hint(task: Task) -> str:
    """Что сказать человеку, когда задача со связью закрыта или снова открыта.

    Ничего не меняет в `features/`: был ли это последний шаг записи, знает
    только человек. Задача подсказки — чтобы запись перечитали сейчас, а не
    нашли «в работе» через месяц после того, как всё сделано."""
    if not task.feature:
        return ""
    ref = f"features/{task.feature}"
    try:
        feat = load_feature(task.feature)
    except Exception:  # noqa: BLE001 — запись удалили или файл битый: связь висит в воздухе
        return f"задача ссылается на {ref}, а такой записи нет — поправь строку в todo.md"
    about = f"{ref} ({feat.title}, статус «{feat.status_title}»)"
    if not task.done:
        return f"задача снова открыта — запись {about} опять ждёт этого шага"
    if feat.status == "done":
        return f"запись {about} уже закрыта — проверь, что how_to_use описывает и этот шаг"
    return (
        f"задача закрыла шаг записи {about}: если это был последний шаг — "
        f"`videoforge feature status {task.feature} done` (нужен how_to_use), "
        f"иначе допиши в notes, что осталось"
    )


def normalize_code(code: str) -> str:
    """«7», «t-7», «T-07» — один и тот же код: человек говорит короче, чем пишет."""
    raw = code.strip().upper()
    if raw.isdigit():
        return f"{CODE_PREFIX}-{int(raw):02d}"
    m = re.fullmatch(r"([A-Z]+)-?(\d+)", raw)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else raw


def find_task(code: str, root: Path | None = None) -> Task | None:
    wanted = normalize_code(code)
    return next((t for t in load_tasks(root) if normalize_code(t.code) == wanted), None)


def close_task(code: str, *, root: Path | None = None, done: bool = True) -> Task | None:
    """Закрыть задачу (или снова открыть). Возвращает её же или None, если нет."""
    wanted = normalize_code(code)
    tasks = load_tasks(root)
    found: Task | None = None
    for t in tasks:
        if normalize_code(t.code) == wanted:
            t.done = done
            t.done_at = date.today().isoformat() if done else ""
            found = t
    if found is not None:
        save_tasks(tasks, root)
    return found
