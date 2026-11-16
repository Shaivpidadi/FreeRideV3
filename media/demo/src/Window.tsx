import React from "react";
import { C, mono } from "./theme";

export const Window: React.FC<{
  title: string;
  width: number;
  style?: React.CSSProperties;
  children: React.ReactNode;
}> = ({ title, width, style, children }) => (
  <div
    style={{
      width,
      borderRadius: 14,
      border: `1px solid ${C.line}`,
      background: C.card,
      boxShadow: "0 30px 60px rgba(0,0,0,0.08), 0 2px 6px rgba(0,0,0,0.04)",
      overflow: "hidden",
      ...style,
    }}
  >
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "12px 16px",
        borderBottom: `1px solid ${C.line}`,
      }}
    >
      {[0.15, 0.1, 0.05].map((a, i) => (
        <span key={i} style={{ width: 10, height: 10, borderRadius: 5, background: `rgba(0,0,0,${a})` }} />
      ))}
      <span
        style={{
          marginLeft: 10,
          fontFamily: mono,
          fontSize: 12,
          letterSpacing: 2,
          textTransform: "uppercase",
          color: C.dim,
        }}
      >
        {title}
      </span>
    </div>
    {children}
  </div>
);
