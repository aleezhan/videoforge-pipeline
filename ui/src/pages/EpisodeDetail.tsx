import { useState } from "react";
import { useParams } from "react-router";
import {
  AudioLines,
  Clapperboard,
  Clock,
  ExternalLink,
  LayoutGrid,
  ListChecks,
  Package,
  Play,
  Receipt,
  ScrollText,
  Send,
  ShieldCheck,
  SquarePlay,
  Users,
  Wallet,
} from "lucide-react";
import { postJson, useApi, type EpisodeDetail as EpisodeDetailT, type PublishReport, type SceneRow } from "@/api";
import {
  Crumbs,
  ErrorBox,
  Loading,
  Markdown,
  MediaModal,
  Money,
  PageHeader,
  ResourceCard,
  StageBadge,
  type MediaTab,
} from "@/components/common";
import { Badge, Card, CardBody, Details, Empty, KV, LinkChip, Mono, Section, Stat, Table, Td, Th, cn } from "@/components/ui/primitives";
import { fmtDateTime, fmtSec, fmtUsd, plural } from "@/lib/format";
import { STAGE_ICONS, WORKFLOW_ICONS } from "@/lib/icons";

const CHECK_LABEL: Record<string, string> = {
  pending: "не проверялось",
  pass: "прошло",
  fail: "провал",
  skipped: "пропущено",
};

const ATTEMPT_LABEL: Record<string, string> = { image: "кадр", video: "видео", audio: "озвучка" };
const OVERLAY_LABEL: Record<string, string> = { caption: "плашка", source: "источник", label: "ярлык" };
// проверка кадра по описанию (пачкой, llm): про то / детали / не про то
const REVIEW_LABEL: Record<string, string> = { ok: "про то", note: "детали", block: "не про то" };
const REVIEW_TONE: Record<string, "ok" | "warn" | "bad"> = { ok: "ok", note: "warn", block: "bad" };

function CheckMark({ status }: { status: string }) {
  const tone = status === "pass" ? "ok" : status === "fail" ? "bad" : "neutral";
  return <Badge tone={tone}>{CHECK_LABEL[status] ?? status}</Badge>;
}

function chipClass(): string {
  return "inline-flex items-center gap-1 rounded-md border border-line bg-panel px-2 py-0.5 text-[12px] hover:border-ink-3 hover:bg-panel-2";
}

