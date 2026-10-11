import { Link } from "react-router";
import { AudioLines, BookOpen, Captions, Film, ListTodo, Package, type LucideIcon } from "lucide-react";
import { useApi, type DecisionInfo, type LibraryOverview } from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, CardBody, Details, IconTile, Mono, Section, Table, Td, Th } from "@/components/ui/primitives";
import { DECISION_ICONS } from "@/lib/icons";

const GROUP_ICONS: Record<string, LucideIcon> = {
  picture: Film,
  text: Captions,
  sound: AudioLines,
  packaging: Package,
};

function DecisionCard({ d }: { d: DecisionInfo }) {
  return (
    <Link to={`/library/${d.id}`} className="block">
      <Card className="h-full p-4 transition-colors hover:border-ink-3">
        <div className="flex items-start gap-3">
          <IconTile icon={DECISION_ICONS[d.id] ?? BookOpen} />
          <div className="min-w-0">
            <div className="font-semibold">{d.title}</div>
            <div className="text-xs text-ink-3">
              <Mono>{d.id}</Mono>
            </div>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-1">
          {d.implemented ? <Badge tone="ok">умеем {d.implemented}</Badge> : null}
          {d.recorded ? <Badge tone="warn">записано {d.recorded}</Badge> : null}
          {!d.implemented && !d.recorded ? <Badge>пусто</Badge> : null}
        </div>
      </Card>
    </Link>
  );
}

export default function Library() {
  const q = useApi<LibraryOverview>("/api/library");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const lib = q.data!;
  const byId = new Map(lib.decisions.map((d) => [d.id, d]));
  const grouped = new Set(lib.groups.flatMap((g) => g.decisions));
  const rest = lib.decisions.filter((d) => !grouped.has(d.id));
  return (
    <>
      <PageHeader
        icon={BookOpen}
        title="Библиотека приёмов"
        meta={
          <span>
            точка решения → приёмы с условиями и доказательством; всего {lib.total} · умеем{" "}
            {lib.decisions.reduce((n, d) => n + d.implemented, 0)} · видели, но не умеем{" "}
            {lib.decisions.reduce((n, d) => n + d.recorded, 0)}
          </span>
        }
      />
      {lib.groups.map((g) => {
        const cards = g.decisions.map((id) => byId.get(id)).filter((d): d is DecisionInfo => Boolean(d));
        if (!cards.length) return null;
        return (
          <Section key={g.id} title={g.title} icon={GROUP_ICONS[g.id]} aside={`${cards.reduce((n, d) => n + d.implemented, 0)} умеем`}>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {cards.map((d) => (
                <DecisionCard key={d.id} d={d} />
              ))}
            </div>
          </Section>
        );
      })}
      {rest.length ? (
        <Section title="Вне групп">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {rest.map((d) => (
              <DecisionCard key={d.id} d={d} />
            ))}
          </div>
        </Section>
      ) : null}
      {lib.unknown_decisions.length ? (
        <div className="text-xs text-bad">Папки вне словаря точек: {lib.unknown_decisions.join(", ")}</div>
      ) : null}
      <Details icon={ListTodo} summary="Справка: условия, по которым стадия сама подбирает приём к ролику">
        <Table>
          <thead>
            <tr>
              <Th>Ключ</Th>
              <Th>Значения</Th>
            </tr>
          </thead>
          <tbody>
            {lib.conditions.map((c) => (
              <tr key={c.key}>
                <Td className="font-mono text-xs">{c.key}</Td>
                <Td className="text-xs text-ink-2">{c.hint}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
        <CardBody className="px-0 pb-0 pt-3 text-xs text-ink-3">
          Переходы между кадрами и эффекты поверх кадра сейчас живут в плане монтажа (<Mono>edit_plan.json</Mono>), а не в
          библиотеке: новая точка решения заводится осознанно, через <Mono>/new-technique</Mono>.
        </CardBody>
      </Details>
    </>
  );
}
