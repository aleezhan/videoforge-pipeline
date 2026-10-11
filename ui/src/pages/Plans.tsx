// «Планы»: всё из features/, что ещё не сделано — в работе, решили делать,
// идеи; отклонённое — свёрнуто внизу с причиной. Сделанное живёт на странице
// «Возможности», это тот же список, разделённый по статусу.

import { useState } from "react";
import { Link } from "react-router";
import { Ban, CheckSquare, Gauge, Lightbulb, ListTodo, Square } from "lucide-react";
import {
  postJson,
  useApi,
  type FeatureItem,
  type FeaturesOverview,
  type FeatureStatus,
  type TodoClosed,
  type TodoOverview,
  type TodoTask,
} from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, CardBody, Details, Empty, LinkChip, Mono, Section, Table, Td, Th, type Tone } from "@/components/ui/primitives";
import { plural } from "@/lib/format";
import { AREA_ICONS, FEATURE_STATUS_ICONS } from "@/lib/icons";

export const OPEN_STATUSES: FeatureStatus[] = ["in_progress", "planned", "idea"];

const TONE: Record<FeatureStatus, Tone> = {
  in_progress: "info",
  planned: "accent",
  idea: "neutral",
  done: "ok",
  dropped: "bad",
};

export function FeatureStatusBadge({ f }: { f: Pick<FeatureItem, "status" | "status_title"> }) {
  const Icon = FEATURE_STATUS_ICONS[f.status];
  return (
    <Badge tone={TONE[f.status] ?? "neutral"}>
      {Icon ? <Icon className="h-3 w-3" /> : null}
      {f.status_title}
    </Badge>
  );
}

/** Область записи с её иконкой — той же, что на карте «Возможности». */
export function AreaBadge({ f }: { f: Pick<FeatureItem, "area" | "area_title"> }) {
  const Icon = AREA_ICONS[f.area] ?? Gauge;
  return (
    <Badge>
      <Icon className="h-3 w-3" />
      {f.area_title}
    </Badge>
  );
}

/** Дата записи хранится как YYYY-MM-DD; показываем как есть, без часового пояса. */
export function fmtDay(value: string | null | undefined): string {
  if (!value) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : value;
}

function Row({ f }: { f: FeatureItem }) {
  return (
    <tr className="hover:bg-panel-2/60">
      <Td>
        <Link to={`/features/${f.id}`} className="font-medium hover:underline">
          {f.title}
        </Link>
        <div className="text-xs text-ink-3">
          <Mono>{f.id}</Mono>
        </div>
        {f.summary ? <div className="mt-1 max-w-2xl whitespace-pre-line text-xs text-ink-2">{f.summary}</div> : null}
        {f.problems.map((p) => (
          <div key={p} className="mt-1 text-xs text-bad">
            {p}
          </div>
        ))}
      </Td>
      <Td>
        <AreaBadge f={f} />
        {f.topic ? <div className="mt-1 text-[11px] text-ink-3">{f.topic_title}</div> : null}
      </Td>
      <Td className="max-w-xs whitespace-pre-line text-xs text-ink-2">{f.gives || "—"}</Td>
      <Td className="whitespace-nowrap text-xs text-ink-3">{fmtDay(f.recorded)}</Td>
    </tr>
  );
}

/** Ссылка задачи на запись в Планах: задача — её последний ручной шаг.
 *  Записи уже нет — ссылка всё равно показывается, но словами id: пусть
 *  человек увидит, что строка в todo.md указывает в никуда. */
function FeatureLink({ t }: { t: Pick<TodoTask, "feature" | "feature_title"> }) {
  if (!t.feature) return null;
  return (
    <LinkChip to={`/features/${t.feature}`}>
      <Lightbulb className="h-3 w-3" />
      {t.feature_title || `features/${t.feature}`}
    </LinkChip>
  );
}

/** Задачи человека — первым разделом на странице.
 *
 *  Отдельно от планов: план — это возможность инструмента, задача — действие с
 *  концом («войти», «нажать», «посмотреть»). Показываются рядом, потому что
 *  смотрят на них в одном и том же месте — с телефона.
 *
 *  Код задачи виден крупно: по нему её называют вслух («делаем T-07»). */
