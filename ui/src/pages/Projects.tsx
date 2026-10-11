import { Link } from "react-router";
import { Clapperboard, Hammer, Signal } from "lucide-react";
import { useApi, type ProjectItem, type ProjectsOverview } from "@/api";
import { ErrorBox, Loading, PageHeader, StatusPill } from "@/components/common";
import { Badge, Card, Empty, IconTile, Mono, Section } from "@/components/ui/primitives";
import { fmtSec, fmtUsd, plural } from "@/lib/format";
import { PROJECT_KIND_ICONS, WORKFLOW_ICONS } from "@/lib/icons";

function ProjectCard({ p }: { p: ProjectItem }) {
  return (
    <Link to={`/projects/${p.id}`} className="block">
      <Card className="h-full p-4 transition-colors hover:border-ink-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex min-w-0 items-start gap-3">
            <IconTile
              icon={p.kind === "test" ? PROJECT_KIND_ICONS.test : (WORKFLOW_ICONS[p.workflow ?? ""] ?? Clapperboard)}
            />
            <div className="min-w-0">
              <div className="truncate text-[15px] font-semibold">{p.name}</div>
              <div className="text-xs text-ink-3">
                <Mono>{p.id}</Mono>
                {p.workflow ? <span> · {p.workflow}</span> : null}
              </div>
            </div>
          </div>
          {p.kind === "test" ? <Badge tone="info">полигон</Badge> : <StatusPill status={p.status} />}
        </div>
        {p.error ? (
          <div className="mt-2 text-xs text-bad">{p.error}</div>
        ) : (
          <>
            {p.description ? <p className="mt-2 line-clamp-3 text-[13px] text-ink-2">{p.description}</p> : null}
            <div className="mt-3 flex flex-wrap gap-1">
              {(p.platform_titles ?? []).map((t) => (
                <Badge key={t}>{t}</Badge>
              ))}
              {p.language ? <Badge>{p.language}</Badge> : null}
              {p.aspect_ratio ? <Badge>{p.aspect_ratio}</Badge> : null}
              {p.target_duration_sec ? <Badge>{fmtSec(p.target_duration_sec)}</Badge> : null}
            </div>
            <div className="mt-3 flex items-center justify-between text-xs text-ink-3">
              <span>{plural(p.episodes ?? 0, "эпизод", "эпизода", "эпизодов")}</span>
              <span className="tabular-nums">потрачено {fmtUsd(p.spent_usd)}</span>
            </div>
            {p.accounts && p.accounts.length ? (
              <div className="mt-2 flex flex-wrap gap-1">
                {p.accounts.map((a) => (
                  <Badge key={a.id} tone="ok">
                    {a.network_title}
                    {a.handle ? ` ${a.handle}` : ""}
                  </Badge>
                ))}
              </div>
            ) : null}
          </>
        )}
      </Card>
    </Link>
  );
}

export default function Projects() {
  const q = useApi<ProjectsOverview>("/api/projects");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const items = q.data?.items ?? [];
  const others = q.data?.other_folders ?? [];
  const hidden = q.data?.hidden ?? [];
  // полигон качества (kind: test) — своя категория: его ролики не публикуют,
  // и в «живых» он путал бы картину
  const tests = items.filter((p) => p.kind === "test");
  const channels = items.filter((p) => p.kind !== "test");
  const live = channels.filter((p) => p.status === "live");
  const demo = channels.filter((p) => p.status !== "live");
  return (
    <>
      <PageHeader
        icon={Clapperboard}
        title="Проекты"
        meta={<span>проект = канал или серия; живой — тот, у которого есть аккаунт на площадке</span>}
      />
      <Section title="Живые" icon={Signal} aside={`${live.length}`}>
        {live.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {live.map((p) => (
              <ProjectCard key={p.id} p={p} />
            ))}
          </div>
        ) : (
          <Empty>
            Ни один проект не привязан к аккаунту. Привязать: <Mono>videoforge account new</Mono>
          </Empty>
        )}
      </Section>
      {tests.length ? (
        <Section
          title="Тестовые"
          icon={PROJECT_KIND_ICONS.test}
          aside={
            <Link to="/tests" className="underline hover:text-ink">
              галерея роликов →
            </Link>
          }
        >
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {tests.map((p) => (
              <ProjectCard key={p.id} p={p} />
            ))}
          </div>
        </Section>
      ) : null}
      <Section title="Демо / в разработке" icon={Hammer} aside={`${demo.length}`}>
        {demo.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {demo.map((p) => (
              <ProjectCard key={p.id} p={p} />
            ))}
          </div>
        ) : (
          <Empty>Пусто</Empty>
        )}
      </Section>
      <div className="text-xs text-ink-3">
        Полигон замеров <Mono>{q.data?.bench_folder}</Mono> — в разделе{" "}
        <Link to="/bench" className="underline hover:text-ink">
          Замеры
        </Link>
        .
        {hidden.length ? (
          <>
            {" "}
            Скрыто как образец (<Mono>hidden: true</Mono> в project.yaml):{" "}
            {hidden.map((id, i) => (
              <span key={id}>
                {i ? ", " : ""}
                <Link to={`/projects/${id}`} className="underline hover:text-ink">
                  {id}
                </Link>
              </span>
            ))}
            .
          </>
        ) : null}
        {others.length ? (
          <>
            {" "}
            Папки без <Mono>project.yaml</Mono>, конвейер их не видит: {others.map((o) => o).join(", ")} — завести
            проект: <Mono>videoforge project new &lt;id&gt;</Mono>.
          </>
        ) : null}
      </div>
    </>
  );
}
