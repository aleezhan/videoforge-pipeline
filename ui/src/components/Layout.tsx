// Оболочка: меню разделов и область страницы.
//
// На узком экране (телефон) боковое меню шириной 224px съедало бы больше
// половины ширины, поэтому там оно ложится полосой сверху с горизонтальной
// прокруткой. Разделов десять, в один ряд они не влезают ни на каком телефоне —
// прокрутка честнее, чем гамбургер: видно, что список длиннее экрана.

import { NavLink, Outlet } from "react-router";
import { BookOpen, Bookmark, Boxes, Clapperboard, FlaskConical, FolderUp, Lightbulb, MessageSquare, Radio, Sparkles } from "lucide-react";
import { useApi } from "@/api";
import { cn } from "@/components/ui/primitives";
import { PROJECT_KIND_ICONS } from "@/lib/icons";

const NAV = [
  { to: "/accounts", label: "Аккаунты", icon: Radio },
  { to: "/projects", label: "Проекты", icon: Clapperboard },
  { to: "/tests", label: "Тесты", icon: PROJECT_KIND_ICONS.test },
  { to: "/resources", label: "Ресурсы", icon: Boxes },
  { to: "/library", label: "Библиотека", icon: BookOpen },
  { to: "/bench", label: "Замеры", icon: FlaskConical },
  { to: "/capabilities", label: "Возможности", icon: Sparkles },
  { to: "/plans", label: "Планы", icon: Lightbulb },
  { to: "/agent-tasks", label: "Команда ИИ", icon: MessageSquare },
  { to: "/links", label: "Источники", icon: Bookmark },
  { to: "/inbox", label: "Файлы", icon: FolderUp },
];

export default function Layout() {
  const meta = useApi<{ version: string; ui: boolean }>("/api/meta");
  return (
    <div className="flex min-h-full flex-col md:flex-row">
      <aside className="sticky top-0 z-20 flex shrink-0 flex-col border-b border-line bg-panel md:h-screen md:w-56 md:border-r md:border-b-0">
        <div className="px-5 pt-5 pb-4 max-md:pt-3 max-md:pb-2">
          <div className="text-[15px] font-semibold tracking-tight">videoforge</div>
          <div className="text-[11px] text-ink-3">
            пульт поверх файлов{meta.data ? ` · ${meta.data.version}` : ""}
          </div>
        </div>
        <nav className="flex gap-0.5 overflow-x-auto px-3 pb-2 md:flex-col md:overflow-x-visible md:pb-0">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  "flex shrink-0 items-center gap-2.5 whitespace-nowrap rounded-lg px-2.5 py-2 text-[13px] font-medium text-ink-2 hover:bg-panel-2 hover:text-ink",
                  isActive ? "bg-panel-2 text-ink" : "",
                )
              }
            >
              <Icon className="h-4 w-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-5 py-4 text-[11px] leading-relaxed text-ink-3 max-md:hidden">
          Просмотр проектов и материалов. Совместные задачи Claude и Codex — в разделе «Команда ИИ».
          <div className="mt-1">
            <a href="/api/docs" target="_blank" rel="noreferrer" className="hover:text-ink">
              API /api/docs
            </a>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <div className="mx-auto max-w-6xl space-y-6 px-8 py-6 max-md:space-y-4 max-md:px-4 max-md:py-4">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
