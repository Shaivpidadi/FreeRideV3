import { interpolate } from "remotion";
import { ease } from "./theme";

/** 0→1 between start and start+dur, clamped, eased. */
export const prog = (frame: number, start: number, dur: number) =>
  interpolate(frame, [start, start + dur], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: ease,
  });

/** Fade + rise into a reserved slot. */
export const fadeUp = (frame: number, start: number, dur = 10, rise = 8) => {
  const p = prog(frame, start, dur);
  return { opacity: p, translate: `0px ${(1 - p) * rise}px` };
};

export const CHAR_FRAMES = 0.9; // ~33 chars/s: fast, still readable as typing
/** Typewriter: characters typed since `start`. */
export const typed = (text: string, frame: number, start: number, charFrames = CHAR_FRAMES) =>
  text.slice(0, Math.max(0, Math.min(text.length, Math.floor((frame - start) / charFrames))));
export const typedDone = (text: string, start: number, charFrames = CHAR_FRAMES) =>
  start + Math.ceil(text.length * charFrames);
