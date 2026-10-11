// Карточка полезного источника: что это, зачем нам и сколько стоит. Ссылка
// открывается наружу, остальное — то, что человек записал своими словами.

import { Link as RouterLink, useParams } from "react-router";
import { Bookmark, StickyNote } from "lucide-react";
import { useApi, type LinkItem } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, CardBody, KV, Mono, Section } from "@/components/ui/primitives";
import { LINK_KIND_ICONS } from "@/lib/icons";
import { ExternalUrl, LinkAreaBadge } from "./Links";
import { fmtDay } from "./Plans";

function Text({ value }: { value: string }) {
  return value ? <span className="whitespace-pre-line">{value}</span> : <span className="text-ink-3">—</span>;
}

export default function LinkDetail() {
  const { id = "" } = useParams();
  const q = useApi<LinkItem>(`/api/links/${encodeURIComponent(id)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const l = q.data!;
  return (
    <>
      <Crumbs items={[{ to: "/links", label: "Полезные источники" }, { label: l.title }]} />
      <PageHeader
        icon={LINK_KIND_ICONS[l.kind] ?? Bookmark}
        title={l.title}
        meta={
          <>
            <Mono>{l.id}</Mono>
            <Badge tone="info">{l.kind_title}</Badge>
            <LinkAreaBadge l={l} />
            <ExternalUrl l={l} />
            <span className="text-xs text-ink-3">
              записано {fmtDay(l.added)}
              {l.checked ? ` · смотрели ${fmtDay(l.checked)}` : ""}
            </span>
          </>
        }
      >
        {l.summary ? <p className="max-w-3xl whitespace-pre-line text-[13px] text-ink-2">{l.summary}</p> : null}
        {l.problems.length ? (
          <div className="rounded-lg border border-bad/30 bg-bad-soft px-4 py-2 text-[13px] text-bad">
            {l.problems.map((p) => (
              <div key={p}>{p}</div>
            ))}
          </div>
        ) : null}
      </PageHeader>

      <Card>
        <CardBody className="pt-4">
          <KV
            items={[
              { k: "Зачем нам", v: <Text value={l.why} /> },
              { k: "Сколько стоит", v: <Text value={l.cost} /> },
              {
                k: "Ссылка",
                v: (
                  <a href={l.url} target="_blank" rel="noreferrer noopener" className="break-all text-xs underline hover:text-ink">
                    {l.url}
                  </a>
                ),
              },
              {
                k: "Метки",
                v: l.tags.length ? (
                  <span className="flex flex-wrap gap-1">
                    {l.tags.map((t) => (
                      <Badge key={t}>{t}</Badge>
                    ))}
                  </span>
                ) : (
                  <span className="text-ink-3">—</span>
                ),
              },
              { k: "Откуда", v: <Text value={l.source} /> },
              { k: "Файл", v: <span className="font-mono text-xs">{l.path}</span> },
            ]}
          />
        </CardBody>
      </Card>

      {l.notes ? (
        <Section title="Оговорки" icon={StickyNote}>
          <Card>
            <CardBody className="whitespace-pre-wrap pt-4 text-[13px] text-ink-2">{l.notes}</CardBody>
          </Card>
        </Section>
      ) : null}
      <div className="text-xs text-ink-3">
        Поправить — в файле <Mono>{l.path}</Mono>; открывали давно — обновить <Mono>checked</Mono>, чужой сайт умирает молча ·
        назад к{" "}
        <RouterLink to="/links" className="underline hover:text-ink">
          источникам
        </RouterLink>
      </div>
    </>
  );
}
