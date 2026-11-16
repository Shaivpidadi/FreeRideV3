import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { C, mono } from "./theme";
import { fadeUp, typed, typedDone } from "./motion";
import { Window } from "./Window";

export type Line =
  | { kind: "cmd"; at: number; text: string }
  | { kind: "out"; at: number; text: string; tone?: "ok" | "dim" | "warn" | "text" };

const toneColor = { ok: C.ok, dim: C.dim, warn: C.warn, text: C.text };

export const Terminal: React.FC<{ lines: Line[]; width: number; style?: React.CSSProperties }> = ({
  lines,
  width,
  style,
}) => {
  const frame = useCurrentFrame();
  const cursorOn = interpolate(frame % 24, [0, 12, 24], [1, 0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // The cursor sits on the last command still being typed, else on a fresh prompt.
  const typing = lines.find((l) => l.kind === "cmd" && frame >= l.at && frame < typedDone(l.text, l.at));
  const allDone = lines.every((l) => frame >= l.at + 14);

  return (
    <Window title="terminal" width={width} style={style}>
      <div
        style={{
          padding: "22px 26px",
          fontFamily: mono,
          fontSize: 21,
          lineHeight: 1.65,
          color: C.text,
          minHeight: 300,
          display: "flex",
          flexDirection: "column",
        }}
      >
        {lines
          .filter((l) => frame >= l.at)
          .map((l, i) => {
            if (l.kind === "cmd") {
              return (
                <div key={i}>
                  <span style={{ color: C.faint }}>$ </span>
                  <span style={{ fontWeight: 600 }}>{typed(l.text, frame, l.at)}</span>
                  {typing === l ? <span style={{ opacity: cursorOn }}>▌</span> : null}
                </div>
              );
            }
            return (
              <div key={i} style={{ color: toneColor[l.tone ?? "text"], ...fadeUp(frame, l.at) }}>
                {"  "}
                {l.text}
              </div>
            );
          })}
        {!typing && allDone ? (
          <div>
            <span style={{ color: C.faint }}>$ </span>
            <span style={{ opacity: cursorOn }}>▌</span>
          </div>
        ) : null}
      </div>
    </Window>
  );
};
