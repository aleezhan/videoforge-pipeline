// Общие детали шоурила: время сцены, изинги, палитра, фон, карточки, курсор,
// значки. Всё движение — от useCurrentFrame: CSS-анимации Remotion не рендерит.
import React from "react";
import { AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { loadFont } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

export const { fontFamily: SANS } = loadFont("normal", {
  weights: ["400", "500", "600", "700", "800", "900"],
  subsets: ["latin"],
});
export const { fontFamily: MONO } = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] });

export const C = {
  red: "#E5242B",
  redSoft: "#FDE8E9",
  ink: "#0E1330",
  muted: "#6B7394",
  line: "#E6E8F0",
  bg: "#F4F5F9",
  blue: "#3B5BFD",
  green: "#16A34A",
  greenSoft: "#E3F6EA",
  amber: "#D97706",
  amberSoft: "#FEF3C7",
  night: "#060A1C",
};

export const OUT = Easing.bezier(0.16, 1, 0.3, 1);
export const IN_OUT = Easing.bezier(0.65, 0, 0.35, 1);
export const BACK = Easing.bezier(0.34, 1.56, 0.64, 1);

/** Секунды от начала сцены. */
export const useT = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return frame / fps;
};

/** Отрезок [a, b] секунд → значение from..to с изингом, края зажаты. */
export const k = (t: number, a: number, b: number, from = 0, to = 1, easing = OUT) =>
  interpolate(t, [a, b], [from, to], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing });

/** Появление: снизу, с прозрачности и лёгкого размытия. */
export const rise = (t: number, at: number, dist = 40, dur = 0.7): React.CSSProperties => ({
  opacity: k(t, at, at + dur * 0.6),
  translate: `0px ${k(t, at, at + dur, dist, 0)}px`,
  filter: `blur(${k(t, at, at + dur * 0.6, 8, 0)}px)`,
});

/** Пружинный «поп» значка. */
export const pop = (t: number, at: number, dur = 0.45): React.CSSProperties => ({
  opacity: k(t, at, at + 0.12),
  scale: String(k(t, at, at + dur, 0.2, 1, BACK)),
});

/** Сцена целиком: вход и выход по 0.5 с — сцены перетекают, а не режутся. */
export const Shell: React.FC<{ dur: number; children: React.ReactNode; dark?: boolean }> = ({
  dur,
  children,
}) => {
  const t = useT();
  const enter = k(t, 0, 0.5);
  const exit = k(t, dur - 0.5, dur, 0, 1, IN_OUT);
  return (
    <AbsoluteFill
      style={{
        opacity: enter * (1 - exit),
        scale: String(k(t, 0, 0.6, 1.05, 1) * (1 - exit * 0.06)),
        filter: `blur(${(1 - enter) * 14 + exit * 18}px)`,
      }}
    >
      {children}
    </AbsoluteFill>
  );
};

/** Светлый фон: сетка точек и медленно плывущие цветные пятна. */
export const LightBg: React.FC = () => {
  const t = useT();
  const blob = (x: number, y: number, r: number, color: string, phase: number) => ({
    position: "absolute" as const,
    left: x + Math.sin(t * 0.35 + phase) * 90 - r,
    top: y + Math.cos(t * 0.28 + phase) * 70 - r,
    width: r * 2,
    height: r * 2,
    borderRadius: "50%",
    background: `radial-gradient(circle, ${color} 0%, transparent 68%)`,
  });
  return (
    <AbsoluteFill style={{ background: C.bg, overflow: "hidden" }}>
      <div style={blob(260, 180, 620, "rgba(229,36,43,0.13)", 0)} />
      <div style={blob(1650, 860, 700, "rgba(59,91,253,0.14)", 2)} />
      <div style={blob(1100, 120, 460, "rgba(255,170,120,0.12)", 4)} />
      <AbsoluteFill
        style={{
          backgroundImage: "radial-gradient(rgba(14,19,48,0.10) 1.4px, transparent 1.6px)",
          backgroundSize: "36px 36px",
          backgroundPosition: `${(t * 6) % 36}px 0px`,
          maskImage: "radial-gradient(ellipse at center, black 30%, transparent 80%)",
        }}
      />
    </AbsoluteFill>
  );
};

export const Card: React.FC<{ style?: React.CSSProperties; children?: React.ReactNode }> = ({
  style,
  children,
}) => (
  <div
    style={{
      background: "rgba(255,255,255,0.92)",
      borderRadius: 28,
      border: `1px solid ${C.line}`,
      boxShadow: "0 30px 80px -20px rgba(14,19,48,0.22), 0 8px 20px -10px rgba(14,19,48,0.12)",
      fontFamily: SANS,
      color: C.ink,
      ...style,
    }}
  >
    {children}
  </div>
);

/** Знак продукта: красный скруглённый квадрат с «J». */
export const Mark: React.FC<{ size: number; style?: React.CSSProperties }> = ({ size, style }) => (
  <div
    style={{
      width: size,
      height: size,
      flexShrink: 0,
      borderRadius: size * 0.28,
      background: `linear-gradient(145deg, #FF4A50, ${C.red} 60%, #B8141B)`,
      boxShadow: `0 ${size * 0.12}px ${size * 0.35}px -${size * 0.1}px rgba(229,36,43,0.55)`,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      color: "white",
      fontFamily: SANS,
      fontWeight: 900,
      fontSize: size * 0.58,
      letterSpacing: -size * 0.02,
      ...style,
    }}
  >
    J
  </div>
);

export const Wordmark: React.FC<{ size: number }> = ({ size }) => (
  <span style={{ fontFamily: SANS, fontWeight: 800, fontSize: size, letterSpacing: -size * 0.035, color: C.ink }}>
    Jome<span style={{ color: C.red }}>Invoice</span>
  </span>
);

