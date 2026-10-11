// Провал музыки: щит дорисовывается контуром, под ним по долям — сертификаты.
import React from "react";
import { AbsoluteFill, Easing } from "remotion";
import { C, Card, Icon, k, pop, rise, SANS, Shell, useT } from "./lib";

const CERTS = ["ISO 27001", "ISO 9001", "ISO 20000-1", "PDPA", "MySTI certified"];

export const Secure: React.FC = () => {
  const t = useT();
  const draw = k(t, 0.25, 1.2, 0, 1, Easing.inOut(Easing.cubic));
  return (
    <Shell dur={6}>
      <AbsoluteFill style={{ fontFamily: SANS, scale: String(k(t, 0, 6, 1, 1.05, Easing.linear)) }}>
        <div style={{ position: "absolute", left: 960 - 110, top: 130, width: 220, height: 260 }}>
          <svg width={220} height={260} viewBox="0 0 24 28" style={{ position: "absolute" }}>
            <path
              d="M12 1.5l10 4v8c0 6.5-4.3 10.8-10 13-5.7-2.2-10-6.5-10-13v-8z"
              fill={`rgba(229,36,43,${0.08 * draw})`}
              stroke={C.red}
              strokeWidth={1.1}
              strokeLinejoin="round"
              pathLength={1}
              strokeDasharray={1}
              strokeDashoffset={1 - draw}
            />
          </svg>
          <div style={{ position: "absolute", left: 60, top: 70, ...pop(t, 1.0) }}>
            <Icon name="lock" size={100} color={C.red} />
          </div>
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 430, textAlign: "center", fontSize: 96, fontWeight: 900, letterSpacing: -4, color: C.ink, ...rise(t, 0.6) }}>
          Secure by design.
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 560, textAlign: "center", fontSize: 32, color: C.muted, fontWeight: 500, ...rise(t, 1.0) }}>
          Enterprise-grade encryption · Complete audit trails · Role-based access
        </div>
        <div style={{ position: "absolute", left: 0, right: 0, top: 690, display: "flex", justifyContent: "center", gap: 26 }}>
          {CERTS.map((c, i) => (
            <div key={c} style={pop(t, 1.75 + i * 0.5)}>
              <Card style={{ padding: "22px 30px", borderRadius: 20, display: "flex", alignItems: "center", gap: 12, fontSize: 28, fontWeight: 800 }}>
                <Icon name="check" size={28} color={C.green} />
                {c}
              </Card>
            </div>
          ))}
        </div>
      </AbsoluteFill>
    </Shell>
  );
};
