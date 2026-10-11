import { useParams } from "react-router";
import { Beaker, BookOpen, Boxes, Images, StickyNote } from "lucide-react";
import { useApi, type Technique } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, CardBody, KV, LinkChip, Mono, Section, Table, Td, Th } from "@/components/ui/primitives";
import { DECISION_ICONS } from "@/lib/icons";
import { condText, ExampleStrip, SOUND_DECISIONS } from "./LibraryDecision";

function Obj({ value }: { value: Record<string, unknown> }) {
  const entries = Object.entries(value);
  if (!entries.length) return <span className="text-ink-3">—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {entries.map(([k, v]) => (
        <Badge key={k}>
          {k}={Array.isArray(v) ? v.join("/") : String(v)}
        </Badge>
      ))}
    </span>
  );
}

export default function TechniqueDetail() {
  const { decision = "", id = "" } = useParams();
  const q = useApi<Technique>(`/api/library/${encodeURIComponent(decision)}/${encodeURIComponent(id)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const t = q.data!;
  return (
    <>
      <Crumbs
        items={[
          { to: "/library", label: "Библиотека" },
          { to: `/library/${decision}`, label: t.decision_title },
          { label: t.title },
        ]}
      />
      <PageHeader
        icon={DECISION_ICONS[decision] ?? BookOpen}
        title={t.title}
        meta={
          <>
            <Mono>{t.id}</Mono>
            {t.status === "implemented" ? <Badge tone="ok">умеем</Badge> : <Badge tone="warn">видели, не умеем</Badge>}
          </>
        }
      >
        {t.summary ? <p className="max-w-3xl text-[13px] text-ink-2">{t.summary}</p> : null}
        {t.problems && t.problems.length ? (
          <div className="rounded-lg border border-bad/30 bg-bad-soft px-4 py-2 text-[13px] text-bad">
            {t.problems.map((p) => (
              <div key={p}>{p}</div>
            ))}
          </div>
        ) : null}
      </PageHeader>

      <Section title="Пример" icon={Images} aside="было → стало; videoforge lib render-example или lib example">
        <Card className="p-4">
          <ExampleStrip t={t} sound={SOUND_DECISIONS.has(decision)} />
        </Card>
      </Section>

      <Card>
        <CardBody className="pt-4">
          <KV
            items={[
              { k: "Когда применять", v: <span className="font-mono text-xs">{condText(t)}</span> },
              { k: "applies_when", v: <Obj value={t.applies_when} /> },
              { k: "never_when", v: <Obj value={t.never_when} /> },
              { k: "Параметры", v: <Obj value={t.params} /> },
              { k: "Чем реализован", v: t.implemented_by || <span className="text-ink-3">—</span> },
              { k: "Файл", v: <span className="font-mono text-xs">{t.path}</span> },
            ]}
          />
        </CardBody>
      </Card>

      <Section title="Доказательства" icon={Beaker} aside="без замера приём — мнение">
        <Card>
          <Table>
            <thead>
              <tr>
                <Th>Источник</Th>
                <Th>Замерено</Th>
                <Th>Дата</Th>
                <Th>Вердикт</Th>
              </tr>
            </thead>
            <tbody>
              {t.evidence.map((e, i) => (
                <tr key={i}>
                  <Td className="text-[13px]">{e.source}</Td>
                  <Td className="text-xs text-ink-2">{e.measured || "—"}</Td>
                  <Td className="text-xs text-ink-3">{e.date || "—"}</Td>
                  <Td>
                    <Badge tone={e.verdict === "works" ? "ok" : e.verdict === "failed" ? "bad" : "neutral"}>{e.verdict}</Badge>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      </Section>

      {t.used_by_resources && t.used_by_resources.length ? (
        <Section title="Где применён" icon={Boxes} aside="ресурсы, собранные с этим приёмом">
          <div className="flex flex-wrap gap-2">
            {t.used_by_resources.map((r) => (
              <LinkChip
                key={`${r.project ?? ""}/${r.kind}/${r.id}`}
                to={`/resources/${r.kind}/${encodeURIComponent(r.id)}${r.project ? `?project=${encodeURIComponent(r.project)}` : ""}`}
              >
                <span className="text-ink-3">{r.kind_title}:</span> {r.name}
                {r.project ? <span className="text-ink-3"> · {r.project}</span> : null}
              </LinkChip>
            ))}
          </div>
        </Section>
      ) : null}

      {t.notes ? (
        <Section title="Заметки" icon={StickyNote}>
          <Card>
            <CardBody className="whitespace-pre-wrap pt-4 text-[13px] text-ink-2">{t.notes}</CardBody>
          </Card>
        </Section>
      ) : null}
    </>
  );
}