/** Курсор со щелчком: волна от точки щелчка. */
export const Cursor: React.FC<{ x: number; y: number; t: number; clicks?: number[]; opacity?: number }> = ({
  x,
  y,
  t,
  clicks = [],
  opacity = 1,
}) => {
  const press = clicks.reduce((m, c) => Math.max(m, t >= c - 0.08 && t < c + 0.12 ? 1 : 0), 0);
  return (
    <div style={{ position: "absolute", left: x, top: y, opacity, zIndex: 50 }}>
      {clicks.map((c) => {
        const p = k(t, c, c + 0.6);
        return p > 0 && p < 1 ? (
          <div
            key={c}
            style={{
              position: "absolute",
              left: -40 * p,
              top: -40 * p,
              width: 80 * p,
              height: 80 * p,
              borderRadius: "50%",
              border: `3px solid ${C.red}`,
              opacity: 1 - p,
            }}
          />
        ) : null;
      })}
      <svg width={38} height={46} viewBox="0 0 19 23" style={{ scale: String(1 - press * 0.15), transformOrigin: "0 0" }}>
        <path
          d="M1 1 L1 18 L5.5 14 L8.5 21 L11.5 19.7 L8.6 13 L14.5 13 Z"
          fill="#0E1330"
          stroke="white"
          strokeWidth={1.4}
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
};

/** Набор текста: сколько символов видно к секунде t. */
export const typed = (text: string, t: number, at: number, dur: number) =>
  text.slice(0, Math.round(k(t, at, at + dur, 0, text.length, Easing.linear)));

/** Число, растущее к цели. */
export const count = (t: number, at: number, dur: number, to: number) => k(t, at, at + dur, 0, to, OUT);

export const fmt = (n: number, digits = 0) =>
  n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

type IconName = "invoice" | "qr" | "shield" | "chart" | "cloud" | "check" | "bolt" | "lock" | "refresh" | "link";

export const Icon: React.FC<{ name: IconName; size?: number; color?: string }> = ({
  name,
  size = 36,
  color = C.ink,
}) => {
  const p = { fill: "none", stroke: color, strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const paths: Record<IconName, React.ReactNode> = {
    invoice: (
      <>
        <path {...p} d="M6 3h9l4 4v14H6z" />
        <path {...p} d="M9 11h7M9 15h7M9 7h3" />
      </>
    ),
    qr: (
      <>
        <rect {...p} x={4} y={4} width={6} height={6} />
        <rect {...p} x={14} y={4} width={6} height={6} />
        <rect {...p} x={4} y={14} width={6} height={6} />
        <path {...p} d="M14 14h2v2h-2zM18 18h2v2h-2zM14 20h2M20 14v2" />
      </>
    ),
    shield: <path {...p} d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />,
    chart: <path {...p} d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
    cloud: <path {...p} d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 18z" />,
    check: <path {...p} d="M5 12.5l4.5 4.5L19 7" />,
    bolt: <path {...p} d="M13 2L4 14h7l-1 8 9-12h-7z" />,
    lock: (
      <>
        <rect {...p} x={5} y={11} width={14} height={10} rx={2} />
        <path {...p} d="M8 11V8a4 4 0 0 1 8 0v3" />
      </>
    ),
    refresh: <path {...p} d="M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4" />,
    link: <path {...p} d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L12 6.3M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />,
  };
  return (
    <svg width={size} height={size} viewBox="0 0 24 24">
      {paths[name]}
    </svg>
  );
};

/** Круглый значок с иконкой — плавает поверх сцен. */
export const Badge: React.FC<{ icon: IconName; size?: number; color?: string; style?: React.CSSProperties }> = ({
  icon,
  size = 92,
  color = C.ink,
  style,
}) => (
  <div
    style={{
      width: size,
      height: size,
      borderRadius: "50%",
      background: "white",
      border: `1px solid ${C.line}`,
      boxShadow: "0 18px 40px -12px rgba(14,19,48,0.25)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      ...style,
    }}
  >
    <Icon name={icon} size={size * 0.44} color={color} />
  </div>
);

/** Заголовок сцены с выделенным словом и подчёркиванием, которое дорисовывается. */
export const Headline: React.FC<{ t: number; at: number; before: string; accent: string; after?: string; size?: number }> = ({
  t,
  at,
  before,
  accent,
  after = "",
  size = 64,
}) => (
  <div style={{ fontFamily: SANS, fontWeight: 800, fontSize: size, letterSpacing: -size * 0.03, color: C.ink, ...rise(t, at) }}>
    {before}
    <span style={{ color: C.red, position: "relative", display: "inline-block" }}>
      {accent}
      <span
        style={{
          position: "absolute",
          left: 0,
          bottom: -size * 0.06,
          height: size * 0.08,
          borderRadius: 99,
          background: C.red,
          width: `${k(t, at + 0.35, at + 0.9) * 100}%`,
        }}
      />
    </span>
    {after}
  </div>
);

/** Детерминированный «QR»: три глазка и шум по сиду. Картинка, а не код. */
export const qrCells = (n = 25, seed = 481) => {
  let s = seed;
  const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
  const cells: [number, number][] = [];
  const eye = (x: number, y: number) => x < 7 && y < 7;
  for (let y = 0; y < n; y++)
    for (let x = 0; x < n; x++) {
      if (eye(x, y) || eye(n - 1 - x, y) || eye(x, n - 1 - y)) continue;
      if (rnd() > 0.52) cells.push([x, y]);
    }
  return cells;
};
