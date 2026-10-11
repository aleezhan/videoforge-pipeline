// Аккаунты карточками: лицо канала с логотипом площадки, имя, ник-ссылка,
// три числа в ряд и снизу проект / давность публикации / дата замера.
// Логотип и есть категория — отдельные разделы по сетям не нужны.

import type { ReactNode } from "react";
import { Link } from "react-router";
import { Radio } from "lucide-react";
import { useApi, type AccountFace, type AccountItem, type AccountsOverview, type RunwayBrief } from "@/api";
import { BrandIcon } from "@/components/brand";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { fmtRunwayDays } from "@/components/Schedule";
import { Badge, Card, Empty, Mono, type Tone } from "@/components/ui/primitives";
import { fmtCompact, fmtDate, fmtNum, plural } from "@/lib/format";

/** Запас проекта при его слотах: «на 3 дня», «нет», «пауза». Стоит на первой
 * странице, потому что кончившийся запас — это канал, который замолчит завтра,
 * а узнают об этом обычно из тишины. */
export function RunwayBadge({ runway }: { runway: RunwayBrief | null }) {
  if (!runway) return <span className="text-xs text-ink-3">расписания нет</span>;
  if (runway.paused) return <Badge tone="info">пауза</Badge>;
  const tone: Tone = runway.level === "bad" ? "bad" : runway.level === "warn" ? "warn" : "ok";
  const text = runway.ready === 0 ? "нет" : `на ${fmtRunwayDays(runway.days)}`;
  const hint = runway.ends
    ? `готовых ${runway.ready} при ${runway.per_day} в день · кончится ${fmtDate(runway.ends)}`
    : `готовых ${runway.ready} при ${runway.per_day} в день`;
  return (
    <Badge tone={tone} title={hint}>
      {text}
    </Badge>
  );
}

/** Давность последней публикации словами и цветом: две недели — жёлтый,
 * месяц — красный. Это первое, что должно попадать в глаз. */
export function SincePublish({ face, verbose = false }: { face: AccountFace; verbose?: boolean }) {
  const d = face.days_since_publish;
  if (d === null) return <span className="text-xs text-ink-3">{verbose ? "публикаций не видно" : "не видно"}</span>;
  const tone: Tone = d > 30 ? "bad" : d > 14 ? "warn" : "ok";
  const text = d === 0 ? "сегодня" : d === 1 ? "вчера" : `${plural(d, "день", "дня", "дней")} назад`;
  return (
    <Badge tone={tone} title={`последний ролик ${fmtDate(face.last_published_at)}`}>
      {text}
    </Badge>
  );
}

/** Круг с аватаром площадки и логотипом сети в углу; без аватара — логотип в круге. */
export function Avatar({ face, network, size = "h-20 w-20" }: { face: AccountFace; network: string; size?: string }) {
  return (
    <span className={`relative inline-block shrink-0 ${size}`}>
      {face.avatar_url ? (
        <img src={face.avatar_url} alt="" className="h-full w-full rounded-full object-cover ring-1 ring-line" />
      ) : (
        <span className="flex h-full w-full items-center justify-center rounded-full bg-panel-2 ring-1 ring-line">
          <BrandIcon network={network} className="h-[45%] w-[45%]" />
        </span>
      )}
      <span className="absolute -right-1 -bottom-1 rounded-full border border-line bg-panel p-1">
        <BrandIcon network={network} className="h-4 w-4" />
      </span>
    </span>
  );
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-lg font-semibold tabular-nums tracking-tight">{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-ink-3">{label}</div>
    </div>
  );
}

function Cell({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wide text-ink-3">{label}</div>
      <div className="mt-0.5 truncate text-xs">{children}</div>
    </div>
  );
}

function AccountCard({ a }: { a: AccountItem }) {
  const last = a.latest;
  return (
    <Card className="flex flex-col items-center gap-3 p-5 text-center">
      <Avatar face={a} network={a.network} />
      <div className="min-w-0">
        <Link to={`/accounts/${a.id}`} className="text-[15px] font-semibold hover:underline">
          {a.title || a.handle || a.id}
        </Link>
        <div className="text-xs text-ink-3">
          {a.url ? (
            <a href={a.url} target="_blank" rel="noreferrer" className="hover:text-ink hover:underline">
              {a.handle || a.url}
            </a>
          ) : (
            a.handle || "—"
          )}
          {" · "}
          <Mono>{a.id}</Mono>
        </div>
      </div>
      <div className="grid w-full grid-cols-3 gap-2 border-t border-line pt-3">
        <Metric label="подписчики" value={fmtNum(a.known.subscribers?.value)} />
        <Metric label="просмотры" value={fmtCompact(a.known.views_total?.value)} />
        <Metric label="роликов" value={fmtNum(a.known.video_count?.value)} />
      </div>
      <div className="grid w-full grid-cols-2 gap-2 border-t border-line pt-3 sm:grid-cols-4">
        <Cell label="проект">
          <Link to={`/projects/${a.project}`} className="hover:underline">
            {a.project_name}
          </Link>
        </Cell>
        <Cell label="публиковал">
          <SincePublish face={a} />
        </Cell>
        <Cell label="запас">
          <RunwayBadge runway={a.runway} />
        </Cell>
        <Cell label="замер">
          {last ? (
            <span title={last.source}>{fmtDate(last.at)}</span>
          ) : (
            <span className="text-ink-3">нет</span>
          )}
        </Cell>
      </div>
      {a.problems.length ? (
        <div className="w-full text-left text-xs text-bad">
          {a.problems.map((p) => (
            <div key={p}>{p}</div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}

export default function Accounts() {
  const q = useApi<AccountsOverview>("/api/accounts");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const { networks, items } = q.data!;
  const order = new Map(networks.map((n, i) => [n.id, i]));
  const sorted = [...items].sort((a, b) => (order.get(a.network) ?? 99) - (order.get(b.network) ?? 99) || a.id.localeCompare(b.id));
  return (
    <>
      <PageHeader
        icon={Radio}
        title="Аккаунты"
        meta={<span>куда уходят ролики; каждый аккаунт привязан к проекту, проект без аккаунта — демо</span>}
      />
      {sorted.length ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {sorted.map((a) => (
            <AccountCard key={a.id} a={a} />
          ))}
        </div>
      ) : (
        <Empty>
          Аккаунтов нет. Завести: <Mono>videoforge account new &lt;id&gt; --network youtube --project &lt;проект&gt;</Mono>
        </Empty>
      )}
      <div className="text-xs text-ink-3">
        Снять замер: <Mono>videoforge account sync &lt;id&gt;</Mono> — YouTube сам, TikTok и Instagram числами руками через{" "}
        <Mono>--manual</Mono>.
      </div>
    </>
  );
}
