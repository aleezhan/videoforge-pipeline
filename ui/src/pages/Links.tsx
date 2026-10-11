// «Полезные источники»: чужие сайты, сервисы, каналы и статьи из links/.
// Закладки, а не планы — возможности инструмента живут в «Планах» и
// «Возможностях». Карточки сгруппированы по виду; ссылка открывается наружу
// прямо отсюда, карточка ведёт на разбор «зачем нам это».

import { Link as RouterLink } from "react-router";
import { Bookmark, ExternalLink, Gauge } from "lucide-react";
import { useApi, type LinkItem, type LinkKind, type LinksOverview } from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, Empty, IconTile, Mono, Section } from "@/components/ui/primitives";
import { plural } from "@/lib/format";
import { AREA_ICONS, LINK_KIND_ICONS } from "@/lib/icons";
import { fmtDay } from "./Plans";

/** Область записи с её иконкой — та же карта, что у планов и возможностей. */
export function LinkAreaBadge({ l }: { l: Pick<LinkItem, "area" | "area_title"> }) {
  const Icon = AREA_ICONS[l.area] ?? Gauge;
  return (
    <Badge>
      <Icon className="h-3 w-3" />
      {l.area_title}
    </Badge>
  );
}

/** Ссылка наружу: клик по ней не должен открывать карточку под ней. */
export function ExternalUrl({ l }: { l: Pick<LinkItem, "url" | "host"> }) {
  return (
    <a
      href={l.url}
      target="_blank"
      rel="noreferrer noopener"
      onClick={(e) => e.stopPropagation()}
      className="inline-flex items-center gap-1 text-xs text-ink-3 hover:text-ink hover:underline"
    >
      {l.host}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

function LinkCard({ l }: { l: LinkItem }) {
  const Icon = LINK_KIND_ICONS[l.kind] ?? Bookmark;
  return (
    <Card className="h-full p-4 transition-colors hover:border-ink-3">
      <div className="flex items-start gap-3">
        <IconTile icon={Icon} />
        <div className="min-w-0 flex-1">
          <RouterLink to={`/links/${l.id}`} className="font-semibold hover:underline">
            {l.title}
          </RouterLink>
          <div className="mt-0.5">
            <ExternalUrl l={l} />
          </div>
        </div>
      </div>
      {l.summary ? <div className="mt-3 line-clamp-4 whitespace-pre-line text-xs text-ink-2">{l.summary}</div> : null}
      <div className="mt-3 flex flex-wrap items-center gap-1">
        <LinkAreaBadge l={l} />
        {l.tags.map((t) => (
          <Badge key={t}>{t}</Badge>
        ))}
        <span className="text-[11px] text-ink-3">смотрели {fmtDay(l.checked || l.added)}</span>
      </div>
      {l.problems.map((p) => (
        <div key={p} className="mt-1 text-xs text-bad">
          {p}
        </div>
      ))}
    </Card>
  );
}

export default function Links() {
  const q = useApi<LinksOverview>("/api/links");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const { items, kinds, total } = q.data!;
  return (
    <>
      <PageHeader
        icon={Bookmark}
        title="Полезные источники"
        meta={
          <span>
            чужие сайты, сервисы, каналы и статьи, на которые стоит вернуться · {plural(total, "запись", "записи", "записей")}
          </span>
        }
      />
      {total === 0 ? (
        <Empty>
          Пусто. Записать: <Mono>videoforge link new &lt;id&gt; --title "…" --url https://… --kind service --why "…"</Mono>
        </Empty>
      ) : null}
      {kinds.map(({ id, title }) => {
        const group = items.filter((l) => l.kind === (id as LinkKind));
        if (!group.length) return null;
        return (
          <Section key={id} title={title} icon={LINK_KIND_ICONS[id]} aside={plural(group.length, "запись", "записи", "записей")}>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {group.map((l) => (
                <LinkCard key={l.id} l={l} />
              ))}
            </div>
          </Section>
        );
      })}
      <div className="text-xs text-ink-3">
        Каждая запись — файл <Mono>links/&lt;id&gt;.yaml</Mono>. Это закладки: что инструмент умеет и что решили сделать —
        в{" "}
        <RouterLink to="/plans" className="underline hover:text-ink">
          Планах
        </RouterLink>{" "}
        и{" "}
        <RouterLink to="/capabilities" className="underline hover:text-ink">
          Возможностях
        </RouterLink>
        .
      </div>
    </>
  );
}
