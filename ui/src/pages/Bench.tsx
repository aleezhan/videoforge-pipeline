import { Link } from "react-router";
import { AlertTriangle, ClipboardCheck, Cpu, FlaskConical, FolderOpen, Globe, Layers, Wallet } from "lucide-react";
import { useApi, type BenchCard, type BenchOverview, type BenchTestCard, type FileKind } from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Card, Empty, IconTile, LinkChip, Mono, Section, Stat } from "@/components/ui/primitives";
import { fmtDate, fmtNum, fmtUsd, plural } from "@/lib/format";
import { FILE_KIND_ICONS } from "@/lib/icons";

const KIND_LABEL: Record<FileKind, string> = {
  image: "картинок",
  video: "клипов",
  audio: "аудио",
  doc: "документов",
  table: "таблиц",
  html: "отчётов",
  text: "логов",
  other: "прочих",
};

export function countsLine(counts: Partial<Record<FileKind, number>>): string {
  return (Object.keys(KIND_LABEL) as FileKind[])
    .filter((k) => counts[k])
    .map((k) => `${counts[k]} ${KIND_LABEL[k]}`)
    .join(" · ");
}

/** Счётчики файлов иконками: «картинка 38 · клип 12» читается без слов (страница теста). */
export function KindCounts({ counts }: { counts: Partial<Record<FileKind, number>> }) {
  const kinds = (Object.keys(KIND_LABEL) as FileKind[]).filter((k) => counts[k]);
  if (!kinds.length) return <span className="text-xs text-ink-3">пусто</span>;
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-2">
      {kinds.map((k) => {
        const Icon = FILE_KIND_ICONS[k];
        return (
          <span key={k} className="inline-flex items-center gap-1 tabular-nums" title={`${counts[k]} ${KIND_LABEL[k]}`}>
            <Icon className="h-3.5 w-3.5 text-ink-3" />
            {counts[k]}
          </span>
        );
      })}
    </span>
  );
}

/** «2 ч 06 мин» / «17 мин» — время карты читают в часах и минутах, не в секундах. */
function fmtMinutes(min: number | null | undefined): string {
  if (min === null || min === undefined) return "—";
  const total = Math.round(min);
  if (total < 60) return `${total} мин`;
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m ? `${h} ч ${String(m).padStart(2, "0")} мин` : `${h} ч`;
}

function Row({ icon: Icon, label, children }: { icon: typeof Globe; label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2 text-xs">
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" />
      <span className="w-14 shrink-0 text-ink-3">{label}</span>
      <span className="min-w-0 text-ink-2">{children}</span>
    </div>
  );
}

function TestCard({ t }: { t: BenchTestCard }) {
  const m = t.meta;
  const h = t.hosted;
  const gpu = m?.gpu ?? null;
  const total = (h?.cost_usd ?? 0) + (gpu?.cost_usd ?? 0);
  return (
    <Link to={`/bench/tests/${encodeURIComponent(t.id)}`} className="block">
      <Card className="flex h-full flex-col p-4 transition-colors hover:border-ink-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2.5">
            <IconTile icon={FlaskConical} />
            <div className="min-w-0">
              <div className="text-[15px] font-semibold">{t.id}</div>
              {m?.question ? <div className="text-[13px] text-ink-2">{m.question}</div> : null}
            </div>
          </div>
          <span className="shrink-0 text-xs text-ink-3">{m?.date ? fmtDate(m.date) : t.mtime ? fmtDate(t.mtime) : ""}</span>
        </div>
        {m?.answer ? <p className="mt-2 text-[13px] leading-snug">{m.answer}</p> : null}
        <div className="mt-3 space-y-1.5">
          <Row icon={Globe} label="hosted">
            {h ? (
              <>
                <span className="tabular-nums">
                  {plural(h.calls, "вызов", "вызова", "вызовов")} · {fmtUsd(h.cost_usd)} · {fmtMinutes(h.wall_s / 60)}
                </span>
                {h.completed < h.calls ? <span className="text-bad"> · {h.calls - h.completed} без ответа</span> : null}
                {h.endpoints.length ? (
                  <span className="block text-ink-3">{h.endpoints.map((e) => `${e.id} ×${e.calls}`).join(" · ")}</span>
                ) : null}
              </>
            ) : (
              "нет"
            )}
          </Row>
          <Row icon={Cpu} label="карта">
            {gpu ? (
              <>
                <span className="tabular-nums">
                  {gpu.name ?? "GPU"} · {fmtMinutes(gpu.minutes)} · {fmtUsd(gpu.cost_usd)}
                </span>
                {gpu.pod ? <span className="block text-ink-3">под {gpu.pod}</span> : null}
              </>
            ) : (
              "не поднималась"
            )}
          </Row>
          {m?.models?.length ? (
            <Row icon={Layers} label="модели">
              {m.models.join(" · ")}
            </Row>
          ) : null}
        </div>
        <div className="mt-auto flex items-center justify-between gap-3 pt-3 text-xs text-ink-3">
          <span className="min-w-0 truncate" title={countsLine(t.counts)}>
            {countsLine(t.counts) || "папка пуста"}
          </span>
          <span className="shrink-0 tabular-nums">итого {fmtUsd(total)}</span>
        </div>
      </Card>
    </Link>
  );
}

