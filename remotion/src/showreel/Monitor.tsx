// Глава 03: панель. Три цифры, столбики по часам и живая лента отправок, где
// упавший счёт сам уходит на повтор и принимается.
import React from "react";
import { AbsoluteFill, Easing } from "remotion";
import { BACK, C, Card, count, fmt, Headline, Icon, k, MONO, pop, rise, SANS, Shell, useT } from "./lib";

const KPI = [
  { label: "Invoices submitted today", to: 12847, digits: 0, suffix: "" },
  { label: "Accepted by LHDN", to: 99.8, digits: 1, suffix: "%" },
  { label: "Avg. time to LHDN", to: 1.4, digits: 1, suffix: " s" },
];
const BARS = [38, 52, 44, 67, 81, 73, 92, 88, 100, 84, 95, 78];
const ROWS = [
  { id: "INV-2026-00488", who: "Acme Retail Sdn Bhd", src: "SAP" },
  { id: "INV-2026-00489", who: "Kedai Maju Enterprise", src: "Shopify" },
  { id: "INV-2026-00490", who: "Syarikat Lestari", src: "NetSuite" },
  { id: "INV-2026-00491", who: "Nusantara Foods", src: "Odoo", retry: true },
  { id: "INV-2026-00492", who: "Pelangi Logistics", src: "D365" },
  { id: "INV-2026-00493", who: "Bintang Textiles", src: "SAP" },
];

const Status: React.FC<{ t: number; retry?: boolean }> = ({ t, retry }) => {
  let label = "Accepted";
  let color = C.green;
  let bg = C.greenSoft;
  let icon: "check" | "refresh" | null = "check";
  if (retry && t < 5.25) [label, color, bg, icon] = ["Failed · timeout", C.red, C.redSoft, null];
  else if (retry && t < 6.25) [label, color, bg, icon] = ["Retrying…", C.amber, C.amberSoft, "refresh"];
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 14px",
        borderRadius: 99,
        background: bg,
        color,
        fontSize: 19,
        fontWeight: 700,
        whiteSpace: "nowrap",
        scale: String(retry ? k(t, 6.25, 6.55, 1.25, 1, BACK) : 1),
      }}
    >
      {icon ? (
        <div style={{ rotate: icon === "refresh" ? `${t * 360}deg` : "0deg", display: "flex" }}>
          <Icon name={icon} size={20} color={color} />
        </div>
      ) : null}
      {label}
    </div>
  );
};

export const Monitor: React.FC = () => {
  const t = useT();
  return (
    <Shell dur={10.75}>
      <AbsoluteFill
        style={{
          transform: `perspective(2400px) rotateX(${k(t, 0, 10.75, 6, 2, Easing.linear)}deg)`,
          scale: String(k(t, 0, 10.75, 0.98, 1.04, Easing.linear)),
        }}
      >
        <div style={{ position: "absolute", left: 150, top: 70 }}>
          <Headline t={t} at={0.25} before="See every submission, " accent="live." />
        </div>
        {KPI.map((m, i) => (
          <div key={m.label} style={{ position: "absolute", left: 150 + i * 550, top: 190, width: 520, height: 170, ...pop(t, 0.75 + i * 0.5) }}>
            <Card style={{ height: "100%", padding: "26px 34px" }}>
              <div style={{ fontSize: 22, color: C.muted, fontWeight: 600 }}>{m.label}</div>
              <div style={{ fontSize: 76, fontWeight: 900, letterSpacing: -3, marginTop: 8, color: i === 1 ? C.green : C.ink }}>
                {fmt(count(t, 0.75 + i * 0.5, 1.6, m.to), m.digits)}
                {m.suffix}
              </div>
            </Card>
          </div>
        ))}
        <div style={{ position: "absolute", left: 150, top: 400, width: 760, height: 560, ...rise(t, 1.9, 50) }}>
          <Card style={{ height: "100%", padding: "30px 36px", display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: 24, fontWeight: 700 }}>Submissions per hour</div>
            <div style={{ flex: 1, display: "flex", alignItems: "flex-end", gap: 16, paddingTop: 30 }}>
              {BARS.map((h, i) => (
                <div
                  key={i}
                  style={{
                    flex: 1,
                    height: `${h * k(t, 2.0 + i * 0.1, 2.6 + i * 0.1, 0, 1, BACK)}%`,
                    borderRadius: 10,
                    background: i === 8 ? C.red : `linear-gradient(180deg, rgba(229,36,43,0.55), rgba(229,36,43,0.2))`,
                  }}
                />
              ))}
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontFamily: MONO, fontSize: 18, color: C.muted, marginTop: 14 }}>
              <span>08:00</span>
              <span>14:00</span>
              <span>19:00</span>
            </div>
          </Card>
        </div>
        <div style={{ position: "absolute", left: 950, top: 400, width: 820, height: 560, ...rise(t, 2.0, 50) }}>
          <Card style={{ height: "100%", padding: "30px 36px", overflow: "hidden" }}>
            <div style={{ display: "flex", alignItems: "center", fontSize: 24, fontWeight: 700 }}>
              Live feed
              <div style={{ marginLeft: 14, width: 12, height: 12, borderRadius: 6, background: C.red, opacity: 0.4 + 0.6 * Math.abs(Math.sin(t * 3)) }} />
            </div>
            <div style={{ marginTop: 16 }}>
              {ROWS.map((r, i) => {
                const at = 2.25 + i * 0.5;
                const glow = r.retry ? k(t, 6.25, 6.4) * (1 - k(t, 6.5, 7.3)) : 0;
                return (
                  <div
                    key={r.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 18,
                      height: 72,
                      borderBottom: `1px solid ${C.line}`,
                      background: `rgba(22,163,74,${glow * 0.12})`,
                      ...rise(t, at, 30, 0.4),
                    }}
                  >
                    <div style={{ fontFamily: MONO, fontSize: 20, width: 210 }}>{r.id}</div>
                    <div style={{ fontSize: 21, fontWeight: 600, flex: 1 }}>{r.who}</div>
                    <div style={{ fontSize: 18, color: C.muted, width: 90 }}>{r.src}</div>
                    <Status t={t} retry={r.retry} />
                  </div>
                );
              })}
            </div>
          </Card>
        </div>
        <div
          style={{
            position: "absolute",
            left: 560,
            top: 700,
            ...pop(t, 5.25),
            opacity: k(t, 5.25, 5.4) * (1 - k(t, 8.5, 8.8)),
          }}
        >
          <div
            style={{
              background: C.ink,
              fontFamily: SANS,
              color: "white",
              fontSize: 22,
              fontWeight: 700,
              padding: "14px 22px",
              borderRadius: 14,
              display: "flex",
              gap: 10,
              alignItems: "center",
              boxShadow: "0 20px 40px -15px rgba(14,19,48,0.5)",
              whiteSpace: "nowrap",
            }}
          >
            <Icon name="refresh" size={22} color="white" /> Auto-retry until LHDN accepts →
          </div>
        </div>
      </AbsoluteFill>
    </Shell>
  );
};
