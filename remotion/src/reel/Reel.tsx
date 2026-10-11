// Моушн-шоурил, 15 с, 1080p60: по куску на приём, рамка с таймкодом поверх.
// Проба против рилса grafigator (tests/t13_motion_reel); куски и звук — по
// шкале timeline.json.
import React from "react";
import { AbsoluteFill, Sequence, staticFile, useVideoConfig } from "remotion";
import { Audio } from "@remotion/media";
import timeline from "./timeline.json";
import { P } from "./lib";
import { Liquid } from "./Liquid";
import { Countdown, Intro } from "./Intro";
import { Words } from "./Words";
import { Grid } from "./Grid";
import { Particles } from "./Particles";
import { Ui } from "./Ui";
import { Flash } from "./Flash";
import { Outro } from "./Outro";
import { Hud } from "./Hud";

const PARTS: Record<string, React.ReactNode> = {
  intro: <Intro />,
  countdown: <Countdown />,
  words: <Words />,
  grid: <Grid />,
  particles: <Particles />,
  liquid: <Liquid />,
  ui: <Ui />,
  flash: <Flash />,
  outro: <Outro />,
};

export const Reel: React.FC<{ withAudio: boolean; only?: string }> = ({ withAudio, only }) => {
  const { fps } = useVideoConfig();
  const f = (s: number) => Math.round(s * fps);
  return (
    <AbsoluteFill style={{ background: P.ink }}>
      {timeline.parts
        .filter((p) => PARTS[p.id] && (!only || only === p.id))
        .map((p) => (
          <Sequence key={p.id} name={p.id} from={f(p.from)} durationInFrames={f(p.to - p.from)}>
            {PARTS[p.id]}
          </Sequence>
        ))}
      <Hud />
      {withAudio ? <Audio src={staticFile("reel/soundtrack.wav")} /> : null}
    </AbsoluteFill>
  );
};
