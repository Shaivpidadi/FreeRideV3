import { Easing } from "remotion";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

export const sans = loadInter("normal", { weights: ["400", "500", "600"], subsets: ["latin"] }).fontFamily;
export const mono = loadMono("normal", { weights: ["400", "600"], subsets: ["latin"] }).fontFamily;

// The easing agent-browser.dev uses for its camera and window moves.
export const ease = Easing.bezier(0.23, 1, 0.32, 1);

export const C = {
  page: "#ffffff",
  card: "#fafafa",
  line: "rgba(0,0,0,0.08)",
  text: "#0a0a0a",
  dim: "#737373",
  faint: "#a3a3a3",
  ok: "#15803d",
  warn: "#b45309",
  accent: "#0a0a0a",
};
