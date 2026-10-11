// 5.75–8.0 с: 2400 частиц. Сгусток → сфера вращается → узел → разлёт →
// слово «FORGE». Каждая частица едет со своей задержкой — форма течёт, а не
// щёлкает. Точки слова — points.json (build.py, шаг points).
import React, { useLayoutEffect, useMemo, useRef } from "react";
import { AbsoluteFill } from "remotion";
import points from "./points.json";
import { hash, IN_OUT, k, MONO, P, useT } from "./lib";

const N = 2400;
type V = [number, number, number];

const lerp = (a: V, b: V, u: number): V => [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u];
const rotY = (p: V, a: number): V => [p[0] * Math.cos(a) + p[2] * Math.sin(a), p[1], -p[0] * Math.sin(a) + p[2] * Math.cos(a)];
const rotX = (p: V, a: number): V => [p[0], p[1] * Math.cos(a) - p[2] * Math.sin(a), p[1] * Math.sin(a) + p[2] * Math.cos(a)];

export const Particles: React.FC = () => {
  const t = useT();
  const ref = useRef<HTMLCanvasElement>(null);
  const shapes = useMemo(() => {
    const cluster: V[] = [];
    const sphere: V[] = [];
    const knot: V[] = [];
    const scatter: V[] = [];
    const word: V[] = [];
    for (let i = 0; i < N; i++) {
      const g = () => (hash(i * 3.1 + cluster.length) + hash(i * 7.7) + hash(i * 1.3) - 1.5) * 60;
      cluster.push([g(), (hash(i * 5.3) - 0.5) * 90, (hash(i * 9.1) - 0.5) * 90]);
      const y = 1 - (2 * (i + 0.5)) / N;
      const r = Math.sqrt(1 - y * y);
      const th = i * Math.PI * (3 - Math.sqrt(5));
      sphere.push([Math.cos(th) * r * 340, y * 340, Math.sin(th) * r * 340]);
      // трилистник с толщиной трубки
      const s = (i / N) * Math.PI * 2;
      const tube = 34 * (hash(i * 2.9) - 0.5);
      const tube2 = 34 * (hash(i * 4.1) - 0.5);
      knot.push([
        (Math.sin(s) + 2 * Math.sin(2 * s)) * 100 + tube,
        (Math.cos(s) - 2 * Math.cos(2 * s)) * 100 + tube2,
        -Math.sin(3 * s) * 110 + tube,
      ]);
      scatter.push([(hash(i * 1.7) - 0.5) * 2200, (hash(i * 2.3) - 0.5) * 1300, (hash(i * 3.7) - 0.5) * 600]);
      const w = points[i % points.length];
      word.push([w[0], w[1], (hash(i * 6.6) - 0.5) * 20]);
    }
    return { cluster, sphere, knot, scatter, word };
  }, []);

  useLayoutEffect(() => {
    const c = ref.current;
    if (!c) return;
    const ctx = c.getContext("2d")!;
    ctx.clearRect(0, 0, 1920, 1080);
    const spin = t * 1.2;
    for (let i = 0; i < N; i++) {
      const d = hash(i * 8.8) * 0.12;
      const u1 = k(t, 0 + d, 0.35 + d, 0, 1, IN_OUT);
      const u2 = k(t, 0.95 + d, 1.3 + d, 0, 1, IN_OUT);
      const u3 = k(t, 1.55 + d * 0.5, 1.7 + d * 0.5, 0, 1, IN_OUT);
      const u4 = k(t, 1.7 + d, 2.05 + d, 0, 1, IN_OUT);
      let p = lerp(shapes.cluster[i], shapes.sphere[i], u1);
      p = lerp(p, shapes.knot[i], u2);
      p = rotX(rotY(p, spin * (1 - u4)), 0.35 * (1 - u4));
      p = lerp(p, shapes.scatter[i], u3 * (1 - u4));
      p = lerp(p, shapes.word[i], u4);
      const z = p[2];
      const f = 900 / (900 - z);
      const x = 960 + p[0] * f;
      const y = 540 + p[1] * f;
      const a = Math.max(0.15, Math.min(1, 0.55 + z / 700)) * (1 - u4) + u4;
      ctx.fillStyle = i % 9 === 0 ? `rgba(212,255,58,${a})` : i % 5 === 0 ? `rgba(160,150,255,${a})` : `rgba(235,230,255,${a})`;
      const s = (2.6 + u4 * 1.2) * f;
      ctx.fillRect(x - s / 2, y - s / 2, s, s);
    }
  }, [t, shapes]);

  return (
    <AbsoluteFill style={{ background: `radial-gradient(ellipse at 50% 50%, #241a55, ${P.ink} 65%)`, opacity: k(t, 0, 0.1) * (1 - k(t, 2.1, 2.25)) }}>
      <canvas ref={ref} width={1920} height={1080} style={{ position: "absolute", inset: 0 }} />
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          top: 800,
          textAlign: "center",
          fontFamily: MONO,
          fontSize: 18,
          letterSpacing: 6,
          color: "rgba(239,235,227,0.7)",
          opacity: k(t, 1.9, 2.0),
        }}
      >
        2,400 PARTICLES · ONE SEED
      </div>
    </AbsoluteFill>
  );
};
