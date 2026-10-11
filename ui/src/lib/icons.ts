// Иконки по смыслу: одна карта на весь пульт, чтобы стадия, вид ресурса, тип
// файла или сеть выглядели одинаково на любой странице. Новая сущность —
// одна строка здесь, а не иконка «на месте».

import {
  AudioLines,
  Ban,
  Beaker,
  Bookmark,
  BookOpen,
  Camera,
  Captions,
  ChartNoAxesColumn,
  CircleCheck,
  Clapperboard,
  Cpu,
  Crop,
  File,
  FileCode,
  FileSpreadsheet,
  FileText,
  Film,
  Frame,
  Gamepad2,
  Gauge,
  Globe,
  Hammer,
  Image,
  Languages,
  Layers,
  LayoutGrid,
  Lightbulb,
  ListTodo,
  MapPin,
  Mic,
  Monitor,
  MonitorPlay,
  Music,
  Newspaper,
  Package,
  Paintbrush,
  Palette,
  Scissors,
  ScrollText,
  Search,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Smartphone,
  Sparkles,
  Target,
  Telescope,
  Terminal,
  Tv,
  Type,
  Users,
  Volume2,
  WandSparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { FeatureStatus, FileKind } from "@/api";

export type { LucideIcon };

/** Области записей features/ — они же стадии на карте «Возможности». */
export const AREA_ICONS: Record<string, LucideIcon> = {
  intel: Telescope,
  resources: Users,
  script: ScrollText,
  planner: LayoutGrid,
  audio: Mic,
  images: Image,
  video: Clapperboard,
  clips: Scissors,
  edit: WandSparkles,
  assemble: Layers,
  qa: ShieldCheck,
  publish: Send,
  accounts: ChartNoAxesColumn,
  gpu: Cpu,
  ui: Monitor,
  other: Gauge,
};

/** Стадии конвейера по имени из workflow (manifest.json). */
export const STAGE_ICONS: Record<string, LucideIcon> = {
  director: ScrollText,
  planner: LayoutGrid,
  audio: Mic,
  images: Image,
  video: Clapperboard,
  edit: WandSparkles,
  assemble: Layers,
  qa: ShieldCheck,
  publish: Send,
  cut: Scissors,
  translate: Languages,
  reframe: Crop,
  collect: Search,
  stats: ChartNoAxesColumn,
  classify: Target,
  deep: Telescope,
  visual: Film,
  formula: Beaker,
  distill: BookOpen,
};

export const WORKFLOW_ICONS: Record<string, LucideIcon> = {
  ai_video: Clapperboard,
  clips_from_long: Scissors,
  analyze_channel: Telescope,
};

/** Вид проекта (project.yaml: kind) — канал или полигон качества. */
export const PROJECT_KIND_ICONS: Record<string, LucideIcon> = {
  channel: Clapperboard,
  test: MonitorPlay,
};

/** Виды ресурсов (resources/<kind>/). */
export const KIND_ICONS: Record<string, LucideIcon> = {
  characters: Users,
  locations: MapPin,
  props: Package,
  styles: Palette,
  voices: Mic,
  niches: Target,
  caption_styles: Captions,
  music: Music,
  sfx: Volume2,
  backgrounds: Gamepad2,
};

/** Файлы по расширению (api/data.kind_of). */
export const FILE_KIND_ICONS: Record<FileKind, LucideIcon> = {
  image: Image,
  video: Film,
  audio: AudioLines,
  doc: FileText,
  table: FileSpreadsheet,
  html: FileCode,
  text: Terminal,
  other: File,
};

/** Сети площадок. Брендовых иконок в lucide больше нет — берём по смыслу. */
export const NETWORK_ICONS: Record<string, LucideIcon> = {
  youtube: Tv,
  instagram: Camera,
  tiktok: Smartphone,
};

/** Точки решения библиотеки приёмов (core/library.DECISIONS). */
export const DECISION_ICONS: Record<string, LucideIcon> = {
  source_prep: SlidersHorizontal,
  shot_selection: LayoutGrid,
  cut_rhythm: Scissors,
  framing: Frame,
  grade: Paintbrush,
  captions: Captions,
  caption_font: Type,
  caption_color: Palette,
  caption_animation: Sparkles,
  audio_layers: AudioLines,
  ending_audio: Volume2,
  packaging: Package,
};

/** Виды полезных источников (core/links.KINDS). */
export const LINK_KIND_ICONS: Record<string, LucideIcon> = {
  service: Globe,
  tool: Wrench,
  channel: Tv,
  article: Newspaper,
  docs: BookOpen,
  assets: Package,
  community: Users,
  other: Bookmark,
};

/** Дорожки монтажного вида (`api/montage.py`: поле icon у дорожки). */
export const TRACK_ICONS: Record<string, LucideIcon> = {
  video: Film,
  captions: Captions,
  type: Type,
  mic: Mic,
  music: Music,
  audio: AudioLines,
  effects: Sparkles,
  sfx: Volume2,
};

export const FEATURE_STATUS_ICONS: Record<FeatureStatus, LucideIcon> = {
  in_progress: Hammer,
  planned: ListTodo,
  idea: Lightbulb,
  done: CircleCheck,
  dropped: Ban,
};

export function iconFor(map: Record<string, LucideIcon>, key: string | null | undefined, fallback: LucideIcon): LucideIcon {
  return (key && map[key]) || fallback;
}
