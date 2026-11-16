import React from "react";
import { useCurrentFrame } from "remotion";
import { C, mono, sans } from "./theme";
import { fadeUp, prog, typed, typedDone } from "./motion";
import { Window } from "./Window";

const PROMPT = "add retries with backoff to fetchUser()";
const REPLY =
  "Wrapped fetchUser in a retry loop: three attempts, exponential backoff from 200 ms, only on network errors and 5xx. Added a test for the retry path.";

export const Agent: React.FC<{ start: number; width: number; style?: React.CSSProperties }> = ({ start, width, style }) => {
  const frame = useCurrentFrame();
  const promptAt = start + 6;
  const promptDone = typedDone(PROMPT, promptAt);
  const log = [
    { at: promptDone + 8, text: "→ openrouter · nemotron-3-ultra", status: "429 rate limit", tone: C.warn },
    { at: promptDone + 20, text: "→ groq · llama-3.3-70b", status: "200 · 241 tok/s", tone: C.ok },
  ];
  const replyAt = promptDone + 30;
  const reply = typed(REPLY, frame, replyAt, 0.38);
  const replyDone = typedDone(REPLY, replyAt, 0.38);

  return (
    <Window title="fx · freeride connection" width={width} style={style}>
      <div style={{ padding: "20px 26px", display: "flex", flexDirection: "column", gap: 12, minHeight: 300 }}>
        <div style={{ fontFamily: mono, fontSize: 21, lineHeight: 1.6, color: C.text }}>
          <span style={{ color: C.faint }}>│ </span>
          <span style={{ fontWeight: 600 }}>{typed(PROMPT, frame, promptAt)}</span>
        </div>
        <div
          style={{
            display: "flex", flexDirection: "column", gap: 6, padding: "10px 14px", borderRadius: 10,
            border: `1px solid ${C.line}`, background: "#ffffff", fontFamily: mono, fontSize: 15, color: C.dim,
            ...fadeUp(frame, log[0].at - 4),
          }}
        >
          <div style={{ fontSize: 11, letterSpacing: 2, textTransform: "uppercase", color: C.faint }}>gateway</div>
          {log.map((row, i) => (
            <div key={i} style={{ display: "flex", justifyContent: "space-between", ...fadeUp(frame, row.at, 8, 5) }}>
              <span>{row.text}</span>
              <span style={{ color: row.tone, fontWeight: 600 }}>{row.status}</span>
            </div>
          ))}
        </div>
        <div style={{ fontFamily: sans, fontSize: 20, lineHeight: 1.5, color: C.text, minHeight: 96, ...fadeUp(frame, replyAt - 3) }}>
          {reply}
          {frame >= replyAt && frame < replyDone ? <span style={{ opacity: 0.6 }}>▌</span> : null}
        </div>
        <div style={{ fontFamily: mono, fontSize: 16, color: C.dim, display: "flex", gap: 18, opacity: prog(frame, replyDone - 10, 10) }}>
          <span style={{ color: C.text, fontWeight: 600 }}>freeride/coding</span>
          <span>groq</span>
          <span>2.1 s</span>
          <span style={{ color: C.text, fontWeight: 600 }}>$0.00</span>
        </div>
      </div>
    </Window>
  );
};
