import React from "react";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { World } from "./World";
import { EndCard } from "./EndCard";

export const FPS = 30;
export const WORLD_FRAMES = Math.round(15.2 * FPS);
export const END_FRAMES = Math.round(4.2 * FPS);
export const XFADE = 22;
export const TOTAL_FRAMES = WORLD_FRAMES + END_FRAMES - XFADE;

export const Demo: React.FC = () => (
  <TransitionSeries>
    <TransitionSeries.Sequence durationInFrames={WORLD_FRAMES}>
      <World />
    </TransitionSeries.Sequence>
    <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: XFADE })} />
    <TransitionSeries.Sequence durationInFrames={END_FRAMES}>
      <EndCard />
    </TransitionSeries.Sequence>
  </TransitionSeries>
);
