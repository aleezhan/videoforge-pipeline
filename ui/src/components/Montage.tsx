// Монтажный вид: ролик сверху, дорожки снизу, один курсор времени на всех.
//
// Зачем он такой. Ошибку «музыка одна, а субтитры другие» в плеере не видно:
// звук идёт мимо ушей, текст мимо глаз, и чтобы их сверить, приходится
// открывать .ass рядом с .srt и считать секунды в уме. Здесь они лежат на
// ОДНОЙ шкале: волна звука, слова песни, речь, текст на экране, эффекты — и
// расхождение видно как расхождение, без чтения файлов.
//
// Две вещи сделаны намеренно и их легко сломать «улучшением».
// 1. Курсор двигает requestAnimationFrame и пишет прямо в style, минуя React:
//    событие timeupdate у <video> приходит 4 раза в секунду, и по нему курсор
//    дёргается; перерисовывать же тысячу слов 60 раз в секунду нельзя.
//    В React уходит только то, что реально меняется — какой элемент сейчас
//    активен на каждой дорожке.
// 2. Волна рисуется в canvas шириной с ЭКРАН, а не с ролик: при зуме 200 px/с
//    десятиминутный ролик — это 120 000 px, а браузер режет canvas на ~32 000.
//    Поэтому canvas едет за прокруткой и перерисовывается на месте.
//
// Пульт ничего не меняет: сдвиг дорожки — линейка для глаза, а не правка.
// Найденное число человек кладёт в план монтажа или в скрипт сборки.

import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { Minus, MoveHorizontal, Plus, Ruler } from "lucide-react";
import { useApi, type Montage as MontageT, type MontageTrack } from "@/api";
import { PlanEditor } from "@/components/PlanEditor";
import { Badge, Empty, cn } from "@/components/ui/primitives";
import { TRACK_ICONS } from "@/lib/icons";

const ROW_H: Record<MontageTrack["kind"], number> = {
  clips: 44,
  lines: 30,
  words: 26,
  wave: 40,
  marks: 26,
};
const LABEL_W = 156; // колонка названий дорожек: имя + кнопки сдвига
// На телефоне 156 px — это сорок процентов ширины, и на дорожки не остаётся
// ничего. Там колонка ужимается до имени, а кнопки сдвига прячутся: линейка
// в 50 мс пальцем всё равно не наводится, это инструмент для мыши
const LABEL_W_NARROW = 84;
const NARROW_PX = 640;
const RULER_H = 22;
// Тап или свайп: палец на «неподвижном» касании всё равно уезжает на пиксель-другой,
// а лента листается движением в десятки пикселей. Восемь — между ними с запасом
const TAP_SLOP_PX = 8;

