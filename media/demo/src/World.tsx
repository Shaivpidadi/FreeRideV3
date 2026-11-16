import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { C, ease } from "./theme";
import { Terminal, Line } from "./Terminal";
import { Agent } from "./Agent";
import { typedDone } from "./motion";

const INSTALL = "curl -sSL https://api.free-ride.xyz/install.sh | sh";
const BIND = "freeride daemon install && freeride bind fx";

/** Install + bind in the terminal, then the camera glides to the agent window. */
export const World: React.FC<{ panAtSec: number }> = ({ panAtSec }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = (sec: number) => Math.round(sec * fps);

  const installDone = typedDone(INSTALL, s(0.25));
  const bindAt = installDone + s(1.0);
  const bindDone = typedDone(BIND, bindAt);
  const lines: Line[] = [
    { kind: "cmd", at: s(0.25), text: INSTALL },
    { kind: "out", at: installDone + s(0.2), text: "Installing freeride-gateway…", tone: "dim" },
    { kind: "out", at: installDone + s(0.55), text: "freeride 1.0.0 · ~/.local/bin/freeride", tone: "ok" },
    { kind: "cmd", at: bindAt, text: BIND },
    { kind: "out", at: bindDone + s(0.2), text: "gateway healthy on 127.0.0.1:11343 · supervised by launchd", tone: "ok" },
    { kind: "out", at: bindDone + s(0.45), text: "fx → providers.freeride (openai-chat-completions, no auth)", tone: "text" },
    { kind: "out", at: bindDone + s(0.7), text: "models.freeride = freeride/coding · skill installed", tone: "dim" },
  ];

  const pan = interpolate(frame, [s(panAtSec), s(panAtSec + 0.8)], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: ease,
  });
  const termW = 860, agentW = 820, gap = 70;
  const agentX = termW + gap;
  const camX = interpolate(pan, [0, 1], [termW / 2, agentX + agentW / 2]);
  const camScale = interpolate(pan, [0, 1], [1, 0.98]);

  return (
    <AbsoluteFill style={{ background: C.page, overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          backgroundImage:
            "linear-gradient(rgba(0,0,0,0.035) 1px, transparent 1px), linear-gradient(90deg, rgba(0,0,0,0.035) 1px, transparent 1px)",
          backgroundSize: "48px 48px",
          maskImage: "radial-gradient(ellipse at center, black 40%, transparent 85%)",
        }}
      />
      <div style={{ position: "absolute", left: 640, top: 360, translate: `${-camX * camScale}px ${-200 * camScale}px`, scale: `${camScale}` }}>
        <div style={{ position: "absolute", left: 0, top: 0 }}>
          <Terminal
            lines={lines}
            width={termW}
            style={{ opacity: interpolate(pan, [0, 1], [1, 0.45]), scale: `${interpolate(pan, [0, 1], [1, 0.96])}`, transformOrigin: "right center" }}
          />
        </div>
        <div style={{ position: "absolute", left: agentX, top: 0, opacity: interpolate(pan, [0, 1], [0, 1]) }}>
          <Agent start={s(panAtSec + 0.45)} width={agentW} />
        </div>
      </div>
    </AbsoluteFill>
  );
};
