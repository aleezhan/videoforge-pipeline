// 8–12.5 с: свет. Красная точка вырастает в знак, рядом выезжает имя, вокруг —
// орбита значков, ниже — «1,500+ компаний».
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, Badge, C, count, fmt, IN_OUT, k, Mark, pop, rise, SANS, useT, Wordmark } from "./lib";

const ORBIT = ["invoice", "qr", "shield", "chart", "cloud", "bolt"] as const;

export const Reveal: React.FC = () => {
  const t = useT();
  const size = k(t, 0.3, 0.8, 64, 200, BACK);
  const radius = k(t, 0.3, 0.8, 0.5, 0.28);
  const shift = k(t, 0.9, 1.5, 0, -360, IN_OUT);
  const exit = k(t, 4.0, 4.5, 0, 1, IN_OUT);
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      <AbsoluteFill style={{ scale: String(k(t, 0, 4.5, 1, 1.06) * (1 + exit * 0.1)), opacity: 1 - exit }}>
        {ORBIT.map((icon, i) => {
          const at = 1.0 + i * 0.25;
          const a = (i / ORBIT.length) * Math.PI * 2 + t * 0.35;
          const x = 960 + Math.cos(a) * 760;
          const y = 520 + Math.sin(a) * 330;
          return (
            <div key={icon} style={{ position: "absolute", left: x - 46, top: y - 46, ...pop(t, at) }}>
              <Badge icon={icon} color={i % 2 ? C.ink : C.red} />
            </div>
          );
        })}
        <div
          style={{
            position: "absolute",
            left: 960 + shift - size / 2,
            top: 470 - size / 2,
            scale: String(k(t, 0, 0.25, 0, 1, BACK)),
          }}
        >
          <Mark
            size={size}
            style={{ borderRadius: size * radius, color: `rgba(255,255,255,${k(t, 0.55, 0.8)})` }}
          />
        </div>
        <div
          style={{
            position: "absolute",
            left: 960 + shift + 130,
            top: 470 - 80,
            height: 160,
            display: "flex",
            alignItems: "center",
            overflow: "hidden",
            width: k(t, 1.0, 1.6, 0, 820, IN_OUT),
          }}
        >
          <div style={{ translate: `${k(t, 1.0, 1.6, -80, 0)}px 0px`, whiteSpace: "nowrap" }}>
            <Wordmark size={140} />
          </div>
        </div>
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 640,
            textAlign: "center",
            fontFamily: SANS,
            fontSize: 38,
            fontWeight: 500,
            color: C.muted,
            ...rise(t, 1.8),
          }}
        >
          The e-invoicing middleware for Malaysian businesses
        </div>
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            top: 730,
            display: "flex",
            justifyContent: "center",
            ...rise(t, 2.5),
          }}
        >
          <div
            style={{
              padding: "16px 34px",
              borderRadius: 99,
              background: "white",
              border: `1px solid ${C.line}`,
              boxShadow: "0 18px 40px -16px rgba(14,19,48,0.25)",
              fontFamily: SANS,
              fontSize: 32,
              fontWeight: 600,
              color: C.ink,
            }}
          >
            Trusted by <span style={{ color: C.red, fontWeight: 800 }}>{fmt(count(t, 2.5, 1.3, 1500))}+</span>{" "}
            Malaysian companies
          </div>
        </div>
      </AbsoluteFill>
      <AbsoluteFill style={{ background: "white", opacity: 1 - k(t, 0, 0.35) }} />
    </AbsoluteFill>
  );
};
