// 0–8 с: тёмная сцена. Человек вбивает счёт в портал налоговой руками, TIN не
// находится — дальше «Every invoice. Every field. By hand.» и перечёркнутое
// «ручное» слово, которое прокручивается.
import React from "react";
import { AbsoluteFill, Easing } from "remotion";
import { C, Cursor, IN_OUT, k, MONO, SANS, typed, useT } from "./lib";

const FIELDS: { label: string; value: string; at: number; dur: number; col: number; row: number }[] = [
  { label: "Supplier TIN", value: "C2158473920", at: 0.4, dur: 1.2, col: 0, row: 0 },
  { label: "Invoice no.", value: "INV-0481", at: 1.7, dur: 0.7, col: 1, row: 0 },
  { label: "Amount (RM)", value: "12,480.00", at: 2.5, dur: 0.7, col: 0, row: 1 },
  { label: "Buyer TIN", value: "IG00041", at: 3.3, dur: 0.5, col: 1, row: 1 },
  { label: "Tax (RM)", value: "", at: 99, dur: 1, col: 0, row: 2 },
  { label: "Classification", value: "", at: 99, dur: 1, col: 1, row: 2 },
];

const Portal: React.FC<{ t: number }> = ({ t }) => {
  const err = t >= 3.9;
  const shake = err ? Math.sin((t - 3.9) * 70) * k(t, 3.9, 4.3, 14, 0) : 0;
  const clock = 4 * 60 + 12 + Math.floor(t * 9);
  const active = FIELDS.findIndex((f) => t >= f.at - 0.1 && t < f.at + f.dur + 0.3);
  return (
    <div
      style={{
        position: "absolute",
        left: 400,
        top: 170,
        width: 1120,
        height: 660,
        borderRadius: 26,
        background: "linear-gradient(180deg, #121937, #0B1029)",
        border: "1px solid rgba(120,140,255,0.22)",
        boxShadow: "0 60px 140px -30px rgba(40,70,255,0.45)",
        fontFamily: SANS,
        color: "#DDE3FF",
        padding: 44,
        rotate: `${k(t, 0, 4.4, -1.5, 0.5, Easing.linear)}deg`,
        transform: `perspective(1800px) rotateX(${k(t, 0, 4.4, 14, 6, Easing.linear)}deg) rotateY(${k(t, 0, 4.4, -16, -6, Easing.linear)}deg) translateX(${shake}px)`,
        opacity: k(t, 0, 0.4) * (1 - k(t, 4.2, 4.6, 0, 1, IN_OUT)),
        filter: `blur(${k(t, 4.2, 4.6, 0, 16)}px)`,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", fontFamily: MONO, fontSize: 18, letterSpacing: 2 }}>
        <span>
          <span style={{ color: C.red }}>●</span> MANUAL ENTRY · MyInvois portal
        </span>
        <span style={{ opacity: 0.6 }}>INVOICE 481 / 2,300</span>
      </div>
      <div style={{ fontSize: 34, fontWeight: 700, marginTop: 22, color: "white" }}>Submit e-invoice</div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "26px 34px", marginTop: 30 }}>
        {FIELDS.map((f, i) => {
          const bad = err && f.label === "Buyer TIN";
          const on = i === active;
          const caret = on && Math.floor(t * 4) % 2 === 0;
          return (
            <div key={f.label}>
              <div style={{ fontSize: 17, opacity: 0.6, marginBottom: 9 }}>{f.label}</div>
              <div
                style={{
                  height: 62,
                  borderRadius: 12,
                  border: `2px solid ${bad ? C.red : on ? "rgba(120,150,255,0.9)" : "rgba(120,140,255,0.18)"}`,
                  background: bad ? "rgba(229,36,43,0.12)" : "rgba(255,255,255,0.04)",
                  display: "flex",
                  alignItems: "center",
                  padding: "0 20px",
                  fontFamily: MONO,
                  fontSize: 26,
                  color: "white",
                }}
              >
                {typed(f.value, t, f.at, f.dur)}
                {caret ? <span style={{ width: 2, height: 30, background: "white", marginLeft: 2 }} /> : null}
              </div>
              {bad ? (
                <div style={{ color: "#FF6B70", fontSize: 18, marginTop: 8, opacity: k(t, 3.9, 4.0) }}>
                  TIN not found — check and retype
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      <div style={{ position: "absolute", left: 44, bottom: 36, fontFamily: MONO, fontSize: 20, opacity: 0.75 }}>
        on this invoice{" "}
        <span style={{ color: "#FF6B70" }}>
          {String(Math.floor(clock / 60)).padStart(2, "0")}:{String(clock % 60).padStart(2, "0")}
        </span>
      </div>
      <Cursor
        t={t}
        x={k(t, 0, 3.3, 170, 700, IN_OUT)}
        y={k(t, 0, 3.3, 250, 360, IN_OUT)}
        clicks={[0.3, 1.6, 2.4, 3.2]}
        opacity={k(t, 0.1, 0.3)}
      />
    </div>
  );
};

const WORDS = ["data entry", "keying", "importing", "editing"];

const Kinetic: React.FC<{ t: number }> = ({ t }) => {
  const line = (text: string, at: number, color = "white") => (
    <div
      style={{
        opacity: k(t, at, at + 0.15) * (1 - k(t, 5.85, 6.0)),
        translate: `0px ${k(t, at, at + 0.4, 60, 0)}px`,
        color,
      }}
    >
      {text}
    </div>
  );
  const step = k(t, 6.4, 6.6, 0, 1, IN_OUT) + k(t, 6.9, 7.1, 0, 1, IN_OUT) + k(t, 7.15, 7.3, 0, 1, IN_OUT);
  const strike = k(t, 7.45, 7.75, 0, 1, IN_OUT);
  const out = k(t, 7.7, 8.0, 0, 1, Easing.in(Easing.cubic));
  return (
    <AbsoluteFill style={{ fontFamily: SANS, fontWeight: 900, letterSpacing: -4, fontSize: 132, lineHeight: 1.02 }}>
      <div style={{ position: "absolute", left: 180, top: 250 }}>
        {line("Every invoice.", 4.5)}
        {line("Every field.", 5.0)}
        {line("By hand.", 5.5, C.red)}
      </div>
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          top: 400,
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          gap: 34,
          lineHeight: "140px",
          fontSize: 110,
          opacity: k(t, 6.0, 6.15) * (1 - out),
          scale: String(k(t, 6.0, 6.5, 0.92, 1) * (1 + out * 0.6)),
          filter: `blur(${out * 20}px)`,
        }}
      >
        <span style={{ color: "white" }}>Stop the manual</span>
        <span style={{ position: "relative", height: 140, overflow: "hidden", display: "inline-block", width: 600, translate: "0px 12px" }}>
          <span style={{ position: "absolute", left: 0, top: -step * 140 }}>
            {WORDS.map((w) => (
              <div key={w} style={{ height: 140, color: C.red, whiteSpace: "nowrap" }}>
                {w}
              </div>
            ))}
          </span>
          <span
            style={{
              position: "absolute",
              left: 0,
              top: 70,
              height: 12,
              borderRadius: 6,
              background: "white",
              width: `${strike * 440}px`,
            }}
          />
        </span>
      </div>
    </AbsoluteFill>
  );
};

export const Hook: React.FC = () => {
  const t = useT();
  return (
    <AbsoluteFill style={{ background: C.night, overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background: "radial-gradient(ellipse at 50% 45%, rgba(59,91,253,0.35), transparent 60%)",
          opacity: 0.7 + Math.sin(t * 2) * 0.1,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage:
            "linear-gradient(rgba(120,140,255,0.07) 1px, transparent 1px), linear-gradient(90deg, rgba(120,140,255,0.07) 1px, transparent 1px)",
          backgroundSize: "80px 80px",
          backgroundPosition: `0px ${t * 20}px`,
          maskImage: "radial-gradient(ellipse at center, black 20%, transparent 75%)",
        }}
      />
      <Portal t={t} />
      <Kinetic t={t} />
      {/* красная вспышка на ошибке */}
      <AbsoluteFill style={{ background: C.red, opacity: k(t, 3.9, 4.0) * (1 - k(t, 4.0, 4.4)) * 0.25 }} />
      {/* разгон перед раскрытием: белый свет из центра */}
      <AbsoluteFill
        style={{
          background: "radial-gradient(circle at 50% 50%, white 0%, rgba(255,255,255,0) 60%)",
          opacity: k(t, 7.6, 8.0, 0, 1, Easing.in(Easing.quad)),
          scale: String(k(t, 7.6, 8.0, 0.3, 2.5, Easing.in(Easing.quad))),
        }}
      />
    </AbsoluteFill>
  );
};
