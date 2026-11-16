import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C, ease, mono, sans } from "./theme";
import { fadeUp } from "./motion";

export const Intro: React.FC = () => {
  const frame = useCurrentFrame();
  const scale = interpolate(frame, [0, 24], [0.96, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: ease });
  return (
    <AbsoluteFill style={{ background: C.page, alignItems: "center", justifyContent: "center" }}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 22, scale: `${scale}` }}>
        <div style={{ fontFamily: mono, fontSize: 15, letterSpacing: 3, textTransform: "uppercase", color: C.dim, ...fadeUp(frame, 0) }}>
          Open source · $0
        </div>
        <div style={{ fontFamily: sans, fontSize: 112, fontWeight: 600, letterSpacing: -5, lineHeight: 1, color: C.text, ...fadeUp(frame, 4, 16, 16) }}>
          FreeRide
        </div>
        <div style={{ fontFamily: sans, fontSize: 34, fontWeight: 400, color: C.dim, ...fadeUp(frame, 14, 16, 12) }}>
          Free inference for any coding agent.
        </div>
      </div>
    </AbsoluteFill>
  );
};