function useNarrow(): boolean {
    const [narrow, setNarrow] = useState(
        () => typeof window !== "undefined" && window.innerWidth < NARROW_PX,
    );
    useEffect(() => {
        const check = () => setNarrow(window.innerWidth < NARROW_PX);
        window.addEventListener("resize", check);
        return () => window.removeEventListener("resize", check);
    }, []);
    return narrow;
}
const MIN_PX_SEC = 2;
const MAX_PX_SEC = 600;
const NUDGE_SEC = 0.05; // шаг сдвига дорожки: полкадра при 25 fps на глаз заметно
const FOLLOW_PAUSE_MS = 2500; // после ручной прокрутки курсор не тащит экран за собой
const TEXT_KINDS = new Set<MontageTrack["kind"]>(["lines", "words"]);
// красным — только то, что ЧИТАЮТ с экрана: слово короче кадра прочитать нельзя.
// Речь в этот счёт не идёт: «в» и «и» звучат 0.09 с, и это правда, а не брак
const READABLE = new Set(["captions", "lyrics", "overlays"]);
const SHORT_SEC = 0.12;

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** Время на шкале: 1:07.4 — секунды с десятой, иначе рассинхрон не обсудить. */
function stamp(sec: number): string {
  const s = Math.max(0, sec);
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${m}:${rest.toFixed(1).padStart(4, "0")}`;
}

function tickStep(pxPerSec: number): number {
  for (const step of [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]) {
    if (step * pxPerSec >= 60) return step;
  }
  return 600;
}

/** Индекс элемента, накрывающего секунду `t` (двоичный поиск по отсортированным). */
function activeIndex(items: { start: number; end: number }[], t: number, shift: number): number {
  let lo = 0;
  let hi = items.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const it = items[mid];
    if (t < it.start + shift) hi = mid - 1;
    else if (t >= it.end + shift) lo = mid + 1;
    else {
      found = mid;
      break;
    }
  }
  return found;
}

type Drawer = () => void;

/** Волна дорожки. Canvas шириной с видимую область, едет за прокруткой. */
function Wave({
  values,
  perSec,
  pxPerSec,
  height,
  scrollRef,
  drawers,
  colorVar,
}: {
  values: number[];
  perSec: number;
  pxPerSec: number;
  height: number;
  scrollRef: React.RefObject<HTMLDivElement | null>;
  drawers: React.RefObject<Set<Drawer>>;
  /** имя переменной темы (--ink-3, --info, --ok): canvas НЕ понимает var(), цвет
   * приходится доставать вычисленным — и перечитывать, когда тема сменилась */
  colorVar: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    // каждая дорожка рисуется в свой рост: музыка звучит на 16 дБ тише голоса,
    // и в общем масштабе она — ниточка, по которой не увидеть ни где вступает
    // трек, ни где дроп. Громкости сравнивают по замерам финала, не по картинке
    const scale = 1 / (values.reduce((m, v) => (v > m ? v : m), 0) || 1);
    const draw: Drawer = () => {
      const canvas = ref.current;
      const box = scrollRef.current;
      if (!canvas || !box) return;
      const color =
        getComputedStyle(document.documentElement).getPropertyValue(colorVar).trim() || "#888";
      const left = box.scrollLeft;
      const width = Math.max(1, Math.min(box.clientWidth, 4000));
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
      }
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      canvas.style.transform = `translateX(${left}px)`;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = color;
      const mid = height / 2;
      // столбик на пиксель: берём максимум окон, попавших в него, — тихий
      // провал между двумя всплесками не должен исчезнуть при отдалении
      for (let x = 0; x < width; x++) {
        const from = ((left + x) / pxPerSec) * perSec;
        const to = ((left + x + 1) / pxPerSec) * perSec;
        let peak = 0;
        for (let i = Math.floor(from); i < Math.max(Math.floor(from) + 1, Math.ceil(to)); i++) {
          const v = values[i];
          if (v !== undefined && v > peak) peak = v;
        }
        if (peak <= 0) continue;
        const h = Math.max(1, Math.min(1, peak * scale) * (height - 4));
        ctx.fillRect(x, mid - h / 2, 1, h);
      }
    };
    const set = drawers.current;
    set?.add(draw);
    draw();
    // тема меняется системно — цвет волны надо перечитать, иначе она сольётся с фоном
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", draw);
    return () => {
      set?.delete(draw);
      media.removeEventListener("change", draw);
    };
  }, [values, perSec, pxPerSec, height, scrollRef, drawers, colorVar]);
  return (
    <canvas
      ref={ref}
      className="pointer-events-none absolute top-0 left-0"
      style={{ height }}
      aria-hidden
    />
  );
}

type TrackItem = NonNullable<MontageTrack["items"]>[number];

/** Один элемент дорожки. Активный рисуется ТОЙ ЖЕ функцией поверх неподвижного слоя. */
function itemNode(track: MontageTrack, it: TrackItem, i: number, pxPerSec: number, shift: number, on: boolean) {
  if (track.kind === "clips") {
    return (
      <div
        key={on ? "active" : i}
        title={`${it.label ?? ""} · ${stamp(it.start)}–${stamp(it.end)}\n${it.text ?? ""}`}
        className={cn(
          "absolute top-1 bottom-1 overflow-hidden rounded-md border bg-panel-2 text-[11px]",
          on ? "z-1 border-accent ring-1 ring-accent/40" : "border-line",
        )}
        style={{ left: it.start * pxPerSec, width: Math.max(2, (it.end - it.start) * pxPerSec) }}
      >
        {it.thumb_url ? (
          <img src={it.thumb_url} alt="" loading="lazy" className="h-full w-full object-cover opacity-60" />
        ) : null}
        <span className="absolute inset-0 flex items-center gap-1 px-1.5 text-ink drop-shadow-[0_1px_0_var(--panel)]">
          <span className="font-mono text-[10px] text-ink-2">{it.label}</span>
          <span className="truncate">{it.text}</span>
        </span>
      </div>
    );
  }
  if (track.kind === "marks") {
    return (
      <div
        key={on ? "active" : i}
        title={`${it.label} на ${stamp(it.start)}${it.text ? ` — ${it.text}` : ""}`}
        className={cn(
          "absolute top-1 bottom-1 flex items-center rounded-sm border px-1 text-[10px] whitespace-nowrap",
          on ? "z-1 border-accent bg-accent-soft text-accent" : "border-line bg-panel-2 text-ink-2",
        )}
        style={{
          left: it.start * pxPerSec,
          width: Math.max(6, (it.end - it.start) * pxPerSec),
        }}
      >
        {it.label}
      </div>
    );
  }
  // lines и words: текст, который зритель читает или слышит
  const word = track.kind === "words";
  const short = READABLE.has(track.id) && it.end - it.start < SHORT_SEC;
  return (
    <div
      key={on ? "active" : i}
      title={
        `${it.text}\n${stamp(it.start + shift)}–${stamp(it.end + shift)} ` +
        `(${(it.end - it.start).toFixed(2)} с)` +
        (short ? `\nкрасным — короче ${SHORT_SEC} с: это не успевают прочитать` : "")
      }
      className={cn(
        "absolute overflow-hidden rounded-sm border px-1 text-[11px] whitespace-nowrap",
        word ? "top-1 bottom-1 leading-4" : "top-0.5 bottom-0.5 leading-5",
        short ? "border-bad/60 bg-bad-soft text-bad" : "",
        on ? "z-1 border-accent bg-accent-soft text-accent" : short ? "" : "border-line bg-panel text-ink-2",
      )}
      style={{
        left: (it.start + shift) * pxPerSec,
        width: Math.max(3, (it.end - it.start) * pxPerSec),
      }}
    >
      {it.text}
    </div>
  );
}

// Элемент уже этого числа пикселей не читается и не наводится мышью: соседние
// такие склеиваются в одну полоску, текст возвращается при зуме
const MERGE_PX = 4;

// Неподвижный слой дорожки. Цена его — не React, а браузер: после ЛЮБОЙ
// перерисовки в окне (курсор, часы, полоска плеера) Chrome заново раскладывает
// по слоям все элементы страницы, и 22-минутный ролик (ep003: ~5 тыс. слов и
// реплик) держал ядро на 100% при проигрывании, интерфейс 25 кадров/с, замер
// 25.09.2026. Поэтому здесь: только видимое окно [from, to] с запасом в экран,
// тонкие элементы — полосками, и от активного элемента слой не зависит
const TrackItems = memo(function TrackItems({
  track,
  pxPerSec,
  shift,
  from,
  to,
}: {
  track: MontageTrack;
  pxPerSec: number;
  shift: number;
  from: number;
  to: number;
}) {
  const out: React.ReactNode[] = [];
  const merge = track.kind !== "clips";
  const word = track.kind === "words";
  let run: { start: number; end: number; n: number; short: number; first: number } | null = null;
  const flush = () => {
    if (!run) return;
    const r = run;
    out.push(
      <div
        key={`m${r.first}`}
        title={
          `${r.n} эл. · ${stamp(r.start + shift)}–${stamp(r.end + shift)} — приблизьте, чтобы прочитать` +
          (r.short ? `\nиз них короче ${SHORT_SEC} с: ${r.short}` : "")
        }
        className={cn(
          "absolute rounded-sm border",
          word || track.kind === "marks" ? "top-1 bottom-1" : "top-0.5 bottom-0.5",
          r.short ? "border-bad/60 bg-bad-soft" : "border-line bg-panel-2",
        )}
        style={{ left: (r.start + shift) * pxPerSec, width: Math.max(3, (r.end - r.start) * pxPerSec) }}
      />,
    );
    run = null;
  };
  (track.items ?? []).forEach((it, i) => {
    if (it.end + shift < from || it.start + shift > to) return;
    if (merge && (it.end - it.start) * pxPerSec < MERGE_PX) {
      const short = READABLE.has(track.id) && it.end - it.start < SHORT_SEC ? 1 : 0;
      if (run && (it.start - run.end) * pxPerSec < MERGE_PX) {
        run.end = Math.max(run.end, it.end);
        run.n += 1;
        run.short += short;
        return;
      }
      flush();
      run = { start: it.start, end: it.end, n: 1, short, first: i };
      return;
    }
    flush();
    out.push(itemNode(track, it, i, pxPerSec, shift, false));
  });
  flush();
  return <>{out}</>;
});

function TrackRow({
  track,
  pxPerSec,
  shift,
  active,
  from,
  to,
}: {
  track: MontageTrack;
  pxPerSec: number;
  shift: number;
  active: number;
  from: number;
  to: number;
}) {
  const it = active >= 0 ? (track.items ?? [])[active] : undefined;
  return (
    <>
      <TrackItems track={track} pxPerSec={pxPerSec} shift={shift} from={from} to={to} />
      {it ? itemNode(track, it, active, pxPerSec, shift, true) : null}
    </>
  );
}

export function Montage({ project, episode }: { project: string; episode: string }) {
  const q = useApi<MontageT>(
    `/api/projects/${encodeURIComponent(project)}/episodes/${encodeURIComponent(episode)}/montage`,
  );
  const videoRef = useRef<HTMLVideoElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const clockRef = useRef<HTMLSpanElement>(null);
  const drawers = useRef<Set<Drawer>>(new Set());
  const followUntil = useRef(0);
  const [pxPerSec, setPxPerSec] = useState(0);
  const [shifts, setShifts] = useState<Record<string, number>>({});
  const narrow = useNarrow();
  const tapFrom = useRef<{ x: number; y: number } | null>(null);
  const [active, setActive] = useState<Record<string, number>>({});
  // видимое окно дорожек в секундах, шагом в полэкрана: React пересобирает
  // дорожки, только когда прокрутка переходит границу шага, а не на каждый пиксель
  const [view, setView] = useState<[number, number]>([0, 0]);
  const lastHead = useRef("");
  const lastClock = useRef("");

  const data = q.data;
  const duration = data?.duration_sec ?? 0;
  const tracks = data?.tracks ?? [];

  // первый показ — весь ролик в ширину окна: сначала обзор, зум по требованию
  useLayoutEffect(() => {
    if (!duration || pxPerSec) return;
    const width = scrollRef.current?.clientWidth ?? 900;
    setPxPerSec(clamp((width - 16) / duration, MIN_PX_SEC, MAX_PX_SEC));
  }, [duration, pxPerSec]);

  const redraw = useCallback(() => {
    drawers.current.forEach((d) => d());
  }, []);

  const syncView = useCallback(() => {
    const box = scrollRef.current;
    if (!box || !pxPerSec) return;
    const half = Math.max(200, box.clientWidth / 2);
    const b = Math.floor(box.scrollLeft / half);
    const from = ((b - 2) * half) / pxPerSec;
    const to = ((b + 5) * half) / pxPerSec;
    setView((prev) => (prev[0] === from && prev[1] === to ? prev : [from, to]));
  }, [pxPerSec]);

  useLayoutEffect(() => {
    syncView();
    window.addEventListener("resize", syncView);
    return () => window.removeEventListener("resize", syncView);
  }, [syncView, tracks.length]);

  const seek = useCallback(
    (clientX: number) => {
      const box = scrollRef.current;
      const video = videoRef.current;
      if (!box || !video || !pxPerSec) return;
      const x = clientX - box.getBoundingClientRect().left + box.scrollLeft;
      video.currentTime = clamp(x / pxPerSec, 0, duration);
      followUntil.current = Date.now() + FOLLOW_PAUSE_MS;
    },
    [duration, pxPerSec],
  );

  // курсор: пишем в DOM напрямую, в React уходит только смена активного элемента
  const paint = useCallback(() => {
    const video = videoRef.current;
    const box = scrollRef.current;
    if (!video || !box || !pxPerSec) return;
    const t = video.currentTime;
    // писать в DOM только то, что видимо сменилось: каждая запись — перерисовка,
    // а в обзоре длинного ролика курсор сдвигается на пиксель в секунду, не в кадр
    const head = `translateX(${Math.round(t * pxPerSec)}px)`;
    if (headRef.current && head !== lastHead.current) {
      headRef.current.style.transform = head;
      lastHead.current = head;
    }
    const clock = stamp(t);
    if (clockRef.current && clock !== lastClock.current) {
      clockRef.current.textContent = clock;
      lastClock.current = clock;
    }
    if (!video.paused && Date.now() > followUntil.current) {
      // сравнивать с целью ПОСЛЕ обрезки: в обзоре цель отрицательная, прокрутка
      // стоит в нуле, и сравнение с сырой целью перерисовывало волны каждый кадр
      const target = clamp(t * pxPerSec - box.clientWidth * 0.35, 0, box.scrollWidth - box.clientWidth);
      if (Math.abs(box.scrollLeft - target) > 2) {
        box.scrollLeft = target;
        redraw();
      }
    }
    setActive((prev) => {
      let changed = false;
      const next: Record<string, number> = {};
      for (const track of tracks) {
        const idx = activeIndex(track.items ?? [], t, shifts[track.id] ?? 0);
        next[track.id] = idx;
        if (prev[track.id] !== idx) changed = true;
      }
      return changed ? next : prev;
    });
  }, [pxPerSec, redraw, shifts, tracks]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let raf = 0;
    const loop = () => {
      paint();
      raf = requestAnimationFrame(loop);
    };
    const start = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(loop);
    };
    const stop = () => {
      cancelAnimationFrame(raf);
      paint();
    };
    video.addEventListener("play", start);
    video.addEventListener("pause", stop);
    video.addEventListener("seeked", paint);
    video.addEventListener("loadedmetadata", paint);
    paint();
    if (!video.paused) start();
    return () => {
      cancelAnimationFrame(raf);
      video.removeEventListener("play", start);
      video.removeEventListener("pause", stop);
      video.removeEventListener("seeked", paint);
      video.removeEventListener("loadedmetadata", paint);
    };
  }, [paint]);

  // зум держит секунду под курсором на месте — иначе после каждого нажатия
  // приходится заново искать то место, которое разглядывал
  const zoom = useCallback(
    (factor: number, anchorX?: number) => {
      const box = scrollRef.current;
      if (!box) return;
      setPxPerSec((prev) => {
        const next = clamp(prev * factor, MIN_PX_SEC, MAX_PX_SEC);
        const anchor = anchorX ?? box.clientWidth / 2;
        const at = (box.scrollLeft + anchor) / prev;
        requestAnimationFrame(() => {
          box.scrollLeft = clamp(at * next - anchor, 0, at * next);
          redraw();
        });
        return next;
      });
    },
    [redraw],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.isContentEditable)) return;
      const video = videoRef.current;
      if (!video) return;
      if (e.code === "Space") {
        e.preventDefault();
        if (video.paused) void video.play();
        else video.pause();
      } else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const step = e.shiftKey ? 1 : 0.04;
        video.currentTime = clamp(
          video.currentTime + (e.key === "ArrowRight" ? step : -step),
          0,
          duration,
        );
        followUntil.current = Date.now() + FOLLOW_PAUSE_MS;
      } else if (e.key === "+" || e.key === "=") {
        zoom(1.5);
      } else if (e.key === "-") {
        zoom(1 / 1.5);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [duration, zoom]);

  // свои заглушки, а не Loading/ErrorBox из common: монтаж вставляется в
  // MediaModal, который живёт там же, и импорт назад замкнул бы модули кольцом
  if (q.isLoading) return <div className="px-1 py-10 text-sm text-ink-3">Читаю дорожки…</div>;
  if (q.error) return <Empty>{q.error.status === 404 ? q.error.message : `Ошибка: ${q.error.message}`}</Empty>;
  if (!data) return null;
  if (!tracks.length) return <Empty>Дорожек нет: у эпизода только готовый файл, без раскадровки и субтитров.</Empty>;

  const width = Math.max(320, duration * pxPerSec);
  const vertical = data.aspect_ratio !== "16:9";
  const nudge = (id: string, delta: number) =>
    setShifts((prev) => ({ ...prev, [id]: Math.round(((prev[id] ?? 0) + delta) * 1000) / 1000 }));
  const shifted = Object.entries(shifts).filter(([, v]) => v);

  const now = (id: string) => {
    const track = tracks.find((t) => t.id === id);
    const idx = active[id] ?? -1;
    return idx >= 0 ? (track?.items ?? [])[idx] : undefined;
  };
  const nowScene = now("scenes");
  const nowCaption = now("captions");
  const nowOverlay = now("overlays");
  const nowSpeech = now("speech");
  const nowLyric = now("lyrics");
  const nowEffect = now("effects");
  const nowSfx = now("sfx");

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {/* shrink-0 обязателен: без него на невысоком окне ряд сжимается ниже
          своего содержимого, плеер вылезает наружу и накрывает панель зума —
          кнопки видно, а нажать нельзя (поймано Playwright-ом на окне 1000 px) */}
      <div className="flex shrink-0 flex-wrap gap-4">
        <video
          ref={videoRef}
          src={data.video_url ?? undefined}
          poster={data.poster_url ?? undefined}
          controls
          preload="auto"
          playsInline
          className={cn("rounded-lg bg-black", vertical ? "max-h-[38vh]" : "max-h-[38vh] w-auto")}
        />
        <div className="min-w-64 flex-1 space-y-2 text-[13px]">
          <div className="flex items-baseline gap-2">
            <span ref={clockRef} className="font-mono text-2xl font-semibold tabular-nums">
              0:00.0
            </span>
            <span className="text-xs text-ink-3">из {stamp(duration)}</span>
            {/* Значок только там, где есть что измерять: он про длины СЦЕН из
                timeline.json. У ролика, собранного скриптом, сцен нет вовсе, а
                планы сняты с самого файла — говорить там «не измерено» значит
                пугать человека тем, чего не существует */}
            {!data.measured && tracks.some((t) => t.id === "scenes") ? (
              <Badge tone="warn" title="timeline.json не найден — время сцен сложено из раскадровки">
                время не измерено
              </Badge>
            ) : null}
            {data.stale.length ? (
              <Badge
                tone="bad"
                title={`новее готового файла: ${data.stale.join(", ")}\nСобрать заново: videoforge run ${data.copy_of ?? data.ref} --only assemble`}
              >
                финал устарел ({data.stale.length})
              </Badge>
            ) : null}
            {/* копия чужого файла: дорожки — оригинала, и править их надо там */}
            {data.copy_of ? (
              <Link
                to={`/projects/${data.copy_of.split("/")[0]}/episodes/${data.copy_of.split("/")[1]}`}
                className="text-xs text-accent hover:underline"
                title="дорожки взяты у оригинала — открыть его"
              >
                копия {data.copy_of}
              </Link>
            ) : null}
          </div>
          <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
            <NowRow label="Сцена" value={nowScene ? `${nowScene.label} — ${nowScene.text || "без реплики"}` : null} />
            <NowRow label="На экране" value={nowCaption?.text ?? null} />
            <NowRow label="Плашка" value={nowOverlay?.text ?? null} />
            <NowRow label="Слышно" value={nowSpeech?.text ?? null} />
            <NowRow label="Поют" value={nowLyric?.text ?? null} />
            <NowRow label="Эффект" value={nowEffect?.label ?? null} />
            <NowRow label="Звук" value={nowSfx?.label ?? null} />
          </dl>
          {data.notes.map((n, i) => (
            <div
              key={i}
              className={cn(
                "rounded-md px-2 py-1 text-xs",
                n.severity === "bad"
                  ? "bg-bad-soft text-bad"
                  : n.severity === "info"
                    ? "bg-panel-2 text-ink-2"
                    : "bg-warn-soft text-warn",
              )}
            >
              {n.text}
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
        <button type="button" onClick={() => zoom(1 / 1.5)} className={btn} title="отдалить (−)">
          <Minus className="h-3.5 w-3.5" />
        </button>
        <button type="button" onClick={() => zoom(1.5)} className={btn} title="приблизить (+)">
          <Plus className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={() => {
            const box = scrollRef.current;
            if (!box || !duration) return;
            setPxPerSec(clamp((box.clientWidth - 16) / duration, MIN_PX_SEC, MAX_PX_SEC));
            // прокрутку надо снять руками: лента стала уже экрана, а браузер
            // держит старый scrollLeft — и человек остаётся смотреть в пустоту
            // за концом ролика (найдено на живом ролике 09.09.2026)
            requestAnimationFrame(() => {
              box.scrollLeft = 0;
              redraw();
            });
          }}
          className={btn}
          title="весь ролик в ширину"
        >
          <Ruler className="h-3.5 w-3.5" /> весь
        </button>
        <span className="tabular-nums">{pxPerSec.toFixed(0)} px/с</span>
        <span className="text-ink-3">· пробел — пуск, ←/→ — кадр, Shift+←/→ — секунда</span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-2.5 rounded-[3px] border border-bad/60 bg-bad-soft" />
          <span className="text-ink-3">короче {SHORT_SEC} с — не успевают прочитать</span>
        </span>
        {shifted.length ? (
          <span className="text-warn">
            сдвиг только для глаза: {shifted.map(([id, v]) => `${id} ${v > 0 ? "+" : ""}${v.toFixed(2)} с`).join(", ")} — число
            применяют в плане монтажа или в сборке, пульт файлов не трогает
          </span>
        ) : null}
      </div>

      {/* shrink-0: в колонке с плеером и замечаниями блок дорожек иначе
          сжимается до нуля на невысоком экране — на телефоне их просто не было
          видно, хотя данные приходили */}
      <div className="flex min-h-0 shrink-0 overflow-hidden rounded-lg border border-line bg-panel">
        <div
          className="shrink-0 border-r border-line bg-panel"
          style={{ width: narrow ? LABEL_W_NARROW : LABEL_W }}
        >
          <div style={{ height: RULER_H }} className="border-b border-line" />
          {tracks.map((track) => {
            const Icon = TRACK_ICONS[track.icon];
            const canShift = TEXT_KINDS.has(track.kind) && !narrow;
            return (
              <div
                key={track.id}
                title={track.hint}
                className="flex items-center gap-1 border-b border-line px-2 text-[11px] text-ink-2 last:border-b-0"
                style={{ height: ROW_H[track.kind] }}
              >
                {Icon ? <Icon className="h-3.5 w-3.5 shrink-0 text-ink-3" /> : null}
                <span className="min-w-0 flex-1 truncate">{track.title}</span>
                {canShift ? (
                  <span className="flex items-center gap-0.5">
                    <button
                      type="button"
                      className="rounded px-1 text-ink-3 hover:bg-panel-2 hover:text-ink"
                      title="сдвинуть дорожку назад на 50 мс"
                      onClick={() => nudge(track.id, -NUDGE_SEC)}
                    >
                      ‹
                    </button>
                    {shifts[track.id] ? (
                      <button
                        type="button"
                        className="font-mono text-[10px] text-warn"
                        title="снять сдвиг"
                        onClick={() => nudge(track.id, -(shifts[track.id] ?? 0))}
                      >
                        {shifts[track.id] > 0 ? "+" : ""}
                        {shifts[track.id].toFixed(2)}
                      </button>
                    ) : (
                      <MoveHorizontal className="h-3 w-3 text-ink-3/60" />
                    )}
                    <button
                      type="button"
                      className="rounded px-1 text-ink-3 hover:bg-panel-2 hover:text-ink"
                      title="сдвинуть дорожку вперёд на 50 мс"
                      onClick={() => nudge(track.id, NUDGE_SEC)}
                    >
                      ›
                    </button>
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>

        <div
          ref={scrollRef}
          className="relative min-w-0 flex-1 overflow-x-auto overflow-y-hidden"
          onScroll={() => {
            redraw();
            syncView();
          }}
          onWheel={(e) => {
            if (e.ctrlKey) {
              e.preventDefault();
              zoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - (scrollRef.current?.getBoundingClientRect().left ?? 0));
            } else {
              followUntil.current = Date.now() + FOLLOW_PAUSE_MS;
            }
          }}
        >
          {/* Протяжка — только по шкале времени (см. ниже), а по дорожкам ловим
              ТАП: палец в 22-пиксельную полоску не попадает, и на телефоне
              перемотки не было вовсе. Тап отличается от свайпа по смещению,
              поэтому лента по-прежнему листается пальцем. Обработчик висит на
              содержимом, а не на области прокрутки: иначе вернётся старая
              беда — клик по полосе прокрутки внизу считался перемоткой */}
          <div
            className="relative"
            style={{ width }}
            onPointerDown={(e) => {
              tapFrom.current = { x: e.clientX, y: e.clientY };
            }}
            onPointerUp={(e) => {
              const from = tapFrom.current;
              tapFrom.current = null;
              if (!from) return;
              const moved = Math.abs(e.clientX - from.x) + Math.abs(e.clientY - from.y);
              if (moved <= TAP_SLOP_PX) seek(e.clientX);
            }}
          >
            <div
              className="relative cursor-crosshair border-b border-line bg-panel-2/40"
              style={{ height: RULER_H }}
              onPointerDown={(e) => {
                // сначала перемотка, потом захват: захват умеет бросить исключение
                // (нет активного указателя), и тогда клик пропадал бы целиком
                seek(e.clientX);
                try {
                  (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
                } catch {
                  /* без захвата просто не будет протяжки — клик уже сработал */
                }
              }}
              onPointerMove={(e) => {
                if (e.buttons === 1) seek(e.clientX);
              }}
            >
              {(() => {
                const step = tickStep(pxPerSec);
                const ticks = [];
                for (let t = 0; t <= duration + 0.001; t += step) {
                  ticks.push(
                    <span
                      key={t}
                      className="absolute top-0 border-l border-line pl-1 text-[10px] text-ink-3 tabular-nums"
                      style={{ left: t * pxPerSec, height: RULER_H }}
                    >
                      {stamp(t)}
                    </span>,
                  );
                }
                return ticks;
              })()}
            </div>
            {tracks.map((track) => (
              <div
                key={track.id}
                className="relative border-b border-line last:border-b-0"
                style={{ height: ROW_H[track.kind] }}
              >
                {track.kind === "wave" ? (
                  <>
                    <Wave
                      values={track.values ?? []}
                      perSec={track.per_sec ?? data.per_sec}
                      pxPerSec={pxPerSec}
                      height={ROW_H.wave}
                      scrollRef={scrollRef}
                      drawers={drawers}
                      colorVar={track.id === "music" ? "--info" : track.id === "voice" ? "--ok" : "--ink-3"}
                    />
                    {(track.marks ?? []).map((m, i) => (
                      <span
                        key={i}
                        title={`${m.label}${m.why ? ` — ${m.why}` : ""} на ${stamp(m.at)}`}
                        className="absolute top-0 bottom-0 border-l border-accent text-[9px] text-accent"
                        style={{ left: m.at * pxPerSec }}
                      >
                        <span className="ml-0.5 bg-panel/80 px-0.5">{m.label}</span>
                      </span>
                    ))}
                  </>
                ) : (
                  <TrackRow
                    track={track}
                    pxPerSec={pxPerSec}
                    shift={shifts[track.id] ?? 0}
                    active={active[track.id] ?? -1}
                    from={view[0]}
                    to={view[1]}
                  />
                )}
              </div>
            ))}
            <div
              ref={headRef}
              className="pointer-events-none absolute top-0 bottom-0 z-10 w-px bg-accent"
              style={{ willChange: "transform" }}
            >
              <span className="absolute -top-px -left-0.75 h-1.5 w-1.5 rounded-full bg-accent" />
            </div>
          </div>
        </div>
      </div>
      {tracks.some((t) => t.id === "music" && t.text) ? (
        <details className="rounded-lg border border-line bg-panel px-3 py-2 text-[12px]">
          <summary className="cursor-pointer text-ink-2">Текст песни (без таймингов)</summary>
          <pre className="mt-2 whitespace-pre-wrap text-ink-2">{tracks.find((t) => t.id === "music")?.text}</pre>
        </details>
      ) : null}
      {/* у копии плана нет: форма правила бы план КОПИИ, а ролик собирают у оригинала */}
      {data.copy_of ? null : (
      <details className="shrink-0 rounded-lg border border-line bg-panel px-3 py-2 text-[12px]">
        <summary className="cursor-pointer text-ink-2">
          Правка — музыка, затухания, переходы, эффекты, звуки, замедленный хвост
          (меняется план монтажа, не ролик)
        </summary>
        <div className="mt-2">
          <PlanEditor
            project={project}
            episode={episode}
            shiftHint={shifts.lyrics ?? 0}
            // секунда эффекта и звука — это секунда финала, и плеер здесь же:
            // новое событие встаёт под курсор, а по кнопке в строке плеер
            // прыгает к нему обратно. Иначе числа набирают вслепую
            now={() => videoRef.current?.currentTime ?? 0}
            onSeek={(t) => {
              const video = videoRef.current;
              if (!video) return;
              video.currentTime = clamp(t, 0, duration);
              followUntil.current = Date.now() + FOLLOW_PAUSE_MS;
            }}
          />
        </div>
      </details>
      )}
      {data.materials.length ? (
        <details className="shrink-0 rounded-lg border border-line bg-panel px-3 py-2 text-[12px]" open>
          <summary className="cursor-pointer text-ink-2">
            Материалы — из чего собран ролик и откуда взято ({data.materials.length})
          </summary>
          {data.passport ? (
            <div className="mt-2 rounded-md bg-panel-2 px-2 py-1.5 text-[11px] text-ink-2">
              <span className="text-ink-3">Собран: </span>
              {data.passport.made_by || "—"}
              {data.passport.rebuild ? (
                <>
                  <span className="text-ink-3"> · пересборка: </span>
                  {data.passport.rebuild}
                </>
              ) : null}
            </div>
          ) : null}
          <div className="mt-2 grid gap-x-4 gap-y-1.5 md:grid-cols-2">
            {data.materials.map((m, i) => (
              <div key={i} className="flex min-w-0 items-baseline gap-2">
                <span className="w-36 shrink-0 text-right text-ink-3">{m.title}</span>
                <span className="min-w-0">
                  {m.state === "gone" ? (
                    <Badge tone="bad" className="mr-1" title="в паспорте записан, а на диске его нет">
                      файла нет
                    </Badge>
                  ) : m.state === "changed" ? (
                    <Badge tone="warn" className="mr-1" title="файл изменился после записи в паспорт">
                      изменился
                    </Badge>
                  ) : null}
                  {m.resource ? (
                    <Link to={`/resources/${m.resource}`} className="hover:underline">
                      {m.value}
                    </Link>
                  ) : (
                    m.value
                  )}
                  {m.origin ? (
                    <span className="text-ink-3">
                      {" · "}
                      {/^https?:/.test(m.origin) ? (
                        <a href={m.origin} target="_blank" rel="noreferrer" className="hover:underline">
                          {m.origin.replace(/^https?:\/\//, "").slice(0, 48)}
                        </a>
                      ) : (
                        m.origin
                      )}
                    </span>
                  ) : null}
                  {m.link ? (
                    <a
                      href={m.link}
                      target="_blank"
                      rel="noreferrer"
                      className="ml-1 text-ink-3 hover:text-ink hover:underline"
                    >
                      ↗
                    </a>
                  ) : null}
                  {m.license ? (
                    <Badge tone={m.credit ? "warn" : "neutral"} className="ml-1.5" title={m.credit || m.license}>
                      {/* лицензия в ресурсе — целый абзац с объяснением; на бейдж
                          берём только её имя, до первого тире или скобки */}
                      {m.credit ? "нужно упоминание" : m.license.split(/[—(,]/)[0].trim().slice(0, 20)}
                    </Badge>
                  ) : null}
                </span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

const btn =
  "inline-flex items-center gap-1 rounded-md border border-line bg-panel px-2 py-1 text-[12px] text-ink-2 hover:border-ink-3 hover:text-ink";

function NowRow({ label, value }: { label: string; value: string | null }) {
  return (
    <>
      <dt className="text-xs text-ink-3">{label}</dt>
      <dd className={cn("min-w-0 truncate", value ? "text-ink" : "text-ink-3/60")}>{value || "—"}</dd>
    </>
  );
}
