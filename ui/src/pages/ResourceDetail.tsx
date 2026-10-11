import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { AudioLines, BookOpen, Boxes, Check, Eye, Images, Link2, ListTodo, Pencil, X } from "lucide-react";
import { patchJson, useApi, type ApiError, type ResourceDetail as ResourceDetailT } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader, ResourceExample } from "@/components/common";
import { Badge, Card, CardBody, Empty, LinkChip, Mono, Section, Table, Td, Th } from "@/components/ui/primitives";
import { KIND_ICONS } from "@/lib/icons";

// techniques показаны отдельной секцией ссылками — в таблице полей это шум
const HIDDEN = new Set(["id", "name", "description", "techniques"]);

function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return <span className="text-ink-3">—</span>;
  if (typeof value === "boolean") return <Badge tone={value ? "ok" : "neutral"}>{value ? "да" : "нет"}</Badge>;
  if (typeof value === "number") return <span className="tabular-nums">{value}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-ink-3">—</span>;
    if (value.every((v) => typeof v === "string" && v.length < 40)) {
      return (
        <span className="flex flex-wrap gap-1">
          {value.map((v, i) => (
            <Badge key={i}>{String(v)}</Badge>
          ))}
        </span>
      );
    }
    return (
      <ul className="list-disc space-y-0.5 pl-4">
        {value.map((v, i) => (
          <li key={i}>{typeof v === "object" ? <pre className="font-mono text-xs">{JSON.stringify(v, null, 1)}</pre> : String(v)}</li>
        ))}
      </ul>
    );
  }
  if (typeof value === "object") {
    return <pre className="whitespace-pre-wrap font-mono text-xs">{JSON.stringify(value, null, 2)}</pre>;
  }
  const s = String(value);
  return s.length > 120 ? <p className="whitespace-pre-wrap">{s}</p> : <span>{s}</span>;
}

const BTN = "inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel-2 px-3 py-1.5 text-[13px] font-medium hover:border-ink-3 disabled:opacity-50";

