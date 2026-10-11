// Мелкие строительные блоки в духе shadcn/ui: карточка, бейдж, плитка,
// таблица. Своих состояний нет — только разметка и классы Tailwind.

import { clsx, type ClassValue } from "clsx";
import type { LucideIcon } from "lucide-react";
import type { HTMLAttributes, ReactNode, TableHTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";
import { Link } from "react-router";

export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}

/** Плитка с иконкой: один вид у заголовка страницы, у карточки-счётчика и у
 * карточки в списке — смысл считывается до чтения текста. */
export function IconTile({ icon: Icon, size = "md", className }: { icon: LucideIcon; size?: "sm" | "md" | "lg"; className?: string }) {
  const box = size === "lg" ? "h-10 w-10 rounded-xl" : size === "sm" ? "h-6 w-6 rounded-md" : "h-8 w-8 rounded-lg";
  const glyph = size === "lg" ? "h-5 w-5" : size === "sm" ? "h-3.5 w-3.5" : "h-4 w-4";
  return (
    <span className={cn("inline-flex shrink-0 items-center justify-center border border-line bg-panel-2 text-ink-2", box, className)}>
      <Icon className={glyph} />
    </span>
  );
}

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-xl border border-line bg-panel", className)} {...props} />;
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex items-center justify-between gap-3 px-5 pt-4 pb-2", className)} {...props} />;
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn("text-[15px] font-semibold tracking-tight", className)} {...props} />;
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 pb-5", className)} {...props} />;
}

export type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "accent";

const TONES: Record<Tone, string> = {
  neutral: "bg-panel-2 text-ink-2 border-line",
  ok: "bg-ok-soft text-ok border-transparent",
  warn: "bg-warn-soft text-warn border-transparent",
  bad: "bg-bad-soft text-bad border-transparent",
  info: "bg-info-soft text-info border-transparent",
  accent: "bg-accent-soft text-accent border-transparent",
};

export function Badge({
  tone = "neutral",
  className,
  children,
  title,
}: {
  tone?: Tone;
  className?: string;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Stat({ label, value, hint, icon }: { label: string; value: ReactNode; hint?: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-line bg-panel px-4 py-3">
      <div className="min-w-0">
        <div className="text-[11px] uppercase tracking-wide text-ink-3">{label}</div>
        <div className="mt-1 text-xl font-semibold tabular-nums tracking-tight">{value}</div>
        {hint ? <div className="mt-0.5 text-xs text-ink-3">{hint}</div> : null}
      </div>
      {icon ? <IconTile icon={icon} /> : null}
    </div>
  );
}

export function Table({ className, ...props }: TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-x-auto">
      <table className={cn("w-full border-collapse text-[13px]", className)} {...props} />
    </div>
  );
}

export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        "border-b border-line px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wide text-ink-3",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b border-line px-3 py-2 align-top", className)} {...props} />;
}

export function KV({ items }: { items: { k: string; v: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-[13px]">
      {items.map(({ k, v }) => (
        <div key={k} className="contents">
          <dt className="text-ink-3">{k}</dt>
          <dd className="min-w-0 wrap-break-word">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line px-5 py-8 text-center text-sm text-ink-3">
      {children}
    </div>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn("rounded bg-panel-2 px-1 py-0.5 font-mono text-[12px]", className)}>{children}</code>;
}

export function LinkChip({ to, children, external }: { to: string; children: ReactNode; external?: boolean }) {
  // min-w-0: чип стоит в ряду с другим текстом (задача в «Планах» — код,
  // текст, метка, ссылка на запись), и без разрешения сжаться длинное
  // название вылезало за край экрана вместе со страницей
  const cls =
    "inline-flex min-w-0 items-center gap-1 rounded-md border border-line bg-panel px-2 py-0.5 text-[12px] hover:border-ink-3 hover:bg-panel-2";
  if (external) {
    return (
      <a href={to} target="_blank" rel="noreferrer" className={cls}>
        {children}
      </a>
    );
  }
  return (
    <Link to={to} className={cls}>
      {children}
    </Link>
  );
}

export function Section({
  title,
  aside,
  children,
  icon: Icon,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  icon?: LucideIcon;
}) {
  return (
    <section className="space-y-3">
      {/* min-w-0 обеим половинам: справа у раздела часто стоит путь
          («projects/podcast_clips/sources»), и на узком экране он распирал
          строку заголовка наружу */}
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="flex min-w-0 items-center gap-2 text-[15px] font-semibold tracking-tight">
          {Icon ? <Icon className="h-4 w-4 shrink-0 text-ink-3" /> : null}
          {title}
        </h2>
        {aside ? <div className="min-w-0 text-xs text-ink-3">{aside}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Details({
  summary,
  children,
  open,
  icon: Icon,
}: {
  summary: ReactNode;
  children: ReactNode;
  open?: boolean;
  icon?: LucideIcon;
}) {
  return (
    <details className="group rounded-xl border border-line bg-panel" open={open}>
      <summary className="cursor-pointer select-none px-5 py-3 text-[15px] font-semibold tracking-tight marker:text-ink-3">
        {Icon ? <Icon className="mr-2 inline h-4 w-4 align-[-2px] text-ink-3" /> : null}
        {summary}
      </summary>
      <div className="border-t border-line px-5 py-4">{children}</div>
    </details>
  );
}
