// 0–1.25 с: загрузка — точка в круге, круг схлопывается в чёрточку, имя
// набирается перебором символов над красной линейкой, оранжевая плашка
// разрастается на весь кадр. 1.25–2.0 с: отсчёт 3 · 2 · 1 курсивом.
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, hash, IN_OUT, k, MONO, P, SERIF, useT } from "./lib";

const NAME = "VIDEOFORGE";
const GLYPHS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/<>#*";

export const Intro: React.FC = () => {
  const t = useT();
  const ring = k(t, 0.2, 0.5, 60, 240) * (1 - k(t, 0.5, 0.62, 0, 1, IN_OUT));
  const plate = k(t, 1.0, 1.25, 0, 1, IN_OUT);
  const scramble = NAME.split("")
    .map((ch, i) => {
      const lock = 0.75 + i * 0.035;
      if (t < 0.62) return "";
      if (t >= lock) return ch;
      return GLYPHS[Math.floor(hash(i * 31 + Math.floor(t * 30)) * GLYPHS.length)];
    })
    .join("");
  return (
    <AbsoluteFill style={{ background: P.ink, justifyContent: "center", alignItems: "center" }}>
      <div
        style={{
          position: "absolute",
          width: ring,
          height: ring,
          borderRadius: "50%",
          border: "1.5px solid rgba(239,235,227,0.45)",
          opacity: ring > 1 ? 1 : 0,
        }}
      />
      <div style={{ position: "absolute", width: 10, height: 10, borderRadius: 5, background: P.cream, opacity: 1 - k(t, 0.55, 0.62) }} />
      <div style={{ position: "absolute", top: 470, fontFamily: MONO, fontWeight: 600, fontSize: 30, letterSpacing: 16, color: P.cream }}>
        {scramble}
      </div>
      <div
        style={{
          position: "absolute",
          top: 530,
          height: 3,
          width: k(t, 0.6, 0.95, 0, 760, IN_OUT),
          background: P.orange,
        }}
      >
        {Array.from({ length: 39 }, (_, i) => (
          <div key={i} style={{ position: "absolute", left: `${(i / 38) * 100}%`, top: -9, width: 1, height: i % 4 ? 6 : 12, background: "rgba(239,235,227,0.5)" }} />
        ))}
      </div>
      <div style={{ position: "absolute", top: 560, fontFamily: MONO, fontSize: 16, letterSpacing: 8, color: "rgba(239,235,227,0.55)", opacity: k(t, 0.85, 1.0) }}>
        MOTION REEL · 2026
      </div>
      <div
        style={{
          position: "absolute",
          width: 1100 + plate * 1000,
          height: 300 + plate * 900,
          borderRadius: 120 * (1 - plate),
          background: P.orange,
          opacity: k(t, 1.0, 1.04),
          scale: String(k(t, 1.0, 1.1, 0.4, 1, BACK)),
        }}
      />
    </AbsoluteFill>
  );
};

export const Countdown: React.FC = () => {
  const t = useT();
  const n = t < 0.25 ? "3" : t < 0.5 ? "2" : "1";
  const local = t % 0.25;
  return (
    <AbsoluteFill style={{ background: P.orange, justifyContent: "center", alignItems: "center" }}>
      <div
        style={{
          fontFamily: SERIF,
          fontStyle: "italic",
          fontSize: 560,
          lineHeight: 1,
          color: P.ink,
          scale: String(k(local, 0, 0.18, 1.35, 1, BACK)),
          rotate: `${k(local, 0, 0.2, -8, 0)}deg`,
          filter: `blur(${k(local, 0, 0.08, 10, 0)}px)`,
        }}
      >
        {n}
      </div>
    </AbsoluteFill>
  );
};
