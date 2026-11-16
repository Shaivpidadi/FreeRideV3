import React from "react";
import { Sequence, staticFile } from "remotion";
import { Audio } from "@remotion/media";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { Intro } from "./Intro";
import { World } from "./World";
import { Clis } from "./Clis";
import { EndCard } from "./EndCard";
import vo from "./vo.json";

export const FPS = 30;
const f = (sec: number) => Math.round(sec * FPS);
const PAD = 0.45; // seconds of air after each narration clip
export const XFADE = 12;

export const INTRO_FRAMES = f(vo.intro + PAD);
const INSTALL_SEC = Math.max(vo.install + PAD, 4.3);
export const WORLD_FRAMES = f(INSTALL_SEC + vo.agent + PAD);
export const CLIS_FRAMES = f(Math.max(vo.clis + PAD, 6.2));
export const END_FRAMES = f(vo.end + PAD);
export const TOTAL_FRAMES = INTRO_FRAMES + WORLD_FRAMES + CLIS_FRAMES + END_FRAMES - 3 * XFADE;

const Vo: React.FC<{ name: keyof typeof vo; from?: number }> = ({ name, from = 0 }) => (
  <Sequence from={from} layout="none">
    <Audio src={staticFile(`vo/${name}.mp3`)} />
  </Sequence>
);

export const Demo: React.FC = () => (
  <TransitionSeries>
    <TransitionSeries.Sequence durationInFrames={INTRO_FRAMES}>
      <Intro />
      <Vo name="intro" from={4} />
    </TransitionSeries.Sequence>
    <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: XFADE })} />
    <TransitionSeries.Sequence durationInFrames={WORLD_FRAMES}>
      <World panAtSec={INSTALL_SEC} />
      <Vo name="install" from={6} />
      <Vo name="agent" from={f(INSTALL_SEC) + 4} />
    </TransitionSeries.Sequence>
    <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: XFADE })} />
    <TransitionSeries.Sequence durationInFrames={CLIS_FRAMES}>
      <Clis />
      <Vo name="clis" from={6} />
    </TransitionSeries.Sequence>
    <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: XFADE })} />
    <TransitionSeries.Sequence durationInFrames={END_FRAMES}>
      <EndCard />
      <Vo name="end" from={4} />
    </TransitionSeries.Sequence>
  </TransitionSeries>
);
