// Приёмы одной точки решения — галереей «было → стало», а не таблицей:
// глаз выбирает по картинке, параметры и доказательства живут в карточке.

import { Link, useParams } from "react-router";
import { ArrowRight, BookOpen } from "lucide-react";
import { useApi, type Technique, type TechniqueExample } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, Empty, Mono } from "@/components/ui/primitives";
import { DECISION_ICONS } from "@/lib/icons";

export function condText(t: Technique): string {
  const parts: string[] = [];
  if (t.always && !Object.keys(t.applies_when).length) return "всегда";
  for (const [k, v] of Object.entries(t.applies_when)) parts.push(`${k}=${Array.isArray(v) ? v.join("/") : String(v)}`);
  for (const [k, v] of Object.entries(t.never_when)) parts.push(`НЕ ${k}=${Array.isArray(v) ? v.join("/") : String(v)}`);
  return parts.join(", ") || "всегда";
}

/** Точки, где пример надо слушать: видео с дорожкой-волной играет со звуком и
 * с кнопками, а не немой петлёй. */
export const SOUND_DECISIONS = new Set(["audio_layers", "ending_audio"]);

function Media({ kind, url, label, sound }: { kind: TechniqueExample["kind"]; url: string; label: string; sound: boolean }) {
  // object-contain, не cover: вертикальные шортсы и волны звука не режутся по краям
  const box = "aspect-video w-full rounded-lg border border-line bg-black object-contain";
  return (
    <figure className="min-w-0 flex-1">
      {kind === "image" ? (
        <img src={url} alt={label} loading="lazy" className={box} />
      ) : kind === "video" && sound ? (
        <video src={url} controls preload="metadata" playsInline className={box} />
      ) : kind === "video" ? (
        <video src={url} muted loop autoPlay playsInline preload="metadata" className={box} />
      ) : (
        <audio controls preload="none" src={url} className="w-full" />
      )}
      <figcaption className="mt-1 text-center text-[11px] uppercase tracking-wide text-ink-3">{label}</figcaption>
    </figure>
  );
}

/** «Было → стало». Без before показывается одно «стало»; без примера —
 * пустая рамка с командой, которая его кладёт. */
export function ExampleStrip({ t, sound = false }: { t: Technique; sound?: boolean }) {
  const ex = t.example;
  if (!ex || !ex.after_url) {
    return (
      <div className="flex aspect-[2.2/1] flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-line px-3 text-center text-xs text-ink-3">
        <span>примера пока нет</span>
        <Mono className="text-[11px]">videoforge lib render-example {t.id}</Mono>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-3">
      {ex.before_url ? (
        <>
          <Media kind={ex.kind} url={ex.before_url} label="было" sound={sound} />
          <ArrowRight className="h-6 w-6 shrink-0 text-ink-3" />
        </>
      ) : null}
      <Media kind={ex.kind} url={ex.after_url} label="стало" sound={sound} />
    </div>
  );
}

function TechniqueCard({ t, decision }: { t: Technique; decision: string }) {
  const sound = SOUND_DECISIONS.has(decision);
  return (
    <Link
      to={`/library/${decision}/${t.id}`}
      className="block"
      onClick={(e) => {
        // клик по плееру — это управление плеером, а не переход в карточку
        const tag = (e.target as HTMLElement).tagName;
        if (tag === "AUDIO" || (sound && tag === "VIDEO")) e.preventDefault();
      }}
    >
      <Card className="h-full p-4 transition-colors hover:border-ink-3">
        <ExampleStrip t={t} sound={sound} />
        <div className="mt-3 flex items-start justify-between gap-2">
          <div className="min-w-0 font-semibold leading-snug">{t.title}</div>
          {t.status !== "implemented" ? (
            <Badge tone="warn" className="shrink-0">
              видели, не умеем
            </Badge>
          ) : null}
        </div>
      </Card>
    </Link>
  );
}

export default function LibraryDecision() {
  const { decision = "" } = useParams();
  const q = useApi<{ id: string; title: string; items: Technique[] }>(`/api/library/${encodeURIComponent(decision)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const { title, items } = q.data!;
  return (
    <>
      <Crumbs items={[{ to: "/library", label: "Библиотека" }, { label: title }]} />
      <PageHeader
        icon={DECISION_ICONS[decision] ?? BookOpen}
        title={title}
        meta={
          <span>
            {items.length} · <Mono>library/{decision}/</Mono> · подробности, условия и замеры — в карточке приёма
          </span>
        }
      />
      {items.length ? (
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((t) => (
            <TechniqueCard key={t.id} t={t} decision={decision} />
          ))}
        </div>
      ) : (
        <Empty>
          Приёмов нет. Завести: <Mono>videoforge lib new {decision} &lt;id&gt;</Mono> или командой /new-technique
        </Empty>
      )}
    </>
  );
}
