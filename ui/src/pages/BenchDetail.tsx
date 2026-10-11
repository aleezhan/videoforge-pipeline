// Замер по схеме: дубли с полным происхождением (кадр, промпт, граф, веса,
// секунды, деньги) и оценка глазами прямо под плеером. Вердикт уходит в
// takes/<дубль>.verdict.json — свой файл, раннер его не трогает; отпечаток
// прочитанного файла едет обратно, и чужая оценка не затирается (409).
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router";
import { AlertTriangle, Check, ExternalLink, FlaskConical, Image as ImageIcon, Layers, ListChecks, Wallet } from "lucide-react";
import {
  ApiError,
  postJson,
  useApi,
  type BenchCriterion,
  type BenchDetail as BenchDetailT,
  type BenchScene,
  type BenchSummary,
  type BenchTake,
  type VerdictReply,
  type VerdictValue,
} from "@/api";
import { Crumbs, ErrorBox, Loading, Markdown, MediaModal, PageHeader } from "@/components/common";
import { Badge, Card, CardBody, Details, Empty, KV, Mono, Section, Stat, Table, Td, Th } from "@/components/ui/primitives";
import { fmtUsd } from "@/lib/format";

const REVIEWER_KEY = "vf.bench.reviewer";

type Open = { kind: "image" | "video"; url: string; title: string } | null;

const STATUS_LABEL: Record<BenchTake["status"], string> = { done: "сделан", failed: "упал", planned: "план" };
const ERROR_LABEL: Record<string, string> = {
  graph: "граф отвергнут",
  input: "вход негоден",
  oom: "не хватило памяти",
  network: "под не отвечает",
  unknown: "ошибка",
};

function AcceptedBadge({ take }: { take: BenchTake }) {
  if (!take.verdict) return <Badge>не оценён</Badge>;
  if (take.accepted === true) return <Badge tone="ok">принят</Badge>;
  if (take.accepted === false) return <Badge tone="bad">отклонён</Badge>;
  return <Badge tone="warn">оценён не до конца</Badge>;
}

