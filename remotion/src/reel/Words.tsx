// 2.0–3.9 с: «CODE» · «BECOMES» (с эхом контуров) · «MOTION.» — буквы падают и
// кувыркаются, «O» заливается чёрным кругом, и камера ныряет в него.
import React from "react";
import { AbsoluteFill } from "remotion";
import { BACK, EASE_IN, hash, HEAVY, IN_OUT, k, P, SERIF, useT } from "./lib";

const Big: React.CSSProperties = { fontFamily: HEAVY, fontSize: 300, letterSpacing: -14, lineHeight: 1 };

export const Words: React.FC = () => {
  const t = useT();
  if (t < 0.4) {
    return (
      <AbsoluteFill style={{ background: P.ink, justifyContent: "center", alignItems: "center" }}>
        <div style={{ ...Big, color: P.cream, display: "flex" }}>
          {"CODE".split("").map((ch, i) => (
            <span
              key={i}
              style={{
                opacity: k(t, i * 0.04, i * 0.04 + 0.08),
                translate: `${k(t, i * 0.04, i * 0.04 + 0.2, 60, 0)}px 0px`,
                filter: `blur(${k(t, i * 0.04, i * 0.04 + 0.15, 14, 0)}px)`,
              }}
            >
              {ch}
            </span>
          ))}
        </div>
      </AbsoluteFill>
    );
  }
  if (t < 0.8) {
    const u = t - 0.4;
    return (
      <AbsoluteFill style={{ background: P.cream, justifyContent: "center", alignItems: "center" }}>
        {[3, 2, 1].map((j) => (
          <div
            key={j}
            style={{
              ...Big,
              position: "absolute",
              color: "transparent",
              WebkitTextStroke: `2px rgba(11,11,15,${0.35 - j * 0.08})`,
              translate: `${j * k(u, 0, 0.3, 0, 26)}px ${-j * k(u, 0, 0.3, 0, 10)}px`,
            }}
          >
            BECOMES
          </div>
        ))}
        <div style={{ ...Big, color: P.ink, position: "absolute", scale: String(k(u, 0, 0.25, 1.12, 1)) }}>BECOMES</div>
      </AbsoluteFill>
    );
  }
  const u = t - 0.8;
  const dive = k(u, 0.7, 1.1, 1, 60, EASE_IN);
  return (
    <AbsoluteFill style={{ background: P.orange, justifyContent: "center", alignItems: "center", overflow: "hidden" }}>
      <div style={{ ...Big, fontSize: 330, color: P.ink, display: "flex" }}>
        {"MOTION.".split("").map((ch, i) => {
          const at = i * 0.035;
          const isO = i === 1;
          return (
            <span
              key={i}
              style={{
                position: "relative",
                display: "inline-block",
                translate: `0px ${k(u, at, at + 0.3, -700, 0, BACK)}px`,
                rotate: `${k(u, at, at + 0.35, (hash(i) - 0.5) * 120, 0, BACK)}deg`,
                zIndex: isO ? 2 : 1,
              }}
            >
              {ch}
              {isO ? (
                <span
                  style={{
                    position: "absolute",
                    left: "50%",
                    top: "52%",
                    width: 250,
                    height: 250,
                    marginLeft: -125,
                    marginTop: -125,
                    borderRadius: "50%",
                    background: P.ink,
                    scale: String(k(u, 0.55, 0.7, 0, 1, BACK) * dive),
                  }}
                />
              ) : null}
            </span>
          );
        })}
      </div>
      <div
        style={{
          position: "absolute",
          top: 700,
          fontFamily: SERIF,
          fontStyle: "italic",
          fontSize: 64,
          color: P.ink,
          opacity: k(u, 0.3, 0.45) * (1 - k(u, 0.7, 0.8, 0, 1, IN_OUT)),
        }}
      >
        frame by frame.
      </div>
    </AbsoluteFill>
  );
};
