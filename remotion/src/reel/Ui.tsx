// 9.75–11.5 с: интерфейс. Карточки влетают пружиной, курсор включает
// «Motion system» — цифры бегут, кольцо и столбики растут; ключевые кадры на
// дорожках, курсор жмёт «RENDER» → «DONE».
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, HEAVY, IN_OUT, k, MONO, P, SERIF, useT } from "./lib";

const card = (t: number, at: number): React.CSSProperties => ({
  position: "absolute",
  borderRadius: 28,
  overflow: "hidden",
  boxShadow: "0 30px 60px -25px rgba(11,11,15,0.35)",
  opacity: k(t, at, at + 0.08),
  translate: `0px ${k(t, at, at + 0.35, 80, 0, BACK)}px`,
  scale: String(k(t, at, at + 0.35, 0.9, 1, BACK)),
});

const Cursor: React.FC<{ x: number; y: number; press: boolean }> = ({ x, y, press }) => (
  <svg width={40} height={48} viewBox="0 0 19 23" style={{ position: "absolute", left: x, top: y, scale: String(press ? 0.85 : 1), transformOrigin: "0 0", zIndex: 9 }}>
    <path d="M1 1 L1 18 L5.5 14 L8.5 21 L11.5 19.7 L8.6 13 L14.5 13 Z" fill={P.ink} stroke="white" strokeWidth={1.4} strokeLinejoin="round" />
  </svg>
);

const KEYS = [
  [0.1, 0.18, 0.26, 0.5, 0.58],
  [0.3, 0.38, 0.46, 0.7, 0.78, 0.86],
  [0.55, 0.63, 0.71, 0.9],
];

