import { useState } from "react";
import { Link, useParams } from "react-router";
import {
  Boxes,
  Clapperboard,
  ExternalLink,
  FileText,
  Film,
  Image as ImageIcon,
  Link2,
  Music,
  Play,
  ScrollText,
  Settings2,
  Telescope,
} from "lucide-react";
import {
  useApi,
  type EpisodeSummary,
  type ProjectDetail as ProjectDetailT,
  type SourceFile,
  type SourceRow,
} from "@/api";
import {
  Crumbs,
  ErrorBox,
  Loading,
  Markdown,
  MediaModal,
  Money,
  PageHeader,
  ResourceCard,
  StageStrip,
  StatusPill,
  UploadPill,
  type MediaTab,
} from "@/components/common";
import { ScheduleSection } from "@/components/Schedule";
import { Badge, Card, CardBody, Details, Empty, KV, LinkChip, Mono, Section, Table, Td, Th } from "@/components/ui/primitives";
import { fmtCompact, fmtDate, fmtNum, fmtSec, fmtUsd } from "@/lib/format";
import { WORKFLOW_ICONS } from "@/lib/icons";

function fmtSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

type OpenMedia = { tab: MediaTab; file: SourceFile } | null;

function FileChip({ f, onOpen }: { f: SourceFile; onOpen: (o: OpenMedia) => void }) {
  const cls =
    "inline-flex max-w-full items-center gap-1 rounded-md border border-line bg-panel px-2 py-0.5 text-[12px] hover:border-ink-3 hover:bg-panel-2";
  const size = <span className="text-ink-3">{fmtSize(f.size)}</span>;
  if (f.kind === "video" && f.url) {
    return (
      <button type="button" className={cls} onClick={() => onOpen({ tab: "video", file: f })} title={f.name}>
        <Play className="h-3 w-3 text-ink-3" /> <span className="truncate">{f.name}</span> {size}
      </button>
    );
  }
  if (f.kind === "audio" && f.url) {
    return (
      <button type="button" className={cls} onClick={() => onOpen({ tab: "audio", file: f })} title={f.name}>
        <Music className="h-3 w-3 text-ink-3" /> <span className="truncate">{f.name}</span> {size}
      </button>
    );
  }
  if (f.kind === "image" && f.url) {
    return (
      <button type="button" className={cls} onClick={() => onOpen({ tab: "image", file: f })} title={f.name}>
        <ImageIcon className="h-3 w-3 text-ink-3" /> <span className="truncate">{f.name}</span> {size}
      </button>
    );
  }
  if (f.url) {
    return (
      <a href={f.url} target="_blank" rel="noreferrer" className={cls} title={f.name}>
        <FileText className="h-3 w-3 text-ink-3" /> <span className="truncate">{f.name}</span> {size}
      </a>
    );
  }
  return (
    <span className={cls}>
      {f.name} {size}
    </span>
  );
}

function SourceCard({ s, onOpen }: { s: SourceRow; onOpen: (o: OpenMedia) => void }) {
  const total = s.groups.reduce((n, g) => n + g.files.length, 0);
  return (
    <Card>
      <CardBody className="space-y-3 pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{s.title || s.id}</span>
          <Mono>{s.id}</Mono>
          {s.kind === "clips" ? <Badge tone="info">источник нарезок</Badge> : <Badge>папка материала</Badge>}
          {s.duration_sec ? <Badge>{fmtSec(s.duration_sec)}</Badge> : null}
          {s.channel ? <span className="text-xs text-ink-3">{s.channel}</span> : null}
          {s.url ? (
            <a href={s.url} target="_blank" rel="noreferrer" className="text-xs text-ink-3 hover:text-ink">
              <ExternalLink className="inline h-3 w-3" />
            </a>
          ) : null}
          {s.error ? <span className="text-xs text-bad">{s.error}</span> : null}
        </div>
        {s.kind === "clips" ? (
          <div className="flex flex-wrap gap-1.5 text-xs">
            {s.word_count ? <Badge>слов {fmtNum(s.word_count)}</Badge> : <Badge tone="warn">транскрипта нет</Badge>}
            {s.transcriber ? <Badge>транскрипт: {s.transcriber}</Badge> : null}
            {s.chapters ? <Badge>глав {s.chapters}</Badge> : null}
            {s.heatmap ? <Badge>карта пересмотров</Badge> : null}
            {s.highlights ? (
              <Badge tone="ok" title={s.highlights.generated_at ? `моменты от ${fmtDate(s.highlights.generated_at)}` : ""}>
                моментов {s.highlights.candidates}
                {s.highlights.rejected ? ` · закрыто ${s.highlights.rejected}` : ""}
              </Badge>
            ) : (
              <Badge>моменты не выбирались</Badge>
            )}
            {s.highlights?.report_url ? (
              <LinkChip to={s.highlights.report_url} external>
                highlights.html
              </LinkChip>
            ) : null}
            {s.proxy_url ? (
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded-md border border-line bg-panel px-2 py-0.5 text-[12px] hover:border-ink-3"
                onClick={() => onOpen({ tab: "video", file: { name: "proxy.mp4", url: s.proxy_url ?? null, kind: "video", size: 0, mtime: "" } })}
              >
                <Play className="h-3 w-3" /> превью 480p
              </button>
            ) : null}
          </div>
        ) : null}
        {s.readme_md ? (
          <Details summary="README.md — что где лежит">
            <Markdown text={s.readme_md} />
          </Details>
        ) : null}
        {s.groups.map((g) => (
          <div key={g.dir} className="space-y-1">
            {g.dir ? <div className="font-mono text-[11px] text-ink-3">{g.dir}/</div> : null}
            <div className="flex flex-wrap gap-1.5">
              {g.files.map((f) => (
                <FileChip key={`${g.dir}/${f.name}`} f={f} onOpen={onOpen} />
              ))}
            </div>
          </div>
        ))}
        <div className="text-[11px] text-ink-3">
          {total} файлов · <span className="font-mono">{s.path}</span>
        </div>
      </CardBody>
    </Card>
  );
}

