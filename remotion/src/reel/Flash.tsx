// 11.5–12.75 с: темп — по слову на четверть секунды. «LOUD» курсивом,
// «exact.» с перекрестием, «LOOP» рядами навстречу, «ALIVE» с дышащими буквами.
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, HEAVY, IN_OUT, k, MONO, P, SERIF, useT } from "./lib";

export const Flash: React.FC = () => {
  const t = useT();
  if (t < 0.25) {
    return (
      <AbsoluteFill style={{ background: P.ink, justifyContent: "center", alignItems: "center" }}>
        <div
          style={{
            fontFamily: HEAVY,
            fontSize: 400,
            letterSpacing: -16,
            color: P.cream,
            transform: `skewX(-12deg) translateX(${Math.sin(t * 90) * 10 * (1 - t / 0.25)}px)`,
            scale: String(k(t, 0, 0.12, 1.2, 1)),
          }}
        >
          LOUD
        </div>
      </AbsoluteFill>
    );
  }
  if (t < 0.5) {
    const u = t - 0.25;
    return (
      <AbsoluteFill style={{ background: P.cream, justifyContent: "center", alignItems: "center" }}>
        <div style={{ position: "absolute", left: 0, top: 540, height: 2, width: `${k(u, 0, 0.12) * 100}%`, background: P.orange }} />
        <div style={{ position: "absolute", left: 960, top: 0, width: 2, height: `${k(u, 0.02, 0.14) * 100}%`, background: P.orange }} />
        <div style={{ position: "absolute", left: 980, top: 555, fontFamily: MONO, fontSize: 16, letterSpacing: 3, color: P.orange }}>
          X 960 · Y 540
        </div>
        <div style={{ fontFamily: SERIF, fontStyle: "italic", fontSize: 320, color: P.ink, opacity: k(u, 0, 0.06) }}>exact.</div>
      </AbsoluteFill>
    );
  }
  if (t < 0.75) {
    const u = t - 0.5;
    return (
      <AbsoluteFill style={{ background: P.ink, overflow: "hidden", justifyContent: "center" }}>
        {[-2, -1, 0, 1, 2].map((r) => (
          <div
            key={r}
            style={{
              position: "absolute",
              top: 540 + r * 210 - 105,
              left: -400,
              whiteSpace: "nowrap",
              fontFamily: HEAVY,
              fontSize: 220,
              letterSpacing: -8,
              lineHeight: "210px",
              color: r === 0 ? P.orange : "transparent",
              WebkitTextStroke: r === 0 ? "none" : `3px ${P.orange}`,
              translate: `${(r % 2 ? 1 : -1) * u * 2400}px 0px`,
            }}
          >
            LOOP LOOP LOOP LOOP LOOP LOOP
          </div>
        ))}
      </AbsoluteFill>
    );
  }
  const u = t - 0.75;
  return (
    <AbsoluteFill style={{ background: P.cream, justifyContent: "center", alignItems: "center" }}>
      <div style={{ display: "flex", fontFamily: HEAVY, fontSize: 330, letterSpacing: -14, color: P.ink }}>
        {"ALIVE".split("").map((ch, i) => (
          <span
            key={i}
            style={{
              display: "inline-block",
              translate: `0px ${Math.sin(u * 22 - i * 0.9) * 18 * (1 - k(u, 0.2, 0.5))}px`,
              scale: String(k(u, i * 0.03, i * 0.03 + 0.2, 0.3, 1, BACK)),
            }}
          >
            {ch}
          </span>
        ))}
      </div>
      <div style={{ position: "absolute", top: 740, height: 16, background: P.orange, width: k(u, 0.1, 0.35, 0, 1080, IN_OUT) }} />
    </AbsoluteFill>
  );
};
