// Правка плана монтажа прямо в монтажном виде.
//
// Правится ФАЙЛ, а не ролик: `05_edit/edit_plan.json`. Готовый `final.mp4`
// остаётся прежним, пока его не пересоберут — осознанным действием, а не
// побочным эффектом движения ползунка.
//
// Что можно менять, решает сервер (`api/planedit.py`), а не эта форма: он
// собирает разрешённую правку сам и отвергает всё, что вышло за белый список.
// Здесь только поля, которые в него входят, и честный показ того, что сервер
// ответил, — включая починки линта: человек двинул громкость за предел, её
// подтянули к границе, и он должен это увидеть, а не гадать, почему не так.

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Crosshair, Hammer, Plus, Save, Undo2, Volume2, X } from "lucide-react";
import { patchJson, postJson, useApi, type ApiError, type EditPlanDoc, type PlanSaved } from "@/api";
import { Badge, Empty, cn } from "@/components/ui/primitives";

type Plan = Record<string, any>;

const TRANSITIONS = ["cut", "crossfade", "dip_black", "fade_in"] as const;

// Хвост включают одним щелчком, значит у него должно быть готовое умолчание —
// то же, с которого начинали руками на живых шортсах: вдвое медленнее, 3 с,
// с падением голоса. Дальше человек крутит числа, а не собирает объект с нуля.
const OUTRO_DEFAULT = {
  take_sec: 3,
  speed: 0.5,
  pitch_drop: true,
  zoom: 0,
  desaturate: 0,
  vignette: false,
  why: "",
};

function num(value: unknown, fallback = 0): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** Число в строке события: узкое поле, подпись — во всплывающей подсказке. */
function Num({
  value,
  onChange,
  title,
  step = 0.1,
  width = "w-16",
  placeholder,
}: {
  value: number | null;
  onChange: (v: number | null) => void;
  title: string;
  step?: number;
  width?: string;
  placeholder?: string;
}) {
  return (
    <input
      type="number"
      step={step}
      title={title}
      placeholder={placeholder}
      value={value === null ? "" : value}
      // пусто — это не ноль, а «как в ресурсе»: у звука свой уровень, и обнулить
      // его молча значило бы сделать все звуки одинаково громкими
      onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      className={cn(
        width,
        "rounded-md border border-line bg-panel px-1.5 py-0.5 text-[11px] tabular-nums focus:border-ink-3 focus:outline-none",
      )}
    />
  );
}

/** Секунда события на таймлайне готового ролика + перемотка плеера на неё.
 *
 * Эффект и звук живут в секундах ФИНАЛА, а не внутри сцены, поэтому число здесь
 * — не абстракция: рядом стоит плеер, и единственный честный способ поставить
 * удар на склейку — доехать до неё и нажать «сюда». Обратный ход тоже нужен:
 * увидел строку в списке — прыгнул и послушал, что там на самом деле. */
function At({
  value,
  onChange,
  onSeek,
}: {
  value: number;
  onChange: (v: number) => void;
  onSeek?: (t: number) => void;
}) {
  return (
    <span className="inline-flex items-center gap-0.5">
      <Num value={value} step={0.05} width="w-16" title="секунда готового ролика" onChange={(v) => onChange(num(v))} />
      {onSeek ? (
        <button
          type="button"
          title="перемотать плеер на эту секунду"
          onClick={() => onSeek(value)}
          className="rounded p-0.5 text-ink-3 hover:bg-panel-2 hover:text-ink"
        >
          <Crosshair className="h-3 w-3" />
        </button>
      ) : null}
    </span>
  );
}

function Why({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <input
      type="text"
      value={value}
      placeholder="зачем"
      title="зачем это здесь — читает человек на воротах и следующий монтажёр"
      onChange={(e) => onChange(e.target.value)}
      className="min-w-24 flex-1 rounded-md border border-line bg-panel px-1.5 py-0.5 text-[11px] focus:border-ink-3 focus:outline-none"
    />
  );
}

