import { Link, useParams } from "react-router";
import { Boxes, FolderOpen, Globe } from "lucide-react";
import { useApi, type ResourceCard as ResourceCardT } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader, ResourceExample, ResourcePreview, resourceLink } from "@/components/common";
import { Badge, Card, Empty, Mono, Section, cn } from "@/components/ui/primitives";
import { KIND_ICONS } from "@/lib/icons";

function Tile({ card }: { card: ResourceCardT }) {
  const inner = (
    <Card className={cn("h-full overflow-hidden transition-colors", card.missing ? "border-dashed border-bad/40" : "hover:border-ink-3")}>
      {card.kind === "characters" || card.kind === "locations" || card.kind === "props" ? (
        <ResourcePreview card={card} className="h-40 w-full rounded-none border-0 border-b border-line" />
      ) : card.example_url && card.example_kind !== "audio" ? (
        // витрина стиля идёт до названия: «Кинетические» без картинки не значат
        // ничего. Высокая: вертикальный клип в низкой плитке нечитаем, а витрина,
        // на которой не разобрать текст, не отвечает на вопрос «как это выглядит»
        <ResourceExample card={card} className="h-96 w-full border-b border-line" />
      ) : null}
      <div className="p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="truncate font-medium">{card.name}</div>
          {card.scope === "project" ? <Badge tone="info">{card.project}</Badge> : null}
        </div>
        <div className="text-xs text-ink-3">
          <Mono>{card.id}</Mono>
          {typeof card.portfolio_count === "number" ? <span> · кадров: {card.portfolio_count}</span> : null}
        </div>
        {card.description ? <div className="mt-1 line-clamp-3 text-xs text-ink-2">{card.description}</div> : null}
        {card.error ? <div className="mt-1 text-xs text-bad">{card.error}</div> : null}
        {card.file_url || card.example_kind === "audio" ? (
          <audio
            controls
            preload="none"
            src={card.file_url ?? card.example_url ?? undefined}
            className="mt-2 h-8 w-full"
            onClick={(e) => e.preventDefault()}
          />
        ) : null}
      </div>
    </Card>
  );
  if (card.missing) return inner;
  return (
    <Link to={resourceLink(card)} className="block">
      {inner}
    </Link>
  );
}

export default function ResourceKind() {
  const { kind = "" } = useParams();
  const q = useApi<{ kind: string; title: string; items: ResourceCardT[] }>(`/api/resources/${encodeURIComponent(kind)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const { title, items } = q.data!;
  const global = items.filter((i) => i.scope !== "project");
  const local = items.filter((i) => i.scope === "project");
  // у видов с витриной плитка шире: вертикальный клип в узкой колонке нечитаем
  const wide = items.some((i) => i.example_url && i.example_kind !== "audio");
  const grid = cn("grid gap-3 sm:grid-cols-2 lg:grid-cols-3", wide ? "" : "xl:grid-cols-4");
  return (
    <>
      <Crumbs items={[{ to: "/resources", label: "Ресурсы" }, { label: title }]} />
      <PageHeader icon={KIND_ICONS[kind] ?? Boxes} title={title} meta={<Mono>resources/{kind}/</Mono>} />
      <Section title="Общие" icon={Globe} aside={`${global.length}`}>
        {global.length ? (
          <div className={grid}>
            {global.map((c) => (
              <Tile key={c.id} card={c} />
            ))}
          </div>
        ) : (
          <Empty>
            Пусто. Завести: <Mono>videoforge resource new {kind.replace(/s$/, "")} &lt;id&gt;</Mono> или командой /new-…
          </Empty>
        )}
      </Section>
      {local.length ? (
        <Section title="Проектные" icon={FolderOpen} aside={`${local.length}`}>
          <div className={grid}>
            {local.map((c) => (
              <Tile key={`${c.project}/${c.id}`} card={c} />
            ))}
          </div>
        </Section>
      ) : null}
    </>
  );
}
