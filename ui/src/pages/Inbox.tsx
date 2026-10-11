// «Файлы»: папка обмена между телефоном и этим ПК (`inbox/`, `api/inbox.py`).
//
// Единственная страница пульта, которая не про конвейер: сюда кладут то, что
// надо показать или забрать — видео «посмотри», PDF на правку, скриншот.
// Загрузка и скачивание в одном месте, раскладка по дням: «что я закинул
// сегодня» — единственный вопрос, который к такой папке задают.
//
// Полоса хода загрузки обязательна: видео с телефона уходит минутами, и кнопка
// без отклика читается как «не нажалось» — человек жмёт второй раз и получает
// две копии.

import { useCallback, useRef, useState, type DragEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Download, ExternalLink, FolderUp, Trash2, Upload, X } from "lucide-react";
import { deleteJson, uploadFile, useApi, type ApiError, type Inbox as InboxT, type InboxFile } from "@/api";
import { ErrorBox, Loading, MediaModal, PageHeader, type MediaTab } from "@/components/common";
import { Badge, Card, Empty, Mono, Section, cn } from "@/components/ui/primitives";
import { fmtBytes, fmtDateTime, plural } from "@/lib/format";
import { FILE_KIND_ICONS } from "@/lib/icons";

/** Строка в очереди отправки: живёт только пока грузится, в файлы не попадает. */
interface Sending {
  id: number;
  name: string;
  size: number;
  done: number;
  error?: string;
}

/** Скопировать текст. `navigator.clipboard` есть только на https и localhost, а
 * с телефона пульт открыт по http://100.x (Tailscale) — там остаётся старый
 * способ через скрытое поле. */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    /* нет разрешения или не защищённый источник — пробуем иначе */
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  document.body.removeChild(area);
}

