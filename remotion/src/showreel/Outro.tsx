// Финал: удар, знак и имя крупно, слоган по словам, три главы строкой и кнопка
// «Book a demo», по которой щёлкает курсор. Последняя секунда — в белое.
import React from "react";
import { AbsoluteFill, Easing } from "remotion";
import { BACK, C, Cursor, IN_OUT, k, Mark, rise, SANS, useT, Wordmark } from "./lib";

const TAG = ["E-invoicing,", "on", "autopilot."];
const CHAPTERS = ["Connect", "Automate", "Monitor"];

export const Outro: React.FC = () => {
  const t = useT();
  const end = k(t, 7.0, 8.0, 0, 1, IN_OUT);
  return (
    <AbsoluteFill style={{ fontFamily: SANS }}>
      <AbsoluteFill style={{ scale: String(k(t, 0, 8, 1, 1.05, Easing.linear)), opacity: k(t, 0, 0.3) }}>
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 180,
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            gap: 40,
            scale: String(k(t, 0, 0.6, 1.4, 1, BACK)),
          }}
        >
          <Mark size={170} />
          <Wordmark size={130} />
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 420, display: "flex", justifyContent: "center", gap: 28 }}>
          {TAG.map((w, i) => (
            <div key={w} style={{ fontSize: 92, fontWeight: 900, letterSpacing: -4, color: i === 2 ? C.red : C.ink, ...rise(t, 0.75 + i * 0.2, 50, 0.5) }}>
              {w}
            </div>
          ))}
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 580, display: "flex", justifyContent: "center", gap: 20 }}>
          {CHAPTERS.map((c, i) => (
            <div key={c} style={{ display: "flex", alignItems: "center", gap: 20, fontSize: 32, fontWeight: 600, color: C.muted, ...rise(t, 1.75 + i * 0.15, 20, 0.4) }}>
              {i ? <span style={{ width: 8, height: 8, borderRadius: 4, background: C.red }} /> : null}
              {c}
            </div>
          ))}
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 700, display: "flex", justifyContent: "center", ...rise(t, 2.5, 40) }}>
          <div
            style={{
              padding: "26px 60px",
              borderRadius: 99,
              background: C.red,
              color: "white",
              fontSize: 40,
              fontWeight: 800,
              boxShadow: "0 24px 50px -16px rgba(229,36,43,0.7)",
              scale: String(t >= 3.92 && t < 4.12 ? 0.94 : 1 + Math.max(0, Math.sin((t - 4.1) * 4)) * 0.02 * k(t, 4.1, 4.3)),
            }}
          >
            Book a demo →
          </div>
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 850, textAlign: "center", fontSize: 30, fontWeight: 600, color: C.muted, ...rise(t, 3.0) }}>
          jomeinvoice.my
        </div>
        <Cursor
          t={t}
          x={k(t, 3.0, 3.8, 1400, 1040, IN_OUT)}
          y={k(t, 3.0, 3.8, 1020, 760, IN_OUT)}
          clicks={[4.0]}
          opacity={k(t, 2.9, 3.1) * (1 - k(t, 5.0, 5.4))}
        />
      </AbsoluteFill>
      <AbsoluteFill style={{ background: "white", opacity: (1 - k(t, 0, 0.35)) * 0.8 + end }} />
    </AbsoluteFill>
  );
};
