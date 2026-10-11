// 12.75–15 с: кольцо раскрывается, по нему бегут буквы VIDEOFORGE, падают на
// строку с отскоком; подчёркивание, подписи, искры по орбите; в конце всё
// схлопывается в точку.
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, EASE_IN, hash, HEAVY, IN_OUT, k, MONO, P, SERIF, useT } from "./lib";

const NAME = "VIDEOFORGE";
const SIZE = 170;
const STEP = 118; // шаг букв для полёта; на месте буквы стоят обычной строкой

export const Outro: React.FC = () => {
  const t = useT();
  const ring = k(t, 0, 0.35, 0, 520, IN_OUT);
  const collapseX = k(t, 1.85, 2.1, 1, 0.004, EASE_IN);
  const collapseY = k(t, 1.75, 1.9, 1, 0.02, EASE_IN);
  const dot = k(t, 2.05, 2.1) * (1 - k(t, 2.15, 2.25));
  return (
    <AbsoluteFill style={{ background: `radial-gradient(ellipse at 50% 55%, #3a1a10, ${P.ink} 70%)`, overflow: "hidden" }}>
      <div
        style={{
          position: "absolute",
          left: 960 - ring,
          top: 540 - ring,
          width: ring * 2,
          height: ring * 2,
          borderRadius: "50%",
          border: `5px solid ${P.orange}`,
          opacity: 1 - k(t, 0.45, 0.7),
        }}
      />
      <AbsoluteFill style={{ scale: `${collapseX} ${collapseY}` }}>
        {/* буквы стоят обычной строкой (кернинг шрифта), а полёт — сдвиг от
            кольца к оценке места; при посадке сдвиг ноль, и оценка не видна */}
        <div style={{ position: "absolute", left: 0, right: 0, top: 430, display: "flex", justifyContent: "center" }}>
          {NAME.split("").map((ch, i) => {
            const a = -Math.PI / 2 + i * 0.5 + t * 4;
            const est = { x: 960 + (i - (NAME.length - 1) / 2) * STEP, y: 430 + SIZE * 0.55 };
            const rx = 960 + Math.cos(a) * ring;
            const ry = 540 + Math.sin(a) * ring;
            const land = k(t, 0.3 + i * 0.035, 0.75 + i * 0.035, 0, 1, BACK);
            return (
              <span
                key={i}
                style={{
                  display: "inline-block",
                  fontFamily: HEAVY,
                  fontSize: SIZE,
                  lineHeight: 1.1,
                  letterSpacing: -4,
                  color: P.cream,
                  translate: `${(rx - est.x) * (1 - land)}px ${(ry - est.y) * (1 - land)}px`,
                  rotate: `${(1 - land) * (hash(i) - 0.5) * 220}deg`,
                  opacity: k(t, 0.05 + i * 0.02, 0.15 + i * 0.02),
                }}
              >
                {ch}
              </span>
            );
          })}
        </div>
        <div style={{ position: "absolute", left: 960 - 600, top: 630, height: 5, background: P.orange, width: k(t, 0.8, 1.15, 0, 1200, IN_OUT) }} />
        <div style={{ position: "absolute", left: 360, top: 660, fontFamily: MONO, fontSize: 18, letterSpacing: 8, color: P.cream, opacity: k(t, 1.0, 1.15) }}>
          MOTION ENGINE
        </div>
        <div style={{ position: "absolute", right: 360, top: 650, fontFamily: SERIF, fontStyle: "italic", fontSize: 40, color: P.cream, opacity: k(t, 1.05, 1.2) }}>
          Showreel 2026
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 820, textAlign: "center", fontFamily: MONO, fontSize: 14, letterSpacing: 5, color: "rgba(239,235,227,0.45)", opacity: k(t, 1.2, 1.35) }}>
          EVERY FRAME OF THIS REEL WAS WRITTEN AS CODE
        </div>
        {[P.orange, P.lime, P.violet].map((c, j) => {
          const a = t * (3 + j) + j * 2.1;
          const x = 960 + Math.cos(a) * 780;
          const y = 520 + Math.sin(a) * 170;
          const front = Math.sin(a) > 0;
          return (
            <div
              key={j}
              style={{
                position: "absolute",
                left: x - 7,
                top: y - 7,
                width: 14,
                height: 14,
                borderRadius: 7,
                background: c,
                boxShadow: `0 0 24px 6px ${c}`,
                opacity: k(t, 0.9, 1.1) * (front ? 1 : 0.35),
              }}
            />
          );
        })}
      </AbsoluteFill>
      <div style={{ position: "absolute", left: 952, top: 532, width: 16, height: 16, borderRadius: 8, background: P.cream, opacity: dot }} />
    </AbsoluteFill>
  );
};
