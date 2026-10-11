import { Link, useParams } from "react-router";
import { Ban, Gauge, StickyNote } from "lucide-react";
import { useApi, type FeatureItem } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, CardBody, KV, LinkChip, Mono, Section } from "@/components/ui/primitives";
import { AREA_ICONS } from "@/lib/icons";
import { AreaBadge, FeatureStatusBadge, fmtDay } from "./Plans";

function Text({ value }: { value: string }) {
  return value ? <span className="whitespace-pre-line">{value}</span> : <span className="text-ink-3">—</span>;
}

export default function FeatureDetail() {
  const { id = "" } = useParams();
  const q = useApi<FeatureItem>(`/api/features/${encodeURIComponent(id)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const f = q.data!;
  const parent = f.status === "done" ? { to: "/capabilities", label: "Возможности" } : { to: "/plans", label: "Планы" };
  return (
    <>
      <Crumbs items={[parent, { label: f.title }]} />
      <PageHeader
        icon={AREA_ICONS[f.area] ?? Gauge}
        title={f.title}
        meta={
          <>
            <Mono>{f.id}</Mono>
            <FeatureStatusBadge f={f} />
            <AreaBadge f={f} />
            {f.topic ? (
              <Badge title="тема внутри области: на карте «Возможности» записи одной темы стоят одной карточкой">
                {f.topic_title}
              </Badge>
            ) : null}
            <span className="text-xs text-ink-3">
              записано {fmtDay(f.recorded)}
              {f.done_at ? ` · сделано ${fmtDay(f.done_at)}` : ""}
            </span>
          </>
        }
      >
        {f.summary ? <p className="max-w-3xl whitespace-pre-line text-[13px] text-ink-2">{f.summary}</p> : null}
        {f.problems.length ? (
          <div className="rounded-lg border border-bad/30 bg-bad-soft px-4 py-2 text-[13px] text-bad">
            {f.problems.map((p) => (
              <div key={p}>{p}</div>
            ))}
          </div>
        ) : null}
      </PageHeader>

      <Card>
        <CardBody className="pt-4">
          <KV
            items={[
              { k: "Что даст", v: <Text value={f.gives} /> },
              { k: "На что влияет", v: <Text value={f.cost} /> },
              {
                k: "Как включить",
                v: f.how_to_use ? (
                  <pre className="whitespace-pre-wrap wrap-break-word font-mono text-[12px] leading-relaxed">{f.how_to_use}</pre>
                ) : (
                  <span className="text-ink-3">{f.status === "done" ? "не записано" : "ещё не сделано"}</span>
                ),
              },
              {
                k: "Нужно прежде",
                v: f.requires.length ? (
                  <span className="flex flex-wrap gap-1">
                    {f.requires.map((r) => (
                      <LinkChip key={r} to={`/features/${r}`}>
                        {r}
                      </LinkChip>
                    ))}
                  </span>
                ) : (
                  <span className="text-ink-3">—</span>
                ),
              },
              { k: "Откуда", v: <Text value={f.source} /> },
              {
                k: "Ссылки",
                v: f.links.length ? (
                  <ul className="space-y-0.5">
                    {f.links.map((l) => (
                      <li key={l}>
                        <a href={l} target="_blank" rel="noreferrer" className="break-all text-xs underline hover:text-ink">
                          {l}
                        </a>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className="text-ink-3">—</span>
                ),
              },
              { k: "Файл", v: <span className="font-mono text-xs">{f.path}</span> },
            ]}
          />
        </CardBody>
      </Card>

      {f.notes ? (
        <Section title={f.status === "dropped" ? "Почему отклонено" : "Заметки"} icon={f.status === "dropped" ? Ban : StickyNote}>
          <Card>
            <CardBody className="whitespace-pre-wrap pt-4 text-[13px] text-ink-2">{f.notes}</CardBody>
          </Card>
        </Section>
      ) : null}
      <div className="text-xs text-ink-3">
        Сменить статус: <Mono>videoforge feature status {f.id} done</Mono> · назад к{" "}
        <Link to={parent.to} className="underline hover:text-ink">
          {parent.label}
        </Link>
      </div>
    </>
  );
}