export default function ResourceDetail() {
  const { kind = "", id = "" } = useParams();
  const [params] = useSearchParams();
  const project = params.get("project");
  const url = `/api/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}${project ? `?project=${encodeURIComponent(project)}` : ""}`;
  const q = useApi<ResourceDetailT>(url);
  const queryClient = useQueryClient();
  // Название — единственное, что человек читает в списке, и меняется оно
  // чаще всего: голос зовут «Ава», пока его не выбрали, и «рассказчик
  // шортсов» — после. id при этом остаётся: на него ссылаются проекты.
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const r = q.data!;
  const fields = Object.entries(r.fields).filter(([k]) => !HIDDEN.has(k));

  async function save() {
    const name = (draft ?? "").trim();
    if (!name || name === r.name) return setDraft(null);
    setBusy(true);
    setError(null);
    try {
      await patchJson<ResourceDetailT>(url, { name });
      // список вида и обзор ресурсов показывают то же имя — они устарели
      await queryClient.invalidateQueries({ queryKey: [url] });
      await queryClient.invalidateQueries({ queryKey: [`/api/resources/${encodeURIComponent(kind)}`] });
      await queryClient.invalidateQueries({ queryKey: ["/api/resources"] });
      setDraft(null);
    } catch (exc) {
      setError((exc as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Crumbs items={[{ to: "/resources", label: "Ресурсы" }, { to: `/resources/${kind}`, label: r.kind_title }, { label: r.name }]} />
      <PageHeader
        icon={KIND_ICONS[kind] ?? Boxes}
        title={
          draft === null ? (
            r.name
          ) : (
            <input
              autoFocus
              value={draft}
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void save();
                if (e.key === "Escape") setDraft(null);
              }}
              className="w-full max-w-xl rounded-lg border border-line bg-panel-2 px-2 py-1 text-2xl font-semibold tracking-tight outline-none focus:border-ink-3"
            />
          )
        }
        actions={
          draft === null ? (
            <button type="button" className={BTN} onClick={() => setDraft(r.name)} title="сменить название; id останется прежним">
              <Pencil size={14} /> Переименовать
            </button>
          ) : (
            <>
              <button type="button" className={BTN} onClick={() => void save()} disabled={busy}>
                <Check size={14} /> {busy ? "Пишу…" : "Сохранить"}
              </button>
              <button type="button" className={BTN} onClick={() => setDraft(null)} disabled={busy}>
                <X size={14} /> Отмена
              </button>
            </>
          )
        }
        meta={
          <>
            <Badge>{r.kind_title}</Badge>
            <Mono>{r.id}</Mono>
            {r.scope === "project" ? <Badge tone="info">проектный · {r.project}</Badge> : <Badge>общий</Badge>}
          </>
        }
      >
        {error ? <p className="text-[13px] text-bad">{error}</p> : null}
        {r.description ? <p className="max-w-3xl text-[13px] text-ink-2">{r.description}</p> : null}
      </PageHeader>

      {r.example_url || r.example_command ? (
        <Section title={r.example_kind === "audio" ? "Как звучит" : "Как выглядит"} icon={Eye}>
          {r.example_url ? (
            <ResourceExample card={r} className={r.example_kind === "audio" ? "" : "mx-auto h-105 w-auto rounded-xl border border-line"} />
          ) : (
            <Empty>
              Витрины нет. Собрать: <Mono>{r.example_command}</Mono>
            </Empty>
          )}
          {/* пробы подачи: та же фраза с другой скоростью и высотой — разницу
              «живая подача / кривлянье» слышит только человек, поэтому выбор
              здесь, ушами, а не параметром в конвейере */}
          {r.examples?.length ? (
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {r.examples.map((v) => (
                <div key={v.label} className="rounded-lg border border-line bg-panel-2 p-2">
                  <div className="mb-1 text-xs text-ink-2">{v.label}</div>
                  {v.url ? <audio controls preload="none" src={v.url} className="w-full" /> : null}
                </div>
              ))}
            </div>
          ) : r.example_url && r.example_kind === "audio" ? (
            <div className="mt-2 text-xs text-ink-3">
              Пробы подачи (скорость ±10%, высота ±4 Hz): <Mono>{r.example_command} --variants</Mono>
            </div>
          ) : null}
        </Section>
      ) : null}

      {r.techniques.length ? (
        <Section title="Собран из приёмов" icon={BookOpen} aside="условия и замеры — в карточке приёма">
          <div className="flex flex-wrap gap-2">
            {r.techniques.map((t) =>
              t.missing ? (
                <Badge key={t.ref} tone="bad">
                  {t.ref} — такого приёма в библиотеке нет
                </Badge>
              ) : (
                <LinkChip key={t.ref} to={`/library/${t.decision}/${t.id}`}>
                  <span className="text-ink-3">{t.decision_title}:</span> {t.title}
                </LinkChip>
              ),
            )}
          </div>
        </Section>
      ) : null}

      {r.portfolio.length ? (
        <Section title="Портфолио" icon={Images} aside={`${r.portfolio.length} кадров из корня папки`}>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {r.portfolio.map((p) => (
              <a key={p.name} href={p.url ?? "#"} target="_blank" rel="noreferrer" className="block">
                <div className="overflow-hidden rounded-lg border border-line bg-panel-2">
                  {p.url ? <img src={p.url} alt={p.name} className="aspect-square w-full object-cover" loading="lazy" /> : null}
                </div>
                <div className="mt-1 truncate font-mono text-[11px] text-ink-3">{p.name}</div>
              </a>
            ))}
          </div>
        </Section>
      ) : kind === "characters" || kind === "locations" || kind === "props" ? (
        <Empty>
          Портфолио пусто: кадры кладут в корень папки ресурса (<Mono>videoforge resource add-ref</Mono> или{" "}
          <Mono>gen-portfolio</Mono>)
        </Empty>
      ) : null}

      {r.file_url ? (
        <Section title="Файл" icon={AudioLines}>
          <audio controls preload="none" src={r.file_url} className="w-full" />
        </Section>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Section title="Поля" icon={ListTodo}>
          <Card>
            <Table>
              <tbody>
                {fields.map(([k, v]) => (
                  <tr key={k}>
                    <Td className="w-44 font-mono text-xs text-ink-3">{k}</Td>
                    <Td className="text-[13px]">
                      <Value value={v} />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Card>
          <div className="text-xs text-ink-3">
            Файл: <span className="font-mono">{r.path}</span>
          </div>
        </Section>
        <Section title="Где используется" icon={Link2}>
          {r.used_by.length ? (
            <Card>
              <Table>
                <thead>
                  <tr>
                    <Th>Проект</Th>
                    <Th>Где</Th>
                  </tr>
                </thead>
                <tbody>
                  {r.used_by.map((u, i) => (
                    <tr key={i}>
                      <Td>
                        <Link to={u.episode ? `/projects/${u.project}/episodes/${u.episode}` : `/projects/${u.project}`} className="hover:underline">
                          {u.project}
                          {u.episode ? ` / ${u.episode}` : ""}
                        </Link>
                      </Td>
                      <Td className="text-xs text-ink-2">{u.where}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
          ) : (
            <Card>
              <CardBody className="pt-4 text-xs text-ink-3">Ни один проект и ни одна сцена на него не ссылаются.</CardBody>
            </Card>
          )}
        </Section>
      </div>
    </>
  );
}
