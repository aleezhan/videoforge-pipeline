// Компоненты, которые повторяются на разных страницах: состояние загрузки,
// ошибка, шапка страницы, бейдж статуса стадии, карточка ресурса, полоса стадий.

import { useEffect, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AlertTriangle, Image as ImageIcon, Music, Volume2, X, type LucideIcon } from "lucide-react";
import type { ApiError, ResourceCard as ResourceCardT, StageRow, StageStatus, UploadState } from "@/api";
import { Montage } from "@/components/Montage";
import { Badge, IconTile, cn, type Tone } from "@/components/ui/primitives";
import { fmtDate, fmtUsd } from "@/lib/format";
import { KIND_ICONS } from "@/lib/icons";

export function Loading({ label = "Читаю файлы…" }: { label?: string }) {
  return <div className="px-1 py-10 text-sm text-ink-3">{label}</div>;
}

export function ErrorBox({ error }: { error: ApiError | Error }) {
  const status = "status" in error ? error.status : undefined;
  return (
    <div className="flex items-start gap-3 rounded-lg border border-bad/30 bg-bad-soft px-4 py-3 text-sm text-bad">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div>
        <div className="font-medium">{status === 404 ? "Не найдено" : "Ошибка"}</div>
        <div className="opacity-90">{error.message}</div>
      </div>
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  meta,
  actions,
  children,
  icon,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  icon?: LucideIcon;
}) {
  return (
    <header className="space-y-2">
      {eyebrow ? <div className="text-xs text-ink-3">{eyebrow}</div> : null}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          {icon ? <IconTile icon={icon} size="lg" className="mt-0.5" /> : null}
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
            {meta ? <div className="mt-1 flex flex-wrap items-center gap-1.5 text-sm text-ink-2">{meta}</div> : null}
          </div>
        </div>
        {/* shrink-0 только на широком экране: на телефоне бейджи аккаунтов
            («Instagram @…», «TikTok @…», «YouTube @…») в один ряд не влезают,
            а сжаться им запрещено — строка уезжала за край экрана */}
        {actions ? <div className="flex flex-wrap items-center gap-2 md:shrink-0">{actions}</div> : null}
      </div>
      {children}
    </header>
  );
}

