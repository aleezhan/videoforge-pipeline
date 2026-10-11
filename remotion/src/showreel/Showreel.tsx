// Шоурил продукта, 60 с, 1080p60 — проба «моушн-графика кодом» против рилса
// yinn_sheng (tests/t12_motion_showreel). Сцены перекрываются на 0.25–0.5 с
// (timeline.json), звук собирает build.py эпизода по той же шкале.
import React from "react";
import { AbsoluteFill, Sequence, staticFile, useVideoConfig } from "remotion";
import { Audio } from "@remotion/media";
import timeline from "./timeline.json";
import { LightBg } from "./lib";
import { Hook } from "./Hook";
import { Reveal } from "./Reveal";
import { Chapter } from "./Chapter";
import { Connect } from "./Connect";
import { Automate } from "./Automate";
import { Monitor } from "./Monitor";
import { Secure } from "./Secure";
import { Outro } from "./Outro";

const SCENES: Record<string, React.ReactNode> = {
  hook: <Hook />,
  reveal: <Reveal />,
  ch1: <Chapter n="01" title="Connect" sub="Plug into the systems you already use" />,
  connect: <Connect />,
  ch2: <Chapter n="02" title="Automate" sub="From ERP to LHDN in one click" />,
  automate: <Automate />,
  ch3: <Chapter n="03" title="Monitor" sub="Every submission, every status" />,
  monitor: <Monitor />,
  secure: <Secure />,
  outro: <Outro />,
};

export const Showreel: React.FC<{ withAudio: boolean }> = ({ withAudio }) => {
  const { fps } = useVideoConfig();
  const f = (s: number) => Math.round(s * fps);
  return (
    <AbsoluteFill style={{ background: "#F4F5F9" }}>
      <Sequence from={f(8)} name="bg">
        <LightBg />
      </Sequence>
      {timeline.scenes.map((s) => (
        <Sequence key={s.id} name={s.id} from={f(s.from)} durationInFrames={f(s.to - s.from)}>
          {SCENES[s.id]}
        </Sequence>
      ))}
      {withAudio ? <Audio src={staticFile("showreel/soundtrack.wav")} /> : null}
    </AbsoluteFill>
  );
};
