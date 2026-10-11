import { CalculateMetadataFunction, Composition } from "remotion";

import { Reel } from "./reel/Reel";
import { Showreel } from "./showreel/Showreel";
import { SceneMotion, SceneMotionProps, sceneMotionSchema } from "./SceneMotion";

// Длительность, размер и fps приходят из props: их считает python-провайдер
// из duration_sec сцены и aspect_ratio раскадровки.
const calculateMetadata: CalculateMetadataFunction<SceneMotionProps> = async ({ props }) => ({
  durationInFrames: Math.max(1, Math.round(props.durationSec * props.fps)),
  fps: props.fps,
  width: props.width,
  height: props.height,
});

export const RemotionRoot: React.FC = () => {
  return (
    <>
    <Composition
      id="Reel"
      component={Reel}
      durationInFrames={15 * 60}
      fps={60}
      width={1920}
      height={1080}
      defaultProps={{ withAudio: false, only: "" }}
    />
    <Composition
      id="Showreel"
      component={Showreel}
      durationInFrames={60 * 60}
      fps={60}
      width={1920}
      height={1080}
      defaultProps={{ withAudio: false }}
    />
    <Composition
      id="SceneMotion"
      component={SceneMotion}
      schema={sceneMotionSchema}
      durationInFrames={120}
      fps={24}
      width={1920}
      height={1080}
      defaultProps={{
        imageSrc: "sample.png",
        durationSec: 5,
        fps: 24,
        width: 1920,
        height: 1080,
        motion: "push_in" as const,
        strength: 0.1,
        focusX: 0.5,
        focusY: 0.5,
        pathFrom: 0,
        pathTo: 1,
      }}
      calculateMetadata={calculateMetadata}
    />
    </>
  );
};
