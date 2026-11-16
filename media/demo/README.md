# FreeRide demo video

A [Remotion](https://www.remotion.dev) composition that renders the 25-second launch video
(intro → install and bind → agent request with failover → Claude Code and Codex → end card)
shown in the top-level README. Narration clips live in `public/vo/` and were generated with
`edge-tts` (voice en-US-AndrewMultilingualNeural, rate +22%); `src/vo.json` holds their
durations and sizes each scene so the picture never outruns the voice. Code-driven motion (bezier
camera pan, typed commands, fade-up output), so edits are text edits.

```bash
cd media/demo
npm ci
npx remotion studio                       # live preview
npx remotion render FreeRideDemo out/demo.mp4 --codec=h264 --crf=19 --audio-codec=aac
ffmpeg -y -i out/demo.mp4 \
  -vf "fps=20,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -loop 0 out/demo.gif
cp out/demo.gif ../../docs/assets/freeride-demo.gif
cp out/demo.mp4 ../../docs/assets/freeride-demo.mp4
```

`src/Intro.tsx`, `src/World.tsx` (terminal script and camera move), `src/Agent.tsx` (agent
window and gateway log), `src/Clis.tsx` (Claude Code and Codex side by side) and
`src/EndCard.tsx` are the scenes. Timing constants live in
`src/Demo.tsx`.
