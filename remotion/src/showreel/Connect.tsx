// Глава 01: слева учётные системы по одной на долю, линии к знаку продукта,
// от него — к LHDN MyInvois; по линиям бегут счета.
import React from "react";
import { AbsoluteFill, Easing } from "remotion";
import { C, Card, count, fmt, Headline, Icon, k, Mark, pop, rise, SANS, Shell, useT, Wordmark } from "./lib";

const SYSTEMS = ["SAP S/4HANA", "Dynamics 365", "Oracle NetSuite", "Odoo", "Shopify", "WooCommerce"];
const PILL_X = 150;
const PILL_W = 420;
const HUB = { x: 1060, y: 590, w: 330, h: 150 };
const LHDN = { x: 1640, y: 590 };

const ys = SYSTEMS.map((_, i) => 290 + i * 118);

const bez = (p0: number[], p1: number[], p2: number[], p3: number[], u: number) => {
  const m = 1 - u;
  return [0, 1].map(
    (j) => m * m * m * p0[j] + 3 * m * m * u * p1[j] + 3 * m * u * u * p2[j] + u * u * u * p3[j],
  );
};

export const Connect: React.FC = () => {
  const t = useT();
  const left = HUB.x - HUB.w / 2;
  const right = HUB.x + HUB.w / 2;
  const curves = ys.map((y) => {
    const a = [PILL_X + PILL_W, y];
    const d = [left, HUB.y];
    const mx = (a[0] + d[0]) / 2;
    return [a, [mx, y], [mx, HUB.y], d];
  });
  const hubPulse = 1 + Math.max(0, Math.cos(((t % 0.5) / 0.5) * Math.PI)) * 0.03 * k(t, 3.5, 4);
  return (
    <Shell dur={8.75}>
      <AbsoluteFill
        style={{
          transform: `perspective(2200px) rotateY(${k(t, 0, 8.75, 7, -4, Easing.linear)}deg) rotateX(${k(t, 0, 8.75, 4, 1, Easing.linear)}deg)`,
          scale: String(k(t, 0, 8.75, 0.98, 1.05, Easing.linear)),
        }}
      >
        <div style={{ position: "absolute", left: 150, top: 90 }}>
          <Headline t={t} at={0.25} before="Integrate " accent="every system" after=" you already run." />
        </div>
        <svg width={1920} height={1080} style={{ position: "absolute", inset: 0 }}>
          {curves.map((c, i) => {
            const at = 0.75 + i * 0.5 + 0.15;
            const len = 700;
            const drawn = k(t, at, at + 0.5, 0, 1, Easing.inOut(Easing.cubic));
            const d = `M${c[0].join(" ")} C${c[1].join(" ")} ${c[2].join(" ")} ${c[3].join(" ")}`;
            return (
              <path
                key={i}
                d={d}
                fill="none"
                stroke={C.red}
                strokeOpacity={0.35}
                strokeWidth={3}
                strokeDasharray={len}
                strokeDashoffset={len * (1 - drawn)}
              />
            );
          })}
          <line
            x1={right}
            y1={HUB.y}
            x2={right + (LHDN.x - 150 - right) * k(t, 3.75, 4.2, 0, 1, Easing.inOut(Easing.cubic))}
            y2={HUB.y}
            stroke={C.green}
            strokeOpacity={0.5}
            strokeWidth={4}
          />
          {t > 3.5
            ? curves.flatMap((c, i) =>
                [0, 0.5].map((off) => {
                  const u = ((t - 3.5) * 0.55 + i * 0.13 + off) % 1;
                  const [x, y] = bez(c[0], c[1], c[2], c[3], u);
                  return (
                    <rect
                      key={`${i}-${off}`}
                      x={x - 11}
                      y={y - 8}
                      width={22}
                      height={16}
                      rx={4}
                      fill={C.red}
                      opacity={k(t, 3.5, 3.9) * Math.sin(u * Math.PI)}
                    />
                  );
                }),
              )
            : null}
          {t > 4.2
            ? [0, 0.33, 0.66].map((off) => {
                const u = ((t - 4.2) * 0.9 + off) % 1;
                return (
                  <circle
                    key={off}
                    cx={right + (LHDN.x - 150 - right) * u}
                    cy={HUB.y}
                    r={9}
                    fill={C.green}
                    opacity={Math.sin(u * Math.PI)}
                  />
                );
              })
            : null}
        </svg>
        {SYSTEMS.map((name, i) => (
          <div
            key={name}
            style={{
              position: "absolute",
              left: PILL_X,
              top: ys[i] - 42,
              width: PILL_W,
              height: 84,
              transformOrigin: "right center",
              ...pop(t, 0.75 + i * 0.5),
            }}
          >
            <Card
              style={{
                height: "100%",
                borderRadius: 20,
                display: "flex",
                alignItems: "center",
                gap: 18,
                padding: "0 24px",
                fontSize: 28,
                fontWeight: 700,
              }}
            >
              <div
                style={{
                  width: 44,
                  height: 44,
                  borderRadius: 12,
                  background: C.bg,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <Icon name="link" size={26} color={C.muted} />
              </div>
              {name}
              <div style={{ marginLeft: "auto", width: 12, height: 12, borderRadius: 6, background: k(t, 1.2 + i * 0.5, 1.3 + i * 0.5) > 0.5 ? C.green : C.line }} />
            </Card>
          </div>
        ))}
        <div style={{ position: "absolute", left: left, top: HUB.y - HUB.h / 2, width: HUB.w, height: HUB.h, scale: String(hubPulse), ...rise(t, 0.4) }}>
          <Card style={{ height: "100%", display: "flex", alignItems: "center", gap: 16, padding: "0 26px" }}>
            <Mark size={78} />
            <Wordmark size={42} />
          </Card>
        </div>
        <div style={{ position: "absolute", left: LHDN.x - 150, top: LHDN.y - 90, width: 300, height: 180, ...pop(t, 3.75) }}>
          <Card
            style={{
              height: "100%",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 10,
              border: `2px solid ${C.green}`,
            }}
          >
            <div style={{ width: 60, height: 60, borderRadius: 30, background: C.greenSoft, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Icon name="check" size={34} color={C.green} />
            </div>
            <div style={{ fontSize: 30, fontWeight: 800 }}>LHDN</div>
            <div style={{ fontSize: 20, color: C.muted, fontWeight: 600, marginTop: -8 }}>MyInvois</div>
          </Card>
        </div>
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: 60,
            textAlign: "center",
            fontFamily: SANS,
            fontSize: 30,
            color: C.muted,
            fontWeight: 600,
            ...rise(t, 4.5),
          }}
        >
          No new ERP. No re-keying. <span style={{ color: C.ink, fontWeight: 800 }}>{fmt(count(t, 4.5, 3.5, 2418))}</span> invoices synced today.
        </div>
      </AbsoluteFill>
    </Shell>
  );
};
