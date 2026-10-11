// Полка роликов как на YouTube: переключатель «Длинные | Shorts», длинные —
// строками с широкой обложкой, шортсы — сеткой вертикальных карточек. Одна и
// та же полка показывает и ролики площадки (videos.json), и упакованные
// эпизоды проекта — источник разный, вид один.

import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { Play } from "lucide-react";
import { cn, Empty } from "@/components/ui/primitives";
import { fmtCompact, fmtDate, fmtSec } from "@/lib/format";

export interface ShelfItem {
  id: string;
  title: string;
  is_short: boolean;
  thumbnail_url: string | null;
  href?: string; // внешняя ссылка (ролик на площадке)
  to?: string; // внутренний маршрут (эпизод)
  views?: number | null;
  date?: string | null;
  duration_sec?: number | null;
  badges?: ReactNode;
}

type Tab = "long" | "short";

function Thumb({ item, vertical }: { item: ShelfItem; vertical: boolean }) {
  return (
    <div
      className={cn(
        "relative shrink-0 overflow-hidden rounded-lg border border-line bg-panel-2",
        vertical ? "aspect-[9/16] w-full" : "aspect-video w-44 sm:w-60",
      )}
    >
      {item.thumbnail_url ? (
        <img src={item.thumbnail_url} alt="" loading="lazy" className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-ink-3">
          <Play className="h-6 w-6" />
        </div>
      )}
      {item.duration_sec ? (
        <span className="absolute right-1.5 bottom-1.5 rounded bg-black/75 px-1 py-0.5 text-[11px] font-medium text-white">
          {fmtSec(item.duration_sec)}
        </span>
      ) : null}
    </div>
  );
}

function Wrap({ item, className, children }: { item: ShelfItem; className: string; children: ReactNode }) {
  if (item.to) {
    return (
      <Link to={item.to} className={className}>
        {children}
      </Link>
    );
  }
  if (item.href) {
    return (
      <a href={item.href} target="_blank" rel="noreferrer" className={className}>
        {children}
      </a>
    );
  }
  return <div className={className}>{children}</div>;
}

function meta(item: ShelfItem): string {
  const parts: string[] = [];
  if (item.views !== undefined && item.views !== null) parts.push(`${fmtCompact(item.views)} просмотров`);
  if (item.date) parts.push(fmtDate(item.date));
  return parts.join(" · ");
}

function LongRow({ item }: { item: ShelfItem }) {
  return (
    <Wrap item={item} className="group flex gap-4 rounded-xl p-2 transition-colors hover:bg-panel-2/60">
      <Thumb item={item} vertical={false} />
      <div className="min-w-0 py-1">
        <div className="line-clamp-2 text-[15px] font-medium leading-snug group-hover:underline">{item.title || item.id}</div>
        <div className="mt-1 text-xs text-ink-3">{meta(item)}</div>
        {item.badges ? <div className="mt-2 flex flex-wrap gap-1">{item.badges}</div> : null}
      </div>
    </Wrap>
  );
}

function ShortCard({ item }: { item: ShelfItem }) {
  return (
    <Wrap item={item} className="group block">
      <Thumb item={item} vertical />
      <div className="mt-2 line-clamp-2 text-[13px] font-medium leading-snug group-hover:underline">{item.title || item.id}</div>
      <div className="mt-0.5 text-xs text-ink-3">{meta(item)}</div>
      {item.badges ? <div className="mt-1.5 flex flex-wrap gap-1">{item.badges}</div> : null}
    </Wrap>
  );
}

export function VideoShelf({ items, empty }: { items: ShelfItem[]; empty: ReactNode }) {
  const longs = items.filter((i) => !i.is_short);
  const shorts = items.filter((i) => i.is_short);
  // открываем ту вкладку, где что-то есть: у канала из одних шортсов — Shorts
  const [tab, setTab] = useState<Tab>(longs.length || !shorts.length ? "long" : "short");
  if (!items.length) return <Empty>{empty}</Empty>;
  const shown = tab === "long" ? longs : shorts;
  const button = (key: Tab, label: string, n: number) => (
    <button
      type="button"
      onClick={() => setTab(key)}
      className={cn(
        "rounded-full px-3.5 py-1.5 text-[13px] font-medium transition-colors",
        tab === key ? "bg-ink text-panel" : "bg-panel-2 text-ink-2 hover:text-ink",
      )}
    >
      {label} <span className={tab === key ? "opacity-70" : "text-ink-3"}>{n}</span>
    </button>
  );
  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        {button("long", "Длинные", longs.length)}
        {button("short", "Shorts", shorts.length)}
      </div>
      {!shown.length ? (
        <Empty>{tab === "long" ? "Длинных роликов нет" : "Шортсов нет"}</Empty>
      ) : tab === "long" ? (
        <div className="space-y-1">
          {shown.map((i) => (
            <LongRow key={i.id} item={i} />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {shown.map((i) => (
            <ShortCard key={i.id} item={i} />
          ))}
        </div>
      )}
    </div>
  );
}
