# FreeRide demo video

A [Remotion](https://www.remotion.dev) composition that renders the install → bind → agent
request → failover sequence shown in the top-level README. Code-driven motion (bezier
camera pan, typed commands, fade-up output), so edits are text edits.

```bash
cd media/demo
npm ci
npx remotion studio                       # live preview
npx remotion render FreeRideDemo out/demo.mp4 --codec=h264 --crf=20
ffmpeg -y -i out/demo.mp4 \
  -vf "fps=20,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -loop 0 out/demo.gif
cp out/demo.gif ../../docs/assets/freeride-demo.gif
cp out/demo.mp4 ../../docs/assets/freeride-demo.mp4
```

`src/World.tsx` holds the terminal script and the camera move; `src/Agent.tsx` the agent
window and gateway log; `src/EndCard.tsx` the closing card. Timing constants live in
`src/Demo.tsx`.
