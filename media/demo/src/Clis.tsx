import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { C, ease, mono, sans } from "./theme";
import { fadeUp, typed, typedDone } from "./motion";
import { Window } from "./Window";

type Beat = { at: number; kind: "cmd" | "out" | "prompt" | "reply"; text: string; tone?: string };

const Cli: React.FC<{ title: string; beats: Beat[]; width: number; enterAt: number }> = ({ title, beats, width, enterAt }) => {
  const frame = useCurrentFrame();
  const enter = interpolate(frame, [enterAt, enterAt + 18], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: ease });
  return (
    <Window title={title} width={width} style={{ opacity: enter, translate: `0px ${(1 - enter) * 22}px` }}>
      <div style={{ padding: "18px 22px", display: "flex", flexDirection: "column", gap: 8, minHeight: 320, fontFamily: mono, fontSize: 18, lineHeight: 1.55, color: C.text }}>
        {beats.filter((b) => frame >= b.at).map((b, i) => {
          if (b.kind === "cmd") return <div key={i}><span style={{ color: C.faint }}>$ </span><span style={{ fontWeight: 600 }}>{typed(b.text, frame, b.at)}</span></div>;
          if (b.kind === "prompt") return <div key={i}><span style={{ color: C.faint }}>{"> "}</span><span style={{ fontWeight: 600 }}>{typed(b.text, frame, b.at)}</span></div>;
          if (b.kind === "reply") {
            const done = typedDone(b.text, b.at, 0.35);
            return <div key={i} style={{ fontFamily: sans, fontSize: 18, color: C.text, ...fadeUp(frame, b.at - 2) }}>{typed(b.text, frame, b.at, 0.35)}{frame < done ? <span style={{ opacity: 0.6 }}>▌</span> : null}</div>;
          }
          return <div key={i} style={{ color: b.tone ?? C.dim, fontSize: 16, ...fadeUp(frame, b.at) }}>{"  "}{b.text}</div>;
        })}
      </div>
    </Window>
  );
};

export const Clis: React.FC = () => {
  const c0 = 10, x0 = 22; // stagger codex slightly behind claude
  const claude: Beat[] = [
    { at: c0, kind: "cmd", text: "freeride run claude" },
    { at: typedDone("freeride run claude", c0) + 6, kind: "out", text: "gateway ready · ANTHROPIC_BASE_URL set for this session", tone: C.ok },
    { at: typedDone("freeride run claude", c0) + 22, kind: "prompt", text: "why does this test time out?" },
    { at: typedDone("freeride run claude", c0) + 22 + 28 + 14, kind: "reply", text: "The fixture opens a real socket and never closes it; the runner waits on it. Patch the client in conftest and the suite finishes in 0.4 s." },
    { at: typedDone("freeride run claude", c0) + 22 + 28 + 14 + 70, kind: "out", text: "freeride/coding · $0.00", tone: C.dim },
  ];
  const codex: Beat[] = [
    { at: x0, kind: "cmd", text: "freeride run codex" },
    { at: typedDone("freeride run codex", x0) + 6, kind: "out", text: "provider: freeride · model: gpt-5-codex → auto", tone: C.ok },
    { at: typedDone("freeride run codex", x0) + 22, kind: "prompt", text: "fix the failing retry test" },
    { at: typedDone("freeride run codex", x0) + 22 + 26 + 14, kind: "reply", text: "The mock returned 500 forever; capped it at two failures. Test passes, three attempts observed." },
    { at: typedDone("freeride run codex", x0) + 22 + 26 + 14 + 60, kind: "out", text: "nvidia_nim · 1.8 s · $0.00", tone: C.dim },
  ];
  return (
    <AbsoluteFill style={{ background: C.page, alignItems: "center", justifyContent: "center" }}>
      <div style={{ display: "flex", gap: 28 }}>
        <Cli title="claude code" beats={claude} width={596} enterAt={0} />
        <Cli title="codex" beats={codex} width={596} enterAt={8} />
      </div>
    </AbsoluteFill>
  );
};
