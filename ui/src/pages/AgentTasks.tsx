import { useEffect, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquare, Plus, Send, Square } from "lucide-react";
import { ApiError, getJson } from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Badge, Card, Empty, cn, type Tone } from "@/components/ui/primitives";

type Status = "draft" | "discussing" | "awaiting_approval" | "working" | "awaiting_answer" | "paused" | "completed" | "failed";
interface Task {
  id: string; title: string; brief: string; status: Status; revision: number;
  phase: string; plan: string; question: string; error: string; round: number;
  max_rounds: number; created_at: string; updated_at: string;
  messages: { id: string; at: string; role: "user" | "claude" | "codex" | "system"; text: string; kind: string }[];
}
interface Overview { tasks: Task[]; capabilities: { codex: boolean; claude: boolean } }
const ROOT = "/api/agent-tasks";
const STATUS: Record<Status, [string, Tone]> = {
  draft: ["Черновик", "neutral"], discussing: ["Обсуждают", "info"],
  awaiting_approval: ["Нужно одобрить план", "warn"], working: ["В работе", "info"],
  awaiting_answer: ["Вопрос к тебе", "warn"], paused: ["Приостановлено", "neutral"],
  completed: ["Завершено", "ok"], failed: ["Ошибка", "bad"],
};
const ROLES = { user: "Ты", claude: "Claude", codex: "Codex", system: "Диспетчер" };
const PHASES: Record<string, string> = {
  claude_understand: "Claude разбирает задачу", codex_understand: "Codex разбирает задачу",
  claude_plan: "Claude составляет общий план", codex_plan: "Codex проверяет план",
  claude_work: "Claude выполняет работу", codex_review: "Codex проверяет результат",
};
const button = "inline-flex items-center justify-center gap-2 rounded-lg border border-line bg-panel-2 px-3 py-2 text-sm font-medium hover:bg-panel disabled:cursor-not-allowed disabled:opacity-45";
const input = "w-full rounded-lg border border-line bg-panel px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-50";