function CopyPath({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      title="скопировать путь"
      onClick={async () => {
        await copyText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1200);
      }}
      className="inline-flex shrink-0 items-center rounded-md p-1 text-ink-3 hover:bg-panel-2 hover:text-ink"
    >
      {done ? <Check className="h-3.5 w-3.5 text-ok" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

/** Что показать вместо картинки: кадр видео, сама картинка, иначе иконка вида. */
function Preview({ f }: { f: InboxFile }) {
  const Icon = FILE_KIND_ICONS[f.kind];
  const box = "flex h-28 w-full items-center justify-center overflow-hidden rounded-lg border border-line bg-panel-2";
  if (f.kind === "image" && f.url)
    return (
      <div className={box}>
        <img src={f.url} alt={f.name} loading="lazy" className="h-full w-full object-cover" />
      </div>
    );
  if (f.kind === "video" && f.url)
    // metadata, а не auto: на странице может лежать десяток роликов, и качать их
    // все ради обложки — это минуты на телефоне
    return (
      <div className={cn(box, "bg-black")}>
        <video src={f.url} preload="metadata" muted playsInline className="h-full w-full object-contain" />
      </div>
    );
  return (
    <div className={cn(box, "text-ink-3")}>
      <Icon className="h-8 w-8" />
    </div>
  );
}

const VIEWABLE: Record<string, MediaTab> = { image: "image", video: "video", audio: "audio" };

function FileCard({ f, onDelete }: { f: InboxFile; onDelete: (f: InboxFile) => void }) {
  const [open, setOpen] = useState(false);
  const tab = VIEWABLE[f.kind];
  return (
    <Card className="flex flex-col gap-2 p-3">
      <button type="button" onClick={() => (tab ? setOpen(true) : undefined)} className={tab ? "cursor-zoom-in" : "cursor-default"}>
        <Preview f={f} />
      </button>
      <div className="min-w-0">
        <div className="truncate text-[13px] font-medium" title={f.name}>
          {f.name}
        </div>
        <div className="text-[11px] text-ink-3">
          {fmtBytes(f.size)} · {fmtDateTime(f.mtime)}
        </div>
      </div>
      <div className="flex items-center gap-1" title={f.path}>
        <Mono className="min-w-0 flex-1 truncate text-[11px]">{f.path}</Mono>
        <CopyPath text={f.path} />
      </div>
      <div className="mt-auto flex items-center gap-1 pt-1">
        {f.download_url ? (
          <a
            href={f.download_url}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[12px] hover:border-ink-3 hover:bg-panel-2"
          >
            <Download className="h-3.5 w-3.5" /> скачать
          </a>
        ) : null}
        {f.url ? (
          <a
            href={f.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[12px] hover:border-ink-3 hover:bg-panel-2"
          >
            <ExternalLink className="h-3.5 w-3.5" /> открыть
          </a>
        ) : null}
        <button
          type="button"
          onClick={() => onDelete(f)}
          title="удалить файл"
          className="ml-auto rounded-md p-1.5 text-ink-3 hover:bg-bad-soft hover:text-bad"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
      {open && tab && f.url ? (
        // ключ вычисляемый, поэтому тип объекта приходится назвать явно
        <MediaModal
          title={f.name}
          sources={{ [tab]: f.url } as Partial<Record<MediaTab, string>>}
          initial={tab}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </Card>
  );
}

export default function Inbox() {
  const q = useApi<InboxT>("/api/inbox");
  const qc = useQueryClient();
  const [queue, setQueue] = useState<Sending[]>([]);
  const [over, setOver] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const nextId = useRef(1);
  const picker = useRef<HTMLInputElement>(null);

  const send = useCallback(
    async (files: FileList | File[] | null) => {
      const list = Array.from(files ?? []);
      if (!list.length) return;
      setFailed(null);
      // по одному, а не пачкой: полоса показывает конкретный файл, а телефонный
      // канал всё равно один — параллельная отправка его только делит
      for (const file of list) {
        const id = nextId.current++;
        setQueue((rows) => [...rows, { id, name: file.name, size: file.size, done: 0 }]);
        try {
          await uploadFile(file, (done) =>
            setQueue((rows) => rows.map((r) => (r.id === id ? { ...r, done } : r))),
          );
          setQueue((rows) => rows.filter((r) => r.id !== id));
          await qc.invalidateQueries({ queryKey: ["/api/inbox"] });
        } catch (err) {
          const message = (err as ApiError).message || "не загрузилось";
          setQueue((rows) => rows.map((r) => (r.id === id ? { ...r, error: message } : r)));
        }
      }
    },
    [qc],
  );

  const drop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setOver(false);
    void send(e.dataTransfer.files);
  };

  const remove = async (f: InboxFile) => {
    if (!window.confirm(`Удалить ${f.name}?`)) return;
    try {
      await deleteJson(`/api/inbox/${encodeURIComponent(f.day || "-")}/${encodeURIComponent(f.name)}`);
      await qc.invalidateQueries({ queryKey: ["/api/inbox"] });
    } catch (err) {
      setFailed((err as ApiError).message);
    }
  };

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const inbox = q.data!;

  return (
    <>
      <PageHeader
        icon={FolderUp}
        title="Файлы"
        meta={
          <span>
            папка обмена с телефоном · {plural(inbox.total, "файл", "файла", "файлов")}
            {inbox.total ? ` · ${fmtBytes(inbox.size)}` : ""}
          </span>
        }
      />

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={drop}
        className={cn(
          "rounded-xl border border-dashed px-5 py-6 text-center transition-colors",
          over ? "border-accent bg-accent-soft" : "border-line bg-panel",
        )}
      >
        <input
          ref={picker}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            void send(e.target.files);
            e.target.value = ""; // тот же файл второй раз тоже должен загружаться
          }}
        />
        <button
          type="button"
          onClick={() => picker.current?.click()}
          className="inline-flex items-center gap-2 rounded-lg border border-line bg-panel-2 px-4 py-2 text-[13px] font-medium hover:border-ink-3"
        >
          <Upload className="h-4 w-4" /> Выбрать файлы
        </button>
        <div className="mt-2 text-xs text-ink-3">
          или перетащите сюда · падает в <Mono>inbox/{inbox.today}/</Mono>
        </div>
      </div>

      {queue.length ? (
        <div className="space-y-2">
          {queue.map((u) => (
            <Card key={u.id} className="px-4 py-3">
              <div className="flex items-center justify-between gap-3 text-[13px]">
                <span className="min-w-0 truncate">{u.name}</span>
                <span className="shrink-0 text-xs text-ink-3">
                  {u.error ? <span className="text-bad">{u.error}</span> : `${Math.round(u.done * 100)}% · ${fmtBytes(u.size)}`}
                </span>
                {u.error ? (
                  <button
                    type="button"
                    onClick={() => setQueue((rows) => rows.filter((r) => r.id !== u.id))}
                    className="shrink-0 rounded-md p-1 text-ink-3 hover:text-ink"
                    aria-label="убрать из списка"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                ) : null}
              </div>
              {u.error ? null : (
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-panel-2">
                  <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${u.done * 100}%` }} />
                </div>
              )}
            </Card>
          ))}
        </div>
      ) : null}

      {failed ? <div className="text-xs text-bad">{failed}</div> : null}

      {inbox.total === 0 ? (
        <Empty>
          Пусто. Выберите файл — он ляжет в <Mono>inbox/{inbox.today}/</Mono>. Открыть эту страницу с телефона: пульт
          запущен с <Mono>--host 0.0.0.0</Mono>, телефон в той же сети Tailscale.
        </Empty>
      ) : null}

      {inbox.days.map((d) => (
        <Section
          key={d.day || "loose"}
          title={d.title}
          aside={
            <span className="flex items-center gap-2">
              {d.day ? <Mono className="text-[11px]">{d.day}</Mono> : <Badge tone="warn">положено мимо страницы</Badge>}
              {plural(d.count, "файл", "файла", "файлов")} · {fmtBytes(d.size)}
            </span>
          }
        >
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {d.files.map((f) => (
              <FileCard key={f.rel} f={f} onDelete={remove} />
            ))}
          </div>
        </Section>
      ))}

      <div className="text-xs text-ink-3">
        Папка <Mono>inbox/</Mono> лежит сбоку от конвейера и в репозиторий не едет. Материал эпизода сюда не кладут — у
        него своя папка. Путь под именем файла копируется кнопкой: по нему файл называют в разговоре.
      </div>
    </>
  );
}