export function Crumbs({ items }: { items: { to?: string; label: string }[] }) {
  return (
    <nav className="flex flex-wrap items-center gap-1 text-xs text-ink-3">
      {items.map((it, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 ? <span className="opacity-60">/</span> : null}
          {it.to ? (
            <Link to={it.to} className="hover:text-ink">
              {it.label}
            </Link>
          ) : (
            <span className="text-ink-2">{it.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}

const STAGE_TONE: Record<StageStatus, Tone> = {
  pending: "neutral",
  running: "info",
  done: "ok",
  failed: "bad",
  skipped: "neutral",
  blocked: "warn",
};

const STAGE_LABEL: Record<StageStatus, string> = {
  pending: "не начата",
  running: "идёт",
  done: "готово",
  failed: "ошибка",
  skipped: "пропущена",
  blocked: "заблокирована",
};

export function StageBadge({ status }: { status: StageStatus }) {
  return <Badge tone={STAGE_TONE[status]}>{STAGE_LABEL[status]}</Badge>;
}

export function stageTone(status: StageStatus): Tone {
  return STAGE_TONE[status];
}

/** Полоса стадий: точка на стадию, цвет — статус, кольцо — ворота. */
export function StageStrip({ stages }: { stages: StageRow[] }) {
  const DOT: Record<StageStatus, string> = {
    pending: "bg-line",
    running: "bg-info",
    done: "bg-ok",
    failed: "bg-bad",
    skipped: "bg-ink-3/40",
    blocked: "bg-warn",
  };
  return (
    <div className="flex items-center gap-1">
      {stages.map((s) => (
        <span
          key={s.name}
          title={`${s.name}: ${STAGE_LABEL[s.status]}${s.awaiting ? " · ждёт одобрения" : ""}${s.gate_after ? " · ворота" : ""}`}
          className={cn(
            "h-2.5 w-2.5 rounded-full",
            DOT[s.status],
            s.awaiting ? "ring-2 ring-warn ring-offset-1 ring-offset-panel" : "",
          )}
        />
      ))}
    </div>
  );
}

export function StatusPill({ status }: { status: "live" | "demo" }) {
  return status === "live" ? <Badge tone="ok">живой</Badge> : <Badge tone="neutral">демо</Badge>;
}

// Куда ролик уехал НА САМОМ ДЕЛЕ. Отдельно от кружка стадии publish: та стадия
// пишет заголовок и описание, то есть «упаковано», а не «выложено» — их путали.
// Загруженный приватно красится оранжевым: он лежит на канале и его не видит
// никто, и это самое лёгкое, что можно забыть.
export function UploadPill({ upload }: { upload?: UploadState | null }) {
  if (!upload || !upload.platforms.length) return null;
  const where = upload.platforms.map((p) => p.network).join(", ");
  const when = upload.platforms[0]?.at ? ` · ${fmtDate(upload.platforms[0].at)}` : "";
  if (upload.state === "failed") {
    return <Badge tone="bad" title={`не ушло: ${where}`}>не ушло</Badge>;
  }
  if (upload.privacy === "private") {
    return (
      <Badge tone="warn" title={`загружено скрытым на ${where}${when} — никто не видит, пока не откроешь`}>
        приват
      </Badge>
    );
  }
  if (upload.privacy === "unlisted") {
    return <Badge tone="warn" title={`по ссылке: ${where}${when}`}>по ссылке</Badge>;
  }
  return <Badge tone="ok" title={`${where}${when}`}>опубликовано</Badge>;
}

export function resourceLink(card: Pick<ResourceCardT, "kind" | "id" | "project" | "scope">): string {
  const base = `/resources/${card.kind}/${encodeURIComponent(card.id)}`;
  return card.scope === "project" && card.project ? `${base}?project=${encodeURIComponent(card.project)}` : base;
}

export function ResourcePreview({ card, className }: { card: ResourceCardT; className?: string }) {
  const Kind = KIND_ICONS[card.kind];
  const icon =
    card.kind === "music" ? (
      <Music className="h-5 w-5" />
    ) : card.kind === "sfx" ? (
      <Volume2 className="h-5 w-5" />
    ) : Kind ? (
      <Kind className="h-5 w-5" />
    ) : (
      <ImageIcon className="h-5 w-5" />
    );
  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden rounded-lg border border-line bg-panel-2 text-ink-3",
        className,
      )}
    >
      {card.preview_url ? (
        <img src={card.preview_url} alt={card.name} className="h-full w-full object-cover" loading="lazy" />
      ) : (
        icon
      )}
    </div>
  );
}

/** Витрина ресурса: клип со стилем субтитров, проба голоса. Имя стиля глазу не
 * говорит ничего, поэтому в списках она идёт вперёд текста. Видео — немой
 * автоплей петлёй, как галерея приёмов; звук — плеер с кнопками. */
export function ResourceExample({
  card,
  className,
}: {
  card: Pick<ResourceCardT, "example_url" | "example_kind" | "name">;
  className?: string;
}) {
  const url = card.example_url;
  if (!url) return null;
  if (card.example_kind === "audio") return <audio controls preload="none" src={url} className={cn("w-full", className)} />;
  // размер задаёт место показа: в плитке — во всю ширину, на странице ресурса —
  // по своей ширине от высоты, иначе вертикальный клип тонет в чёрных полях
  const box = cn("bg-black object-contain", className);
  if (card.example_kind === "image") return <img src={url} alt={card.name} loading="lazy" className={box} />;
  return <video src={url} muted loop autoPlay playsInline preload="metadata" className={box} />;
}

/** Свой или общий. Общий голос и свой персонаж оба стоят в project.yaml и
 * выглядели одинаково «ресурсами проекта», а разница практическая: правка
 * общего меняет все проекты, где он стоит. */
function OwnershipBadge({ card }: { card: Pick<ResourceCardT, "scope" | "project"> }) {
  if (card.scope === "project") {
    return (
      <Badge tone="info" title={`лежит в папке проекта: projects/${card.project ?? "…"}/resources/`}>
        свой
      </Badge>
    );
  }
  if (card.scope === "global") {
    return <Badge title="лежит в resources/: правка отразится на всех проектах, где он стоит">общий</Badge>;
  }
  return null;
}

/** Где используется — строки приходят только со страницы проекта: у общего —
 * другие проекты, где он стоит; у визуального — эпизоды этого проекта, где он
 * в касте, со счётом сцен. Свой ресурс, которого нет ни в одной сцене, — скорее
 * всего забытый, поэтому подсвечен. */
function ResourceUsage({ card }: { card: ResourceCardT }) {
  const rows: ReactNode[] = [];
  if (card.scope === "global" && card.shared_with) {
    rows.push(
      card.shared_with.length ? (
        <span key="shared">ещё в: {card.shared_with.join(", ")}</span>
      ) : (
        <span key="shared">в других проектах не используется</span>
      ),
    );
  }
  if (card.used_in) {
    rows.push(
      card.used_in.length ? (
        <span key="used">в сценах: {card.used_in.map((u) => `${u.episode} ×${u.scenes}`).join(", ")}</span>
      ) : (
        <span key="used" className={card.scope === "project" ? "text-warn" : undefined}>
          в сценах не задействован
        </span>
      ),
    );
  }
  if (!rows.length) return null;
  return <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-ink-3">{rows}</div>;
}

export function ResourceCard({ card, label }: { card: ResourceCardT; label?: string }) {
  const body = (
    <div
      className={cn(
        "flex gap-3 rounded-xl border border-line bg-panel p-3 transition-colors",
        card.missing ? "border-dashed border-bad/40" : "hover:border-ink-3",
      )}
    >
      <ResourcePreview card={card} className="h-16 w-16" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          {label ? <span className="text-[11px] uppercase tracking-wide text-ink-3">{label}</span> : null}
          <OwnershipBadge card={card} />
          {card.missing ? <Badge tone="bad">не найден</Badge> : null}
        </div>
        <div className="truncate font-medium">{card.name}</div>
        <div className="text-xs text-ink-3">
          {card.kind_title} · <span className="font-mono">{card.id}</span>
        </div>
        {card.description ? <div className="mt-1 line-clamp-2 text-xs text-ink-2">{card.description}</div> : null}
        <ResourceUsage card={card} />
        {card.error ? <div className="mt-1 text-xs text-bad">{card.error}</div> : null}
      </div>
    </div>
  );
  if (card.missing) return body;
  return (
    <Link to={resourceLink(card)} className="block">
      {body}
    </Link>
  );
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-vf text-[13px]">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------- медиа в модалке

export type MediaTab = "clip" | "video" | "image" | "audio" | "montage";

const MEDIA_ORDER: MediaTab[] = ["clip", "video", "image", "audio"];
const MEDIA_LABEL: Record<MediaTab, string> = {
  clip: "сцена со звуком",
  video: "клип без звука",
  image: "исходный кадр",
  audio: "озвучка",
  montage: "монтаж",
};

/** Просмотр файлов сцены поверх страницы: один файл за раз, грузится по
 * открытию — сто карточек с видео страницу не тянут. Esc и клик по фону закрывают.
 *
 * `montage` добавляет вкладку с таймлайном эпизода: тот же ролик, но с
 * дорожками под ним. Она приходит эпизодом, а не файлом, потому что дорожки —
 * это весь эпизод целиком, а не один файл сцены. */
export function MediaModal({
  title,
  sources,
  initial,
  labels,
  montage,
  onClose,
}: {
  title: ReactNode;
  sources: Partial<Record<MediaTab, string | null | undefined>>;
  initial?: MediaTab;
  labels?: Partial<Record<MediaTab, string>>;
  montage?: { project: string; episode: string };
  onClose: () => void;
}) {
  const label = (t: MediaTab) => labels?.[t] ?? MEDIA_LABEL[t];
  const available: MediaTab[] = [
    ...MEDIA_ORDER.filter((t) => sources[t]),
    ...(montage ? (["montage"] as MediaTab[]) : []),
  ];
  const [tab, setTab] = useState<MediaTab>(
    initial && (sources[initial] || (initial === "montage" && montage)) ? initial : available[0],
  );
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);
  if (!available.length) return null;
  const src = sources[tab] as string;
  const wide = tab === "montage"; // таймлайну нужна вся ширина, ролику — нет
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4 md:p-8" onClick={onClose}>
      <div
        className={cn(
          "flex max-h-full w-full flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-2xl",
          wide ? "max-w-[1800px]" : "max-w-5xl",
        )}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <div className="min-w-0 truncate text-[13px] font-medium">{title}</div>
          <div className="flex items-center gap-1">
            {available.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={cn(
                  "rounded-md px-2 py-1 text-[12px]",
                  t === tab ? "bg-panel-2 text-ink" : "text-ink-3 hover:text-ink",
                )}
              >
                {label(t)}
              </button>
            ))}
            {src ? (
              <a
                href={src}
                target="_blank"
                rel="noreferrer"
                className="ml-2 rounded-md px-2 py-1 text-[12px] text-ink-3 hover:text-ink"
                title="открыть файл в новой вкладке"
              >
                файл
              </a>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              className="ml-1 rounded-md p-1 text-ink-3 hover:bg-panel-2 hover:text-ink"
              aria-label="закрыть"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div
          className={cn(
            "flex min-h-0 flex-1",
            tab === "montage" ? "flex-col overflow-auto p-3" : "items-center justify-center bg-black/90 p-2",
          )}
        >
          {tab === "montage" && montage ? (
            <Montage project={montage.project} episode={montage.episode} />
          ) : tab === "image" ? (
            <img key={src} src={src} alt="" className="max-h-[78vh] max-w-full object-contain" />
          ) : tab === "audio" ? (
            <audio key={src} controls autoPlay src={src} className="w-full max-w-xl" />
          ) : (
            <video key={src} controls autoPlay src={src} className="max-h-[78vh] max-w-full" />
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** CSV как таблица: числа вправо, длинные ячейки не растягивают страницу. */
export function CsvTable({ table }: { table: { columns: string[]; rows: string[][] } }) {
  const numeric = table.columns.map((_, i) =>
    table.rows.length > 0 && table.rows.every((r) => r[i] === undefined || r[i] === "" || !Number.isNaN(Number(r[i]))),
  );
  return (
    <div className="max-h-[70vh] overflow-auto rounded-lg border border-line">
      <table className="w-full border-collapse text-[12px]">
        <thead className="sticky top-0 bg-panel">
          <tr>
            {table.columns.map((c, i) => (
              <th
                key={i}
                className={cn(
                  "border-b border-line px-2 py-1.5 text-left text-[11px] font-medium uppercase tracking-wide text-ink-3 whitespace-nowrap",
                  numeric[i] ? "text-right" : "",
                )}
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r, ri) => (
            <tr key={ri} className="hover:bg-panel-2/60">
              {table.columns.map((_, ci) => (
                <td
                  key={ci}
                  className={cn(
                    "max-w-[24rem] truncate border-b border-line px-2 py-1 align-top",
                    numeric[ci] ? "text-right tabular-nums" : "",
                  )}
                  title={r[ci]}
                >
                  {r[ci] ?? ""}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Money({ spent, budget }: { spent: number | undefined; budget: number | undefined }) {
  const over = spent !== undefined && budget !== undefined && spent > budget;
  return (
    <span className={cn("tabular-nums", over ? "text-bad" : "")}>
      {fmtUsd(spent)}
      {budget !== undefined ? <span className="text-ink-3"> / {fmtUsd(budget)}</span> : null}
    </span>
  );
}
