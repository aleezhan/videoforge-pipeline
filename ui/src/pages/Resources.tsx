import { Link } from "react-router";
import { Boxes } from "lucide-react";
import { useApi, type KindInfo } from "@/api";
import { ErrorBox, Loading, PageHeader } from "@/components/common";
import { Card, IconTile } from "@/components/ui/primitives";
import { KIND_ICONS } from "@/lib/icons";

const HINTS: Record<string, string> = {
  characters: "лицо, тело, характер + портфолио референсов",
  locations: "места, которые держатся между эпизодами",
  props: "предметы-фишки",
  styles: "техника отрисовки: префикс/суффикс промпта, чеклист",
  voices: "голос озвучки (edge-tts и другие)",
  niches: "аудитория, тон, хуки, темы",
  caption_styles: "как выглядят субтитры: шрифт, караоке, отступы",
  music: "фоновые треки с лицензией",
  sfx: "удары, пролёты, нагнетания",
};

export default function Resources() {
  const q = useApi<{ kinds: KindInfo[] }>("/api/resources");
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <>
      <PageHeader
        icon={Boxes}
        title="Ресурсы"
        meta={<span>то, из чего собирается ролик; глобальные — resources/, проектные — projects/&lt;id&gt;/resources/</span>}
      />
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {q.data!.kinds.map((k) => (
          <Link key={k.kind} to={`/resources/${k.kind}`} className="block">
            <Card className="flex h-full gap-3 p-4 transition-colors hover:border-ink-3">
              <IconTile icon={KIND_ICONS[k.kind] ?? Boxes} size="lg" />
              <div className="min-w-0">
                <div className="text-[15px] font-semibold">{k.title}</div>
                <div className="text-xs text-ink-3">{HINTS[k.kind] ?? k.kind}</div>
                <div className="mt-2 text-xs text-ink-2 tabular-nums">
                  {k.count_global} общих{k.count_projects ? ` · ${k.count_projects} проектных` : ""}
                </div>
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </>
  );
}
