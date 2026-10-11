// Рамка поверх всего ролика: уголки, подпись, таймкод, глава и линейка хода.
// Разностное наложение — видна и на чёрном, и на лаймовом, и на кремовом.
import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import timeline from "./timeline.json";
import { MONO } from "./lib";

const Corner: React.FC<{ x: "left" | "right"; y: "top" | "bottom" }> = ({ x, y }) => (
  <div
    style={{
      position: "absolute",
      [x]: 36,
      [y]: 36,
      width: 26,
      height: 26,
      [`border${y === "top" ? "Top" : "Bottom"}`]: "2px solid white",
      [`border${x === "left" ? "Left" : "Right"}`]: "2px solid white",
    }}
  />
);

export const Hud: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const t = frame / fps;
  const part = [...timeline.parts].reverse().find((p) => t >= p.from) ?? timeline.parts[0];
  const tc = `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t) % 60).padStart(2, "0")}:${String(frame % fps).padStart(2, "0")}`;
  const text: React.CSSProperties = { position: "absolute", fontFamily: MONO, fontSize: 15, letterSpacing: 3, color: "white" };
  const end = frame > durationInFrames - fps * 0.4 ? 1 - (frame - (durationInFrames - fps * 0.4)) / (fps * 0.4) : 1;
  return (
    <AbsoluteFill style={{ mixBlendMode: "difference", opacity: 0.75 * Math.min(1, t / 0.3) * end, pointerEvents: "none" }}>
      <Corner x="left" y="top" />
      <Corner x="right" y="top" />
      <Corner x="left" y="bottom" />
      <Corner x="right" y="bottom" />
      <div style={{ ...text, left: 76, top: 42 }}>VIDEOFORGE / MOTION REEL 2026</div>
      <div style={{ ...text, right: 76, top: 42 }}>TC 00:{tc}</div>
      <div style={{ ...text, left: 76, bottom: 44 }}>{part.chapter}</div>
      <div style={{ ...text, right: 76, bottom: 44 }}>1920×1080 / 60 FPS</div>
      <div style={{ position: "absolute", left: 420, right: 420, bottom: 52, height: 1, background: "rgba(255,255,255,0.5)" }}>
        {Array.from({ length: 31 }, (_, i) => (
          <div key={i} style={{ position: "absolute", left: `${(i / 30) * 100}%`, top: i % 5 ? -3 : -7, width: 1, height: i % 5 ? 4 : 8, background: "white" }} />
        ))}
        <div style={{ position: "absolute", left: 0, top: -1, height: 3, width: `${(frame / durationInFrames) * 100}%`, background: "white" }} />
      </div>
    </AbsoluteFill>
  );
};
