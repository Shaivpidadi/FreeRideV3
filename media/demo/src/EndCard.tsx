import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { C, mono, sans } from "./theme";
import { fadeUp } from "./motion";

const PROVIDERS = ["OpenRouter", "Groq", "Cerebras", "NVIDIA NIM", "Gemini", "SambaNova", "Hugging Face", "Ollama Cloud"];

export const EndCard: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: C.page, alignItems: "center", justifyContent: "center" }}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 26, maxWidth: 980, textAlign: "center" }}>
        <div style={{ fontFamily: mono, fontSize: 15, letterSpacing: 3, textTransform: "uppercase", color: C.dim, ...fadeUp(frame, 0) }}>
          Open source · $0
        </div>
        <div style={{ fontFamily: sans, fontSize: 64, fontWeight: 500, letterSpacing: -2.6, lineHeight: 1.05, color: C.text, ...fadeUp(frame, 6, 18, 14) }}>
          Free inference for any coding agent.
        </div>
        <div style={{ fontFamily: mono, fontSize: 22, color: C.dim, ...fadeUp(frame, 16) }}>
          pip install freeride-gateway · free-ride.xyz
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "center", gap: 10, marginTop: 10 }}>
          {PROVIDERS.map((p, i) => (
            <span
              key={p}
              style={{
                fontFamily: sans,
                fontSize: 16,
                padding: "7px 14px",
                borderRadius: 999,
                border: `1px solid ${C.line}`,
                background: C.card,
                color: C.text,
                ...fadeUp(frame, 24 + i * 3, 12, 6),
              }}
            >
              {p}
            </span>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};
