// Карточка главы, 1.75 с: большой номер контуром, название по буквам, красная
// линия. Удар музыки — на 0.25 с от начала.
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, C, IN_OUT, k, SANS, Shell, useT } from "./lib";

export const Chapter: React.FC<{ n: string; title: string; sub: string }> = ({ n, title, sub }) => {
  const t = useT();
  return (
    <Shell dur={1.75}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", fontFamily: SANS }}>
        <div style={{ display: "flex", alignItems: "center", gap: 60 }}>
          <div
            style={{
              fontSize: 300,
              fontWeight: 900,
              letterSpacing: -12,
              color: "transparent",
              WebkitTextStroke: `4px ${C.red}`,
              translate: `${k(t, 0.2, 0.8, -120, 0)}px 0px`,
              opacity: k(t, 0.2, 0.4),
            }}
          >
            {n}
          </div>
          <div>
            <div style={{ fontSize: 30, fontWeight: 700, letterSpacing: 8, color: C.muted, opacity: k(t, 0.3, 0.6) }}>
              CHAPTER {n}
            </div>
            <div style={{ fontSize: 150, fontWeight: 900, letterSpacing: -6, color: C.ink, display: "flex" }}>
              {title.split("").map((ch, i) => (
                <span
                  key={i}
                  style={{
                    display: "inline-block",
                    opacity: k(t, 0.25 + i * 0.035, 0.4 + i * 0.035),
                    translate: `0px ${k(t, 0.25 + i * 0.035, 0.7 + i * 0.035, 70, 0, BACK)}px`,
                  }}
                >
                  {ch}
                </span>
              ))}
            </div>
            <div style={{ height: 8, borderRadius: 4, background: C.red, width: k(t, 0.45, 1.0, 0, 420, IN_OUT) }} />
            <div style={{ fontSize: 34, fontWeight: 500, color: C.muted, marginTop: 22, opacity: k(t, 0.6, 0.9) }}>{sub}</div>
          </div>
        </div>
      </AbsoluteFill>
    </Shell>
  );
};
