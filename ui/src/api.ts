// Типы ответов API и загрузчик. Один источник — `videoforge/api/data.py`;
// поля здесь повторяют его словари, лишнего не придумываем.

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* тело не JSON — оставляем statusText */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export function useApi<T>(url: string | null): UseQueryResult<T, ApiError> {
  return useQuery<T, ApiError>({
    queryKey: [url],
    queryFn: () => getJson<T>(url as string),
    enabled: url !== null,
  });
}

// ---------------------------------------------------------------- общее

export type Scope = "global" | "project" | null;

export interface ResourceCard {
  kind: string;
  kind_title: string;
  id: string;
  name: string;
  description: string;
  scope: Scope;
  project: string | null;
  preview_url: string | null;
  file_url: string | null;
  /** витрина ресурса: клип со стилем субтитров, проба голоса (`resource gen-example`) */
  example_url: string | null;
  example_kind: "image" | "video" | "audio" | null;
  missing: boolean;
  portfolio_count?: number;
  slot?: string;
  slot_title?: string;
  error?: string;
  /** страница проекта: у общего — другие проекты, где он стоит (пусто — больше нигде) */
  shared_with?: string[];
  /** страница проекта, визуальные виды: эпизоды этого проекта, где ресурс в касте, и сколько сцен */
  used_in?: { episode: string; scenes: number }[];
}

export type StageStatus = "pending" | "running" | "done" | "failed" | "skipped" | "blocked";