function Toggle({ value, onChange }: { value: VerdictValue | undefined; onChange: (v: VerdictValue) => void }) {
  const opts: { v: VerdictValue; label: string; on: string }[] = [
    { v: "pass", label: "да", on: "bg-ok-soft text-ok border-ok/40" },
    { v: "fail", label: "нет", on: "bg-bad-soft text-bad border-bad/40" },
    { v: "na", label: "н/п", on: "bg-panel-2 text-ink border-ink-3" },
  ];
  return (
    <div className="flex gap-1">
      {opts.map((o) => (
        <button
          key={o.v}
          type="button"
          onClick={() => onChange(o.v)}
          className={
            "rounded-md border px-2 py-0.5 text-[12px] font-medium " +
            (value === o.v ? o.on : "border-line bg-panel text-ink-3 hover:border-ink-3")
          }
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function VerdictForm({ benchId, take, criteria }: { benchId: string; take: BenchTake; criteria: BenchCriterion[] }) {
  const qc = useQueryClient();
  const v = take.verdict;
  const [values, setValues] = useState<Record<string, VerdictValue>>(v?.criteria ?? {});
  const [defects, setDefects] = useState((v?.defect_times ?? []).join(", "));
  const [from, setFrom] = useState(v?.accepted_interval ? String(v.accepted_interval[0]) : "");
  const [to, setTo] = useState(v?.accepted_interval ? String(v.accepted_interval[1]) : "");
  const [notes, setNotes] = useState(v?.notes ?? "");
  const [reviewer, setReviewer] = useState(() => {
    try {
      return v?.reviewer || localStorage.getItem(REVIEWER_KEY) || "";
    } catch {
      return v?.reviewer ?? "";
    }
  });
  const [base, setBase] = useState(take.verdict_base);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const missing = criteria.filter((c) => !values[c.id]).length;

  async function save() {
    setSaving(true);
    setError(null);
    const defect_times = defects
      .split(/[,\s;]+/)
      .map((s) => s.trim())
      .filter(Boolean)
      .map(Number)
      .filter((n) => !Number.isNaN(n));
    const interval = from !== "" && to !== "" ? [Number(from), Number(to)] : null;
    try {
      const reply = await postJson<VerdictReply>(
        `/api/bench/${encodeURIComponent(benchId)}/takes/${encodeURIComponent(take.take_id)}/verdict`,
        { reviewer, criteria: values, defect_times, accepted_interval: interval, notes, base },
      );
      setBase(reply.base);
      setSavedAt(reply.verdict.at);
      try {
        localStorage.setItem(REVIEWER_KEY, reviewer);
      } catch {
        /* приватное окно — имя просто не запомнится */
      }
      await qc.invalidateQueries({ queryKey: [`/api/bench/${encodeURIComponent(benchId)}`] });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError(`${e.message}`);
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3 text-[13px]">
      <div className="space-y-1.5">
        {criteria.map((c) => (
          <div key={c.id} className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div>{c.title}</div>
              {c.hint ? <div className="text-xs text-ink-3">{c.hint}</div> : null}
            </div>
            <Toggle value={values[c.id]} onChange={(val) => setValues((s) => ({ ...s, [c.id]: val }))} />
          </div>
        ))}
      </div>
      {take.image ? null : (
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block text-xs text-ink-3">
          дефекты, секунды через запятую
          <input
            value={defects}
            onChange={(e) => setDefects(e.target.value)}
            placeholder="3.4, 5.5"
            className="mt-1 w-full rounded-md border border-line bg-panel px-2 py-1 text-[13px] text-ink"
          />
        </label>
        <div className="text-xs text-ink-3">
          принятый отрезок, с
          <div className="mt-1 flex items-center gap-1">
            <input
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              placeholder="0"
              className="w-full rounded-md border border-line bg-panel px-2 py-1 text-[13px] text-ink"
            />
            <span>–</span>
            <input
              value={to}
              onChange={(e) => setTo(e.target.value)}
              placeholder="5"
              className="w-full rounded-md border border-line bg-panel px-2 py-1 text-[13px] text-ink"
            />
          </div>
        </div>
      </div>
      )}
      <label className="block text-xs text-ink-3">
        заметка
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={2}
          className="mt-1 w-full rounded-md border border-line bg-panel px-2 py-1 text-[13px] text-ink"
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
          placeholder="кто смотрел"
          className="w-36 rounded-md border border-line bg-panel px-2 py-1 text-[13px] text-ink"
        />
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent-soft px-3 py-1 text-[13px] font-medium text-accent hover:bg-accent/15 disabled:opacity-50"
        >
          <Check className="h-3.5 w-3.5" /> {saving ? "сохраняю…" : "сохранить оценку"}
        </button>
        {missing ? <span className="text-xs text-ink-3">без ответа: {missing} — можно сохранить и вернуться</span> : null}
        {savedAt ? <span className="text-xs text-ok">сохранено</span> : null}
      </div>
      {error ? (
        <div className="flex items-start gap-2 rounded-md border border-bad/40 bg-bad-soft px-3 py-2 text-xs text-bad">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
        </div>
      ) : null}
    </div>
  );
}

function TakeCard({
  benchId,
  take,
  criteria,
  onOpen,
}: {
  benchId: string;
  take: BenchTake;
  criteria: BenchCriterion[];
  onOpen: (o: Open) => void;
}) {
  const m = take.metrics;
  const models = Object.entries(take.method.models);
  return (
    <Card className="flex flex-col overflow-hidden">
      <div className="bg-black">
        {take.video_url ? (
          <video controls preload="metadata" src={take.video_url} className="mx-auto max-h-[440px] w-full object-contain" />
        ) : take.image_url ? (
          <button
            type="button"
            onClick={() => onOpen({ kind: "image", url: take.image_url!, title: take.take_id })}
            className="block w-full"
            title="открыть кадр"
          >
            <img src={take.image_url} alt={take.take_id} className="mx-auto max-h-[440px] w-full object-contain" />
          </button>
        ) : take.status === "failed" ? (
          <div className="px-4 py-6 text-sm text-bad">
            {ERROR_LABEL[take.error_kind] ?? take.error_kind}: {take.error}
          </div>
        ) : (
          <div className="px-4 py-6 text-sm text-ink-3">файла ещё нет</div>
        )}
      </div>
      <CardBody className="space-y-2 pt-3">
        <div className="flex flex-wrap items-center gap-1.5 text-xs">
          <Badge tone="accent">{take.branch}</Badge>
          <Badge>сид {take.seed}</Badge>
          <Badge tone={take.status === "done" ? "ok" : take.status === "failed" ? "bad" : "neutral"}>
            {STATUS_LABEL[take.status]}
          </Badge>
          {take.status === "done" ? (
            <span className="tabular-nums text-ink-3">
              {take.gen_sec.toFixed(0)} с · {fmtUsd(take.cost_usd)}
            </span>
          ) : null}
          {m ? (
            <span className="tabular-nums text-ink-3" title="энергия движения по окнам 0.5 с: медиана · доля тихих окон">
              движение {m.motion_median.toFixed(1)} · тихих {Math.round(m.quiet_share * 100)}%
            </span>
          ) : null}
          <span className="ml-auto">
            <AcceptedBadge take={take} />
          </span>
        </div>

        <Details summary="как получен" icon={ImageIcon}>
          <div className="flex flex-col gap-3 sm:flex-row">
            {take.frame_url ? (
              <button
                type="button"
                onClick={() => onOpen({ kind: "image", url: take.frame_url!, title: take.inputs.frame })}
                className="shrink-0"
                title="стартовый кадр"
              >
                <img src={take.frame_url} alt={take.inputs.frame} className="h-40 rounded-md border border-line object-contain" />
              </button>
            ) : null}
            {take.ref_urls?.map((url, i) =>
              url ? (
                <button
                  key={url}
                  type="button"
                  onClick={() => onOpen({ kind: "image", url, title: take.inputs.refs[i] })}
                  className="shrink-0"
                  title={`референс: ${take.inputs.refs[i]}`}
                >
                  <img src={url} alt={take.inputs.refs[i]} className="h-40 rounded-md border border-line object-contain" />
                </button>
              ) : null,
            )}
            <div className="min-w-0 flex-1 space-y-2">
              <KV
                items={[
                  { k: "метод", v: `${take.method.method} ${take.method.preset}`.trim() },
                  { k: "кадр", v: <Mono>{take.inputs.frame}</Mono> },
                  ...(take.inputs.reference
                    ? [
                        {
                          k: take.method.method === "comfy_upscale" ? "исходный клип" : "референс",
                          v: take.reference_url ? (
                            <a href={take.reference_url} target="_blank" rel="noreferrer" className="underline">
                              <Mono>{take.inputs.reference}</Mono>
                            </a>
                          ) : (
                            <Mono>{take.inputs.reference}</Mono>
                          ),
                        },
                      ]
                    : []),
                  {
                    k: "размер",
                    v: take.image
                      ? take.inputs.size
                      : `${take.inputs.size} · ${take.inputs.frames} кадров · ${take.inputs.fps} fps · ${take.inputs.duration_sec} с`,
                  },
                  ...(take.inputs.refs?.length
                    ? [{ k: "референсы", v: <Mono>{take.inputs.refs.join(", ")}</Mono> }]
                    : []),
                  { k: "сид", v: String(take.seed) },
                  ...(take.method.weights ? [{ k: "веса с пода", v: <Mono>{take.method.weights}</Mono> }] : []),
                  ...(models.length
                    ? [
                        {
                          k: "модели",
                          v: (
                            <div className="space-y-0.5">
                              {models.map(([role, file]) => (
                                <div key={role} className="font-mono text-[11px] text-ink-2">
                                  {role}: {file}
                                </div>
                              ))}
                            </div>
                          ),
                        },
                      ]
                    : []),
                  ...(Object.keys(take.method.params).length
                    ? [{ k: "параметры", v: <Mono>{JSON.stringify(take.method.params)}</Mono> }]
                    : []),
                  ...(take.method.pod_id ? [{ k: "под", v: <Mono>{take.method.pod_id}</Mono> }] : []),
                  { k: "отпечаток", v: <Mono>{take.fingerprint}</Mono> },
                ]}
              />
              <div>
                <div className="text-xs text-ink-3">промпт, слово в слово</div>
                <pre className="mt-1 whitespace-pre-wrap rounded-md border border-line bg-panel-2 px-3 py-2 font-sans text-[12px] leading-snug">
                  {take.inputs.prompt}
                </pre>
                {take.inputs.negative ? (
                  <>
                    <div className="mt-2 text-xs text-ink-3">негатив</div>
                    <pre className="mt-1 whitespace-pre-wrap rounded-md border border-line bg-panel-2 px-3 py-2 font-sans text-[12px] leading-snug">
                      {take.inputs.negative}
                    </pre>
                  </>
                ) : null}
              </div>
              {take.graph_url ? (
                <a href={take.graph_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs underline">
                  <ExternalLink className="h-3 w-3" /> граф ComfyUI как ушёл на под
                </a>
              ) : null}
            </div>
          </div>
        </Details>

        {take.status === "done" ? (
          <Details summary="оценка глазами" icon={ListChecks} open={!take.verdict}>
            <VerdictForm benchId={benchId} take={take} criteria={criteria} />
          </Details>
        ) : null}
      </CardBody>
    </Card>
  );
}

function SceneHeader({ scene, onOpen }: { scene: BenchScene; onOpen: (o: Open) => void }) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row">
      {scene.frame_url ? (
        <button type="button" onClick={() => onOpen({ kind: "image", url: scene.frame_url!, title: scene.frame })} className="shrink-0">
          <img src={scene.frame_url} alt={scene.frame} className="h-32 rounded-md border border-line object-contain" />
        </button>
      ) : (
        <div className="flex h-32 w-20 shrink-0 items-center justify-center rounded-md border border-dashed border-line text-xs text-ink-3">
          кадра нет
        </div>
      )}
      <div className="min-w-0 text-[13px]">
        <div className="text-xs text-ink-3">стартовый кадр {scene.frame} · промпт по умолчанию</div>
        {scene.video ? (
          <div className="text-xs text-ink-3">
            исходный клип{" "}
            {scene.video_url ? (
              <a href={scene.video_url} target="_blank" rel="noreferrer" className="underline">
                {scene.video}
              </a>
            ) : (
              <span>{scene.video} · файла нет</span>
            )}
          </div>
        ) : null}
        <p className="mt-1 leading-snug">{scene.prompt}</p>
        {Object.entries(scene.prompts).map(([b, p]) => (
          <p key={b} className="mt-1 leading-snug">
            <span className="text-xs text-ink-3">{b}: </span>
            {p}
          </p>
        ))}
      </div>
    </div>
  );
}

function num(v: number | null | undefined, d = 2): string {
  return v === null || v === undefined ? "—" : v.toFixed(d);
}

function SummaryTables({ s }: { s: BenchSummary }) {
  return (
    <div className="space-y-4">
      <Table>
        <thead>
          <tr>
            <Th>ветка</Th>
            <Th>дублей</Th>
            <Th>сделано</Th>
            <Th>упало</Th>
            <Th>оценено</Th>
            <Th>принято</Th>
            <Th>доля</Th>
            <Th>$</Th>
            <Th>принятых с</Th>
            <Th>$ / принятая с</Th>
            <Th>с / дубль</Th>
            <Th>провалы</Th>
          </tr>
        </thead>
        <tbody>
          {s.branches.map((b) => (
            <tr key={b.id}>
              <Td>
                <span className="font-medium">{b.id}</span> <span className="text-xs text-ink-3">{b.method} {b.preset}</span>
              </Td>
              <Td className="tabular-nums">{b.takes}</Td>
              <Td className="tabular-nums">{b.done}</Td>
              <Td className="tabular-nums">
                {b.failed}
                {b.error_kinds.length ? <span className="text-xs text-ink-3"> {b.error_kinds.join(", ")}</span> : null}
              </Td>
              <Td className="tabular-nums">
                {b.reviewed}
                {b.partial ? <span className="text-xs text-ink-3"> (+{b.partial} не до конца)</span> : null}
              </Td>
              <Td className="tabular-nums">{b.accepted}</Td>
              <Td className="tabular-nums">{num(b.share)}</Td>
              <Td className="tabular-nums">{fmtUsd(b.cost_usd)}</Td>
              <Td className="tabular-nums">{num(b.accepted_sec, 1)}</Td>
              <Td className="tabular-nums">{num(b.usd_per_accepted_sec, 4)}</Td>
              <Td className="tabular-nums">{num(b.gen_sec_mean, 0)}</Td>
              <Td className="text-xs">
                {Object.entries(b.fails)
                  .filter(([, n]) => n)
                  .map(([c, n]) => `${c} ${n}`)
                  .join(", ") || "—"}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="mb-1 text-xs text-ink-3">
            лестница: принято / оценено из сделанных; ✓ — держится правило {s.criteria.length ? "" : ""}
          </div>
          <Table>
            <thead>
              <tr>
                <Th>сцена</Th>
                {s.branches.map((b) => (
                  <Th key={b.id}>{b.id}</Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {s.ladder.map((row) => (
                <tr key={row.scene}>
                  <Td>
                    {row.scene} <span className="text-xs text-ink-3">{row.title}</span>
                  </Td>
                  {s.branches.map((b) => {
                    const r = row.branches[b.id];
                    return (
                      <Td key={b.id} className="tabular-nums">
                        {r ? `${r.accepted}/${r.reviewed} из ${r.done}` : "—"} {r?.passes_rule ? <span className="text-ok">✓</span> : null}
                      </Td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
        <div>
          <div className="mb-1 text-xs text-ink-3">автозамеры против вердикта: среднее (число дублей) — счёт, не аттестат</div>
          <Table>
            <thead>
              <tr>
                <Th>замер</Th>
                <Th>принятые</Th>
                <Th>отклонённые</Th>
              </tr>
            </thead>
            <tbody>
              {s.metrics.map((m) => (
                <tr key={m.metric}>
                  <Td>{m.title}</Td>
                  <Td className="tabular-nums">
                    {num(m.accepted_mean, 3)} ({m.accepted_n})
                  </Td>
                  <Td className="tabular-nums">
                    {num(m.rejected_mean, 3)} ({m.rejected_n})
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      </div>
    </div>
  );
}

export default function BenchDetail() {
  const { id = "" } = useParams();
  const [open, setOpen] = useState<Open>(null);
  const q = useApi<BenchDetailT>(`/api/bench/${encodeURIComponent(id)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const s = d.summary;
  return (
    <>
      <Crumbs items={[{ to: "/bench", label: "Замеры" }, { label: d.id }]} />
      <PageHeader
        icon={FlaskConical}
        title={d.id}
        meta={
          <>
            <span>{d.spec.question}</span>
            <span>·</span>
            <Mono>{d.rel}</Mono>
          </>
        }
      />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat icon={Wallet} label="Потрачено" value={fmtUsd(s.spent_usd)} hint={`из ${fmtUsd(s.budget_usd)}; стоп до дубля, который превысит`} />
        <Stat label="Дублей" value={s.takes} hint={`сделано ${s.done} · упало ${s.failed}`} />
        <Stat label="Оценено" value={s.reviewed} hint="глазами, по критериям ниже" />
        <Stat label="Принято" value={s.accepted} hint="ни одного «нет» и хотя бы одно «да»" />
      </div>
      {d.spec.answer ? (
        <Card>
          <CardBody className="pt-3 text-[13px]">
            <span className="text-xs text-ink-3">ответ · </span>
            {d.spec.answer}
          </CardBody>
        </Card>
      ) : null}

      <Section title="Схема" icon={Layers} aside={d.spec.compare === "cause" ? "одна причина при общем сиде" : "метод против метода: вывод про эти конфигурации"}>
        <Card>
          <CardBody className="pt-3">
            <KV
              items={[
                {
                  k: "ветки",
                  v: (
                    <div className="space-y-0.5">
                      {d.spec.branches.map((b) => (
                        <div key={b.id}>
                          <Badge tone="accent">{b.id}</Badge> <span className="text-xs text-ink-2">{b.method} {b.preset}</span>
                          {b.note ? <span className="text-xs text-ink-3"> — {b.note}</span> : null}
                        </div>
                      ))}
                    </div>
                  ),
                },
                { k: "сиды", v: d.spec.seeds.join(", ") },
                d.spec.output === "image"
                  ? { k: "кадр", v: d.spec.aspect_ratio }
                  : { k: "клип", v: `${d.spec.duration_sec} с · ${d.spec.aspect_ratio}` },
                { k: "правило", v: `дальше по лестнице, если принято ${d.spec.stop_rule.min_accepted} из ${d.spec.stop_rule.of}` },
                {
                  k: "критерии",
                  v: (
                    <div className="space-y-0.5 text-[13px]">
                      {d.spec.criteria.map((c) => (
                        <div key={c.id}>
                          {c.title}
                          {c.hint ? <span className="text-xs text-ink-3"> — {c.hint}</span> : null}
                        </div>
                      ))}
                    </div>
                  ),
                },
                ...(d.spec.notes ? [{ k: "заметки", v: d.spec.notes }] : []),
              ]}
            />
          </CardBody>
        </Card>
      </Section>

      {d.scenes.map((scene) => {
        const takes = d.takes.filter((t) => t.scene === scene.id);
        return (
          <Section
            key={scene.id}
            title={`${scene.id} · ${scene.title || "сцена"}`}
            icon={ImageIcon}
            aside={scene.missing ? "кадра нет — его выбирает человек, сцена ждёт" : `дублей ${takes.length}`}
          >
            <SceneHeader scene={scene} onOpen={setOpen} />
            {takes.length ? (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {takes.map((t) => (
                  <TakeCard key={t.take_id} benchId={d.id} take={t} criteria={d.spec.criteria} onOpen={setOpen} />
                ))}
              </div>
            ) : (
              <Empty>
                дублей нет: <Mono>videoforge bench run {d.id} --scene {scene.id}</Mono>
              </Empty>
            )}
          </Section>
        );
      })}

      <Section title="Сводка" icon={ListChecks} aside="videoforge bench summary — то же текстом в summary.md">
        <Card>
          <CardBody className="pt-3">
            <SummaryTables s={s} />
          </CardBody>
        </Card>
        {d.summary_md ? (
          <Details summary="summary.md">
            <Markdown text={d.summary_md} />
          </Details>
        ) : null}
      </Section>

      {open ? (
        <MediaModal
          title={<Mono>{open.title}</Mono>}
          sources={{ [open.kind]: open.url }}
          labels={{ image: "кадр", video: "клип" }}
          initial={open.kind}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </>
  );
}
