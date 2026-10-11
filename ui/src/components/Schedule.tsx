// Расписание публикаций проекта: слоты дня, план «когда → что», запас и
// предупреждения. Всё это — `queue.yaml` проекта плюс манифесты эпизодов и
// след тика планировщика (`queue.tick.json`); своего состояния у страницы нет,
// правка слотов — PATCH тем же файлом, что правит `videoforge queue --times`.
//
// Задача планировщика Windows часов не знает: она тикает раз в 15 минут и
// спрашивает файл, поэтому новое время вступает само, без перерегистрации.
// Слот — начало окна: секунду выхода внутри `spread_min` минут считает
// `core/queue.publish_at`, и план здесь показывает её, а не слот.

import { useState } from "react";
import { Link } from "react-router";
import { AlertTriangle, CalendarClock, Info, Pause, Play, Plus, X } from "lucide-react";
import { patchJson, type Notice, type PlanRow, type QueueRow, type QueueView, type RunwayLevel } from "@/api";
import { Badge, Card, CardBody, Details, Mono, Section, Stat, Table, Td, Th, cn, type Tone } from "@/components/ui/primitives";
import { fmtDateTime, plural } from "@/lib/format";

const RUNWAY_TONE: Record<RunwayLevel, Tone> = { ok: "ok", warn: "warn", bad: "bad", none: "neutral" };
const STATE_TONE: Record<QueueRow["state"], Tone> = {
  published: "ok",
  ready: "info",
  waiting: "warn",
  unfinished: "neutral",
};

/** Запас в днях: меньше суток — часами, дальше — «1.6 дн.», той же цифрой,
 * что в предупреждении (округление до половины дня давало 1.5 против 1.6 рядом). */
export function fmtRunwayDays(days: number | null): string {
  if (days === null) return "—";
  if (days < 1) return `${Math.max(1, Math.round(days * 24))} ч`;
  return `${days.toFixed(1).replace(/\.0$/, "")} дн.`;
}

export function NoticeLine({ n }: { n: Notice }) {
  const cls =
    n.level === "bad"
      ? "border-bad/30 bg-bad-soft text-bad"
      : n.level === "warn"
        ? "border-warn/30 bg-warn-soft text-warn"
        : "border-info/30 bg-info-soft text-info";
  const Icon = n.level === "info" ? Info : AlertTriangle;
  return (
    <div className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-[13px]", cls)}>
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{n.text}</span>
    </div>
  );
}

function PlanTable({ rows }: { rows: PlanRow[] }) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Когда</Th>
          <Th>Что уйдёт</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.at} className={r.episode ? "hover:bg-panel-2/60" : ""}>
            <Td className="whitespace-nowrap tabular-nums">
              {fmtDateTime(r.at)}
              {r.pending ? (
                <Badge tone="info" className="ml-2" title="слот уже прошёл: уйдёт на ближайшем тике планировщика">
                  ждёт тика
                </Badge>
              ) : null}
            </Td>
            <Td>
              {r.episode && r.ref ? (
                <>
                  <Link to={`/projects/${r.ref.replace("/", "/episodes/")}`} className="font-medium hover:underline">
                    {r.title || r.episode}
                  </Link>
                  <div className="text-xs text-ink-3">
                    <Mono>{r.episode}</Mono>
                  </div>
                </>
              ) : (
                <span className="text-warn">— нечего: очередь кончилась</span>
              )}
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function TimesEditor({
  view,
  project,
  onSaved,
}: {
  view: QueueView;
  project: string;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  const { times, paused } = view.schedule;

  async function save(next: { times: string[]; paused: boolean }) {
    setBusy(true);
    setFailed("");
    try {
      await patchJson<QueueView>(`/api/projects/${encodeURIComponent(project)}/schedule`, next);
      onSaved();
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-ink-3">слоты дня:</span>
        {times.length ? (
          times.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 rounded-md border border-line bg-panel-2 px-2 py-0.5 font-mono text-[12px]">
              {t}
              <button
                type="button"
                title={`убрать слот ${t}`}
                disabled={busy}
                onClick={() => save({ times: times.filter((x) => x !== t), paused })}
                className="text-ink-3 hover:text-bad disabled:opacity-40"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))
        ) : (
          <span className="text-xs text-ink-3">нет — ролики уходят только по команде руками</span>
        )}
        <span className="inline-flex items-center gap-1">
          <input
            type="time"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={busy}
            className="rounded-md border border-line bg-panel px-2 py-0.5 font-mono text-[12px]"
            aria-label="новый слот"
          />
          <button
            type="button"
            title="добавить слот"
            disabled={busy || !draft}
            onClick={() => {
              save({ times: [...times, draft], paused });
              setDraft("");
            }}
            className="inline-flex items-center gap-1 rounded-md border border-line bg-panel px-2 py-0.5 text-[12px] hover:border-ink-3 disabled:opacity-40"
          >
            <Plus className="h-3 w-3" /> добавить
          </button>
        </span>
        {times.length ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => save({ times, paused: !paused })}
            className={cn(
              "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[12px] disabled:opacity-40",
              paused ? "border-ok/40 text-ok hover:bg-ok-soft" : "border-line text-ink-2 hover:border-ink-3",
            )}
            title={paused ? "снять паузу: слоты снова выполняются" : "пауза: слоты стоят, ничего не выходит само"}
          >
            {paused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />}
            {paused ? "продолжить" : "пауза"}
          </button>
        ) : null}
      </div>
      {failed ? <div className="text-xs text-bad">{failed}</div> : null}
    </div>
  );
}