function SceneCard({
  s,
  showOriginal,
  episode,
}: {
  s: SceneRow;
  showOriginal: boolean;
  /** есть только у собранного эпизода: без готового файла монтаж показывать не на чем */
  episode?: { project: string; episode: string };
}) {
  const [modal, setModal] = useState<MediaTab | null>(null);
  const thumb = s.assets.image ?? s.assets.thumbs[0] ?? null;
  const hasMedia = Boolean(s.assets.clip || s.assets.video || s.assets.image || s.assets.audio);
  const visual = !showOriginal && s.translated ? s.translated.visual : s.visual_prompt;
  const motion = !showOriginal && s.translated ? s.translated.motion : s.motion_prompt;
  const open = (tab: MediaTab) => setModal(tab);
  return (
    <div className="grid gap-3 rounded-xl border border-line bg-panel p-3 md:grid-cols-[112px_1fr]">
      <div className="space-y-1">
        <button
          type="button"
          onClick={() => hasMedia && open("clip")}
          disabled={!hasMedia}
          title={hasMedia ? "открыть сцену" : undefined}
          className={cn(
            "group relative flex h-20 w-28 items-center justify-center overflow-hidden rounded-lg bg-panel-2 text-xs text-ink-3",
            hasMedia ? "cursor-pointer ring-offset-1 ring-offset-panel hover:ring-2 hover:ring-accent" : "",
          )}
        >
          {thumb ? <img src={thumb} alt={s.id} className="h-full w-full object-cover" loading="lazy" /> : "нет кадра"}
          {s.assets.clip || s.assets.video ? (
            <span className="absolute inset-0 flex items-center justify-center bg-black/0 transition-colors group-hover:bg-black/30">
              <Play className="h-6 w-6 fill-white/90 text-white opacity-80 drop-shadow" />
            </span>
          ) : null}
        </button>
        <div className="text-xs text-ink-3">
          #{s.order} <Mono>{s.id}</Mono>
        </div>
      </div>
      <div className="min-w-0 space-y-1.5 text-[13px]">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge title="длительность сцены по озвучке (голос + хвост), не больше потолка провайдера видео">
            {fmtSec(s.duration_sec)}
          </Badge>
          {s.planned_sec ? (
            <Badge title="сколько просил планнер для этого бита">план {fmtSec(s.planned_sec)}</Badge>
          ) : null}
          {s.continues ? <Badge tone="info">продолжает {s.continues}</Badge> : null}
          {s.cast.map((c) => (
            <Badge key={c} tone="accent">
              {c}
            </Badge>
          ))}
          {s.source ? (
            <Badge tone="info">
              источник {fmtSec(s.source.start_sec)}–{fmtSec(s.source.end_sec)}
            </Badge>
          ) : null}
          {s.cost_usd ? (
            <Badge title="расход на сцену: кадр + видео + озвучка вместе">{fmtUsd(s.cost_usd, 3)}</Badge>
          ) : null}
        </div>
        {s.narration ? <p className="text-ink">{s.narration}</p> : <p className="text-ink-3">без реплики</p>}
        {s.overlay ? (
          <p className="text-xs text-ink-2" title="текст поверх кадра: рисует сборка, в картинке текста нет">
            <Badge tone="info">{OVERLAY_LABEL[s.overlay.kind] ?? s.overlay.kind}</Badge> {s.overlay.text}
            {s.overlay.emphasis.length ? (
              <span className="text-ink-3"> · цветом: {s.overlay.emphasis.join(", ")}</span>
            ) : null}
          </p>
        ) : null}
        {visual ? (
          <p className="text-xs text-ink-2">
            <span className="text-ink-3">кадр: </span>
            {visual}
          </p>
        ) : null}
        {motion ? (
          <p className="text-xs text-ink-2">
            <span className="text-ink-3">движение: </span>
            {motion}
          </p>
        ) : null}
        {s.review ? (
          <p className="text-xs text-ink-2" title="проверка по описанию: что модель увидела в кадре и что не так">
            <Badge tone={REVIEW_TONE[s.review.severity] ?? "neutral"}>
              по описанию: {REVIEW_LABEL[s.review.severity] ?? s.review.severity}
            </Badge>{" "}
            {s.review.see}
            {s.review.issue ? <span className="text-ink-3"> · не так: {s.review.issue}</span> : null}
          </p>
        ) : null}
        {s.references.length ? (
          <div
            className="flex flex-wrap items-center gap-2 text-xs text-ink-2"
            title="референс композиции: из него художник взял расстановку и перспективу, предметы и стиль — из промпта"
          >
            <span className="text-ink-3">референс:</span>
            {s.references.map((r) =>
              r.url ? (
                <a key={r.path} href={r.url} target="_blank" rel="noreferrer" title={r.path}>
                  <img src={r.url} alt={r.path} className="h-12 rounded border border-line" loading="lazy" />
                </a>
              ) : (
                <Mono key={r.path}>{r.path}</Mono>
              ),
            )}
          </div>
        ) : null}
        {s.fixes.length ? (
          <p className="text-xs text-ink-2" title="фразы из вердикта проверки, дописанные к описанию сцены при повторе">
            <span className="text-ink-3">правки к описанию: </span>
            {s.fixes.join("; ")}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-1.5 pt-1 text-xs">
          <span className="text-ink-3" title="вердикт проверяющей модели по кадру и клипу; звук — есть ли озвучка">
            проверки:
          </span>
          <span>кадр</span> <CheckMark status={s.checks.image} />
          <span>видео</span> <CheckMark status={s.checks.video} />
          <span>звук</span> <CheckMark status={s.checks.audio} />
          {Object.entries(s.attempts).map(([k, n]) => (
            <Badge key={k} title={`сколько раз генерировали: ${ATTEMPT_LABEL[k] ?? k}`}>
              {ATTEMPT_LABEL[k] ?? k} ×{n}
            </Badge>
          ))}
          {s.assets.video ? (
            <button type="button" className={chipClass()} onClick={() => open("video")}>
              клип
            </button>
          ) : null}
          {s.assets.audio ? (
            <button type="button" className={chipClass()} onClick={() => open("audio")}>
              озвучка
            </button>
          ) : null}
        </div>
        {modal ? (
          <MediaModal
            title={
              <>
                #{s.order} <Mono>{s.id}</Mono> {s.narration ? <span className="text-ink-2">· {s.narration}</span> : null}
              </>
            }
            sources={{ clip: s.assets.clip, video: s.assets.video, image: s.assets.image, audio: s.assets.audio }}
            initial={modal}
            montage={episode}
            onClose={() => setModal(null)}
          />
        ) : null}
        {Object.keys(s.checks.notes).length ? (
          <ul className="list-disc pl-4 text-xs text-ink-2">
            {Object.entries(s.checks.notes).map(([k, v]) => (
              <li key={k}>
                <span className="text-ink-3">{k}: </span>
                {v}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}

/** Публикация: кнопка зовёт сухой прогон — тот же код, что `videoforge publish`,
 * но без единого байта наружу. Настоящая загрузка остаётся за CLI с `--yes`:
 * нажать в браузере проще, чем передумать, а забрать ролик с площадки нельзя.
 *
 * Правка уже уехавшего — другое дело, и она здесь доделывается до конца: ролик
 * на канале остаётся тем же, а сухой прогон отдаёт список «было → станет», то
 * есть сам работает экраном подтверждения. Ролики смотрят с телефона, и «открой
 * терминал, чтобы поправить язык» означает «поправим потом», то есть никогда. */
function PublishPanel({ episode }: { episode: EpisodeDetailT }) {
  const [report, setReport] = useState<PublishReport | null>(null);
  const [kind, setKind] = useState<"publish" | "update" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const state = episode.publish_state;
  const landed = Object.entries(state?.platforms ?? {});
  if (!episode.publish_result && !landed.length) return null;
  const ask = async (path: "publish" | "update", dry: boolean) => {
    setBusy(true);
    setError(null);
    setKind(path);
    try {
      setReport(
        await postJson<PublishReport>(`/api/projects/${episode.project}/episodes/${episode.id}/${path}`, {
          dry_run: dry,
        }),
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setReport(null);
    } finally {
      setBusy(false);
    }
  };
  const check = () => ask("publish", true);
  // правку показываем только у уехавшего: править нечего, пока ролика на
  // площадке нет, а кнопка, которая всегда отвечает «нечего», мешает
  const pending = report?.dry_run ? report.platforms.flatMap((p) => p.changes) : [];
  const TONE: Record<string, "ok" | "warn" | "bad" | "neutral" | "info"> = {
    done: "ok",
    dry_run: "info",
    skipped: "neutral",
    failed: "bad",
    pending: "warn",
  };
  return (
    <Section title="Публикация" icon={Send}>
      <Card>
        <CardBody className="space-y-3 pt-4">
          {landed.length ? (
            <div className="flex flex-wrap gap-2">
              {landed.map(([network, row]) => (
                <Badge key={network} tone="ok" title={`${row.provider} · ${fmtDateTime(row.at)}`}>
                  {network}: {row.url || row.video_id || "залито"}
                </Badge>
              ))}
            </div>
          ) : (
            <div className="text-[13px] text-ink-2">Ролик ещё нигде не опубликован.</div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={check}
              disabled={busy}
              className="rounded-lg border border-line bg-panel-2 px-3 py-1.5 text-[13px] font-medium hover:border-ink-3 disabled:opacity-50"
            >
              {busy ? "Считаю…" : "Проверить публикацию (сухой прогон)"}
            </button>
            {landed.length ? (
              <button
                type="button"
                onClick={() => ask("update", true)}
                disabled={busy}
                className="rounded-lg border border-line bg-panel-2 px-3 py-1.5 text-[13px] font-medium hover:border-ink-3 disabled:opacity-50"
              >
                {busy ? "Считаю…" : "Проверить правку на канале"}
              </button>
            ) : null}
            <span className="text-xs text-ink-3">ничего не отправляет — показывает, что ушло бы</span>
          </div>
          {error ? <div className="text-[13px] text-bad">{error}</div> : null}
          {report ? (
            <div className="space-y-2">
              <div className="text-[13px]">
                «{report.title}» · <Mono>{report.video}</Mono>
              </div>
              {report.warnings.map((w) => (
                <div key={w} className="text-xs text-warn">
                  {w}
                </div>
              ))}
              <div className="flex flex-wrap gap-2">
                {report.platforms.map((p) => (
                  <Badge key={p.platform} tone={TONE[p.state] ?? "neutral"} title={p.error || p.warnings.join("; ")}>
                    {p.platform}: {p.state}
                    {p.account ? ` · ${p.account}` : " · аккаунта нет"}
                  </Badge>
                ))}
              </div>
              {report.platforms.flatMap((p) => p.warnings).length ? (
                <div className="space-y-1">
                  {report.platforms.flatMap((p) =>
                    p.warnings.map((w) => (
                      <div key={`${p.platform}-${w}`} className="text-xs text-warn">
                        {p.platform}: {w}
                      </div>
                    )),
                  )}
                </div>
              ) : null}
              {kind === "update" ? (
                pending.length ? (
                  <div className="space-y-2 rounded-lg border border-line bg-panel-2 p-3">
                    <div className="text-[13px] font-medium">Что изменится на канале</div>
                    {pending.map((c) => (
                      <div key={c} className="text-[13px] text-ink-2">
                        {c}
                      </div>
                    ))}
                    <button
                      type="button"
                      onClick={() => ask("update", false)}
                      disabled={busy}
                      className="rounded-lg border border-line bg-ok/15 px-3 py-1.5 text-[13px] font-medium hover:border-ink-3 disabled:opacity-50"
                    >
                      {busy ? "Отправляю…" : "Отправить правку на канал"}
                    </button>
                  </div>
                ) : report.dry_run ? (
                  <div className="text-[13px] text-ink-2">На канале уже то же самое — менять нечего.</div>
                ) : (
                  <div className="text-[13px] text-ok">Поправлено.</div>
                )
              ) : null}
            </div>
          ) : null}
          <div className="text-xs text-ink-3">
            Загрузить по-настоящему: <Mono>videoforge publish {episode.ref} --to youtube --yes</Mono>. Пометка «сделано
            ИИ» и приватность private уходят вместе с файлом. Правка ролик не перезагружает: ссылка и просмотры те же, а
            видимость меняется только через <Mono>channel.privacy</Mono> проекта.
          </div>
        </CardBody>
      </Card>
    </Section>
  );
}

export default function EpisodeDetail() {
  const { id = "", ep = "" } = useParams();
  const q = useApi<EpisodeDetailT>(`/api/projects/${encodeURIComponent(id)}/episodes/${encodeURIComponent(ep)}`);
  // хуки — до ранних return, иначе их порядок меняется между рендерами (React #310)
  const [showOriginal, setShowOriginal] = useState(false);
  const [montageOpen, setMontageOpen] = useState(false);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const e = q.data!;
  const stages = e.stages ?? [];
  const done = stages.filter((s) => s.status === "done").length;
  const failedChecks = e.final_checks.filter((c) => !c.ok);
  const tr = e.translations;
  const translatedAny = e.scenes.some((s) => s.translated);
  return (
    <>
      <Crumbs
        items={[
          { to: "/projects", label: "Проекты" },
          { to: `/projects/${e.project}`, label: e.project },
          { label: e.id },
        ]}
      />
      <PageHeader
        icon={WORKFLOW_ICONS[e.workflow ?? ""] ?? Clapperboard}
        title={e.title || e.id}
        meta={
          <>
            <Mono>{e.ref}</Mono>
            <span>·</span>
            <span>{e.workflow}</span>
            <span>·</span>
            <span>{e.language}</span>
            <span>·</span>
            <span>{e.aspect_ratio}</span>
          </>
        }
        actions={
          <LinkChip to={e.report_url} external>
            <ExternalLink className="h-3 w-3" /> HTML-отчёт
          </LinkChip>
        }
      >
        {e.awaiting_approval ? (
          <div className="rounded-lg border border-warn/40 bg-warn-soft px-4 py-2 text-[13px] text-warn">
            Стадия «{e.awaiting_approval}» ждёт одобрения: <Mono>videoforge approve {e.ref} {e.awaiting_approval}</Mono>
          </div>
        ) : null}
        {e.brief ? <p className="max-w-3xl text-[13px] text-ink-2">{e.brief}</p> : null}
      </PageHeader>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat icon={Clock} label="Длительность" value={fmtSec(e.duration_sec)} hint={e.final.video ? "по готовому файлу" : "сумма сцен"} />
        <Stat icon={LayoutGrid} label="Сцен" value={e.scenes.length} hint={plural(e.cast.length, "участник", "участника", "участников")} />
        <Stat icon={ListChecks} label="Стадии" value={`${done} / ${stages.length}`} hint="готово" />
        <Stat icon={Wallet} label="Расход" value={<Money spent={e.spent_usd} budget={e.budget_usd} />} hint="потрачено / бюджет" />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_1.2fr]">
        <Section title="Стадии" icon={ListChecks}>
          <Card>
            <Table>
              <thead>
                <tr>
                  <Th>Стадия</Th>
                  <Th>Статус</Th>
                  <Th>Сообщение</Th>
                  <Th className="text-right">$</Th>
                </tr>
              </thead>
              <tbody>
                {stages.map((s) => {
                  const StageIcon = STAGE_ICONS[s.name];
                  return (
                  <tr key={s.name} className={cn(s.awaiting ? "bg-warn-soft/40" : "")}>
                    <Td>
                      {StageIcon ? <StageIcon className="mr-1.5 inline h-3.5 w-3.5 align-[-2px] text-ink-3" /> : null}
                      <span className="font-medium">{s.name}</span>
                      {s.gate_after ? (
                        <span className="ml-1 text-[11px] text-ink-3" title="после стадии — ворота">
                          ⏸
                        </span>
                      ) : null}
                      {s.approved ? <Badge tone="ok" className="ml-1">одобрено</Badge> : null}
                      {s.elapsed_sec !== null ? <div className="text-[11px] text-ink-3">{fmtSec(s.elapsed_sec)}</div> : null}
                    </Td>
                    <Td>
                      <StageBadge status={s.status} />
                    </Td>
                    <Td className="max-w-md text-xs text-ink-2">
                      {s.error ? <span className="text-bad">{s.error}</span> : s.message}
                    </Td>
                    <Td className="text-right tabular-nums">{s.cost_usd ? fmtUsd(s.cost_usd, 3) : ""}</Td>
                  </tr>
                  );
                })}
              </tbody>
            </Table>
          </Card>
        </Section>

        <Section title="Итог" icon={SquarePlay}>
          {e.final_video_url ? (
            <Card>
              <CardBody className="pt-4">
                <video controls preload="metadata" src={e.final_video_url} className="max-h-105 w-full rounded-lg bg-black" />
                <button
                  type="button"
                  onClick={() => setMontageOpen(true)}
                  className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel-2 px-3 py-1.5 text-[13px] font-medium hover:border-ink-3"
                  title="таймлайн: сцены, текст на экране, речь, слова песни и волна звука на одной шкале"
                >
                  <AudioLines className="h-4 w-4 text-ink-3" /> Монтаж
                </button>
                <div className="mt-3">
                  {e.final_checks.length ? (
                    <Table>
                      <tbody>
                        {e.final_checks.map((c) => (
                          <tr key={c.label}>
                            <Td className="text-xs text-ink-3">{c.label}</Td>
                            <Td className={cn("text-xs tabular-nums", c.ok ? "" : "text-bad")}>{c.value}</Td>
                            <Td className="text-xs text-ink-3">{c.target}</Td>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  ) : null}
                  {failedChecks.length ? (
                    <div className="mt-2 text-xs text-bad">
                      Вне порогов: {failedChecks.map((c) => c.label).join(", ")} — решает человек на воротах.
                    </div>
                  ) : null}
                </div>
              </CardBody>
            </Card>
          ) : (
            <Empty>Готового файла ещё нет — сборка не запускалась или не дошла.</Empty>
          )}
          {e.qa ? (
            <Card>
              <CardBody className="pt-4">
                <div className="mb-1 flex items-center justify-between">
                  <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-ink-3">
                    <ShieldCheck className="h-3.5 w-3.5" /> QA
                  </div>
                  <div className="text-xs text-ink-3">
                    {e.qa.llm} · стыков {e.qa.cuts} · кадров {e.qa.frames} · {fmtDateTime(e.qa.generated_at)}
                  </div>
                </div>
                {e.qa.findings.length ? (
                  <ul className="space-y-1 text-[13px]">
                    {e.qa.findings.map((f, i) => (
                      <li key={i} className="flex gap-2">
                        <Badge tone={f.severity === "block" || f.severity === "error" ? "bad" : "warn"}>{f.severity}</Badge>
                        <span>
                          {f.scene_id ? <Mono>{f.scene_id}</Mono> : null} {f.message}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-xs text-ink-3">Замечаний нет.</div>
                )}
              </CardBody>
            </Card>
          ) : null}
        </Section>
      </div>

      <Section title="Каст" icon={Users} aside="главный персонаж проекта и каст сцен">
        {e.cast.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {e.cast.map((c) => (
              <ResourceCard key={`${c.kind}/${c.id}`} card={c} />
            ))}
          </div>
        ) : (
          <Empty>В сценах никто не назван, и у проекта нет главного персонажа.</Empty>
        )}
      </Section>

      <Section
        title="Сцены"
        icon={LayoutGrid}
        aside={
          <span className="flex items-center gap-2">
            <span>{e.scenes.length}</span>
            {translatedAny ? (
              <button
                type="button"
                onClick={() => setShowOriginal((v) => !v)}
                className="rounded-md border border-line px-2 py-0.5 text-[11px] hover:bg-panel-2"
                title="промпты моделям идут по-английски; перевод — только для чтения"
              >
                {showOriginal ? `показать ${tr?.language ?? "перевод"}` : "показать оригинал (en)"}
              </button>
            ) : null}
          </span>
        }
      >
        {e.scenes.length && !translatedAny && e.language !== "en" ? (
          <div className="rounded-lg border border-line bg-panel-2/60 px-4 py-2 text-xs text-ink-2">
            Промпты кадра и движения по-английски — так работают модели. Перевод для чтения:{" "}
            <Mono>videoforge episode translate-prompts {e.ref}</Mono>
            {tr && tr.stale ? ` (устарело сцен: ${tr.stale})` : ""}
          </div>
        ) : null}
        {translatedAny && tr && tr.stale ? (
          <div className="text-xs text-warn">
            У {tr.stale} сцен промпт изменился после перевода — показан оригинал; обновить:{" "}
            <Mono>videoforge episode translate-prompts {e.ref}</Mono>
          </div>
        ) : null}
        {e.scenes.length ? (
          <div className="space-y-2">
            {e.scenes.map((s) => (
              <SceneCard
                key={s.id}
                s={s}
                showOriginal={showOriginal}
                episode={e.final_video_url ? { project: e.project, episode: e.id } : undefined}
              />
            ))}
          </div>
        ) : (
          <Empty>Раскадровка пуста — ждёт планнера или ручного заполнения storyboard.json.</Empty>
        )}
      </Section>

      {e.publish.title || e.publish.description || e.publish.hashtags.length ? (
        <Section title="Упаковка" icon={Package}>
          <Card>
            <CardBody className="pt-4">
              <KV
                items={[
                  { k: "Заголовок", v: e.publish.title || "—" },
                  { k: "Описание", v: <span className="whitespace-pre-wrap">{e.publish.description || "—"}</span> },
                  {
                    k: "Хэштеги",
                    v: e.publish.hashtags.length ? (
                      <span className="flex flex-wrap gap-1">
                        {e.publish.hashtags.map((h) => (
                          <Badge key={h}>#{h}</Badge>
                        ))}
                      </span>
                    ) : (
                      "—"
                    ),
                  },
                ]}
              />
              {e.publish_result && Array.isArray(e.publish_result.checks) ? (
                <div className="mt-3 flex flex-wrap gap-1">
                  {(e.publish_result.checks as { label: string; value: string; ok: boolean; target: string }[]).map((c) => (
                    <Badge key={c.label} tone={c.ok ? "ok" : "warn"} title={`${c.value} · цель ${c.target}`}>
                      {c.label}
                    </Badge>
                  ))}
                </div>
              ) : null}
            </CardBody>
          </Card>
        </Section>
      ) : null}

      <PublishPanel episode={e} />

      {e.script_md ? (
        <Details icon={ScrollText} summary="Сценарий (01_script/script.md)">
          <Markdown text={e.script_md} />
        </Details>
      ) : null}

      {e.ledger.length ? (
        <Details icon={Receipt} summary={`Журнал расходов — ${e.ledger.length} записей, ${fmtUsd(e.spent_usd)}`}>
          <Table>
            <thead>
              <tr>
                <Th>Когда</Th>
                <Th>Стадия</Th>
                <Th>Провайдер</Th>
                <Th>Сцена</Th>
                <Th className="text-right">$</Th>
                <Th>Заметка</Th>
              </tr>
            </thead>
            <tbody>
              {e.ledger.map((l, i) => (
                <tr key={i}>
                  <Td className="text-xs text-ink-3">{fmtDateTime(l.at)}</Td>
                  <Td>{l.stage}</Td>
                  <Td>{l.provider}</Td>
                  <Td>{l.scene_id ?? ""}</Td>
                  <Td className="text-right tabular-nums">{fmtUsd(l.usd, 4)}</Td>
                  <Td className="text-xs text-ink-2">{l.note}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Details>
      ) : null}

      {montageOpen ? (
        <MediaModal
          title={<>Монтаж · {e.title || e.id}</>}
          sources={{}}
          initial="montage"
          montage={{ project: e.project, episode: e.id }}
          onClose={() => setMontageOpen(false)}
        />
      ) : null}

      <div className="text-xs text-ink-3">
        Папка: <span className="font-mono">{e.path}</span> · провайдеры последнего прогона:{" "}
        {Object.entries(e.providers).map(([k, v]) => `${k}=${v}`).join(", ") || "—"}
      </div>
    </>
  );
}
