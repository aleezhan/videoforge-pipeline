// 3.75–5.85 с: сетка фигур. Волна из центра → круги становятся квадратами и
// ромбами → вытягиваются в диагональные «таблетки» и текут → снова точки →
// стягиваются в центр, откуда начнутся частицы.
import React from "react";
import { AbsoluteFill } from "remotion";
import { hash, IN_OUT, k, P, SERIF, useT } from "./lib";

const COLS = 25;
const ROWS = 14;
const STEP = 80;
const COLORS = [P.cream, P.cream, P.orange, P.lime, P.violet];

export const Grid: React.FC = () => {
  const t = useT();
  const square = k(t, 0.5, 0.8, 0, 1, IN_OUT);
  const turn = k(t, 0.6, 1.1, 0, 45, IN_OUT);
  const pill = k(t, 1.1, 1.4, 0, 1, IN_OUT) * (1 - k(t, 1.6, 1.85, 0, 1, IN_OUT));
  const gather = k(t, 1.85, 2.1, 0, 1, IN_OUT);
  const cells = [];
  for (let r = 0; r < ROWS; r++)
    for (let c = 0; c < COLS; c++) {
      const i = r * COLS + c;
      const x0 = 960 + (c - (COLS - 1) / 2) * STEP;
      const y0 = 540 + (r - (ROWS - 1) / 2) * STEP;
      const d = Math.hypot(x0 - 960, y0 - 540);
      const born = k(t, d / 3200, d / 3200 + 0.18);
      const wave = 0.65 + 0.35 * Math.sin(d / 90 - t * 9);
      const flow = pill * ((t * 260 + hash(i) * 40) % STEP);
      const x = x0 + flow * 0.8 + (960 - x0) * gather;
      const y = y0 - flow * 0.6 + (540 - y0) * gather;
      const s = 38 * wave * born * (1 - gather * 0.85);
      cells.push(
        <div
          key={i}
          style={{
            position: "absolute",
            left: x - s / 2,
            top: y - s / 2,
            width: s * (1 + pill * 1.3),
            height: s * (1 - pill * 0.35),
            borderRadius: `${50 - square * 40 + pill * 40}%`,
            background: COLORS[Math.floor(hash(i + 7) * COLORS.length)],
            rotate: `${turn * (1 - pill) - pill * 35 + (1 - pill) * Math.sin(d / 120 - t * 6) * 20 * square}deg`,
            opacity: born,
          }}
        />,
      );
    }
  return (
    <AbsoluteFill style={{ background: P.ink, overflow: "hidden" }}>
      {cells}
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div
          style={{
            fontFamily: SERIF,
            fontStyle: "italic",
            fontSize: 250,
            color: P.white,
            textShadow: `-4px 0 ${P.orange}, 4px 0 ${P.violet}`,
            opacity: k(t, 0.7, 0.8) * (1 - k(t, 1.5, 1.6)),
            scale: String(k(t, 0.7, 1.6, 0.94, 1.06)),
          }}
        >
          Pattern.
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
