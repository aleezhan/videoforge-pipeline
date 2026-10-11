// «Возможности» — карта конвейера, а не список. Сверху вниз стадии в порядке
// производства, начиная с анализа (он идёт до проекта); на каждой стадии узлы:
// сделанное сплошной рамкой, запланированное пунктиром — видно, где конвейер
// растёт. Два workflow — параллельные дорожки, сходятся на монтаже. Тексты «что
// это» и «как включить» живут в карточке записи, здесь только заголовки.
// Записи одной темы (`topic`: четыре записи про обложку) стоят одной карточкой
// с деталями внутри, а не четырьмя узлами в ряд — иначе стадия с тринадцатью
// записями читалась с телефона как длинный список, а не как одна возможность.

import { useState } from "react";
import { Link } from "react-router";
import { ArrowDown, CircleCheck, Film, Gauge, Lightbulb, Sparkles, type LucideIcon } from "lucide-react";
import { useApi, type FeatureItem, type FeaturesOverview } from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Card, cn, Mono } from "@/components/ui/primitives";
import { AREA_ICONS } from "@/lib/icons";
import { OPEN_STATUSES } from "./Plans";

const ICONS: Record<string, LucideIcon> = {
  ...AREA_ICONS,
  material: Film, // не область записей, а ряд с двумя дорожками
};

interface Step {
  area: string;
  title?: string; // короткое имя для карты; без него — название области из API
  hint?: string;
}

interface Lane {
  id: string;
  title: string;
  hint: string;
  steps: Step[];
}

type Row = { kind: "step"; step: Step } | { kind: "lanes"; title: string; hint: string; lanes: Lane[] };

// Порядок стадий на карте. Анализ стоит до всего: прежде чем заводить проект,
// смотрят, что работает у других. Озвучка раньше картинок — ритм задаёт голос.
const FLOW: Row[] = [
  { kind: "step", step: { area: "intel", title: "Анализ каналов", hint: "до проекта: что работает у других" } },
  { kind: "step", step: { area: "resources", title: "Каст и ресурсы", hint: "персонаж, стиль, голос, приёмы" } },
  {
    kind: "lanes",
    title: "Материал",
    hint: "два пути к готовым сценам",
    lanes: [
      {
        id: "ai_video",
        title: "Ролик с нуля",
        hint: "workflow ai_video",
        steps: [
          { area: "script", title: "Сценарий" },
          { area: "planner", title: "Раскадровка" },
          { area: "audio", title: "Озвучка" },
          { area: "images", title: "Картинки" },
          { area: "video", title: "Видео" },
        ],
      },
      {
        id: "clips_from_long",
        title: "Шортсы из длинного видео",
        hint: "workflow clips_from_long",
        steps: [{ area: "clips", title: "Источник → моменты → отрезок" }],
      },
    ],
  },
  { kind: "step", step: { area: "edit", title: "Монтаж", hint: "план монтажа, эффекты, звуки" } },
  { kind: "step", step: { area: "assemble", title: "Сборка", hint: "субтитры, музыка, замеры финала" } },
  { kind: "step", step: { area: "qa", title: "Проверка", hint: "ворота перед публикацией" } },
  { kind: "step", step: { area: "publish", title: "Публикация" } },
  { kind: "step", step: { area: "accounts", title: "Аккаунты", hint: "статистика → обратно в анализ" } },
];

// Не стадии, а то, на чём всё стоит. Здесь варианты не нужны: одна карточка
// на область, сделанное свёрнуто в счётчик, запланированное — подпунктом.
const AROUND: Step[] = [
  { area: "gpu", title: "Своя карта", hint: "кадры и клипы на арендованной GPU" },
  { area: "ui", title: "Веб-приложение", hint: "пульт в браузере, отчёты, прогноз" },
  { area: "other", title: "Правила конвейера", hint: "ворота, бюджет, идемпотентность" },
];