function Drop({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      title="убрать из плана"
      onClick={onClick}
      className="rounded p-0.5 text-ink-3 hover:bg-bad-soft hover:text-bad"
    >
      <X className="h-3.5 w-3.5" />
    </button>
  );
}

function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="встанет на секунде, где сейчас стоит плеер"
      className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-0.5 text-[11px] text-ink-2 hover:border-ink-3 hover:text-ink"
    >
      <Plus className="h-3 w-3" /> {label}
    </button>
  );
}

/** Вставить событие в список по времени, а не в конец: список читается как
 * лента ролика. Правка числа потом порядок не пересобирает — иначе строка
 * уезжала бы из-под курсора прямо во время набора. */
function insertAt<T extends { at_sec: number }>(items: T[], item: T): T[] {
  const next = [...items];
  const i = next.findIndex((x) => num(x.at_sec) > item.at_sec);
  next.splice(i < 0 ? next.length : i, 0, item);
  return next;
}

function Field({
  label,
  hint,
  value,
  step = 0.1,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  step?: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="flex items-center gap-2" title={hint}>
      <span className="w-40 shrink-0 text-right text-ink-3">{label}</span>
      <input
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-24 rounded-md border border-line bg-panel px-2 py-1 text-[12px] tabular-nums focus:border-ink-3 focus:outline-none"
      />
    </label>
  );
}

