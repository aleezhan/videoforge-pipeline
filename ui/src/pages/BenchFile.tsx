import { useParams } from "react-router";
import { ExternalLink } from "lucide-react";
import { useApi, type BenchFile as BenchFileT } from "@/api";
import { Crumbs, CsvTable, ErrorBox, Loading, Markdown, PageHeader } from "@/components/common";
import { Card, CardBody, LinkChip, Mono } from "@/components/ui/primitives";
import { fmtDateTime } from "@/lib/format";
import { FILE_KIND_ICONS } from "@/lib/icons";

export default function BenchFile() {
  const params = useParams();
  const rel = params["*"] ?? "";
  const q = useApi<BenchFileT>(`/api/bench/text/${rel.split("/").map(encodeURIComponent).join("/")}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const f = q.data!;
  const parts = rel.split("/");
  const testId = parts[0] === "out" && parts.length > 2 ? parts[1] : null;
  return (
    <>
      <Crumbs
        items={[
          { to: "/bench", label: "Замеры" },
          ...(testId ? [{ to: `/bench/tests/${testId}`, label: testId }] : []),
          { label: f.name },
        ]}
      />
      <PageHeader
        icon={FILE_KIND_ICONS[f.kind]}
        title={f.name}
        meta={
          <>
            <Mono>{f.rel}</Mono>
            <span>·</span>
            <span>{fmtDateTime(f.mtime)}</span>
          </>
        }
        actions={
          f.url ? (
            <LinkChip to={f.url} external>
              <ExternalLink className="h-3 w-3" /> файл
            </LinkChip>
          ) : null
        }
      />
      <Card>
        <CardBody className="pt-4">
          {f.table ? (
            <>
              <CsvTable table={f.table} />
              {f.table.truncated ? (
                <div className="pt-2 text-xs text-ink-3">
                  Показаны первые {f.table.rows.length} из {f.table.total} строк — остальное в файле.
                </div>
              ) : null}
            </>
          ) : f.kind === "doc" ? (
            <Markdown text={f.text ?? ""} />
          ) : (
            <pre className="max-h-[75vh] overflow-auto whitespace-pre-wrap font-mono text-[12px] leading-relaxed">{f.text}</pre>
          )}
        </CardBody>
      </Card>
    </>
  );
}