export const Ui: React.FC = () => {
  const t = useT();
  const on = t >= 0.5;
  const go = k(t, 0.5, 1.2);
  const done = t >= 1.35;
  const exit = k(t, 1.62, 1.75, 0, 1, IN_OUT);
  const cx = t < 0.9 ? k(t, 0.2, 0.45, 1500, 1640, IN_OUT) : k(t, 0.9, 1.25, 1640, 1560, IN_OUT);
  const cy = t < 0.9 ? k(t, 0.2, 0.45, 520, 262, IN_OUT) : k(t, 0.9, 1.25, 262, 805, IN_OUT);
  const chart = Array.from({ length: 30 }, (_, i) => {
    const x = (i / 29) * 600;
    const y = 150 - (i / 29) ** 1.6 * 120 - Math.sin(i * 1.3) * 8;
    return `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
  return (
    <AbsoluteFill
      style={{
        background: P.cream,
        clipPath: `inset(0 0 0 ${k(t, 0, 0.18, 100, 0, IN_OUT)}%)`,
        scale: String(1 - exit * 0.08),
        opacity: 1 - exit,
      }}
    >
      <div style={{ ...card(t, 0.08), left: 170, top: 170, width: 720, height: 410, background: "white", padding: 36 }}>
        <div style={{ fontFamily: MONO, fontSize: 16, letterSpacing: 4, color: "#8a8680" }}>ENGAGEMENT</div>
        <div style={{ fontFamily: HEAVY, fontSize: 96, letterSpacing: -4, color: P.ink, marginTop: 6 }}>+{Math.round(go * 248)}%</div>
        <svg width={620} height={170} style={{ position: "absolute", left: 50, bottom: 30 }}>
          <path d={chart} fill="none" stroke={P.orange} strokeWidth={5} strokeLinecap="round" pathLength={1} strokeDasharray={1} strokeDashoffset={1 - go} />
          <circle cx={600 * go} cy={150 - go ** 1.6 * 120} r={9} fill={P.orange} opacity={go > 0 ? 1 : 0} />
        </svg>
      </div>
      <div style={{ ...card(t, 0.14), left: 940, top: 170, width: 810, height: 190, background: P.orange, padding: "34px 40px", color: "white" }}>
        <div style={{ fontFamily: HEAVY, fontSize: 58, letterSpacing: -2 }}>Motion system</div>
        <div style={{ fontFamily: MONO, fontSize: 15, letterSpacing: 4, marginTop: 10, opacity: 0.8 }}>EASING · TIMING · SPACING</div>
        <div style={{ position: "absolute", right: 44, top: 62, width: 120, height: 64, borderRadius: 32, background: P.ink }}>
          <div
            style={{
              position: "absolute",
              top: 8,
              left: k(t, 0.5, 0.65, 8, 64, BACK),
              width: 48,
              height: 48,
              borderRadius: 24,
              background: on ? P.lime : "#777",
            }}
          />
        </div>
      </div>
      <div style={{ ...card(t, 0.2), left: 940, top: 390, width: 390, height: 190, background: P.ink, color: "white", display: "flex", alignItems: "center", gap: 26, padding: "0 36px" }}>
        <svg width={120} height={120} viewBox="0 0 120 120">
          <circle cx={60} cy={60} r={48} fill="none" stroke="#333" strokeWidth={12} />
          <circle cx={60} cy={60} r={48} fill="none" stroke={P.orange} strokeWidth={12} strokeLinecap="round" pathLength={1} strokeDasharray={1} strokeDashoffset={1 - go * 0.99} transform="rotate(-90 60 60)" />
        </svg>
        <div>
          <div style={{ fontFamily: HEAVY, fontSize: 64 }}>{Math.round(go * 99)}%</div>
          <div style={{ fontFamily: MONO, fontSize: 14, letterSpacing: 4, opacity: 0.6 }}>SMOOTHNESS</div>
        </div>
      </div>
      <div style={{ ...card(t, 0.26), left: 1360, top: 390, width: 390, height: 190, background: P.lime, padding: "28px 34px" }}>
        <div style={{ fontFamily: MONO, fontSize: 14, letterSpacing: 4, color: P.ink }}>RENDER QUEUE</div>
        <div style={{ position: "absolute", left: 34, right: 34, bottom: 26, height: 100, display: "flex", alignItems: "flex-end", gap: 12 }}>
          {[40, 65, 50, 80, 70, 95, 60, 88].map((h, i) => (
            <div key={i} style={{ flex: 1, height: `${h * k(t, 0.55 + i * 0.05, 0.85 + i * 0.05, 0.08, 1, BACK)}%`, background: P.ink, borderRadius: 5 }} />
          ))}
        </div>
      </div>
      <div style={{ ...card(t, 0.32), left: 170, top: 620, width: 1580, height: 280, background: P.violet, color: P.cream, padding: "44px 50px" }}>
        <div style={{ fontFamily: SERIF, fontStyle: "italic", fontSize: 80, lineHeight: 1 }}>
          Keyframes are
          <br />a language.
        </div>
        <div style={{ position: "absolute", left: 700, right: 250, top: 60, height: 170 }}>
          {KEYS.map((row, r) => (
            <div key={r} style={{ position: "absolute", left: 0, right: 0, top: 20 + r * 55, height: 2, background: "rgba(239,235,227,0.25)" }}>
              {row.map((x, j) => (
                <div
                  key={j}
                  style={{
                    position: "absolute",
                    left: `${x * 100}%`,
                    top: -10,
                    width: 20,
                    height: 20,
                    rotate: "45deg",
                    background: P.cream,
                    scale: String(k(t, 0.45 + x * 0.6, 0.6 + x * 0.6, 0, 1, BACK)),
                  }}
                />
              ))}
            </div>
          ))}
          <div style={{ position: "absolute", top: 0, bottom: 0, width: 3, background: P.orange, left: `${k(t, 0.45, 1.45, 0, 100) }%` }} />
        </div>
        <div
          style={{
            position: "absolute",
            right: 50,
            top: 170,
            padding: "14px 30px",
            borderRadius: 99,
            background: done ? P.lime : P.cream,
            color: P.ink,
            fontFamily: MONO,
            fontWeight: 600,
            fontSize: 20,
            letterSpacing: 4,
            scale: String(t >= 1.3 && t < 1.42 ? 0.9 : 1),
          }}
        >
          {done ? "DONE ✓" : "RENDER"}
        </div>
      </div>
      <Cursor x={cx} y={cy} press={(t > 0.45 && t < 0.55) || (t > 1.3 && t < 1.4)} />
    </AbsoluteFill>
  );
};
