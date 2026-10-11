// Жидкие капли: метаболы (MarchingCubes) с радужной плёнкой, за ними слово
// «LIQUID». Капли кружат, сливаются в одну капсулу, капсула уходит вправо.
import React, { useMemo } from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import { ThreeCanvas } from "@remotion/three";
import { useThree } from "@react-three/fiber";
import * as THREE from "three";
import { MarchingCubes } from "three/addons/objects/MarchingCubes.js";
import { EASE_IN, HEAVY, IN_OUT, k, P, SERIF, useT } from "./lib";

/** Окружение для отражений: тёмная комната с цветными панелями — радуга на
 * каплях берётся отсюда, а не из цвета материала. */
const envScene = () => {
  const s = new THREE.Scene();
  s.background = new THREE.Color("#120c2e");
  const panel = (color: string, pos: [number, number, number], size: [number, number, number], power = 3) => {
    const m = new THREE.Mesh(
      new THREE.BoxGeometry(...size),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(power) }),
    );
    m.position.set(...pos);
    s.add(m);
  };
  panel("#ff4f1f", [-4, 2, 2], [1, 3, 3]);
  panel("#6b5bff", [4, 1, -1], [1, 4, 4]);
  panel("#39e6ff", [0, 5, 0], [6, 0.5, 3], 2);
  panel("#d4ff3a", [2, -3, 3], [3, 0.5, 2], 1.5);
  panel("#ffffff", [-2, 4, -4], [2, 1, 1], 4);
  panel("#ff3fa4", [-3, -2, -3], [2, 2, 1], 2);
  return s;
};

const Blobs: React.FC<{ t: number }> = ({ t }) => {
  const { gl, scene, camera } = useThree();
  const env = useMemo(() => {
    const pm = new THREE.PMREMGenerator(gl);
    return pm.fromScene(envScene(), 0.02).texture;
  }, [gl]);
  // среда — сразу в отрисовке: эффект срабатывает после снимка кадра
  scene.environment = env;
  camera.lookAt(0, 0, 0);
  const material = useMemo(
    () =>
      new THREE.MeshPhysicalMaterial({
        color: "#d9d4ff",
        metalness: 0.55,
        roughness: 0.03,
        iridescence: 1,
        iridescenceIOR: 1.35,
        iridescenceThicknessRange: [150, 900],
        clearcoat: 1,
        clearcoatRoughness: 0.05,
        envMapIntensity: 2.2,
      }),
    [],
  );
  const mc = useMemo(() => {
    const m = new MarchingCubes(72, material, false, false, 120000);
    m.isolation = 70;
    return m;
  }, [material]);

  // шесть капель по своим кругам → к 1.0–1.4 с стягиваются в две точки
  // капсулы → капсула уходит вправо
  const merge = k(t, 0.95, 1.4, 0, 1, IN_OUT);
  const fly = k(t, 1.4, 1.75, 0, 1, EASE_IN);
  mc.reset();
  for (let i = 0; i < 7; i++) {
    const a = t * (1.1 + i * 0.17) + i * 1.9;
    const free = [0.5 + Math.cos(a) * (0.24 + 0.025 * i), 0.5 + Math.sin(a * 1.3) * 0.2, 0.5 + Math.sin(a) * 0.14];
    const end = i % 2 ? [0.4, 0.42, 0.5] : [0.6, 0.58, 0.5];
    const p = free.map((v, j) => v + (end[j] - v) * merge);
    mc.addBall(p[0] + fly * 0.6, p[1], p[2], 0.45 + 0.14 * (i % 3), 12);
  }
  mc.update();
  return <primitive object={mc} scale={3.1} rotation={[0.2, t * 0.3, 0]} />;
};

export const Liquid: React.FC = () => {
  const t = useT();
  const { width, height } = useVideoConfig();
  return (
    <AbsoluteFill style={{ background: `radial-gradient(ellipse at 50% 60%, #2a1f6b, ${P.ink} 70%)` }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <div
          style={{
            fontFamily: HEAVY,
            fontSize: 380,
            letterSpacing: -18,
            color: P.cream,
            scale: String(k(t, 0, 0.5, 1.25, 1)),
            opacity: k(t, 0, 0.15),
            filter: `blur(${k(t, 1.45, 1.75, 0, 30)}px)`,
          }}
        >
          LIQUID
        </div>
      </AbsoluteFill>
      <ThreeCanvas width={width} height={height} camera={{ position: [0, 0, 7], fov: 40 }} gl={{ alpha: true, antialias: true }}>
        <ambientLight intensity={0.2} />
        <pointLight position={[-4, 3, 4]} intensity={60} color="#ff6a3d" />
        <pointLight position={[4, -2, 4]} intensity={60} color="#6b5bff" />
        <Blobs t={t} />
      </ThreeCanvas>
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 150,
          textAlign: "center",
          fontFamily: SERIF,
          fontStyle: "italic",
          fontSize: 44,
          color: "rgba(239,235,227,0.8)",
          opacity: k(t, 0.3, 0.6) * (1 - k(t, 1.4, 1.6)),
        }}
      >
        seven blobs, one surface.
      </div>
    </AbsoluteFill>
  );
};