export function PlanEditor({
  project,
  episode,
  shiftHint = 0,
  now,
  onSeek,
}: {
  project: string;
  episode: string;
  /** сдвиг, подобранный глазом на дорожке слов песни: его можно перенести в план */
  shiftHint?: number;
  /** где сейчас стоит плеер: новое событие встаёт туда, а не в «примерно там» */
  now?: () => number;
  /** перемотать плеер на секунду события — проверить ухом и глазом, что там */
  onSeek?: (t: number) => void;
}) {
  const url = `/api/projects/${encodeURIComponent(project)}/episodes/${encodeURIComponent(episode)}/plan`;
  const q = useApi<EditPlanDoc>(url);
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Plan | null>(null);
  const [saved, setSaved] = useState<PlanSaved | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [built, setBuilt] = useState<{ ok: boolean; message: string } | null>(null);
  // проба звука: один проигрыватель на форму, чтобы второе нажатие перебивало
  // первое, а не звучало поверх — иначе ухом не выберешь
  const sample = useRef<HTMLAudioElement | null>(null);

  // черновик берётся с сервера и сбрасывается, когда приходит новая версия:
  // так после записи в форме оказывается то, что реально легло в файл
  useEffect(() => {
    if (q.data?.plan) setDraft(structuredClone(q.data.plan) as Plan);
  }, [q.data?.plan, q.data?.fingerprint]);

  if (q.isLoading) return <div className="text-xs text-ink-3">Читаю план монтажа…</div>;
  if (q.error || !q.data?.exists || !draft) {
    return (
      <Empty>
        Плана монтажа нет — его делает стадия edit: <span className="font-mono">videoforge run {project}/{episode} --only edit</span>
      </Empty>
    );
  }

  const music = (draft.music ?? {}) as Plan;
  const effects = (draft.effects ?? []) as Plan[];
  const sfx = (draft.sfx ?? []) as Plan[];
  const outro = (draft.outro ?? null) as Plan | null;
  const kinds = q.data.effect_kinds ?? [];
  const sounds = q.data.sfx ?? [];
  const patch = (change: (p: Plan) => void) => {
    const next = structuredClone(draft) as Plan;
    change(next);
    setDraft(next);
    setSaved(null);
  };
  const dirty = JSON.stringify(draft) !== JSON.stringify(q.data.plan);
  // секунда плеера с точностью до сотой: кадр — это 0.04 с, и лишние знаки
  // здесь означали бы точность, которой у пальца на перемотке нет
  const at = () => Math.max(0, Math.round((now?.() ?? 0) * 100) / 100);
  const listen = (src: string | null | undefined) => {
    if (!src) return;
    sample.current?.pause();
    sample.current = new Audio(src);
    void sample.current.play().catch(() => undefined);
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await patchJson<PlanSaved>(url, {
        plan: draft,
        base_fingerprint: q.data.fingerprint,
      });
      setSaved(result);
      // монтажный вид читает тот же план — после записи он устарел
      await queryClient.invalidateQueries({ queryKey: [url] });
      await queryClient.invalidateQueries({
        queryKey: [`/api/projects/${encodeURIComponent(project)}/episodes/${encodeURIComponent(episode)}/montage`],
      });
    } catch (exc) {
      const api = exc as ApiError;
      setError(
        api.status === 409
          ? `${api.message} (правку не потеряли — скопируй числа и обнови)`
          : api.message,
      );
    } finally {
      setBusy(false);
    }
  };

  // пересборка — отдельное осознанное действие, а не хвост записи в план:
  // это единственный момент, когда меняется сам ролик
  const rebuild = async () => {
    setRebuilding(true);
    setBuilt(null);
    setError(null);
    try {
      const result = await postJson<{ ok: boolean; status: string; message: string }>(
        `/api/projects/${encodeURIComponent(project)}/episodes/${encodeURIComponent(episode)}/rebuild`,
        {},
      );
      setBuilt({ ok: result.ok, message: result.message });
      await queryClient.invalidateQueries({
        queryKey: [`/api/projects/${encodeURIComponent(project)}/episodes/${encodeURIComponent(episode)}/montage`],
      });
    } catch (exc) {
      setBuilt({ ok: false, message: (exc as ApiError).message });
    } finally {
      setRebuilding(false);
    }
  };

  return (
    <div className="space-y-2 text-[12px]">
      <div className="grid gap-x-6 gap-y-1.5 md:grid-cols-2">
        <Field
          label="Музыка, дБ"
          hint="уровень трека относительно голоса: −12 значит на 12 дБ тише речи"
          value={num(music.gain_db, -12)}
          onChange={(v) => patch((p) => ((p.music ??= {}), (p.music.gain_db = v)))}
        />
        <Field
          label="Начало трека, с"
          hint="в какую секунду ролика начинается трек; отрицательное — пропустить столько секунд самого трека (так дроп сажают на кульминацию)"
          value={num(music.start_sec)}
          onChange={(v) => patch((p) => ((p.music ??= {}), (p.music.start_sec = v)))}
        />
        <Field
          label="Даккинг, дБ"
          hint="насколько музыка уходит под речь"
          value={num(music.duck_db, 10)}
          onChange={(v) => patch((p) => ((p.music ??= {}), (p.music.duck_db = v)))}
        />
        <label className="flex items-center gap-2" title="выключить музыку в этом ролике">
          <span className="w-40 shrink-0 text-right text-ink-3">Музыка включена</span>
          <input
            type="checkbox"
            checked={music.enabled !== false}
            onChange={(e) => patch((p) => ((p.music ??= {}), (p.music.enabled = e.target.checked)))}
          />
        </label>
        <Field
          label="Затухание в начале, с"
          value={num(draft.fade_in_sec)}
          onChange={(v) => patch((p) => (p.fade_in_sec = v))}
        />
        <Field
          label="Затухание в конце, с"
          value={num(draft.fade_out_sec)}
          onChange={(v) => patch((p) => (p.fade_out_sec = v))}
        />
      </div>

      {shiftHint ? (
        <button
          type="button"
          className="rounded-md border border-warn/50 bg-warn-soft px-2 py-1 text-[12px] text-warn hover:border-warn"
          onClick={() =>
            patch((p) => ((p.music ??= {}), (p.music.start_sec = num(music.start_sec) + shiftHint)))
          }
        >
          Перенести сдвиг {shiftHint > 0 ? "+" : ""}
          {shiftHint.toFixed(2)} с в начало трека
        </button>
      ) : null}

      {Array.isArray(draft.scenes) && draft.scenes.length ? (
        <div className="flex flex-wrap gap-2 pt-1">
          {(draft.scenes as Plan[]).map((scene, i) => (
            <label key={String(scene.scene_id)} className="flex items-center gap-1">
              <span className="font-mono text-[11px] text-ink-3">{String(scene.scene_id)}</span>
              <select
                value={String(scene.transition_in?.type ?? "cut")}
                title="переход В эту сцену; у первой его снимет линт — переходить неоткуда"
                onChange={(e) =>
                  patch((p) => {
                    const s = (p.scenes as Plan[])[i];
                    s.transition_in = {
                      type: e.target.value,
                      duration_sec: e.target.value === "cut" ? 0 : num(s.transition_in?.duration_sec, 0.4) || 0.4,
                    };
                  })
                }
                className="rounded-md border border-line bg-panel px-1 py-0.5 text-[11px]"
              >
                {TRANSITIONS.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      ) : null}

      {/* Эффекты, звуки и хвост живут в секундах ГОТОВОГО РОЛИКА, а не внутри
          сцены: удар ставят НА склейку, пролёт — ДО неё. Поэтому у каждого
          события секунда финала и кнопка «на курсоре»: рядом плеер, и это
          единственный способ попасть в склейку, а не рядом с ней */}
      <div className="space-y-1 border-t border-line pt-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-ink-2">Эффекты кадра</span>
          <span className="text-ink-3">
            {effects.length || "нет"} · секунда финала, вид, длительность, сила 0–2
          </span>
          <AddButton
            label="эффект на курсоре"
            onClick={() =>
              patch((p) => {
                p.effects = insertAt((p.effects ?? []) as any[], {
                  at_sec: at(),
                  duration_sec: 0.4,
                  kind: kinds[0] ?? "zoom_punch",
                  strength: 1,
                  why: "",
                });
              })
            }
          />
        </div>
        {effects.map((e, i) => (
          <div key={i} className="flex flex-wrap items-center gap-1.5">
            <At
              value={num(e.at_sec)}
              onSeek={onSeek}
              onChange={(v) => patch((p) => ((p.effects as Plan[])[i].at_sec = v))}
            />
            <select
              value={String(e.kind ?? kinds[0] ?? "")}
              title="вид эффекта — список из схемы плана, рендерер знает ровно эти"
              onChange={(ev) => patch((p) => ((p.effects as Plan[])[i].kind = ev.target.value))}
              className="rounded-md border border-line bg-panel px-1 py-0.5 text-[11px]"
            >
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <Num
              value={num(e.duration_sec, 0.4)}
              step={0.1}
              title="длительность, с; дольше 6 с линт подрежет — это уже не эффект, а цветокоррекция всего ролика"
              onChange={(v) => patch((p) => ((p.effects as Plan[])[i].duration_sec = num(v, 0.4)))}
            />
            <Num
              value={num(e.strength, 1)}
              step={0.1}
              width="w-14"
              title="сила 0–2; 1 — как задумано по умолчанию"
              onChange={(v) => patch((p) => ((p.effects as Plan[])[i].strength = num(v, 1)))}
            />
            <Why
              value={String(e.why ?? "")}
              onChange={(v) => patch((p) => ((p.effects as Plan[])[i].why = v))}
            />
            <Drop onClick={() => patch((p) => (p.effects as Plan[]).splice(i, 1))} />
          </div>
        ))}
      </div>

      <div className="space-y-1 border-t border-line pt-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-ink-2">Звуки</span>
          <span className="text-ink-3">
            {sfx.length || "нет"} · id из resources/sfx/, дБ относительно голоса (пусто — как в ресурсе)
          </span>
          <AddButton
            label="звук на курсоре"
            onClick={() =>
              patch((p) => {
                p.sfx = insertAt((p.sfx ?? []) as any[], {
                  at_sec: at(),
                  id: sounds[0]?.id ?? "",
                  gain_db: null,
                  why: "",
                });
              })
            }
          />
        </div>
        {sounds.length === 0 ? (
          <div className="text-warn">
            В resources/sfx/ пусто — звуки синтезируются командой{" "}
            <span className="font-mono">videoforge resource gen-sfx</span>
          </div>
        ) : null}
        {sfx.map((s, i) => {
          const chosen = sounds.find((c) => c.id === s.id);
          return (
            <div key={i} className="flex flex-wrap items-center gap-1.5">
              <At
                value={num(s.at_sec)}
                onSeek={onSeek}
                onChange={(v) => patch((p) => ((p.sfx as Plan[])[i].at_sec = v))}
              />
              {/* выбор, а не ввод: неизвестный id линт БЛОКИРУЕТ, и одна
                  опечатка отвергала бы всю правку целиком */}
              <select
                value={String(s.id ?? "")}
                title="звук из resources/sfx/ — список тот же, по которому судит линт"
                onChange={(ev) => patch((p) => ((p.sfx as Plan[])[i].id = ev.target.value))}
                className="rounded-md border border-line bg-panel px-1 py-0.5 text-[11px]"
              >
                {sounds.some((c) => c.id === s.id) ? null : (
                  <option value={String(s.id ?? "")}>{String(s.id ?? "—")} (нет такого)</option>
                )}
                {sounds.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id} · {c.kind}
                  </option>
                ))}
              </select>
              <button
                type="button"
                title="послушать звук — по имени он не звучит"
                disabled={!chosen?.url}
                onClick={() => listen(chosen?.url)}
                className="rounded p-0.5 text-ink-3 hover:bg-panel-2 hover:text-ink disabled:opacity-40"
              >
                <Volume2 className="h-3.5 w-3.5" />
              </button>
              <Num
                value={s.gain_db === null || s.gain_db === undefined ? null : num(s.gain_db)}
                step={1}
                width="w-16"
                placeholder={chosen ? String(chosen.gain_db) : "дБ"}
                title="уровень относительно голоса, −30…0; пусто — как в ресурсе. Звук, который перекрикивает речь, — брак, а не акцент"
                onChange={(v) => patch((p) => ((p.sfx as Plan[])[i].gain_db = v))}
              />
              <Why
                value={String(s.why ?? "")}
                onChange={(v) => patch((p) => ((p.sfx as Plan[])[i].why = v))}
              />
              <Drop onClick={() => patch((p) => (p.sfx as Plan[]).splice(i, 1))} />
            </div>
          );
        })}
      </div>

      {/* Единственное поле формы, которое МЕНЯЕТ ХРОНОМЕТРАЖ, — и потому оно
          выключено по умолчанию и включается явно. Замедлять середину нельзя
          (уедет всё, что после), конец можно: после него ничего нет */}
      <div className="space-y-1 border-t border-line pt-2">
        <label className="flex flex-wrap items-center gap-2" title="последние секунды идут медленнее; ролик становится длиннее">
          <input
            type="checkbox"
            checked={outro !== null}
            onChange={(e) => patch((p) => (p.outro = e.target.checked ? { ...OUTRO_DEFAULT } : null))}
          />
          <span className="text-ink-2">Замедленный хвост</span>
          <span className="text-ink-3">
            {outro
              ? `ролик станет длиннее на ${(
                  num(outro.take_sec, 3) / Math.max(0.25, num(outro.speed, 0.5)) -
                  num(outro.take_sec, 3)
                ).toFixed(2)} с`
              : "выключен — хронометраж остаётся как есть"}
          </span>
        </label>
        {outro ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <Num
              value={num(outro.take_sec, 3)}
              step={0.5}
              title="сколько секунд с конца замедлить (0.5–15 и не больше половины ролика)"
              onChange={(v) => patch((p) => (p.outro.take_sec = num(v, 3)))}
            />
            <Num
              value={num(outro.speed, 0.5)}
              step={0.05}
              width="w-14"
              title="скорость 0.25–1.0; 0.5 — вдвое медленнее"
              onChange={(v) => patch((p) => (p.outro.speed = num(v, 0.5)))}
            />
            <label className="flex items-center gap-1 text-ink-3" title="ронять голос вместе со скоростью — тот самый приём «сигмы»; без него честное slow-motion">
              <input
                type="checkbox"
                checked={outro.pitch_drop !== false}
                onChange={(e) => patch((p) => (p.outro.pitch_drop = e.target.checked))}
              />
              голос вниз
            </label>
            <Num
              value={num(outro.zoom)}
              step={0.05}
              width="w-14"
              title="наезд за время хвоста: 0.15 → +15% к концу"
              onChange={(v) => patch((p) => (p.outro.zoom = num(v)))}
            />
            <Num
              value={num(outro.desaturate)}
              step={0.1}
              width="w-14"
              title="уход цвета к концу хвоста, 0…1"
              onChange={(v) => patch((p) => (p.outro.desaturate = num(v)))}
            />
            <label className="flex items-center gap-1 text-ink-3" title="затемнение краёв на хвосте">
              <input
                type="checkbox"
                checked={outro.vignette === true}
                onChange={(e) => patch((p) => (p.outro.vignette = e.target.checked))}
              />
              виньетка
            </label>
            <Why value={String(outro.why ?? "")} onChange={(v) => patch((p) => (p.outro.why = v))} />
          </div>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <button
          type="button"
          disabled={!dirty || busy}
          onClick={save}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border px-2.5 py-1 font-medium",
            dirty && !busy
              ? "border-accent bg-accent-soft text-accent hover:border-accent"
              : "border-line text-ink-3",
          )}
        >
          <Save className="h-3.5 w-3.5" /> {busy ? "Пишу…" : "Записать в план"}
        </button>
        {dirty ? (
          <button
            type="button"
            onClick={() => setDraft(structuredClone(q.data!.plan) as Plan)}
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-ink-3 hover:text-ink"
          >
            <Undo2 className="h-3.5 w-3.5" /> вернуть
          </button>
        ) : null}
        <button
          type="button"
          disabled={dirty || rebuilding}
          title={
            dirty
              ? "сначала запиши правку в план — пересобирать пока нечего"
              : "собрать ролик заново по этому плану (та же стадия, что videoforge run --only assemble)"
          }
          onClick={rebuild}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border px-2.5 py-1 font-medium",
            !dirty && !rebuilding ? "border-line hover:border-ink-3" : "border-line text-ink-3",
          )}
        >
          <Hammer className="h-3.5 w-3.5" /> {rebuilding ? "Собираю…" : "Пересобрать"}
        </button>
        <span className="text-ink-3">
          правка меняет план; ролик меняется только пересборкой{" "}
          <span className="font-mono">
            (videoforge run {project}/{episode} --only assemble)
          </span>
        </span>
      </div>
      {built ? (
        <div className={cn("rounded-md px-2 py-1", built.ok ? "bg-ok-soft text-ok" : "bg-bad-soft text-bad")}>
          {built.message}
        </div>
      ) : null}

      {error ? <div className="rounded-md bg-bad-soft px-2 py-1 text-bad">{error}</div> : null}
      {saved ? (
        <div className="space-y-1">
          <div className="text-ok">
            Записано · план теперь <Badge tone="info">{saved.author}</Badge>
            {saved.backup ? <span className="text-ink-3"> · прежний рядом: {saved.backup}</span> : null}
          </div>
          {saved.fixes.map((f, i) => (
            <div key={i} className="text-warn">
              линт поправил: {f}
            </div>
          ))}
          {saved.warnings.map((w, i) => (
            <div key={i} className="text-ink-2">
              {w}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
