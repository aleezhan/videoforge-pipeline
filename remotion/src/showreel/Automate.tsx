// Глава 02: счёт заполняется сам, TIN сверяется с LHDN, курсор жмёт «Submit»,
// шаги проверки отмечаются по долям, в конце — принятый счёт с QR и печатью.
import React from "react";
import { AbsoluteFill, Easing } from "remotion";
import { BACK, C, Card, count, Cursor, fmt, Headline, Icon, IN_OUT, k, MONO, pop, SANS, qrCells, rise, Shell, useT } from "./lib";

const FIELDS = [
  { label: "Supplier", value: "Acme Retail Sdn Bhd", at: 0.5 },
  { label: "Supplier TIN", value: "C2158473920", at: 0.7 },
  { label: "Buyer", value: "Kedai Maju Enterprise", at: 0.9 },
  { label: "Buyer TIN", value: "IG00041527080", at: 1.1 },
];
const ITEMS = [
  ["POS terminal lease", "1", "4,800.00"],
  ["Cloud sync (12 mo)", "12", "5,280.00"],
  ["Onboarding", "1", "2,400.00"],
];
const STEPS = ["Schema validated", "Digitally signed", "Submitted to MyInvois", "Accepted by LHDN"];
const QR = qrCells();

const Field: React.FC<{ t: number; label: string; value: string; at: number; chip?: React.ReactNode }> = ({
  t,
  label,
  value,
  at,
  chip,
}) => (
  <div style={{ display: "flex", alignItems: "center", height: 58, borderBottom: `1px solid ${C.line}` }}>
    <div style={{ width: 200, fontSize: 21, color: C.muted, fontWeight: 600 }}>{label}</div>
    <div
      style={{
        position: "relative",
        fontSize: 25,
        fontWeight: 700,
        padding: "4px 10px",
        borderRadius: 8,
        background: `rgba(229,36,43,${0.14 * k(t, at, at + 0.1) * (1 - k(t, at + 0.15, at + 0.7))})`,
        opacity: k(t, at, at + 0.12),
        translate: `${k(t, at, at + 0.35, 24, 0)}px 0px`,
      }}
    >
      {value}
    </div>
    <div style={{ marginLeft: "auto" }}>{chip}</div>
  </div>
);

const Chip: React.FC<{ color: string; bg: string; icon?: "check" | "refresh"; children: React.ReactNode; style?: React.CSSProperties }> = ({
  color,
  bg,
  icon,
  children,
  style,
}) => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "7px 16px",
      borderRadius: 99,
      background: bg,
      color,
      fontSize: 19,
      fontWeight: 700,
      whiteSpace: "nowrap",
      ...style,
    }}
  >
    {icon ? <Icon name={icon} size={20} color={color} /> : null}
    {children}
  </div>
);