/** Замер по схеме (bench.yaml): вопрос, бюджет, дубли и оценки — одна карточка. */
function BenchCardView({ b }: { b: BenchCard }) {
  if (b.error) {
    return (
      <Card className="p-4">
        <div className="text-[15px] font-semibold">{b.id}</div>
        <div className="mt-1 text-xs text-bad">схема не читается: {b.error}</div>
      </Card>
    );
  }
  return (
    <Link to={`/bench/${encodeURIComponent(b.id)}`} className="block">
      <Card className="flex h-full flex-col p-4 transition-colors hover:border-ink-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2.5">
            <IconTile icon={ClipboardCheck} />
            <div className="min-w-0">
              <div className="text-[15px] font-semibold">{b.id}</div>
              {b.question ? <div className="text-[13px] text-ink-2">{b.question}</div> : null}
            </div>
          </div>
          <span className="shrink-0 text-xs text-ink-3">{b.date ? fmtDate(b.date) : b.mtime ? fmtDate(b.mtime) : ""}</span>
        </div>
        {b.answer ? <p className="mt-2 text-[13px] leading-snug">{b.answer}</p> : null}
        <div className="mt-3 space-y-1.5">
          <Row icon={Wallet} label="бюджет">
            <span className="tabular-nums">
              {fmtUsd(b.spent_usd ?? 0)} из {fmtUsd(b.budget_usd ?? 0)}
            </span>
          </Row>
          <Row icon={ClipboardCheck} label="дубли">
            <span className="tabular-nums">
              {b.takes ?? 0} · сделано {b.done ?? 0} · упало {b.failed ?? 0} · оценено {b.reviewed ?? 0} · принято {b.accepted ?? 0}
            </span>
          </Row>
          {b.branches?.length ? (
            <Row icon={Layers} label="ветки">
              {b.branches.join(" · ")} · сцен {b.scenes ?? 0}
            </Row>
          ) : null}
          {b.missing_frames?.length ? (
            <Row icon={AlertTriangle} label="ждёт">
              <span className="text-warn">кадра нет у {b.missing_frames.join(", ")} — выбирает человек</span>
            </Row>
          ) : null}
        </div>
      </Card>
    </Link>
  );
}

export default function Bench() {
  const q = useApi<BenchOverview>("/api/bench");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const b = q.data!;
  if (!b.exists) {
    return (
      <>
        <PageHeader icon={FlaskConical} title="Замеры" />
        <Empty>
          Папки полигона нет: <Mono>{b.path}</Mono>
        </Empty>
      </>
    );
  }
  const withoutCard = b.tests.filter((t) => !t.meta).map((t) => t.id);
  return (
    <>
      <PageHeader
        icon={FlaskConical}
        title="Замеры"
        meta={
          <span>
            полигон тестов моделей и карт; папка показывается как есть: <Mono>{b.path}</Mono>
          </span>
        }
      />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat icon={FolderOpen} label="Тестов" value={b.totals.tests} hint="папок в out/" />
        <Stat
          icon={Globe}
          label="Hosted-вызовов"
          value={fmtNum(b.totals.calls)}
          hint={`${fmtUsd(b.totals.hosted_usd)} по runs.csv`}
        />
        <Stat
          icon={Cpu}
          label="Карта"
          value={fmtMinutes(b.totals.gpu_minutes)}
          hint={`${fmtUsd(b.totals.gpu_usd)} по карточкам тестов`}
        />
        <Stat
          icon={Wallet}
          label="Потрачено"
          value={fmtUsd(b.totals.cost_usd)}
          hint={`hosted ${fmtUsd(b.totals.hosted_usd)} + карта ${fmtUsd(b.totals.gpu_usd)} + замеры ${fmtUsd(b.totals.bench_usd)}`}
        />
      </div>

      <Section
        title="Замеры по схеме"
        icon={ClipboardCheck}
        aside="bench.yaml → дубли с происхождением → оценка глазами здесь → сводка; videoforge bench"
      >
        {b.benches.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {b.benches.map((x) => (
              <BenchCardView key={x.id} b={x} />
            ))}
          </div>
        ) : (
          <Empty>
            Замеров по схеме ещё нет: <Mono>videoforge bench new &lt;id&gt; --question «…»</Mono>
          </Empty>
        )}
      </Section>

      <Section title="Тесты" icon={FolderOpen} aside="карточка — tests.yaml, hosted — runs.csv, файлы — out/<тест>/">
        {b.tests.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {b.tests.map((t) => (
              <TestCard key={t.id} t={t} />
            ))}
          </div>
        ) : (
          <Empty>Папки out/ ещё нет — скрипты тестов складывают выходы туда.</Empty>
        )}
      </Section>

      <div className="text-xs text-ink-3">
        Схемы у полигона нет намеренно: каждый тест свой. Скрипты пишут вызовы в <Mono>runs.csv</Mono>, выходы в{" "}
        <Mono>out/&lt;тест&gt;/</Mono>; при закрытии теста человек добавляет карточку в <Mono>tests.yaml</Mono> — вопрос,
        ответ, карта и деньги.
        {withoutCard.length ? <> Без карточки: {withoutCard.join(", ")}.</> : null}{" "}
        <LinkChip to="/bench/file/LOG.md">журнал LOG.md</LinkChip> <LinkChip to="/bench/file/README.md">карта папки</LinkChip>{" "}
        <LinkChip to="/projects">← проекты</LinkChip>
      </div>
    </>
  );
}