async function post<T>(url: string, data: unknown): Promise<T> {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  if (!response.ok) {
    let message = response.statusText;
    try { const body = await response.json(); message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body); } catch { /* keep HTTP error */ }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

function StatusBadge({ status }: { status: Status }) {
  const [label, tone] = STATUS[status];
  return <Badge tone={tone}>{label}</Badge>;
}

export default function AgentTasks() {
  const cache = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [brief, setBrief] = useState("");
  const [rounds, setRounds] = useState(3);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const overview = useQuery<Overview, ApiError>({ queryKey: [ROOT], queryFn: () => getJson(ROOT), refetchInterval: 5000 });
  const firstId = overview.data?.tasks[0]?.id;
  useEffect(() => { if (!selected && !creating && firstId) setSelected(firstId); }, [firstId, selected, creating]);
  const taskId = creating ? null : selected ?? overview.data?.tasks[0]?.id ?? null;
  const detailUrl = taskId ? `${ROOT}/${encodeURIComponent(taskId)}` : null;
  const detail = useQuery<Task, ApiError>({ queryKey: [detailUrl], queryFn: () => getJson(detailUrl!), enabled: !!detailUrl, refetchInterval: 2000 });
  const task = detail.data;
  const available = overview.data?.capabilities.codex && overview.data?.capabilities.claude;

  async function perform(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null);
    try { await action(); }
    catch (e) {
      setError(e instanceof ApiError && e.status === 409
        ? new Error("Задача уже изменилась. Обновили состояние — проверь его и повтори действие.")
        : e instanceof Error ? e : new Error(String(e)));
    } finally {
      await cache.invalidateQueries({ predicate: q => typeof q.queryKey[0] === "string" && q.queryKey[0].startsWith(ROOT) });
      setBusy(false);
    }
  }

  function create(event: FormEvent) {
    event.preventDefault();
    void perform(async () => {
      const result = await post<Task>(ROOT, { title: title.trim(), brief: brief.trim(), max_rounds: rounds });
      setSelected(result.id); setCreating(false); setTitle(""); setBrief("");
      cache.setQueryData([`${ROOT}/${result.id}`], result);
    });
  }

  function command(action: string) {
    if (!task || !detailUrl) return;
    void perform(async () => { await post(detailUrl + "/" + action, { revision: task.revision }); });
  }

  function message(event: FormEvent) {
    event.preventDefault();
    if (!task || !detailUrl || !drafts[task.id]?.trim()) return;
    const id = task.id;
    void perform(async () => {
      await post(detailUrl + "/message", { revision: task.revision, text: drafts[id].trim() });
      setDrafts(current => ({ ...current, [id]: "" }));
    });
  }

  return <>
    <PageHeader title="Команда ИИ" icon={MessageSquare} meta="Ты ставишь задачу · Claude делает · Codex проверяет"
      actions={<button className={button} disabled={busy} onClick={() => { setCreating(true); setError(null); }}><Plus size={16} />Новая задача</button>}>
      <p className="text-sm text-ink-3">Сначала оба предложат и согласуют план. Изменения в проекте начнутся после твоего одобрения. Вопросы и весь ход работы — здесь.</p>
      {overview.data && <div className="flex flex-wrap gap-2">{(["claude", "codex"] as const).map(agent => <Badge key={agent} tone={overview.data.capabilities[agent] ? "ok" : "warn"}>{ROLES[agent]}: {overview.data.capabilities[agent] ? "клиент найден" : "клиент не найден"}</Badge>)}<span className="text-xs text-ink-3">Авторизация проверяется при запуске.</span></div>}
    </PageHeader>
    {error && <div role="alert"><ErrorBox error={error} /></div>}
    {overview.error && <ErrorBox error={overview.error} />}
    <div className="grid items-start gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
      <aside aria-label="Список задач" className="space-y-2">
        {overview.isLoading && <Loading />}
        {overview.data?.tasks.length === 0 && <Empty>Здесь появятся твои задачи.</Empty>}
        {overview.data?.tasks.map(item => <button key={item.id} disabled={busy} onClick={() => { setSelected(item.id); setCreating(false); setError(null); }} aria-current={item.id === taskId ? "true" : undefined}
          className={cn("block w-full space-y-2 rounded-xl border p-3 text-left disabled:opacity-50", item.id === taskId ? "border-accent bg-accent-soft" : "border-line bg-panel hover:bg-panel-2")}>
          <span className="block wrap-break-word text-sm font-medium">{item.title}</span><StatusBadge status={item.status} />
        </button>)}
      </aside>
      <section className="min-w-0 space-y-4" aria-label="Задача" aria-busy={busy}>
        {detail.error && task && <div role="alert"><ErrorBox error={detail.error} /><p className="mt-1 text-xs text-ink-3">Показана последняя загруженная версия. Пробуем обновить автоматически.</p></div>}
        {(creating || (!taskId && !overview.isLoading)) ? <Card className="p-5">
          <form onSubmit={create} className="space-y-4">
            <h2 className="font-semibold">Новая задача</h2>
            <label className="block space-y-1 text-sm"><span>Название</span><input className={input} value={title} onChange={e => setTitle(e.target.value)} required maxLength={160} disabled={busy} placeholder="Например, сравнить модели для кота" /></label>
            <label className="block space-y-1 text-sm"><span>Что нужно сделать</span><textarea className={input} rows={7} value={brief} onChange={e => setBrief(e.target.value)} required maxLength={20000} disabled={busy} placeholder="Опиши результат, ограничения и как понять, что всё готово. Укажи допустимые расходы, если они нужны." /></label>
            <label className="block space-y-1 text-sm"><span>Максимум кругов работы и проверки</span><input type="number" min={1} max={6} className={cn(input, "max-w-24")} value={rounds} onChange={e => setRounds(Number(e.target.value))} required disabled={busy} /></label>
            <p className="text-xs text-ink-3">Сохранение создаёт черновик. Обсуждение запускается отдельной кнопкой и расходует лимиты CLI.</p>
            <button className={button} disabled={busy || !title.trim() || !brief.trim()}>Сохранить задачу</button>
          </form>
        </Card> : detail.isLoading ? <Loading label="Загружаю задачу…" /> : !task && detail.error ? <ErrorBox error={detail.error} /> : task ? <>
          <Card className="space-y-3 p-5">
            <div className="flex flex-wrap items-start justify-between gap-2"><h2 className="wrap-break-word text-lg font-semibold">{task.title}</h2><StatusBadge status={task.status} /></div>
            <p className="whitespace-pre-wrap wrap-break-word text-sm text-ink-2">{task.brief}</p>
            <div className="flex flex-wrap gap-3 text-xs text-ink-3" role="status"><span>{task.status === "draft" ? "Ожидает запуска" : PHASES[task.phase] || task.phase}</span><span>Круг {task.round} / {task.max_rounds}</span></div>
            <div className="flex flex-wrap gap-2">
              {task.status === "draft" && <button className={button} disabled={busy || !available} onClick={() => command("start")}>Начать обсуждение</button>}
              {(task.status === "paused" || task.status === "failed") && <button className={button} disabled={busy || !available} onClick={() => command("resume")}>Продолжить</button>}
              {["discussing", "working", "awaiting_answer"].includes(task.status) && <button className={cn(button, "text-bad")} disabled={busy} onClick={() => command("stop")}><Square size={14} />Остановить</button>}
            </div>
          </Card>
          {task.error && <ErrorBox error={new Error(task.error)} />}
          {task.plan && <Card className="space-y-3 border-accent/40 p-5"><h3 className="font-semibold">{task.status === "awaiting_approval" ? "План на твоё одобрение" : "План"}</h3><div className="whitespace-pre-wrap wrap-break-word text-sm leading-relaxed">{task.plan}</div>
            {task.status === "awaiting_approval" && <><p className="text-xs text-ink-3">Проверь объём и ограничения. После одобрения Claude начнёт работу, Codex проверит результат. Поправки можно отправить ниже.</p><button className={cn(button, "border-accent text-accent")} disabled={busy || !available} onClick={() => command("approve")}>Одобрить план и начать работу</button></>}
          </Card>}
          {task.question && task.status === "awaiting_answer" && <Card className="space-y-2 border-warn/40 bg-warn-soft p-4" role="status"><h3 className="font-semibold">Нужен твой ответ</h3><p className="whitespace-pre-wrap wrap-break-word text-sm">{task.question}</p><p className="text-xs text-ink-2">Ответь ниже — работа продолжится автоматически.</p></Card>}
          <div className="space-y-3"><h3 className="text-sm font-semibold">Обсуждение и работа</h3>{task.messages.length === 0 && <Empty>Обсуждение ещё не началось.</Empty>}
            {task.messages.map(item => <Card key={item.id} className={cn("p-4", item.role === "user" && "border-accent/30", item.role === "system" && "bg-panel-2")}>
              <div className="mb-2 flex flex-wrap justify-between gap-2 text-xs"><strong>{ROLES[item.role]}</strong><time className="text-ink-3" dateTime={item.at}>{new Date(item.at).toLocaleString("ru-RU")}</time></div><div className="whitespace-pre-wrap wrap-break-word text-sm leading-relaxed">{item.text}</div>
            </Card>)}
          </div>
          {task.status !== "completed" && <Card className="p-4"><form onSubmit={message} className="space-y-3">
            <label className="block space-y-2 text-sm"><span>{task.status === "awaiting_answer" ? "Твой ответ" : "Уточнение или изменение задачи"}</span><textarea className={input} rows={3} maxLength={20000} value={drafts[task.id] ?? ""} onChange={e => setDrafts(current => ({ ...current, [task.id]: e.target.value }))} disabled={busy} placeholder="Оба участника получат это сообщение." /></label>
            <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-ink-3">Во время работы сообщение будет учтено на следующем шаге.</p><button className={button} disabled={busy || !drafts[task.id]?.trim()}><Send size={14} />Отправить</button></div>
          </form></Card>}
        </> : null}
      </section>
    </div>
  </>;
}
