// Тесты: галерея проверочных роликов. Смотреть, а не читать — поэтому карточка
// это кадр из ролика, а не строка таблицы: заходишь и сразу видишь, что собрано.
// Клик открывает плеер прямо здесь; что именно проверяли — строка из check.md.

import { useState } from "react";
import { Link } from "react-router";
import { Clock, FileQuestion, MonitorPlay, Play, Wrench } from "lucide-react";
import { useApi, type TestCard, type TestsOverview } from "@/api";
import { ErrorBox, Loading, MediaModal, Markdown, PageHeader } from "@/components/common";
import { Badge, Card, Details, Empty, Mono, Section, Stat } from "@/components/ui/primitives";
import { fmtDate, fmtSec, fmtUsd, plural } from "@/lib/format";

function Poster({ card }: { card: TestCard }) {
  const vertical = card.aspect_ratio !== "16:9";
  return (
    <div
      className={`relative overflow-hidden rounded-lg border border-line bg-panel-2 ${
        vertical ? "aspect-9/16" : "aspect-video"
      }`}
    >
      {card.poster_url ? (
        <img src={card.poster_url} alt="" loading="lazy" className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-ink-3">
          <Play className="h-6 w-6" />
        </div>
      )}
      <span className="absolute inset-0 flex items-center justify-center bg-black/0 opacity-0 transition-opacity group-hover:bg-black/35 group-hover:opacity-100">
        <Play className="h-9 w-9 text-white drop-shadow" />
      </span>
      {/* код эпизода в углу: в разговоре ролик зовут «t04», и на карточке это
          должно читаться без открывания — иначе непонятно, о каком речь */}
      <span className="absolute top-1.5 left-1.5 rounded bg-black/75 px-1 py-0.5 text-[11px] font-medium text-white">
        {card.episode}
      </span>
      {card.duration_sec ? (
        <span className="absolute right-1.5 bottom-1.5 rounded bg-black/75 px-1 py-0.5 text-[11px] font-medium text-white tabular-nums">
          {fmtSec(card.duration_sec)}
        </span>
      ) : null}
    </div>
  );
}

function TestTile({ card, onOpen }: { card: TestCard; onOpen: (card: TestCard) => void }) {
  return (
    <div className="group space-y-2">
      <button type="button" onClick={() => onOpen(card)} className="block w-full text-left" title="смотреть">
        <Poster card={card} />
      </button>
      <div className="space-y-1">
        <button type="button" onClick={() => onOpen(card)} className="block text-left text-[14px] font-medium leading-snug hover:underline">
          {card.title}
        </button>
        {card.question ? <div className="line-clamp-2 text-[12px] text-ink-2">{card.question}</div> : null}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-3">
          <span>{fmtDate(card.made_at)}</span>
          <span>·</span>
          <Link to={`/projects/${card.project}/episodes/${card.episode}`} className="hover:text-ink">
            эпизод
          </Link>
          {card.spent_usd ? <span>· {fmtUsd(card.spent_usd)}</span> : null}
          {card.failed_stages.length ? <Badge tone="bad">ошибка: {card.failed_stages.join(", ")}</Badge> : null}
        </div>
      </div>
    </div>
  );
}

function PendingRow({ card }: { card: TestCard }) {
  const stage = card.stages.filter((s) => s.status === "done").at(-1);
  return (
    <Card className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
      <div className="min-w-0">
        <Link to={`/projects/${card.project}/episodes/${card.episode}`} className="font-medium hover:underline">
          {card.title}
        </Link>
        <div className="text-xs text-ink-3">
          <Mono>{card.ref}</Mono>
          {card.question ? ` · ${card.question}` : ""}
        </div>
      </div>
      <div className="flex items-center gap-2 text-xs text-ink-3">
        {card.awaiting_approval ? <Badge tone="warn">ждёт одобрения: {card.awaiting_approval}</Badge> : null}
        {card.failed_stages.length ? <Badge tone="bad">ошибка: {card.failed_stages.join(", ")}</Badge> : null}
        <span>сделано до: {stage ? stage.name : "—"}</span>
      </div>
    </Card>
  );
}

export default function Tests() {
  const q = useApi<TestsOverview>("/api/tests");
  const [open, setOpen] = useState<TestCard | null>(null);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const items = q.data?.items ?? [];
  const pending = q.data?.pending ?? [];
  const projects = q.data?.projects ?? [];
  const seconds = items.reduce((sum, c) => sum + (c.duration_sec ?? 0), 0);
  const notes = items.filter((c) => c.check_md.trim());
  return (
    <>
      <PageHeader
        icon={MonitorPlay}
        title="Тесты"
        meta={<span>ролики, которые отвечают на вопрос «что мы уже умеем». Их смотрят, а не публикуют</span>}
      />
      {projects.length ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Stat label="Готовых роликов" value={items.length} icon={MonitorPlay} hint={`${fmtSec(seconds)} всего`} />
          <Stat label="В работе" value={pending.length} icon={Wrench} />
          <Stat
            label="Полигоны"
            value={projects.length}
            icon={FileQuestion}
            hint={projects.map((p) => p.id).join(", ")}
          />
        </div>
      ) : null}
      <Section title="Готовые" icon={Play} aside={plural(items.length, "ролик", "ролика", "роликов")}>
        {items.length ? (
          <div className="grid grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-4">
            {items.map((c) => (
              <TestTile key={c.ref} card={c} onOpen={setOpen} />
            ))}
          </div>
        ) : (
          <Empty>
            Готовых тестов нет. Завести: <Mono>videoforge project new tests --kind test</Mono>, потом{" "}
            <Mono>videoforge episode new tests/t01 --check "что проверяем"</Mono>
          </Empty>
        )}
      </Section>
      {pending.length ? (
        <Section title="В работе" icon={Clock} aside={`${pending.length}`}>
          <div className="space-y-2">
            {pending.map((c) => (
              <PendingRow key={c.ref} card={c} />
            ))}
          </div>
        </Section>
      ) : null}
      {open ? (
        <MediaModal
          title={
            <span className="flex flex-wrap items-baseline gap-2">
              <span className="font-medium">{open.title}</span>
              {open.question ? <span className="text-ink-3">{open.question}</span> : null}
            </span>
          }
          sources={{ clip: open.video_url }}
          labels={{ clip: "ролик" }}
          montage={{ project: open.project, episode: open.episode }}
          onClose={() => setOpen(null)}
        />
      ) : null}
      {notes.length ? (
        <Section title="Что проверяли" icon={FileQuestion} aside={`${notes.length}`}>
          <div className="space-y-2">
            {notes.map((c) => (
              <Details key={c.ref} summary={c.title}>
                <div className="mb-2 text-[11px] text-ink-3">
                  <Mono>{c.check_path}</Mono>
                </div>
                <Markdown text={c.check_md} />
              </Details>
            ))}
          </div>
        </Section>
      ) : null}
      <div className="text-xs text-ink-3">
        Тест заводится как обычный эпизод: <Mono>videoforge episode new tests/&lt;id&gt; --check "…"</Mono>. Строка на
        карточке — первая строка <Mono>check.md</Mono> рядом с раскадровкой; после просмотра допиши туда, что увидел.
      </div>
    </>
  );
}
