import React from "react";
import { Composition } from "remotion";
import { Demo, FPS, TOTAL_FRAMES } from "./Demo";

export const RemotionRoot: React.FC = () => (
  <Composition id="FreeRideDemo" component={Demo} durationInFrames={TOTAL_FRAMES} fps={FPS} width={1280} height={720} />
);