// Ширина узла: на широком экране фиксированная (ряд вариантов читается как
// ряд), на телефоне — половина строки у узла и вся строка у темы. Иначе
// узел в 160 px не влезал в колонку карты и распирал страницу вбок.
const NODE_W = "w-40 max-md:w-[calc(50%-0.375rem)]";
const TOPIC_W = "w-64 max-md:w-full";

function isOpen(f: FeatureItem): boolean {
  return OPEN_STATUSES.includes(f.status);
}

/** Записи одной темы — одной карточкой: заголовок темы, сделанное списком,
 * планы пунктирными строками. Клик по строке открывает запись. */
function TopicNode({ title, items }: { title: string; items: FeatureItem[] }) {
  const done = items.filter((f) => !isOpen(f));
  const open = items.filter(isOpen);
  return (
    <div className={cn("flex flex-col gap-2 rounded-xl border border-line bg-panel px-3 py-2.5", TOPIC_W)}>
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[13px] font-semibold leading-snug">{title}</div>
        {done.length ? (
          <div className="flex shrink-0 items-center gap-1 text-[11px] text-ok">
            <CircleCheck className="h-3.5 w-3.5" />
            умеем {done.length}
          </div>
        ) : null}
      </div>
      <ul className="space-y-1 border-l border-line pl-2.5 text-[12px] leading-snug">
        {done.map((f) => (
          <li key={f.id}>
            <Link to={`/features/${f.id}`} title={f.summary} className="text-ink-2 hover:text-ink hover:underline">
              {f.title}
            </Link>
          </li>
        ))}
        {open.map((f) => (
          <li key={f.id}>
            <Link to={`/features/${f.id}`} title={f.summary} className="text-ink-3 hover:text-ink hover:underline">
              <Lightbulb className="mr-1 inline h-3 w-3 text-warn" />
              <span className="text-warn">{f.status_title}: </span>
              {f.title}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Узлы стадии: темы карточками в порядке первого появления, записи без темы —
 * узлами сами по себе. Порядок внутри уже задан API: сделанное, потом планы. */
function stageNodes(items: FeatureItem[]) {
  const topics = new Map<string, { title: string; items: FeatureItem[] }>();
  const singles: FeatureItem[] = [];
  for (const f of items) {
    if (!f.topic) {
      singles.push(f);
      continue;
    }
    const group = topics.get(f.topic) ?? { title: f.topic_title || f.topic, items: [] };
    group.items.push(f);
    topics.set(f.topic, group);
  }
  return { topics: [...topics.entries()], singles };
}

function Node({ f }: { f: FeatureItem }) {
  const open = isOpen(f);
  return (
    <Link
      to={`/features/${f.id}`}
      title={f.summary}
      className={cn(
        "flex flex-col gap-1.5 rounded-xl border bg-panel px-3 py-2.5 transition-colors hover:border-ink-3",
        NODE_W,
        open ? "border-dashed border-ink-3/60" : "border-line",
      )}
    >
      <div className={cn("flex items-center gap-1.5 text-[11px]", open ? "text-warn" : "text-ok")}>
        {open ? <Lightbulb className="h-3.5 w-3.5" /> : <CircleCheck className="h-3.5 w-3.5" />}
        {open ? f.status_title : "умеем"}
      </div>
      <div className={cn("text-[13px] font-medium leading-snug", open ? "text-ink-2" : "")}>{f.title}</div>
    </Link>
  );
}

/** Узлы стадии в одной рамке: варианты одного уровня, равномерно по ширине,
 * одной высоты в строке (flex-wrap растягивает элементы строки). */
function Nodes({ items, inner }: { items: FeatureItem[]; inner?: boolean }) {
  const { topics, singles } = stageNodes(items);
  const count = topics.length + singles.length;
  return (
    <div
      className={cn(
        "flex flex-wrap items-stretch gap-3 rounded-2xl border p-3",
        inner ? "border-line/50 bg-panel/60" : "border-line/70 bg-panel-2/30",
        count > 1 ? "justify-evenly" : "justify-center",
      )}
    >
      {count ? (
        <>
          {topics.map(([id, group]) => (
            <TopicNode key={id} title={group.title} items={group.items} />
          ))}
          {singles.map((f) => (
            <Node key={f.id} f={f} />
          ))}
        </>
      ) : (
        <div className="rounded-xl border border-dashed border-line px-3 py-2.5 text-xs text-ink-3">пока пусто</div>
      )}
    </div>
  );
}

function StepLabel({ step, title }: { step: Step; title: string }) {
  const Icon = ICONS[step.area] ?? Gauge;
  return (
    <div className="flex items-start gap-2.5">
      <span className="shrink-0 rounded-lg border border-line bg-panel-2 p-1.5 text-ink-2">
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0">
        <div className="text-[13px] font-semibold leading-tight">{step.title ?? title}</div>
        {step.hint ? <div className="mt-0.5 text-[11px] leading-snug text-ink-3">{step.hint}</div> : null}
      </div>
    </div>
  );
}

/** Карточка области вне конвейера: заголовок, «умеем N» со списком под
 * спойлером и запланированное подпунктами. Доделанный подпункт уходит в список,
 * карточка становится целой. */
function AroundCard({ step, title, items }: { step: Step; title: string; items: FeatureItem[] }) {
  const done = items.filter((f) => !isOpen(f));
  const open = items.filter(isOpen);
  return (
    <Card className="flex flex-col gap-3 p-4">
      <StepLabel step={step} title={title} />
      {done.length ? (
        <details className="group text-xs">
          <summary className="flex cursor-pointer list-none select-none items-center gap-1.5 text-ok [&::-webkit-details-marker]:hidden">
            <CircleCheck className="h-3.5 w-3.5" />
            умеем {done.length}
            <span className="text-ink-3 group-open:hidden">· что именно</span>
          </summary>
          <ul className="mt-2 space-y-1 border-l border-line pl-3">
            {done.map((f) => (
              <li key={f.id}>
                <Link to={`/features/${f.id}`} title={f.summary} className="text-ink-2 hover:text-ink hover:underline">
                  {f.title}
                </Link>
              </li>
            ))}
          </ul>
        </details>
      ) : (
        <div className="text-xs text-ink-3">пока пусто</div>
      )}
      {open.map((f) => (
        <Link
          key={f.id}
          to={`/features/${f.id}`}
          title={f.summary}
          className="flex items-start gap-2 rounded-xl border border-dashed border-ink-3/60 px-3 py-2 text-xs text-ink-2 transition-colors hover:border-ink-3"
        >
          <Lightbulb className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn" />
          <span>
            <span className="text-warn">{f.status_title}: </span>
            {f.title}
          </span>
        </Link>
      ))}
    </Card>
  );
}

// Строка карты: слева подпись стадии, справа её узлы. На телефоне колонки
// не расходятся — подпись встаёт над узлами, иначе правой колонке
// оставалось 147 px на узлы шириной 160 и карта торчала за край экрана.
const GRID = "grid gap-x-5 gap-y-2 md:grid-cols-[11rem_1fr] md:items-center";

function Arrow({ inner }: { inner?: boolean }) {
  const arrow = (
    <div className={cn("flex justify-center text-ink-3", inner ? "py-1.5" : "py-4")}>
      <ArrowDown className={inner ? "h-4 w-4" : "h-6 w-6"} />
    </div>
  );
  if (inner) return arrow;
  return (
    <div className={GRID}>
      {/* пустая клетка под подписью стадии; на телефоне колонки одна, и лишняя
          клетка дала бы пустую строку */}
      <div className="max-md:hidden" />
      {arrow}
    </div>
  );
}

export default function Capabilities() {
  const q = useApi<FeaturesOverview>("/api/features");
  const [showPlans, setShowPlans] = useState(true);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const { items, areas, counts } = q.data!;
  const areaTitle = new Map(areas.map((a) => [a.id, a.title]));
  const byArea = (area: string) =>
    items
      .filter((f) => f.area === area && (f.status === "done" || (showPlans && isOpen(f))))
      .sort((a, b) => Number(isOpen(a)) - Number(isOpen(b)));
  const placed = new Set<string>();
  for (const row of FLOW) {
    if (row.kind === "step") placed.add(row.step.area);
    else for (const lane of row.lanes) for (const s of lane.steps) placed.add(s.area);
  }
  for (const s of AROUND) placed.add(s.area);
  const leftover = [...new Set(items.map((f) => f.area))].filter((a) => !placed.has(a));
  const openCount = counts.in_progress + counts.planned + counts.idea;

  return (
    <>
      <PageHeader
        icon={Sparkles}
        title="Возможности"
        meta={
          <span>
            карта конвейера: что умеем на каждой стадии · умеем {counts.done} · в{" "}
            <Link to="/plans" className="underline hover:text-ink">
              Планах
            </Link>{" "}
            ещё {openCount}
          </span>
        }
        actions={
          <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={showPlans}
              onChange={(e) => setShowPlans(e.target.checked)}
              className="accent-current"
            />
            показывать планы пунктиром
          </label>
        }
      >
        <div className="flex flex-wrap items-center gap-3 text-[11px] text-ink-3">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-3 w-5 rounded border border-line bg-panel" /> умеем
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-3 w-5 rounded border border-dashed border-ink-3/60" /> в планах
          </span>
          <span>
            узлы в одной рамке — варианты одной стадии · карточка с заголовком — одна тема, записи внутри · клик открывает
            запись
          </span>
        </div>
      </PageHeader>

      <div className="space-y-0">
        {FLOW.map((row, i) => (
          <div key={i}>
            {i > 0 ? <Arrow /> : null}
            {row.kind === "step" ? (
              <div className={GRID}>
                <StepLabel step={row.step} title={areaTitle.get(row.step.area) ?? row.step.area} />
                <Nodes items={byArea(row.step.area)} />
              </div>
            ) : (
              <div className={GRID}>
                <StepLabel step={{ area: "material", title: row.title, hint: row.hint }} title={row.title} />
                {/* левой дорожке пять стадий и до семи узлов в ряд, правой один —
                    правая получает фиксированную ширину под один узел, остальное левой */}
                <div className="grid gap-3 md:grid-cols-[1fr_17rem]">
                  {row.lanes.map((lane) => (
                    <Card key={lane.id} className="bg-panel-2/40 p-3">
                      <div className="mb-3 flex flex-col gap-1 px-1">
                        <div className="text-[13px] font-semibold">{lane.title}</div>
                        <Mono className="self-start text-[11px]">{lane.hint}</Mono>
                      </div>
                      <div className="space-y-0">
                        {lane.steps.map((s, j) => (
                          <div key={s.area}>
                            {j > 0 ? <Arrow inner /> : null}
                            <div className="mb-2 flex justify-center">
                              <StepLabel step={s} title={areaTitle.get(s.area) ?? s.area} />
                            </div>
                            <Nodes items={byArea(s.area)} inner />
                          </div>
                        ))}
                      </div>
                    </Card>
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      <section className="mt-8 space-y-3">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-[15px] font-semibold tracking-tight">Поверх конвейера</h2>
          <div className="text-xs text-ink-3">не стадии, а то, на чём всё стоит</div>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {[...AROUND, ...leftover.map((a) => ({ area: a }))].map((s) => (
            <AroundCard key={s.area} step={s} title={areaTitle.get(s.area) ?? s.area} items={byArea(s.area)} />
          ))}
        </div>
      </section>
    </>
  );
}