function EpisodeRow({ e }: { e: EpisodeSummary }) {
  if (e.error) {
    return (
      <tr>
        <Td colSpan={6}>
          <Mono>{e.id}</Mono> <span className="text-bad">{e.error}</span>
        </Td>
      </tr>
    );
  }
  return (
    <tr className="hover:bg-panel-2/60">
      <Td>
        <Link to={`/projects/${e.project}/episodes/${e.id}`} className="font-medium hover:underline">
          {e.title || e.id}
        </Link>
        <div className="text-xs text-ink-3">
          <Mono>{e.id}</Mono>
        </div>
      </Td>
      <Td className="tabular-nums">{e.scenes ?? 0}</Td>
      <Td className="tabular-nums">{fmtSec(e.duration_sec)}</Td>
      <Td>
        <div className="flex items-center gap-2">
          <StageStrip stages={e.stages ?? []} />
          {e.awaiting_approval ? <Badge tone="warn">ждёт: {e.awaiting_approval}</Badge> : null}
          {/* без зелёного: «упаковано» читали как «опубликовано» — зелёным
              горит только то, что реально лежит на площадке (UploadPill) */}
          {e.published ? <Badge title="заголовок, описание и publish.md готовы — это ещё не загрузка">упаковано</Badge> : null}
          <UploadPill upload={e.upload} />
        </div>
      </Td>
      <Td>
        <Money spent={e.spent_usd} budget={e.budget_usd} />
      </Td>
      <Td>{e.final_video_url ? <Badge tone="ok">final.mp4</Badge> : <span className="text-ink-3">—</span>}</Td>
    </tr>
  );
}

