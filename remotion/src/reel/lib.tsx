// Общие детали моушн-шоурила (tests/t13_motion_reel): палитра, шрифты, время
// куска, изинги. Движение — только от useCurrentFrame.
import { Easing, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { loadFont as loadArchivo } from "@remotion/google-fonts/ArchivoBlack";
import { loadFont as loadSerif } from "@remotion/google-fonts/InstrumentSerif";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

const archivo = loadArchivo("normal", { weights: ["400"], subsets: ["latin"] });
export const HEAVY = archivo.fontFamily;
/** Шрифт загружен — только после этого замер ширин букв верен. */
export const heavyReady = archivo.waitUntilDone();
export const { fontFamily: SERIF } = loadSerif("italic", { weights: ["400"], subsets: ["latin"] });
export const { fontFamily: MONO } = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] });

export const P = {
  ink: "#0B0B0F",
  cream: "#EFEBE3",
  orange: "#FF4F1F",
  lime: "#D4FF3A",
  violet: "#6B5BFF",
  white: "#FFFFFF",
};

export const OUT = Easing.bezier(0.16, 1, 0.3, 1);
export const IN_OUT = Easing.bezier(0.65, 0, 0.35, 1);
export const EASE_IN = Easing.bezier(0.55, 0, 1, 0.45);
export const BACK = Easing.bezier(0.34, 1.7, 0.64, 1);

export const useT = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return frame / fps;
};

export const k = (t: number, a: number, b: number, from = 0, to = 1, easing = OUT) =>
  interpolate(t, [a, b], [from, to], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing });

/** Детерминированный шум по номеру. */
export const hash = (n: number) => {
  const x = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};
