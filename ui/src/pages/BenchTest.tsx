import { useState } from "react";
import { Link, useParams } from "react-router";
import { ExternalLink, FileSpreadsheet, FileText, FlaskConical, Folder, Play } from "lucide-react";
import { useApi, type BenchFile, type BenchGroup, type BenchTest as BenchTestT } from "@/api";
import { Crumbs, CsvTable, ErrorBox, Loading, Markdown, MediaModal, PageHeader } from "@/components/common";
import { Card, CardBody, Details, Mono, Section } from "@/components/ui/primitives";
import { fmtDateTime } from "@/lib/format";
import { KindCounts } from "./Bench";

function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

type Open = { kind: "image" | "video" | "audio"; file: BenchFile } | null;

function Group({ g, onOpen }: { g: BenchGroup; onOpen: (o: Open) => void }) {
  return (
    <Section
      title={g.dir ? g.dir : "корень теста"}
      icon={Folder}
      aside={
        <KindCounts
          counts={{ image: g.images.length, video: g.videos.length, audio: g.audio.length, doc: g.docs.length, table: g.tables.length, other: g.other.length }}
        />
      }
    >
      {g.docs.map((d) => (
        <Details key={d.rel} icon={FileText} summary={d.name} open={g.dir === ""}>
          {d.text !== undefined ? (
            <Markdown text={d.text} />
          ) : (
            <Link to={`/bench/file/${d.rel}`} className="text-sm underline">
              открыть документ
            </Link>
          )}
        </Details>
      ))}
      {g.tables.map((t) => (
        <Details key={t.rel} icon={FileSpreadsheet} summary={`${t.name} — ${t.table?.total ?? 0} строк`}>
          {t.table ? <CsvTable table={t.table} /> : null}
          <div className="pt-2 text-xs text-ink-3">
            <Link to={`/bench/file/${t.rel}`} className="underline">
              целиком
            </Link>
          </div>
        </Details>
      ))}
      {g.images.length ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {g.images.map((f) => (
            <button
              key={f.rel}
              type="button"
              onClick={() => onOpen({ kind: "image", file: f })}
              className="group text-left"
              title={`${f.name} · ${fmtSize(f.size)}`}
            >
              <div className="overflow-hidden rounded-lg border border-line bg-panel-2 ring-offset-1 ring-offset-panel group-hover:ring-2 group-hover:ring-accent">
                {f.url ? <img src={f.url} alt={f.name} loading="lazy" className="aspect-[4/3] w-full object-contain" /> : null}
              </div>
              <div className="mt-1 truncate font-mono text-[11px] text-ink-3">{f.name}</div>
            </button>
          ))}
        </div>
      ) : null}
      {g.videos.length ? (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {g.videos.map((f) => (
            <button
              key={f.rel}
              type="button"
              onClick={() => onOpen({ kind: "video", file: f })}
              className="group text-left"
              title={`${f.name} · ${fmtSize(f.size)}`}
            >
              <div className="flex aspect-video items-center justify-center rounded-lg border border-line bg-black/80 text-white/80 ring-offset-1 ring-offset-panel group-hover:ring-2 group-hover:ring-accent">
                <Play className="h-7 w-7 fill-white/80" />
              </div>
              <div className="mt-1 truncate font-mono text-[11px] text-ink-3">
                {f.name} · {fmtSize(f.size)}
              </div>
            </button>
          ))}
        </div>
      ) : null}
      {g.audio.length ? (
        <div className="space-y-1">
          {g.audio.map((f) => (
            <div key={f.rel} className="flex items-center gap-3 text-xs">
              <span className="w-56 truncate font-mono text-ink-3">{f.name}</span>
              {f.url ? <audio controls preload="none" src={f.url} className="h-8 flex-1" /> : null}
            </div>
          ))}
        </div>
      ) : null}
      {g.other.length ? (
        <Card>
          <CardBody className="pt-3">
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
              {g.other.map((f) =>
                f.kind === "text" ? (
                  <Link key={f.rel} to={`/bench/file/${f.rel}`} className="inline-flex items-center gap-1 hover:underline">
                    <FileText className="h-3 w-3 text-ink-3" /> {f.name}
                    <span className="text-ink-3">{fmtSize(f.size)}</span>
                  </Link>
                ) : f.url ? (
                  <a key={f.rel} href={f.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline">
                    <ExternalLink className="h-3 w-3 text-ink-3" /> {f.name}
                    <span className="text-ink-3">{fmtSize(f.size)}</span>
                  </a>
                ) : (
                  <span key={f.rel}>{f.name}</span>
                ),
              )}
            </div>
          </CardBody>
        </Card>
      ) : null}
    </Section>
  );
}

export default function BenchTest() {
  const { id = "" } = useParams();
  const [open, setOpen] = useState<Open>(null);
  const q = useApi<BenchTestT>(`/api/bench/tests/${encodeURIComponent(id)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const t = q.data!;
  return (
    <>
      <Crumbs items={[{ to: "/bench", label: "Замеры" }, { label: t.id }]} />
      <PageHeader
        icon={FlaskConical}
        title={t.id}
        meta={
          <>
            <Mono>{t.rel}</Mono>
            <span>·</span>
            <KindCounts counts={t.counts} />
          </>
        }
      />
      {t.groups.map((g) => (
        <Group key={g.dir} g={g} onOpen={setOpen} />
      ))}
      {open ? (
        <MediaModal
          title={
            <>
              <Mono>{open.file.rel}</Mono> <span className="text-ink-3">· {fmtDateTime(open.file.mtime)}</span>
            </>
          }
          sources={{ [open.kind]: open.file.url }}
          labels={{ image: "картинка", video: "клип", audio: "аудио" }}
          initial={open.kind}
          onClose={() => setOpen(null)}
        />
      ) : null}
    </>
  );
}