export default function ProjectDetail() {
  const { id = "" } = useParams();
  const q = useApi<ProjectDetailT>(`/api/projects/${encodeURIComponent(id)}`);
  const [open, setOpen] = useState<OpenMedia>(null);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const p = q.data!;
  const v = p.video;
  return (
    <>
      <Crumbs items={[{ to: "/projects", label: "Проекты" }, { label: p.name }]} />
      <PageHeader
        icon={WORKFLOW_ICONS[p.workflow] ?? Clapperboard}
        title={
          <span className="flex items-center gap-2">
            {p.name} <StatusPill status={p.status} />
          </span>
        }
        meta={
          <>
            <Mono>{p.id}</Mono>
            <span>·</span>
            <span>{p.workflow}</span>
            {!p.workflow_exists ? (
              <Badge tone="warn" title="файла workflows/<имя>.yaml нет: эпизоды пока не создать">
                workflow не написан
              </Badge>
            ) : null}
            {p.channel.name ? (
              <>
                <span>·</span>
                <span>канал «{p.channel.name}»</span>
              </>
            ) : null}
          </>
        }
        actions={
          p.accounts.length ? (
            <div className="flex flex-wrap gap-1">
              {p.accounts.map((a) => (
                <LinkChip key={a.id} to={`/accounts/${a.id}`}>
                  {a.network_title}
                  {a.handle ? ` ${a.handle}` : ""}
                </LinkChip>
              ))}
            </div>
          ) : (
            <span className="text-xs text-ink-3">
              аккаунтов нет — <Mono>videoforge account new</Mono>
            </span>
          )
        }
      >
        {p.description ? <p className="max-w-3xl text-[13px] text-ink-2">{p.description}</p> : null}
      </PageHeader>

      {/* у полигона качества (kind: test) ролики не публикуются — расписание ему ни к чему */}
      {p.kind !== "test" ? <ScheduleSection project={p.id} view={p.queue} onSaved={() => void q.refetch()} /> : null}

      <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
        <Section title="Настройки" icon={Settings2} aside="project.yaml">
        <Card>
          <CardBody className="pt-4">
            <KV
              items={[
                { k: "Площадки", v: p.platform_titles.join(", ") || "—" },
                { k: "Язык", v: p.channel.language },
                { k: "Формат кадра", v: v.aspect_ratio },
                { k: "Целевая длительность", v: fmtSec(v.target_duration_sec) },
                { k: "Сцена (ориентир)", v: `${v.scene_duration_sec} с · ≈${p.scenes_estimate} сцен` },
                { k: "Хвост сцены", v: `${v.scene_tail_sec} с` },
                {
                  k: "Субтитры",
                  v: v.captions ? `да${v.captions_language ? ` · ${v.captions_language}` : ""}` : "нет",
                },
                { k: "Музыка", v: v.music ? "да" : "нет" },
                { k: "Fade", v: `${v.fade_in_sec} с / ${v.fade_out_sec} с` },
                { k: "Бюджет на эпизод", v: fmtUsd(p.budget_per_episode_usd) },
                { k: "Потрачено всего", v: fmtUsd(p.spent_usd) },
                { k: "Папка", v: <span className="font-mono text-xs">{p.path}</span> },
              ]}
            />
          </CardBody>
        </Card>
        </Section>
        <Section title="Анализы" icon={Telescope} aside="research: в project.yaml">
        <Card>
          <CardBody className="pt-4">
            {p.research.length ? (
              <ul className="space-y-2">
                {p.research.map((r) => (
                  <li key={r.slug} className="flex flex-wrap items-center gap-2 text-[13px]">
                    <span className={r.exists ? "font-medium" : "text-bad"}>{r.title}</span>
                    <Mono>{r.slug}</Mono>
                    {r.subscribers ? <span className="text-xs text-ink-3">{fmtCompact(r.subscribers)} подп.</span> : null}
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" className="text-xs text-ink-3 hover:text-ink">
                        <ExternalLink className="inline h-3 w-3" />
                      </a>
                    ) : null}
                    {r.blueprint_url ? (
                      <LinkChip to={r.blueprint_url} external>
                        blueprint.md
                      </LinkChip>
                    ) : null}
                    {r.report_url ? (
                      <LinkChip to={r.report_url} external>
                        отчёт
                      </LinkChip>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-xs text-ink-3">Канал придуман без анализа чужого контента.</div>
            )}
          </CardBody>
        </Card>
        </Section>
      </div>

      <Section
        title="Ресурсы проекта"
        icon={Boxes}
        aside="свой — лежит в папке проекта · общий — в resources/, его видят все проекты"
      >
        {p.resources.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {p.resources.map((r) => (
              <ResourceCard key={`${r.slot}-${r.id}`} card={r} label={r.slot_title} />
            ))}
          </div>
        ) : (
          <Empty>Ресурсы не заданы</Empty>
        )}
        {p.local_resources.length ? (
          <>
            <div className="pt-2 text-xs text-ink-3">
              Свои — projects/{p.id}/resources/: каст этой серии и всё, что заведено только для неё
            </div>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {p.local_resources.map((r) => (
                <ResourceCard key={`${r.kind}-${r.id}`} card={r} label={r.slot_title} />
              ))}
            </div>
          </>
        ) : null}
      </Section>

      <Section title="Эпизоды" icon={Film} aside={`${p.episodes.length}`}>
        {p.episodes.length ? (
          <Card>
            <Table>
              <thead>
                <tr>
                  <Th>Эпизод</Th>
                  <Th>Сцен</Th>
                  <Th>Длительность</Th>
                  <Th>Стадии</Th>
                  <Th>Расход</Th>
                  <Th>Итог</Th>
                </tr>
              </thead>
              <tbody>
                {p.episodes.map((e) => (
                  <EpisodeRow key={e.id} e={e} />
                ))}
              </tbody>
            </Table>
          </Card>
        ) : (
          <Empty>
            Эпизодов нет. Создать: <Mono>videoforge episode new {p.id}/ep001</Mono> или командой /new-episode
          </Empty>
        )}
      </Section>

      {p.sources.length ? (
        <Section title="Источники" icon={Link2} aside={`projects/${p.id}/sources/ · ${p.sources.length}`}>
          <div className="space-y-3">
            {p.sources.map((s) => (
              <SourceCard key={s.id} s={s} onOpen={setOpen} />
            ))}
          </div>
        </Section>
      ) : null}

      <Details icon={ScrollText} summary="rules.md — правила для режиссёра и планнера">
        {p.rules_md ? <Markdown text={p.rules_md} /> : <div className="text-sm text-ink-3">Файла нет.</div>}
      </Details>

      {open ? (
        <MediaModal
          title={<Mono>{open.file.name}</Mono>}
          sources={{ [open.tab]: open.file.url }}
          labels={{ image: "картинка", video: "видео", audio: "аудио" }}
          initial={open.tab}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </>
  );
}
