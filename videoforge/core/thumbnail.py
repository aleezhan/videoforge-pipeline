"""Обложка ролика: кадр, который зритель видит до того, как нажал.

Площадка ставит обложкой случайный кадр, и на шортсе это чаще всего кадр с
открытым ртом посреди слова. Выбирать надо иначе, и правило берётся из того,
зачем зритель нажимает: **в кадре герой, а на экране — ключевое слово реплики.**
Человек видит лицо и слова «Yamal, Mbappé» — и ему становится интересно, что
про них говорят. Кадр без текста этого не обещает, текст без лица не держит.

Поэтому кандидаты берутся не по времени, а по СОДЕРЖАНИЮ: секунды, в которые
на экране стоит слово из заголовка (`Storyboard.publish.title`), — заголовок и
есть обещание ролика. Дальше среди них выбирается тот кадр, где есть лицо
(YuNet, тот же детектор, что кадрирует нарезки) и который резче прочих: смаз на
обложке виден раньше, чем сюжет.

Обложка снимается с ГОТОВОГО файла — с вжжёнными субтитрами и цветокоррекцией,
то есть ровно то, что увидит зритель. Ничего не рисуется поверх: плашки и
стрелки — отдельное решение, и его принимает человек.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from videoforge.core.captions import parse_ass_events
from videoforge.core.ffmpeg import frames_at, probe_duration

THUMB_FILE = "thumbnail.jpg"
# Слова, которые в заголовке есть всегда и обещанием не являются: по ним кадр
# выбирать бессмысленно. Имя героя тоже: оно в каждом заголовке канала.
STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "his",
    "her",
    "you",
    "your",
    "was",
    "were",
    "into",
    "that",
    "this",
    "but",
    "not",
    "are",
    "him",
    "she",
    "they",
    "them",
    "from",
    "when",
    "what",
    "who",
    "why",
    "how",
    "there",
    "here",
    "only",
    "just",
    "all",
    "one",
    "и",
    "в",
    "не",
    "на",
    "что",
    "как",
    "это",
    "он",
    "она",
    "они",
    "его",
    "её",
    "у",
    "за",
    "по",
    "то",
    "так",
    "же",
    "мне",
    "мы",
    "вы",
    "я",
    "а",
    "но",
    "из",
    "для",
}
MIN_WORD = 4  # короче — предлог или частица, а не обещание
FRAME_WIDTH = 1080  # обложка шортса: та же ширина, что у ролика


@dataclass(frozen=True)
class ThumbChoice:
    """Выбранный кадр и почему именно он."""

    path: Path
    at_sec: float
    keyword: str = ""  # слово на экране, из-за которого кадр выбран
    face: bool = False
    sharpness: float = 0.0

    @property
    def why(self) -> str:
        parts = [f"{self.at_sec:.1f} с"]
        if self.keyword:
            parts.append(f"на экране «{self.keyword}»")
        parts.append("лицо в кадре" if self.face else "лица не нашлось")
        return ", ".join(parts)


def fingerprint(image: Path) -> str:
    """Отпечаток файла обложки: по нему видно, что картинка стала другой.

    Сравнить с тем, что лежит на площадке, нельзя в принципе — YouTube отдаёт
    свою перекодированную копию, и байты у неё другие всегда. Поэтому помним,
    что отправляли мы, и второй раз ту же картинку не отправляем."""
    return hashlib.sha1(image.read_bytes()).hexdigest()[:16]


def _norm(word: str) -> str:
    """Слово к сравнимому виду: без регистра, без диакритики, без знаков.

    Mbappé в заголовке и MBAPPÉ в субтитрах — одно слово, и «é» не должно
    решать, найдём мы его или нет."""
    plain = unicodedata.normalize("NFKD", word.casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.sub(r"[^\w]", "", plain, flags=re.UNICODE)


def keywords(title: str) -> list[str]:
    """Слова заголовка, по которым стоит искать кадр.

    Порядок — обещание сильнее к началу списка: сначала имена собственные
    (заглавная буква не в начале фразы), потом всё остальное длинное."""
    raw = re.findall(r"[^\W\d_]+", title, flags=re.UNICODE)
    proper: list[str] = []
    plain: list[str] = []
    for i, word in enumerate(raw):
        norm = _norm(word)
        if len(norm) < MIN_WORD or norm in STOP_WORDS:
            continue
        # имя собственное: с заглавной и не первое слово фразы
        if i > 0 and word[:1].isupper():
            proper.append(norm)
        else:
            plain.append(norm)
    seen: set[str] = set()
    out: list[str] = []
    for word in proper + plain:
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def moments_with(ass_text: str, words: list[str]) -> list[tuple[float, str]]:
    """Секунды, в которые на экране стоит одно из слов, и какое именно.

    Берётся середина реплики: на её краях текст ещё появляется или уже гаснет."""
    events = parse_ass_events(ass_text)
    found: list[tuple[float, str]] = []
    for word in words:  # порядок слов = порядок важности, его и сохраняем
        for ev in events:
            if word in {_norm(w) for w in re.findall(r"[^\W\d_]+", ev.text, flags=re.UNICODE)}:
                found.append(((ev.start + ev.end) / 2, word))
    return found


def _sharpness(path: Path) -> float:
    try:
        import cv2
    except ImportError:  # pragma: no cover — зависимость объявлена в pyproject
        return 0.0
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return 0.0 if img is None else float(cv2.Laplacian(img, cv2.CV_64F).var())


def _faces_in(frames: list[Path]) -> list[bool]:
    """Есть ли лицо на каждом из кадров — на самих JPEG, одной пачкой.

    До 14.09.2026 лицо искалось заново в видео на секунде каждого кандидата:
    ролик открывался восемь раз, а кадр 1080×1920 шёл в детектор целиком —
    2.4 с на выбор обложки шортса против 1.0 с по вынутым кадрам, кадр выбран
    тот же (замер 14.09.2026, podcast_clips/src01_h08 и h19). Кадр, на
    котором ищем, и кадр, который станет обложкой, теперь один и тот же файл."""
    from videoforge.core.faces import FaceError, detect_images

    try:
        return [bool(found) for found in detect_images(frames)]
    except (FaceError, OSError):
        return [False] * len(frames)  # нет детектора — не повод отказываться от обложки


def choose(
    video: Path,
    *,
    title: str,
    ass_text: str = "",
    out_dir: Path | None = None,
    limit: int = 8,
) -> ThumbChoice | None:
    """Выбрать кадр под обложку и положить его рядом с роликом.

    Нет субтитров или ни одно слово заголовка на экран не попало — берём кадры
    по всей длине: обложка нужна всё равно, просто выбирать будем только по
    лицу и резкости."""
    if not video.exists():
        return None
    out_dir = out_dir or video.parent
    duration = probe_duration(video)
    if duration <= 0:
        return None

    hits = moments_with(ass_text, keywords(title)) if ass_text else []
    if hits:
        picks = hits[:limit]
    else:
        # равномерно по ролику, без первых и последних секунд: там fade
        step = duration / (limit + 1)
        picks = [(round(step * (i + 1), 2), "") for i in range(limit)]

    # Две поправки, без которых обложка берётся не из той секунды.
    # Одна реплика содержит несколько слов заголовка («Yamal, Mbappé and
    # Haaland» — три попадания в одну секунду), а выемка кадров пачкой отдаёт
    # МЕНЬШЕ файлов, чем просили, и по контракту возвращает пустой список.
    # И она же отдаёт кадры по возрастанию времени, поэтому кандидаты должны
    # быть отсортированы: иначе подпись «на экране Ямаль» уедет к чужому кадру.
    unique: dict[float, str] = {}
    for sec, word in picks:
        unique.setdefault(round(sec, 2), word)
    picks = sorted(unique.items())

    work = out_dir / "_thumb"
    frames = frames_at(video, [sec for sec, _ in picks], work, width=FRAME_WIDTH)
    if len(frames) != len(picks):
        # выемка пачкой отдала не столько кадров, сколько просили, и по своему
        # контракту вернула пустой список: обложки не будет, но и неправильной
        # подписи «на экране Ямаль» под чужим кадром — тоже
        return None
    best: ThumbChoice | None = None
    # strict: кадров ровно столько, сколько секунд — иначе подпись «на экране
    # такое-то слово» уедет к чужому кадру, а это и есть та ошибка, из-за
    # которой пришлось сортировать кандидатов
    for (sec, word), frame, face in zip(picks, frames, _faces_in(frames), strict=True):
        sharp = _sharpness(frame)
        # лицо важнее резкости: резкий кадр затылка обложкой быть не может
        score = sharp * (3.0 if face else 1.0)
        if best is None or score > best.sharpness * (3.0 if best.face else 1.0):
            best = ThumbChoice(path=frame, at_sec=sec, keyword=word, face=face, sharpness=sharp)
    if best is None:
        return None

    dest = out_dir / THUMB_FILE
    dest.write_bytes(best.path.read_bytes())
    for f in frames:  # промежуточные кадры не нужны: решение принято
        f.unlink(missing_ok=True)
    work.rmdir() if not any(work.iterdir()) else None
    return ThumbChoice(
        path=dest,
        at_sec=best.at_sec,
        keyword=best.keyword,
        face=best.face,
        sharpness=best.sharpness,
    )