export function ScheduleSection({ project, view, onSaved }: { project: string; view: QueueView; onSaved: () => void }) {
  const rw = view.runway;
  const tick = view.tick;
  const queued = view.items.filter((i) => i.state !== "published");
  return (
    <Section
      title="Расписание публикаций"
      icon={CalendarClock}
      aside={
        <span>
          queue.yaml · тик планировщика каждые {view.tick_every_min} мин
          {view.schedule.paused ? " · ПАУЗА" : ""}
        </span>
      }
    >
      {view.notices.length ? (
        <div className="space-y-1.5">
          {view.notices.map((n) => (
            <NoticeLine key={n.text} n={n} />
          ))}
        </div>
      ) : null}
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="в день"
          value={view.schedule.per_day || "—"}
          hint={
            view.schedule.times.length
              ? view.schedule.times.join(" · ") +
                (view.schedule.spread_min ? ` · каждый — окно до ${view.schedule.spread_min} мин` : "")
              : "слоты не заданы"
          }
        />
        <Stat
          label="готовых"
          value={rw.ready}
          hint={
            <>
              {rw.waiting ? `ждут ворот: ${rw.waiting}` : null}
              {rw.waiting && rw.unfinished ? " · " : null}
              {rw.unfinished ? `не собрано: ${rw.unfinished}` : null}
              {!rw.waiting && !rw.unfinished ? "всё, что есть, одобрено" : null}
            </>
          }
        />
        <Stat
          label="запас"
          value={
            rw.days === null ? (
              "—"
            ) : (
              <Badge tone={RUNWAY_TONE[rw.level]}>{rw.ready === 0 ? "нет" : fmtRunwayDays(rw.days)}</Badge>
            )
          }
          hint={rw.ends ? `кончится ${fmtDateTime(rw.ends)}` : "считается по слотам и готовым"}
        />
      </div>
      <Card>
        <CardBody className="space-y-4 pt-4">
          <TimesEditor view={view} project={project} onSaved={onSaved} />
          {view.plan.length ? (
            <PlanTable rows={view.plan} />
          ) : (
            <div className="text-xs text-ink-3">
              {view.schedule.paused
                ? "Пауза: план не строится, пока слоты стоят."
                : "Плана нет: задай хотя бы один слот — и здесь появится «когда → что»."}
            </div>
          )}
          <div className={cn("text-[11px]", tick?.stale ? "text-bad" : tick?.failed ? "text-warn" : "text-ink-3")}>
            {tick
              ? `планировщик: тик ${fmtDateTime(tick.at)} — ${tick.why}`
              : "планировщик ещё не тикал: задачу ставит run.ps1 -Install (скилл агента `post-today` (приватный, не опубликован))"}
            {tick?.handled && tick.attempts > 1 ? ` · попыток по слоту: ${tick.attempts}` : null}
          </div>
          {queued.length ? (
            <Details summary={`Очередь целиком — ${plural(view.items.length, "эпизод", "эпизода", "эпизодов")}`}>
              <ul className="space-y-1.5 text-[13px]">
                {view.items.map((i) => (
                  <li key={i.episode} className="flex flex-wrap items-center gap-2">
                    <Badge tone={STATE_TONE[i.state]}>{i.state_title}</Badge>
                    <Link to={`/projects/${i.ref.replace("/", "/episodes/")}`} className="hover:underline">
                      {i.title || i.episode}
                    </Link>
                    <Mono>{i.episode}</Mono>
                    {i.note ? <span className="text-xs text-ink-3">{i.note}</span> : null}
                  </li>
                ))}
              </ul>
              <div className="mt-2 text-[11px] text-ink-3">
                Порядок — список <Mono>episodes</Mono> в <span className="font-mono">{view.path}</span>; эпизод, которого там нет,
                встаёт в конец.
              </div>
            </Details>
          ) : null}
        </CardBody>
      </Card>
    </Section>
  );
}
