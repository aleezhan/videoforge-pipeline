import {
  AbsoluteFill,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { z } from "zod";

// Схема параметров сцены. Провайдер remotion_video собирает props из
// motion_prompt раскадровки; всё, что он не распознал, падает в дефолты.
export const sceneMotionSchema = z.object({
  imageSrc: z.string(), // имя файла в public/
  durationSec: z.number().min(0.5),
  fps: z.number().int().min(1).default(24),
  width: z.number().int().min(16).default(1920),
  height: z.number().int().min(16).default(1080),
  motion: z
    .enum(["push_in", "pull_out", "pan_left", "pan_right", "tilt_up", "tilt_down", "static"])
    .default("push_in"),
  // сила движения: доля зума за клип (0.10 = наезд на 10%)
  strength: z.number().min(0).max(0.5).default(0.1),
  // точка, к которой едет камера (доли кадра)
  focusX: z.number().min(0).max(1).default(0.5),
  focusY: z.number().min(0).max(1).default(0.5),
  // Отрезок пути камеры, который проходит ЭТОТ клип, в долях всего хода.
  // Один клип — весь путь [0, 1]. Продолжение плана (Scene.continues,
  // провайдер передаёт по ChainLink) проходит свой кусок: звено с 6-й по 12-ю
  // секунду плана длиной 18 с идёт [0.333, 0.667] с силой, умноженной на 3, —
  // та же скорость, что у головы, и на стыке масштаб не отпрыгивает к
  // исходному (до 14.09.2026 каждое звено начинало путь заново). Верх может
  // выйти за 1: клип последнего звена длиннее звена на хвост округления,
  // сборка его отрезает.
  pathFrom: z.number().min(0).max(2).default(0),
  pathTo: z.number().min(0).max(2).default(1),
});

export type SceneMotionProps = z.infer<typeof sceneMotionSchema>;

// едва заметная жизнь статичного кадра вместо мёртвого стоп-кадра
const STATIC_ZOOM = 0.02;

// Один непрерывный медленный ход камеры по неподвижной иллюстрации —
// ровно то, что motion_hint стиля просит у генеративных моделей, но
// детерминированно: сама картинка не меняется ни на пиксель.
export const SceneMotion: React.FC<SceneMotionProps> = ({
  imageSrc,
  motion,
  strength,
  focusX,
  focusY,
  pathFrom,
  pathTo,
}) => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const last = Math.max(1, durationInFrames - 1);

  // Где на пути камеры этот кадр: путь целиком — 0..1, клип идёт по своему
  // отрезку. Все движения — функции прогресса, а не номера кадра: так звено
  // цепочки продолжает ход соседа, а не начинает свой.
  const progress = interpolate(frame, [0, last], [pathFrom, pathTo], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // ходы, ограниченные рамкой кадра (отъезд, панорамы), за 1.0 не выходят —
  // иначе в хвосте за концом пути показался бы чёрный край
  const bounded = Math.min(1, progress);

  // База для панорам: кадр слегка увеличен, чтобы было куда ехать.
  const panScale = 1 + strength;
  const panPx = ((panScale - 1) * width) / 2;
  const tiltPx = ((panScale - 1) * height) / 2;

  const scale = (() => {
    switch (motion) {
      case "push_in":
        return 1 + strength * progress;
      case "pull_out":
        return 1 + strength * (1 - bounded);
      case "static":
        return 1 + STATIC_ZOOM * progress;
      default:
        return panScale;
    }
  })();

  const translate = (() => {
    switch (motion) {
      case "pan_left":
        return -panPx + 2 * panPx * bounded;
      case "pan_right":
        return panPx - 2 * panPx * bounded;
      default:
        return 0;
    }
  })();

  const translateY = (() => {
    switch (motion) {
      case "tilt_up":
        return -tiltPx + 2 * tiltPx * bounded;
      case "tilt_down":
        return tiltPx - 2 * tiltPx * bounded;
      default:
        return 0;
    }
  })();

  return (
    <AbsoluteFill style={{ backgroundColor: "black", overflow: "hidden" }}>
      <Img
        src={staticFile(imageSrc)}
        style={{
          width,
          height,
          objectFit: "cover",
          scale: String(scale),
          translate: `${translate}px ${translateY}px`,
          transformOrigin: `${focusX * 100}% ${focusY * 100}%`,
        }}
      />
    </AbsoluteFill>
  );
};