function TodoSection() {
  const q = useApi<TodoOverview>("/api/todo");
  const [busy, setBusy] = useState<string>("");
  const [failed, setFailed] = useState<string>("");
  // подсказка после «сделано»: какая запись в Планах ждала этого шага —
  // закрыть её или дописать «осталось» решает человек, пульт запись не трогает
  const [hint, setHint] = useState<string>("");
  if (q.isLoading || q.error || !q.data) return null;
  const todo = q.data;
  if (!todo.open.length && !todo.done.length) return null;

  async function close(task: TodoTask, undo = false) {
    setBusy(task.code);
    setFailed("");
    setHint("");
    try {
      const r = await postJson<TodoClosed>(`/api/todo/${encodeURIComponent(task.code)}/done${undo ? "?undo=true" : ""}`, null);
      setHint(r.hint ?? "");
      await q.refetch();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <Section title="Задачи" icon={ListTodo} aside="todo.md">
      <Card>
        <CardBody className="space-y-3 pt-4">
          {failed ? <div className="text-xs text-bad">{failed}</div> : null}
          {hint ? <div className="rounded-lg border border-line bg-panel-2 px-3 py-2 text-xs text-ink-2">{hint}</div> : null}
          {todo.open.length ? (
            <ul className="space-y-2">
              {todo.open.map((t) => (
                <li key={t.code} className="flex items-start gap-2.5 text-[13px]">
                  <button
                    type="button"
                    title="сделано"
                    disabled={busy === t.code}
                    onClick={() => close(t)}
                    className="mt-0.5 shrink-0 text-ink-3 hover:text-ok disabled:opacity-40"
                  >
                    <Square className="h-4 w-4" />
                  </button>
                  <Mono>{t.code}</Mono>
                  <span className="min-w-0 flex-1">{t.text}</span>
                  {t.tag ? <Badge>{t.tag}</Badge> : null}
                  <FeatureLink t={t} />
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-ink-3">Задач нет — всё, что можно, сделано.</div>
          )}
          {todo.done.length ? (
            <Details summary={`сделано: ${todo.done.length}`}>
              <ul className="space-y-1.5">
                {todo.done.map((t) => (
                  <li key={t.code} className="flex items-start gap-2.5 text-[13px] text-ink-3">
                    <button
                      type="button"
                      title="вернуть в работу"
                      disabled={busy === t.code}
                      onClick={() => close(t, true)}
                      className="mt-0.5 shrink-0 text-ok hover:text-ink-3 disabled:opacity-40"
                    >
                      <CheckSquare className="h-4 w-4" />
                    </button>
                    <Mono>{t.code}</Mono>
                    <span className="min-w-0 flex-1 line-through decoration-ink-3/50">{t.text}</span>
                    <FeatureLink t={t} />
                    {t.done_at ? <span className="whitespace-nowrap text-[11px]">{fmtDay(t.done_at)}</span> : null}
                  </li>
                ))}
              </ul>
            </Details>
          ) : null}
          <div className="text-[11px] text-ink-3">
            Записать: <Mono>videoforge todo add «текст» --tag podcast_clips</Mono> · последний шаг записи из Планов:{" "}
            <Mono>--feature &lt;id&gt;</Mono>
          </div>
        </CardBody>
      </Card>
    </Section>
  );
}

export default function Plans() {
  const q = useApi<FeaturesOverview>("/api/features");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const { items, statuses, counts } = q.data!;
  const open = items.filter((f) => OPEN_STATUSES.includes(f.status));
  const dropped = items.filter((f) => f.status === "dropped");
  const title = (id: FeatureStatus) => statuses.find((s) => s.id === id)?.title ?? id;
  return (
    <>
      <PageHeader
        icon={Lightbulb}
        title="Планы"
        meta={
          <span>
            что решили сделать и что записали как идею; сделанное уходит в{" "}
            <Link to="/capabilities" className="underline hover:text-ink">
              Возможности
            </Link>{" "}
            · открыто {open.length} · умеем {counts.done}
          </span>
        }
      />
      <TodoSection />
      {open.length === 0 ? (
        <Empty>
          Планов нет. Записать: <Mono>videoforge feature new &lt;id&gt; --title "…" --area video --summary "…"</Mono>
        </Empty>
      ) : null}
      {OPEN_STATUSES.map((status) => {
        const group = open.filter((f) => f.status === status);
        if (!group.length) return null;
        return (
          <Section key={status} title={title(status)} icon={FEATURE_STATUS_ICONS[status]} aside={plural(group.length, "запись", "записи", "записей")}>
            <Card>
              <Table>
                <thead>
                  <tr>
                    <Th>Что</Th>
                    <Th>Где</Th>
                    <Th>Что даст</Th>
                    <Th>Записано</Th>
                  </tr>
                </thead>
                <tbody>
                  {group.map((f) => (
                    <Row key={f.id} f={f} />
                  ))}
                </tbody>
              </Table>
            </Card>
          </Section>
        );
      })}
      {dropped.length ? (
        <Details icon={Ban} summary={`Отклонено — ${dropped.length}`}>
          <div className="space-y-3">
            {dropped.map((f) => (
              <div key={f.id} className="text-[13px]">
                <Link to={`/features/${f.id}`} className="font-medium hover:underline">
                  {f.title}
                </Link>{" "}
                <AreaBadge f={f} />
                <div className="mt-0.5 whitespace-pre-line text-xs text-ink-2">{f.notes || f.summary}</div>
              </div>
            ))}
          </div>
        </Details>
      ) : null}
      <div className="text-xs text-ink-3">
        Каждая запись — файл <Mono>features/&lt;id&gt;.yaml</Mono>. Статус меняется командой{" "}
        <Mono>videoforge feature status &lt;id&gt; done</Mono>; сделанное обязано сказать, как его включить.
      </div>
    </>
  );
}