export interface StageRow {
  name: string;
  status: StageStatus;
  gate_after: boolean;
  approved: boolean;
  awaiting: boolean;
  message: string;
  error: string | null;
  elapsed_sec: number | null;
  cost_usd: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface AccountBrief {
  id: string;
  network: "youtube" | "tiktok" | "instagram";
  network_title: string;
  handle: string;
  url: string;
  title: string;
}

// ---------------------------------------------------------------- проекты

export interface ProjectItem {
  id: string;
  name: string;
  description?: string;
  workflow?: string;
  platforms?: string[];
  platform_titles?: string[];
  language?: string;
  aspect_ratio?: string;
  target_duration_sec?: number;
  status: "live" | "demo";
  kind?: ProjectKind;
  accounts?: AccountBrief[];
  episodes?: number;
  spent_usd?: number;
  budget_per_episode_usd?: number;
  error?: string;
}

/** channel — канал или серия; test — полигон качества (страница «Тесты»). */
export type ProjectKind = "channel" | "test";

export interface ProjectsOverview {
  items: ProjectItem[];
  /** образцы и полигоны с hidden: true в project.yaml — в списке не показываются */
  hidden: string[];
  other_folders: string[];
  bench_folder: string;
}

export interface EpisodeSummary {
  id: string;
  project: string;
  ref: string;
  title?: string;
  brief?: string;
  workflow?: string;
  scenes?: number;
  duration_sec?: number | null;
  spent_usd?: number;
  budget_usd?: number;
  awaiting_approval?: string | null;
  final_video_url?: string | null;
  created_at?: string;
  stages?: StageRow[];
  published?: boolean; // упаковано (стадия publish), а НЕ выложено
  upload?: UploadState | null; // куда ролик уехал на самом деле
  error?: string;
}

/** Задача человека: действие с концом, у которого есть код (T-07). */
export interface TodoTask {
  code: string;
  text: string;
  done: boolean;
  tag: string;
  done_at: string;
  /** id записи features/, чей последний шаг эта задача; "" — без связи */
  feature: string;
  /** заголовок той записи; "" — записи уже нет (ссылка в никуда) */
  feature_title: string;
}

export interface TodoOverview {
  open: TodoTask[];
  done: TodoTask[];
  path: string;
}

/** Ответ кнопки «сделано»: задача и подсказка, какая запись в Планах ждала этого шага. */
export interface TodoClosed extends TodoTask {
  hint: string;
}

export interface UploadPlatform {
  network: string;
  url: string;
  at: string | null;
  privacy: string;
}

export interface UploadState {
  state: string; // done | failed | dry_run | pending
  privacy: string; // private | unlisted | public
  platforms: UploadPlatform[];
}

export interface ResearchRow {
  slug: string;
  exists: boolean;
  title: string;
  url: string;
  subscribers: number | null;
  blueprint_url: string | null;
  report_url: string | null;
}

export interface SourceFile {
  name: string;
  url: string | null;
  kind: FileKind;
  size: number;
  mtime: string;
}

export interface SourceRow {
  id: string;
  kind: "clips" | "folder";
  path: string;
  readme_md: string;
  groups: { dir: string; files: SourceFile[] }[];
  error?: string;
  // только у нарезок (source.json)
  title?: string;
  url?: string;
  file?: string;
  channel?: string;
  duration_sec?: number;
  language?: string;
  word_count?: number;
  transcriber?: string;
  chapters?: number;
  heatmap?: boolean;
  proxy_url?: string | null;
  highlights?: {
    candidates: number;
    rejected: number;
    generated_at: string | null;
    report_url: string | null;
  } | null;
}

// ---------------------------------------------------------------- очередь и расписание

export type QueueState = "published" | "ready" | "waiting" | "unfinished";

export interface QueueRow {
  episode: string;
  ref: string;
  state: QueueState;
  state_title: string;
  title: string;
  note: string;
}

/** Слот плана: когда и что уйдёт. `episode: null` — к этому часу выкладывать
 * нечего; `pending` — слот уже прошёл и ждёт ближайшего тика планировщика. */
export interface PlanRow {
  at: string;
  episode: string | null;
  ref: string | null;
  title: string;
  pending: boolean;
}

export type RunwayLevel = "ok" | "warn" | "bad" | "none";

export interface Runway {
  ready: number;
  waiting: number;
  unfinished: number;
  per_day: number;
  ends: string | null; // первый пустой слот
  days: number | null; // до него, в днях
  level: RunwayLevel;
}

export interface RunwayBrief extends Runway {
  paused: boolean;
}

export interface Notice {
  level: "info" | "warn" | "bad";
  text: string;
}

export interface QueueTick {
  at: string;
  due: boolean;
  slot: string | null;
  target: string | null; // секунда выхода слота: слот + разброс
  handled: string | null; // слот, по которому запускали публикацию
  handled_at: string | null;
  attempts: number;
  failed: boolean; // попытка упала — следующий тик повторит
  item: string | null;
  why: string;
  stale: boolean;
}

export interface QueueView {
  schedule: { times: string[]; paused: boolean; per_day: number; active: boolean; spread_min: number };
  items: QueueRow[];
  plan: PlanRow[];
  runway: Runway;
  notices: Notice[];
  tick: QueueTick | null;
  tick_every_min: number;
  live: boolean;
  now: string;
  path: string;
}

export interface ProjectDetail {
  id: string;
  name: string;
  description: string;
  workflow: string;
  workflow_exists: boolean;
  sources: SourceRow[];
  status: "live" | "demo";
  kind?: ProjectKind;
  queue: QueueView;
  accounts: AccountBrief[];
  channel: { name: string; platforms: string[]; language: string };
  platform_titles: string[];
  video: {
    aspect_ratio: string;
    target_duration_sec: number;
    scene_duration_sec: number;
    scene_tail_sec: number;
    captions: boolean;
    captions_language: string | null;
    music: boolean;
    fade_in_sec: number;
    fade_out_sec: number;
  };
  budget_per_episode_usd: number;
  scenes_estimate: number;
  resources: ResourceCard[];
  local_resources: ResourceCard[];
  research: ResearchRow[];
  rules_md: string;
  episodes: EpisodeSummary[];
  spent_usd: number;
  path: string;
}

// ---------------------------------------------------------------- тесты

/** Карточка проверочного ролика: обложка, что проверяли, когда собран. */
export interface TestCard {
  ref: string;
  project: string;
  project_name: string;
  episode: string;
  title: string;
  question: string;
  check_md: string;
  check_path: string;
  duration_sec: number | null;
  made_at: string;
  video_url: string | null;
  poster_url: string | null;
  aspect_ratio: string;
  scenes: number;
  spent_usd: number;
  workflow: string;
  awaiting_approval: string | null;
  failed_stages: string[];
  stages: StageRow[];
  report_url: string;
  path: string;
}

export interface TestsOverview {
  projects: { id: string; name: string; description: string; workflow: string; episodes: number }[];
  items: TestCard[];
  pending: TestCard[];
  total: number;
}

// ---------------------------------------------------------------- публикация

export interface PublishPlatformRow {
  platform: string;
  state: "done" | "dry_run" | "skipped" | "failed" | "pending";
  video_id: string;
  url: string;
  provider: string;
  account: string;
  warnings: string[];
  error: string;
  /** «было → станет» по полям: заполняет правка уехавшего, у загрузки пусто */
  changes: string[];
}

export interface PublishReport {
  dry_run: boolean;
  state: "pending" | "dry_run" | "done" | "failed";
  ok: boolean;
  title: string;
  video: string;
  warnings: string[];
  platforms: PublishPlatformRow[];
}

export interface PublishState {
  state: "pending" | "dry_run" | "done" | "failed";
  at: string | null;
  error: string;
  platforms: Record<
    string,
    { video_id: string; url: string; at: string | null; provider: string; warnings: string[] }
  >;
}

/** POST на пульте: тот же код, что у `videoforge publish`. По умолчанию сухой прогон. */
export async function postJson<T>(url: string, body: unknown): Promise<T> {
  return sendJson<T>("POST", url, body);
}

/** DELETE: убрать входящий файл. Тела у запроса нет — только адрес. */
export async function deleteJson<T>(url: string): Promise<T> {
  return sendJson<T>("DELETE", url, null);
}

/** PATCH: правка плана монтажа. Тело — весь план и отпечаток прочитанного. */
export async function patchJson<T>(url: string, body: unknown): Promise<T> {
  return sendJson<T>("PATCH", url, body);
}

async function sendJson<T>(method: string, url: string, body: unknown): Promise<T> {
  const resp = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const payload = (await resp.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      /* тело не JSON — оставляем statusText */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

// ---------------------------------------------------------------- эпизод

export interface SceneRow {
  id: string;
  order: number;
  narration: string;
  visual_prompt: string;
  motion_prompt: string;
  translated: { visual: string; motion: string } | null;
  duration_sec: number;
  planned_sec: number | null;
  continues: string | null;
  // плашка поверх кадра: caption — вывод реплики, source — источник, label — ярлык пункта
  overlay: { text: string; kind: "caption" | "source" | "label"; emphasis: string[] } | null;
  cast: string[];
  // референсы композиции сцены: нашла стадия по вердикту проверки или положил человек
  references: { path: string; url: string | null }[];
  // правки к описанию из вердиктов проверки — дописаны к промпту
  fixes: string[];
  // вердикт проверки по описанию: ok — про то, note — детали, block — не про то
  review: {
    severity: string;
    see: string;
    issue: string;
    fix: string;
    search: string;
    reference: string;
    model: string;
  } | null;
  checks: { image: string; video: string; audio: string; notes: Record<string, string> };
  attempts: Record<string, number>;
  cost_usd: number;
  source: { path: string; start_sec: number; end_sec: number } | null;
  assets: {
    image: string | null;
    video: string | null;
    audio: string | null;
    clip: string | null;
    thumbs: string[];
  };
}

export interface FinalCheck {
  label: string;
  value: string;
  ok: boolean;
  target: string;
}

export interface QaFinding {
  code: string;
  severity: string;
  message: string;
  scene_id?: string | null;
}

export interface LedgerEntry {
  at: string;
  stage: string;
  provider: string;
  usd: number;
  scene_id: string | null;
  note: string;
}

export interface EpisodeDetail extends Omit<EpisodeSummary, "scenes"> {
  language: string;
  captions_language: string | null;
  aspect_ratio: string;
  resources: Record<string, string | null>;
  publish: { title: string; description: string; hashtags: string[] };
  cast: ResourceCard[];
  scenes: SceneRow[];
  translations: {
    language: string;
    llm: string;
    generated_at: string | null;
    fresh: number;
    stale: number;
  } | null;
  final: Record<string, unknown> & { video: string | null; duration_sec: number | null };
  final_checks: FinalCheck[];
  script_md: string;
  qa: {
    llm: string;
    generated_at: string | null;
    findings: QaFinding[];
    cuts: number;
    frames: number;
    url: string | null;
  } | null;
  publish_result: Record<string, unknown> | null;
  /** куда ролик уже уехал (manifest.publish); null — не публиковали */
  publish_state: PublishState | null;
  providers: Record<string, string>;
  approvals: string[];
  ledger: LedgerEntry[];
  report_url: string;
  path: string;
}

// ---------------------------------------------------------------- аккаунты

export interface Snapshot {
  at: string;
  source: string;
  subscribers: number | null;
  views_total: number | null;
  likes_total: number | null;
  video_count: number | null;
  note: string;
}

export interface KnownValue {
  value: number;
  at: string;
  source: string;
}

export type KnownMetrics = Record<"subscribers" | "views_total" | "likes_total" | "video_count", KnownValue | null>;

/** Что бросается в глаза первым: лицо и давность последней публикации. */
export interface AccountFace {
  avatar_url: string | null;
  last_published_at: string | null;
  days_since_publish: number | null;
}

export interface AccountItem extends AccountBrief, AccountFace {
  project: string;
  project_name: string;
  latest: Snapshot | null;
  known: KnownMetrics;
  snapshots: number;
  problems: string[];
  /** запас проекта при его слотах; null — расписания у проекта нет */
  runway: RunwayBrief | null;
}

export interface AccountVideo {
  id: string;
  url: string;
  title: string;
  published_at: string | null;
  duration_sec: number | null;
  views: number | null;
  likes: number | null;
  comments: number | null;
  thumbnail_url: string;
  is_short: boolean | null;
}

export interface PublishedEpisode {
  episode: string;
  ref: string;
  title: string;
  platforms: string[];
  duration_sec: number | null;
  generated_at: string | null;
  video_url: string | null;
  thumbnail_url: string | null;
  is_short: boolean;
  uploaded: boolean; // лежит на площадке (manifest.publish), а не «упакован»
  upload?: UploadState | null; // куда уехал и виден ли там — как у эпизода в проекте
  checks_failed: number;
}

export interface AccountDetail extends AccountBrief, AccountFace {
  project: string;
  project_name: string;
  channel_id: string;
  notes: string;
  problems: string[];
  history: Snapshot[];
  known: KnownMetrics;
  videos: { collected_at: string; source: string; videos: AccountVideo[] } | null;
  published: PublishedEpisode[];
  /** расписание и очередь проекта аккаунта; null — аккаунт ни к чему не привязан */
  queue: QueueView | null;
  path: string;
}

export interface AccountsOverview {
  networks: { id: string; title: string; platforms: string[] }[];
  items: AccountItem[];
}

// ---------------------------------------------------------------- ресурсы

export interface KindInfo {
  kind: string;
  title: string;
  visual: boolean;
  count_global: number;
  count_projects: number;
}

export interface TechniqueRef {
  ref: string;
  decision: string;
  decision_title: string;
  id: string;
  title: string;
  status: "implemented" | "recorded" | null;
  missing: boolean;
}

export interface ResourceDetail extends ResourceCard {
  fields: Record<string, unknown>;
  path: string;
  portfolio: { name: string; url: string | null }[];
  /** из каких приёмов библиотеки собран ресурс (поле techniques) */
  techniques: TechniqueRef[];
  /** чем собрать витрину; null — у этого вида её не бывает */
  example_command: string | null;
  /** пробы подачи голоса рядом с витриной (`gen-example voice <id> --variants`): подпись — что изменено */
  examples: { label: string; url: string | null }[];
  used_by: { project: string; episode: string; where: string }[];
}

// ---------------------------------------------------------------- библиотека

export interface DecisionInfo {
  id: string;
  title: string;
  implemented: number;
  recorded: number;
}

export interface TechniqueExample {
  kind: "image" | "video" | "audio";
  after_url: string | null;
  before_url: string | null;
}

export interface Evidence {
  source: string;
  measured: string;
  date: string;
  verdict: "works" | "failed" | "n/a";
}

export interface Technique {
  id: string;
  decision: string;
  decision_title: string;
  title: string;
  summary: string;
  status: "implemented" | "recorded";
  always: boolean;
  applies_when: Record<string, unknown>;
  never_when: Record<string, unknown>;
  params: Record<string, unknown>;
  implemented_by: string;
  evidence: Evidence[];
  notes: string;
  path: string;
  example: TechniqueExample | null;
  problems?: string[];
  /** обратная сторона `techniques:` — ресурсы, собранные с этим приёмом */
  used_by_resources?: { kind: string; kind_title: string; id: string; name: string; project: string | null }[];
}

// ---------------------------------------------------------------- планы и возможности

export type FeatureStatus = "idea" | "planned" | "in_progress" | "done" | "dropped";

export interface FeatureItem {
  id: string;
  title: string;
  status: FeatureStatus;
  status_title: string;
  area: string;
  area_title: string;
  /** тема внутри области: записи одной темы карта «Возможности» показывает одной карточкой; "" — сама по себе */
  topic: string;
  topic_title: string;
  summary: string;
  gives: string;
  cost: string;
  how_to_use: string;
  requires: string[];
  links: string[];
  source: string;
  recorded: string;
  done_at: string | null;
  notes: string;
  problems: string[];
  path: string;
}

export interface FeaturesOverview {
  items: FeatureItem[];
  statuses: { id: FeatureStatus; title: string }[];
  areas: { id: string; title: string }[];
  topics: { id: string; title: string }[];
  counts: Record<FeatureStatus, number>;
  total: number;
}

// ---------------------------------------------------------------- полезные источники

export type LinkKind = "service" | "tool" | "channel" | "article" | "docs" | "assets" | "community" | "other";

export interface LinkItem {
  id: string;
  title: string;
  url: string;
  host: string;
  kind: LinkKind;
  kind_title: string;
  area: string;
  area_title: string;
  summary: string;
  why: string;
  cost: string;
  tags: string[];
  added: string;
  checked: string;
  source: string;
  notes: string;
  problems: string[];
  path: string;
}

export interface LinksOverview {
  items: LinkItem[];
  kinds: { id: LinkKind; title: string }[];
  areas: { id: string; title: string }[];
  counts: Record<LinkKind, number>;
  total: number;
}

// ---------------------------------------------------------------- полигон замеров

export type FileKind = "image" | "video" | "audio" | "doc" | "table" | "html" | "text" | "other";

export interface BenchFile {
  name: string;
  rel: string;
  url: string | null;
  kind: FileKind;
  size: number;
  mtime: string;
  rows?: number;
  text?: string;
  table?: CsvTable;
}

export interface CsvTable {
  columns: string[];
  rows: string[][];
  total: number;
  truncated: boolean;
}

export interface RunsRow {
  test: string;
  calls: number;
  completed: number;
  cost_usd: number;
  wall_mean_s: number | null;
  first: string | null;
  last: string | null;
}

/** Карточка теста из projects/_bench/tests.yaml — пишет человек при закрытии теста. */
export interface BenchTestMeta {
  question?: string;
  answer?: string;
  date?: string;
  models?: string[];
  gpu?: { name?: string; pod?: string; minutes?: number; rate_usd_hr?: number; cost_usd?: number } | null;
}

/** hosted-вызовы теста по runs.csv */
export interface BenchHosted {
  calls: number;
  completed: number;
  cost_usd: number;
  wall_s: number;
  endpoints: { id: string; calls: number; cost_usd: number }[];
}

export interface BenchTestCard {
  id: string;
  rel: string;
  counts: Partial<Record<FileKind, number>>;
  mtime: string | null;
  docs: BenchFile[];
  reports: BenchFile[];
  meta: BenchTestMeta | null;
  hosted: BenchHosted | null;
}

export interface BenchOverview {
  exists: boolean;
  path: string;
  docs: BenchFile[];
  tables: BenchFile[];
  reports: BenchFile[];
  tests: BenchTestCard[];
  benches: BenchCard[];
  runs: RunsRow[];
  /** cost_usd = hosted_usd (runs.csv) + gpu_usd (карта из tests.yaml) + bench_usd (дубли замеров) */
  totals: {
    calls: number;
    hosted_usd: number;
    gpu_minutes: number;
    gpu_usd: number;
    bench_usd: number;
    cost_usd: number;
    tests: number;
    benches: number;
  };
}

export interface BenchGroup {
  dir: string;
  images: BenchFile[];
  videos: BenchFile[];
  audio: BenchFile[];
  docs: BenchFile[];
  tables: BenchFile[];
  other: BenchFile[];
}

export interface BenchTest {
  id: string;
  rel: string;
  groups: BenchGroup[];
  counts: Partial<Record<FileKind, number>>;
}

// ---------------------------------------------------------------- замеры по схеме
// projects/_bench/<id>/bench.yaml → дубли с происхождением → вердикты → сводка
// (videoforge/bench/, `videoforge bench`). Поля повторяют api/bench.py.

export type VerdictValue = "pass" | "fail" | "na";

/** Карточка замера в обзоре (`/api/bench` → benches). error — схема не читается. */
export interface BenchCard {
  id: string;
  rel: string;
  error: string;
  question?: string;
  answer?: string;
  date?: string;
  compare?: "cause" | "method";
  budget_usd?: number;
  spent_usd?: number;
  takes?: number;
  done?: number;
  failed?: number;
  reviewed?: number;
  accepted?: number;
  branches?: string[];
  scenes?: number;
  missing_frames?: string[];
  mtime?: string | null;
}

export interface BenchCriterion {
  id: string;
  title: string;
  hint: string;
}

export interface BenchScene {
  id: string;
  title: string;
  frame: string;
  prompt: string;
  prompts: Record<string, string>;
  negative: string;
  /** исходный клип сцены (comfy_upscale) */
  video: string;
  frame_url: string | null;
  video_url: string | null;
  missing: boolean;
}

export interface BenchBranch {
  id: string;
  method: string;
  preset: string;
  params: Record<string, unknown>;
  note: string;
}

export interface BenchSpec {
  id: string;
  question: string;
  compare: "cause" | "method";
  budget_usd: number;
  /** что делает дубль: клип или кадр (замер кадров — панель манги → кадр аниме) */
  output: "video" | "image";
  aspect_ratio: string;
  duration_sec: number;
  seeds: number[];
  branches: BenchBranch[];
  criteria: BenchCriterion[];
  stop_rule: { min_accepted: number; of: number };
  answer: string;
  date: string;
  notes: string;
}

export interface BenchTakeMetrics {
  duration_sec: number;
  motion_mean: number;
  motion_median: number;
  motion_peak: number;
  quiet_share: number;
}

export interface BenchVerdict {
  take_id: string;
  reviewer: string;
  at: string;
  criteria: Record<string, VerdictValue>;
  defect_times: number[];
  accepted_interval: [number, number] | null;
  notes: string;
}

/** Дубль: всё, чем получен (кадр, промпт, граф, веса, секунды, $) + вердикт. */
export interface BenchTake {
  take_id: string;
  scene: string;
  branch: string;
  seed: number;
  status: "done" | "failed" | "planned";
  error_kind: string;
  error: string;
  inputs: {
    frame: string;
    frame_sha256: string;
    /** видео на входе: управляющее (comfy_control) или исходное (comfy_upscale) */
    reference: string;
    reference_sha256: string;
    /** картинки-референсы сцены (лист героя) */
    refs: string[];
    refs_sha256: string[];
    prompt: string;
    negative: string;
    aspect_ratio: string;
    duration_sec: number;
    frames: number;
    fps: number;
    size: string;
  };
  method: {
    method: string;
    preset: string;
    params: Record<string, unknown>;
    models: Record<string, string>;
    weights: string;
    pod_id: string;
    rate_usd_hr: number;
  };
  fingerprint: string;
  graph: string;
  video: string;
  /** результат замера кадров (output: image) */
  image: string;
  started_at: string;
  finished_at: string;
  gen_sec: number;
  cost_usd: number;
  estimate_usd: number;
  metrics: BenchTakeMetrics | null;
  video_url: string | null;
  image_url: string | null;
  frame_url: string | null;
  reference_url: string | null;
  ref_urls: (string | null)[];
  graph_url: string | null;
  graph_rel: string | null;
  verdict: BenchVerdict | null;
  /** отпечаток файла вердикта: уходит обратно с оценкой, несовпадение → 409 */
  verdict_base: string;
  accepted: boolean | null;
}

export interface BenchBranchSummary {
  id: string;
  method: string;
  preset: string;
  takes: number;
  done: number;
  failed: number;
  error_kinds: string[];
  reviewed: number;
  partial: number;
  accepted: number;
  rejected: number;
  share: number | null;
  cost_usd: number;
  accepted_sec: number;
  usd_per_accepted_sec: number | null;
  gen_sec_mean: number | null;
  fails: Record<string, number>;
  defect_times: number[];
}

export interface BenchSummary {
  id: string;
  question: string;
  answer: string;
  compare: "cause" | "method";
  budget_usd: number;
  spent_usd: number;
  takes: number;
  done: number;
  failed: number;
  reviewed: number;
  accepted: number;
  branches: BenchBranchSummary[];
  ladder: { scene: string; title: string; branches: Record<string, { done: number; reviewed: number; accepted: number; passes_rule: boolean }> }[];
  metrics: { metric: string; title: string; accepted_n: number; accepted_mean: number | null; rejected_n: number; rejected_mean: number | null }[];
  criteria: { id: string; title: string }[];
}

export interface BenchDetail {
  id: string;
  rel: string;
  spec: BenchSpec;
  scenes: BenchScene[];
  takes: BenchTake[];
  summary: BenchSummary;
  summary_md: string;
}

export interface VerdictReply {
  verdict: BenchVerdict;
  base: string;
  accepted: boolean | null;
}

export interface LibraryOverview {
  decisions: DecisionInfo[];
  groups: { id: string; title: string; decisions: string[] }[];
  conditions: { key: string; hint: string }[];
  unknown_decisions: string[];
  total: number;
}

// ---------------------------------------------------------------- монтаж (таймлайн)

export interface MontageItem {
  start: number;
  end: number;
  text?: string;
  label?: string;
  kind?: string;
  words?: { start: number; end: number; text: string }[];
  thumb_url?: string | null;
  clip_url?: string | null;
  badges?: string[];
}

export interface MontageMark {
  at: number;
  duration?: number;
  label: string;
  kind: string;
  why?: string;
}

/** Дорожка таймлайна. `kind` решает, как её рисовать: коробки сцен, реплики,
 * слова, волна звука, метки. Поля payload зависят от вида. */
export interface MontageTrack {
  id: string;
  kind: "clips" | "lines" | "words" | "wave" | "marks";
  title: string;
  icon: string;
  hint: string;
  items?: MontageItem[];
  values?: number[];
  per_sec?: number;
  marks?: MontageMark[];
  text?: string;
}

export interface MontageNote {
  severity: "info" | "warn" | "bad";
  at_sec: number;
  text: string;
}

/** Из чего собран ролик и откуда взято: модель кадра, лицензия трека, ссылка на исходник. */
export interface MontageMaterial {
  kind: string;
  title: string;
  value: string;
  origin: string;
  license: string;
  /** строка упоминания, которую требует лицензия (CC BY) */
  credit: string;
  link: string;
  url: string | null;
  /** "<вид>/<id>" — карточка ресурса в пульте */
  resource: string | null;
  /** откуда строка: "конвейер" (выведено из файлов стадий) или "паспорт" (materials.json) */
  from: string;
  /** для записанных в паспорт: ok | changed | gone */
  state: string;
}

export interface Montage {
  ref: string;
  project: string;
  episode: string;
  title: string;
  aspect_ratio: string;
  language: string;
  captions_language: string | null;
  duration_sec: number;
  video_url: string | null;
  poster_url: string | null;
  per_sec: number;
  plan: { author: string; note: string | null; url: string | null };
  /** true — время сцен измерено сборкой (timeline.json), false — сложено из раскадровки */
  measured: boolean;
  /** входы, которые новее готового файла: ролик собран не из них */
  stale: string[];
  /** копия чужого готового файла: `<проект>/<эпизод>` оригинала, чьи дорожки показаны; null — ролик свой */
  copy_of: string | null;
  tracks: MontageTrack[];
  materials: MontageMaterial[];
  /** паспорт ролика (05_assemble/materials.json) — есть у собранных мимо конвейера */
  passport: { made_by: string; rebuild: string; note: string } | null;
  notes: MontageNote[];
}

/** План монтажа для правки из пульта (`api/planedit.py`). */
export interface EditPlanDoc {
  ref: string;
  exists: boolean;
  plan: Record<string, unknown> | null;
  /** отпечаток прочитанного: с ним PATCH отвергает затирание чужой правки */
  fingerprint: string;
  /** что пульт вправе менять — белый список путей полей */
  editable: string[];
  /** виды эффектов кадра — из схемы плана, чтобы форма с ней не разошлась */
  effect_kinds: string[];
  /** звуки, которые примет линт: неизвестный id блокирует правку целиком */
  sfx: { id: string; kind: string; gain_db: number; url: string | null }[];
  path: string;
}

export interface PlanSaved {
  fingerprint: string;
  author: string;
  /** что поправил линт в присланной правке */
  fixes: string[];
  warnings: string[];
  /** имя файла с прежней версией плана рядом */
  backup: string | null;
}

// ---------------------------------------------------------------- входящие файлы

/** Файл из inbox/ (`api/inbox.py`). `path` показывается человеку: по нему файл
 * называют в разговоре, а не ищут в проводнике. */
export interface InboxFile {
  name: string;
  /** путь внутри inbox/: «2026-09-10/письмо.pdf» */
  rel: string;
  /** путь от корня репозитория: «inbox/2026-09-10/письмо.pdf» */
  path: string;
  /** день-папка; пусто — файл положили в корень мимо страницы */
  day: string;
  url: string | null;
  /** та же ссылка с `&download=1`: браузер сохранит файл, а не покажет */
  download_url: string | null;
  kind: FileKind;
  size: number;
  mtime: string;
}

export interface InboxDay {
  day: string;
  /** «сегодня», «вчера», «8 сентября» — считает сервер по своим часам */
  title: string;
  count: number;
  size: number;
  files: InboxFile[];
}

export interface Inbox {
  days: InboxDay[];
  today: string;
  total: number;
  size: number;
}

/** Загрузка файла: тело запроса — сам файл, имя — в адресе.
 *
 * XHR, а не fetch: у fetch нет события о ходе отправки, а видео с телефона
 * уходит минутами — полоса без движения читается как «повисло». */
export function uploadFile(file: File, onProgress?: (done: number) => void): Promise<InboxFile> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/inbox?name=${encodeURIComponent(file.name)}`);
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      const body = xhr.response as (InboxFile & { detail?: string }) | null;
      if (xhr.status >= 200 && xhr.status < 300 && body) resolve(body);
      else reject(new ApiError(xhr.status, body?.detail ?? xhr.statusText ?? "не загрузилось"));
    };
    xhr.onerror = () => reject(new ApiError(0, "связь оборвалась"));
    xhr.onabort = () => reject(new ApiError(0, "загрузка отменена"));
    xhr.send(file);
  });
}
