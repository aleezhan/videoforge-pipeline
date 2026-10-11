import { Link, useParams } from "react-router";
import { CalendarDays, ExternalLink, Eye, Heart, IdCard, ListVideo, Package, TrendingUp, Users, Video } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useApi, type AccountDetail as AccountDetailT, type KnownValue } from "@/api";
import { Crumbs, ErrorBox, Loading, PageHeader, UploadPill } from "@/components/common";
import { ScheduleSection } from "@/components/Schedule";
import { Badge, Card, CardBody, Empty, KV, LinkChip, Mono, Section, Stat } from "@/components/ui/primitives";
import { BrandIcon } from "@/components/brand";
import { VideoShelf, type ShelfItem } from "@/components/VideoShelf";
import { fmtCompact, fmtDate, fmtNum } from "@/lib/format";
import { Avatar, SincePublish } from "./Accounts";

export default function AccountDetail() {
  const { id = "" } = useParams();
  const q = useApi<AccountDetailT>(`/api/accounts/${encodeURIComponent(id)}`);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const a = q.data!;
  const last = a.history.length ? a.history[a.history.length - 1] : null;
  // последнее ИЗВЕСТНОЕ значение по каждой метрике считает API (core.accounts.latest_known):
  // ручной замер без числа роликов не стирает число, снятое автоматически раньше
  const { subscribers: subs, views_total: views, likes_total: likes, video_count: count } = a.known;
  const hint = (k: KnownValue | null, fallback: string) => (k ? `${fmtDate(k.at)} · ${k.source}` : fallback);
  const series = a.history
    .filter((s) => s.subscribers !== null)
    .map((s) => ({ at: s.at, date: fmtDate(s.at), subscribers: s.subscribers as number }));
  const videos = a.videos?.videos ?? [];
  const onPlatform = (url: string) => videos.some((v) => v.id && url.includes(v.id));
  // Полка «на площадке» — это список площадки (videos.json) ПЛЮС то, что уехало
  // по манифесту, но в снимок ещё не попало: снимок снимается yt-dlp сразу после
  // загрузки, и свежий шортс в нём появляется позже. До 12.09.2026 такой ролик
  // стоял в нижней полке «упакованы» с зелёным «опубликовано» рядом — читалось
  // как противоречие. Ролик, который площадка уже показывает, здесь один раз
  const uploaded = a.published.filter((p) => p.uploaded);
  const unseen = uploaded.filter((p) => !p.upload?.platforms.some((pl) => onPlatform(pl.url)));
  const platformItems: ShelfItem[] = [
    ...unseen.map((p) => {
      const first = p.upload?.platforms[0];
      return {
        id: p.ref,
        title: p.title || p.episode,
        is_short: p.is_short,
        thumbnail_url: p.thumbnail_url,
        href: first?.url || undefined,
        date: first?.at ?? p.generated_at,
        duration_sec: p.duration_sec,
        badges: (
          <>
            <UploadPill upload={p.upload} />
            <Badge title="площадка приняла ролик (manifest.publish), но в последнем снимке videos.json его ещё нет — появится после videoforge account sync">
              снимок ещё не видит
            </Badge>
          </>
        ),
      } satisfies ShelfItem;
    }),
    ...videos.map((v) => ({
      id: v.id,
      title: v.title,
      is_short: Boolean(v.is_short),
      thumbnail_url: v.thumbnail_url || null,
      href: v.url || undefined,
      views: v.views,
      date: v.published_at,
      duration_sec: v.duration_sec,
    })),
  ];
  // упакованное, которого на площадке ещё нет — ждёт своего слота или команды
  const waiting = a.published.filter((p) => !p.uploaded);
  const packedItems: ShelfItem[] = waiting.map((p) => ({
    id: p.ref,
    title: p.title || p.episode,
    is_short: p.is_short,
    thumbnail_url: p.thumbnail_url,
    to: `/projects/${a.project}/episodes/${p.episode}`,
    date: p.generated_at,
    duration_sec: p.duration_sec,
    badges: (
      <>
        {/* куда ролик уехал на самом деле — та же плашка, что у эпизода в проекте */}
        {p.upload ? <UploadPill upload={p.upload} /> : <Badge>не загружен</Badge>}
        {p.platforms.map((pl) => (
          <Badge key={pl}>{pl}</Badge>
        ))}
        {p.checks_failed ? <Badge tone="warn">{p.checks_failed} вне цели</Badge> : null}
      </>
    ),
  }));
  return (
    <>
      <Crumbs items={[{ to: "/accounts", label: "Аккаунты" }, { label: a.title || a.id }]} />
      <PageHeader
        title={
          <span className="flex items-center gap-3">
            <Avatar face={a} network={a.network} size="h-14 w-14" />
            {a.title || a.handle || a.id}
          </span>
        }
        meta={
          <>
            <Badge>
              <BrandIcon network={a.network} className="h-3 w-3" />
              {a.network_title}
            </Badge>
            <SincePublish face={a} verbose />
            {a.handle ? <span>{a.handle}</span> : null}
            {a.url ? (
              <a href={a.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-ink">
                открыть <ExternalLink className="h-3 w-3" />
              </a>
            ) : null}
            <span>·</span>
            <span>
              проект{" "}
              <Link to={`/projects/${a.project}`} className="font-medium hover:underline">
                {a.project_name}
              </Link>
            </span>
          </>
        }
      >
        {a.problems.length ? (
          <div className="rounded-lg border border-bad/30 bg-bad-soft px-4 py-2 text-[13px] text-bad">
            {a.problems.join(" · ")}
          </div>
        ) : null}
      </PageHeader>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <Stat
          icon={CalendarDays}
          label="Последний ролик"
          value={a.last_published_at ? fmtDate(a.last_published_at) : "—"}
          hint={a.days_since_publish !== null ? `${a.days_since_publish} дн. назад` : "списка роликов нет"}
        />
        <Stat icon={Users} label="Подписчики" value={fmtNum(subs?.value)} hint={hint(subs, "замеров нет")} />
        <Stat icon={Eye} label="Просмотры" value={fmtCompact(views?.value)} hint={hint(views, "сумма по роликам")} />
        <Stat icon={Heart} label="Лайки" value={fmtCompact(likes?.value)} hint={hint(likes, "сумма по роликам")} />
        <Stat icon={Video} label="Роликов" value={fmtNum(count?.value)} hint={hint(count, last?.note || "")} />
      </div>

      {/* тот же блок, что на странице проекта: «когда следующий ролик» спрашивают у канала */}
      {a.queue ? <ScheduleSection project={a.project} view={a.queue} onSaved={() => void q.refetch()} /> : null}

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Section title="Подписчики по замерам" icon={TrendingUp} aside={`${a.history.length} замеров`}>
          <Card>
            <CardBody className="pt-4">
              {series.length >= 2 ? (
                <div className="h-56">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={series} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke="var(--line)" vertical={false} />
                      <XAxis dataKey="date" tick={{ fontSize: 11, fill: "var(--ink-3)" }} axisLine={false} tickLine={false} />
                      <YAxis
                        tick={{ fontSize: 11, fill: "var(--ink-3)" }}
                        axisLine={false}
                        tickLine={false}
                        width={48}
                        domain={["auto", "auto"]}
                        tickFormatter={(v: number) => fmtCompact(v)}
                      />
                      <Tooltip
                        contentStyle={{
                          background: "var(--panel)",
                          border: "1px solid var(--line)",
                          borderRadius: 8,
                          fontSize: 12,
                        }}
                        formatter={(v) => [fmtNum(Number(v)), "подписчики"]}
                      />
                      <Line type="monotone" dataKey="subscribers" stroke="var(--accent)" strokeWidth={2} dot={{ r: 3 }} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : (
                <Empty>
                  График появится со второго замера. Снять: <Mono>videoforge account sync {a.id}</Mono>
                </Empty>
              )}
            </CardBody>
          </Card>
        </Section>
        <Section title="Аккаунт" icon={IdCard}>
          <Card>
            <CardBody className="pt-4">
              <KV
                items={[
                  { k: "id", v: <Mono>{a.id}</Mono> },
                  { k: "Сеть", v: a.network_title },
                  { k: "Handle", v: a.handle || "—" },
                  { k: "channel_id", v: a.channel_id ? <Mono>{a.channel_id}</Mono> : "—" },
                  { k: "Заметки", v: a.notes || "—" },
                  { k: "Файл", v: <span className="font-mono text-xs">{a.path}</span> },
                ]}
              />
            </CardBody>
          </Card>
        </Section>
      </div>

      <Section
        title="Ролики на площадке"
        icon={ListVideo}
        aside={
          a.videos
            ? `${platformItems.length} · снимок ${fmtDate(a.videos.collected_at)} (${a.videos.source})${
                unseen.length ? ` + ${unseen.length} по манифесту, снимок ещё не видит` : ""
              }`
            : unseen.length
              ? `${unseen.length} по манифесту · снимка площадки ещё не было`
              : undefined
        }
      >
        <VideoShelf
          items={platformItems}
          empty={
            <>
              Список роликов появится после <Mono>videoforge account sync {a.id}</Mono>
              {a.network !== "youtube"
                ? " (для этой сети — только ручные замеры, список не собирается)"
                : videos.length === 0 && a.videos
                  ? " — площадка не показала ни одного публичного ролика; скрытые и «по ссылке» снаружи не видны"
                  : ""}
            </>
          }
        />
      </Section>

      <Section
        title="Упакованы к публикации"
        icon={Package}
        aside="эпизоды проекта после стадии publish, которых на площадке ещё нет; уходят по расписанию выше или командой videoforge publish"
      >
        <VideoShelf items={packedItems} empty="Всё упакованное уже на площадке; новых эпизодов после стадии publish нет." />
      </Section>
      <div className="text-xs text-ink-3">
        <LinkChip to="/accounts">← все аккаунты</LinkChip>
      </div>
    </>
  );
}