export const Automate: React.FC = () => {
  const t = useT();
  const verified = t >= 2.25;
  const clicked = t >= 4.25;
  const accepted = t >= 6.25;
  const cx = k(t, 3.25, 3.95, 1200, 765, IN_OUT);
  const cy = k(t, 3.25, 3.95, 1060, 772, IN_OUT);
  const pipeOut = k(t, 6.6, 7.0, 0, 1, IN_OUT);
  return (
    <Shell dur={10.75}>
      <AbsoluteFill style={{ scale: String(k(t, 0, 10.75, 1, 1.04, Easing.linear)) }}>
        <div style={{ position: "absolute", left: 150, top: 80 }}>
          <Headline t={t} at={0.25} before="One click. " accent="Fully compliant." />
        </div>
        <div
          style={{
            position: "absolute",
            left: 150,
            top: 210,
            width: 820,
            transform: `perspective(2000px) rotateY(${k(t, 0, 10.75, 9, 3, Easing.linear)}deg)`,
            ...rise(t, 0.25, 60),
          }}
        >
          <Card style={{ padding: "34px 40px 30px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <div style={{ fontSize: 18, letterSpacing: 4, fontWeight: 700, color: C.muted }}>TAX INVOICE</div>
                <div style={{ fontFamily: MONO, fontSize: 34, fontWeight: 600, marginTop: 4 }}>INV-2026-00481</div>
              </div>
              <Chip color={C.blue} bg="rgba(59,91,253,0.1)" icon="refresh">
                Auto-filled from SAP
              </Chip>
            </div>
            <div style={{ marginTop: 16 }}>
              {FIELDS.map((f) => (
                <Field
                  key={f.label}
                  t={t}
                  {...f}
                  chip={
                    f.label === "Buyer TIN" && t >= 1.3 ? (
                      verified ? (
                        <div style={pop(t, 2.25)}>
                          <Chip color={C.green} bg={C.greenSoft} icon="check">
                            TIN verified
                          </Chip>
                        </div>
                      ) : (
                        <Chip color={C.amber} bg={C.amberSoft}>
                          Verifying with LHDN…
                        </Chip>
                      )
                    ) : null
                  }
                />
              ))}
            </div>
            <div style={{ marginTop: 18 }}>
              {ITEMS.map((row, i) => (
                <div
                  key={row[0]}
                  style={{
                    display: "flex",
                    fontSize: 22,
                    padding: "9px 0",
                    borderBottom: `1px dashed ${C.line}`,
                    ...rise(t, 2.5 + i * 0.15, 20, 0.4),
                  }}
                >
                  <div style={{ flex: 1, fontWeight: 600 }}>{row[0]}</div>
                  <div style={{ width: 80, color: C.muted }}>×{row[1]}</div>
                  <div style={{ width: 150, textAlign: "right", fontFamily: MONO }}>{row[2]}</div>
                </div>
              ))}
            </div>
            <div style={{ display: "flex", alignItems: "center", marginTop: 20 }}>
              <div style={{ fontSize: 22, color: C.muted, fontWeight: 600 }}>Total (RM)</div>
              <div style={{ marginLeft: 18, fontFamily: MONO, fontSize: 34, fontWeight: 600 }}>
                {fmt(count(t, 2.9, 0.8, 12480), 2)}
              </div>
              <div
                style={{
                  marginLeft: "auto",
                  width: 290,
                  height: 66,
                  borderRadius: 16,
                  background: accepted ? C.green : C.red,
                  color: "white",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 10,
                  fontSize: 24,
                  fontWeight: 800,
                  scale: String(clicked && t < 4.45 ? 0.95 : 1),
                  boxShadow: `0 16px 30px -12px ${accepted ? "rgba(22,163,74,0.6)" : "rgba(229,36,43,0.6)"}`,
                }}
              >
                {accepted ? (
                  <>
                    <Icon name="check" size={26} color="white" /> Accepted
                  </>
                ) : clicked ? (
                  "Submitting…"
                ) : (
                  "Submit to LHDN"
                )}
              </div>
            </div>
          </Card>
        </div>
        <Cursor t={t} x={cx} y={cy} clicks={[4.25]} opacity={k(t, 3.1, 3.3) * (1 - k(t, 5.0, 5.3))} />

        {/* шаги проверки */}
        <div
          style={{
            position: "absolute",
            left: 1090,
            top: 300,
            width: 660,
            opacity: k(t, 4.4, 4.6) * (1 - pipeOut),
            translate: `${k(t, 4.4, 4.8, 60, 0) + pipeOut * 120}px 0px`,
          }}
        >
          <Card style={{ padding: "36px 40px" }}>
            <div style={{ fontSize: 20, letterSpacing: 4, color: C.muted, fontWeight: 700, marginBottom: 20 }}>LIVE SUBMISSION</div>
            {STEPS.map((s, i) => {
              const at = 4.75 + i * 0.5;
              const done = t >= at;
              return (
                <div key={s} style={{ display: "flex", alignItems: "center", gap: 22, height: 86, position: "relative" }}>
                  {i < STEPS.length - 1 ? (
                    <div style={{ position: "absolute", left: 23, top: 62, width: 4, height: 48, background: C.line }}>
                      <div style={{ width: 4, height: `${k(t, at, at + 0.5) * 100}%`, background: C.green }} />
                    </div>
                  ) : null}
                  <div
                    style={{
                      width: 50,
                      height: 50,
                      borderRadius: 25,
                      border: `3px solid ${done ? C.green : C.line}`,
                      background: done ? C.green : "white",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      scale: String(done ? k(t, at, at + 0.35, 0.6, 1, BACK) : 1),
                    }}
                  >
                    {done ? <Icon name="check" size={28} color="white" /> : null}
                  </div>
                  <div style={{ fontSize: 30, fontWeight: 700, color: done ? C.ink : C.muted }}>{s}</div>
                  <div style={{ marginLeft: "auto", fontFamily: MONO, fontSize: 20, color: C.muted, opacity: done ? 1 : 0 }}>
                    {(0.18 + i * 0.27).toFixed(2)}s
                  </div>
                </div>
              );
            })}
          </Card>
        </div>

        {/* принятый счёт */}
        <div
          style={{
            position: "absolute",
            left: 1090,
            top: 230,
            width: 660,
            transform: `perspective(2000px) rotateY(${k(t, 6.75, 10.75, -10, -4, Easing.linear)}deg)`,
            ...rise(t, 6.75, 80),
          }}
        >
          <Card style={{ padding: "34px 40px" }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <div>
                <div style={{ fontSize: 18, letterSpacing: 4, color: C.muted, fontWeight: 700 }}>E-INVOICE UUID</div>
                <div style={{ fontFamily: MONO, fontSize: 20, marginTop: 6 }}>F9D4KQ8TK2P2RX5TBNNC218</div>
                <div style={{ fontSize: 18, letterSpacing: 4, color: C.muted, fontWeight: 700, marginTop: 24 }}>VALIDATED AT</div>
                <div style={{ fontFamily: MONO, fontSize: 25, marginTop: 6 }}>28 Sep 2026 · 10:42:18</div>
                <div style={{ fontSize: 18, letterSpacing: 4, color: C.muted, fontWeight: 700, marginTop: 24 }}>SOURCE</div>
                <div style={{ fontSize: 25, fontWeight: 700, marginTop: 6 }}>SAP S/4HANA</div>
              </div>
              <svg width={170} height={170} viewBox="0 0 25 25" style={{ opacity: k(t, 7.0, 7.1) }}>
                {[
                  [0, 0],
                  [18, 0],
                  [0, 18],
                ].map(([x, y]) => (
                  <g key={`${x}${y}`}>
                    <rect x={x + 0.5} y={y + 0.5} width={6} height={6} fill="none" stroke={C.ink} strokeWidth={1} />
                    <rect x={x + 2} y={y + 2} width={3} height={3} fill={C.ink} />
                  </g>
                ))}
                {QR.map(([x, y], i) => (
                  <rect key={i} x={x} y={y} width={1} height={1} fill={C.ink} opacity={t >= 7.0 + ((i * 37) % QR.length) / QR.length * 0.7 ? 1 : 0} />
                ))}
              </svg>
            </div>
            <div
              style={{
                marginTop: 30,
                display: "flex",
                alignItems: "center",
                gap: 16,
                padding: "18px 22px",
                borderRadius: 16,
                background: C.greenSoft,
                color: C.green,
                fontSize: 26,
                fontWeight: 800,
                ...rise(t, 7.4, 20, 0.4),
              }}
            >
              <Icon name="shield" size={32} color={C.green} /> Validated by LHDN MyInvois
            </div>
          </Card>
          <div
            style={{
              position: "absolute",
              right: -70,
              bottom: -110,
              fontFamily: SANS,
              width: 200,
              height: 200,
              borderRadius: 100,
              border: `6px solid ${C.red}`,
              color: C.red,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 900,
              fontSize: 28,
              letterSpacing: 2,
              background: "rgba(255,255,255,0.85)",
              opacity: k(t, 7.75, 7.8),
              scale: String(k(t, 7.75, 8.0, 2.2, 1, Easing.in(Easing.quad))),
              rotate: `${k(t, 7.75, 8.0, -40, -14)}deg`,
            }}
          >
            <div>COMPLIANT</div>
            <div style={{ fontSize: 18, letterSpacing: 4 }}>LHDN · 2026</div>
          </div>
        </div>
      </AbsoluteFill>
    </Shell>
  );
};
