import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { C, ease } from "./theme";
import { Terminal, Line } from "./Terminal";
import { Agent } from "./Agent";
import { typedDone } from "./motion";

const INSTALL = "curl -sSL https://api.free-ride.xyz/install.sh | sh";
const BIND = "freeride daemon install && freeride bind fx";

export const AGENT_AT = 7.6; // seconds: when the camera pans to the agent

export const World: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = (sec: number) => Math.round(sec * fps);

  const installDone = typedDone(INSTALL, s(0.4));
  const bindAt = installDone + s(1.9);
  const bindDone = typedDone(BIND, bindAt);
  const lines: Line[] = [
    { kind: "cmd", at: s(0.4), text: INSTALL },
    { kind: "out", at: installDone + s(0.35), text: "Installing freeride-gateway…", tone: "dim" },
    { kind: "out", at: installDone + s(1.0), text: "freeride 1.0.0 · ~/.local/bin/freeride", tone: "ok" },
    { kind: "cmd", at: bindAt, text: BIND },
    { kind: "out", at: bindDone + s(0.3), text: "gateway healthy on 127.0.0.1:11343 · supervised by launchd", tone: "ok" },
    { kind: "out", at: bindDone + s(0.75), text: "fx → providers.freeride (openai-chat-completions, no auth)", tone: "text" },
    { kind: "out", at: bindDone + s(1.1), text: "models.freeride = freeride/coding · skill installed", tone: "dim" },
  ];

  // Camera: centred on the terminal, then glides right to the agent window.
  const pan = interpolate(frame, [s(AGENT_AT), s(AGENT_AT + 1.1)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: ease,
  });
  const termW = 860;
  const agentW = 820;
  const gap = 70;
  // World coordinates: terminal at x=0, agent to its right.
  const agentX = termW + gap;
  const camX = interpolate(pan, [0, 1], [termW / 2, agentX + agentW / 2]);
  const camScale = interpolate(pan, [0, 1], [1, 0.98]);

  return (
    <AbsoluteFill style={{ background: C.page, overflow: "hidden" }}>
      {/* subtle hatch like the site gutters */}
      <AbsoluteFill
        style={{
          backgroundImage:
            "linear-gradient(rgba(0,0,0,0.035) 1px, transparent 1px), linear-gradient(90deg, rgba(0,0,0,0.035) 1px, transparent 1px)",
          backgroundSize: "48px 48px",
          maskImage: "radial-gradient(ellipse at center, black 40%, transparent 85%)",
        }}
      />
      <div
        style={{
          position: "absolute",
          left: 640,
          top: 360,
          translate: `${-camX * camScale}px ${-200 * camScale}px`,
          scale: `${camScale}`,
        }}
      >
        <div style={{ position: "absolute", left: 0, top: 0 }}>
          <Terminal
            lines={lines}
            width={termW}
            style={{
              opacity: interpolate(pan, [0, 1], [1, 0.45]),
              scale: `${interpolate(pan, [0, 1], [1, 0.96])}`,
              transformOrigin: "right center",
            }}
          />
        </div>
        <div style={{ position: "absolute", left: agentX, top: 0, opacity: interpolate(pan, [0, 1], [0, 1]) }}>
          <Agent start={s(AGENT_AT + 0.6)} width={agentW} />
        </div>
      </div>
    </AbsoluteFill>
  );
};
