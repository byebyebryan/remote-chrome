# H.264 bitrate decision, October 30, 2026

## Decision

Use **`bpf=500000`** for new video sessions, replacing `bpf=250000` in launcher
1.5.2. Apply the launcher through chezmoi on Snap, Starship, and Carbon.
Keep hardware selection and the stock lossless recovery path unchanged.
Existing sessions retain their recorded bitrate, including across reset;
stop and launch a fresh session to adopt the new default.

The trigger was visible fuzziness/artifacts during ordinary remote browsing
after the initial hardware-video rollout. In this sweep, 500k approximately
halved document text-edge error relative to 250k, while retaining a large traffic
reduction from lossless. Moving to 1M roughly doubled traffic again for a much
smaller quality gain. This is a quality/bandwidth choice; the experiment does
not establish that 500k improves interactive smoothness.

`bpf` means **bits per frame**, not bits per second. It is an encoder target,
not a network traffic cap: frame rate, codec behavior, non-video surfaces,
compression and SSH overhead affect the observed rate.

## Results

Arithmetic means of two successful repeats per setting, at 1600 × 1342 content
pixels and DPR 1. Traffic is received Mbit/s on the isolated test SSH socket.
PSNR is in dB (higher is better); edge error is mean absolute RGB error on a
0–255 scale (lower is better).

| Setting | Document traffic | Image traffic | Document PSNR | Image PSNR | Document text-edge error | Image text-edge error |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Stock lossless | 45.30 | 119.85 | Pixel-exact | Pixel-exact | 0.00 | 0.00 |
| H.264 100k | 1.55 | 1.56 | 34.53 | 27.20 | 9.68 | 23.85 |
| H.264 250k (previous default) | 3.14 | 3.72 | 37.06 | 32.65 | 7.24 | 6.49 |
| **H.264 500k (selected)** | **6.26** | **8.15** | **41.12** | **34.18** | **3.80** | **4.48** |
| H.264 1M | 12.37 | 16.08 | 41.45 | 34.73 | 3.56 | 3.98 |

500k reduced received traffic by about **86% for documents and 93% for images**
relative to this lossless baseline. Against 500k, 1M added only 0.33 dB document
PSNR and 0.54 dB image PSNR at roughly twice the traffic. All four lossless
captures were pixel-exact. All 3,375 observed video buffers used hardware
decoding; none fell back to software. No Chrome GPU crashes were recorded in
the ten successful cases.

Browser-host Waypipe CPU averaged about 22–24% of one core for lossless versus
1.9% at 500k. Viewer Waypipe averaged 4.9–6.2% versus 0.64–0.69%. These are
process CPU deltas, not total system or GPU utilization.

![Native-scale document crops comparing 250k, 500k, and 1M](quality-contact.png)

The immutable [interactive report and full image comparisons](http://snap:8787/reports/226a55f1578dc26eede15d0ddf083ea9af0f87efb65c1268d9ae704310fffb26/)
retain both repeats and scenes. That historical report correctly describes
250k as the default at measurement time; the decision above was made afterward.
Its hostname requires access to Snap. The committed data and workload below
remain available independently of the report server.

## Environment and method

- Browser host: **Snap**, NVIDIA RTX A2000 8GB Laptop GPU, Chrome using NVIDIA
  OpenGL through ANGLE. Native Wayland; Chrome's Vulkan feature disabled.
- Viewer: **Starship**, NVIDIA RTX 5070 Ti, Niri, NVIDIA Vulkan video decoding.
  Direct Tailscale connection; no traffic shaping or injected packet loss.
- Launcher at measurement time: **1.5.1**, commit
  `9b4c7351d5e7210443dd5534c5214a5e9faaf42a`, SHA-256
  `641a99f25ab3a5db9b531e26ece9a308a588bbc0f2f7916bcb8dba573001190e`.
- Installed video package: **waypipe-remote-chrome 0.11.3-3**; stock Waypipe
  **0.11.3-1** coinstalled. Chezmoi recipe stamp:
  `7b3d8e2a624edcb5c469014798b14c4f29c13f5328a42520679a0bdb698f3487`.
  The opaque-alpha patch and lossy 4:2:0 conversion were active for video.
- NVIDIA driver reported by Chrome: **610.57.04**. A fresh temporary Chrome
  profile was used for every case. Existing production Chrome stayed open;
  socket and process-tree measurements isolated the test sessions.

Each case displayed the same offline [workload](workload.html): a document with
12px colored text, 13px code, tables and fine borders, followed by a separate
36-image gallery with generated textures, charts and labels. Only the selected
scene was visible; the harness checked scene identity and rendered-image count.
The content viewport was 1600 × 1342 inside a 1600 × 1432 Chrome window.

After a two-second warmup per scene, `runSweep(12000)` scrolled for 12 seconds
at 650 CSS pixels/second, bouncing through a maximum 4800px span. Every mode
used `zstd=1`. Video cases used `h264,hw,bpf=N` with the installed policy
wrapper on both endpoints, debug/validation disabled, and equal video tracing.
The stock lossless recovery case used `waypipe --no-gpu` and Chrome
`--disable-gpu`; it is therefore not a pure codec-only comparison.

Repeat A ran lossless, 250k, 100k, 1M, 500k. Repeat B reversed that order.
The first 1M B capture timed out; a fresh-profile retry ran at the end and is
the included B result. Optional GPU-enabled lossless diagnostics failed twice
(window discovery and page-evaluation timeout) and are excluded from the table.
An earlier workload revision accidentally showed the document in both phases;
none of those earlier results are included here.

The viewer sampled the exact SSH socket's cumulative TCP byte counters before
and after each phase. Receive Mbit/s is `delta_bytes_received * 8 / elapsed /
1e6`, including SSH/application traffic on that socket and excluding unrelated
browser downloads and production traffic. Process CPU uses matching PID and
start-time identities across snapshots. Frame timing comes from the workload's
`requestAnimationFrame` callbacks; trace markers count decoded video buffers.

After scrolling stopped and approximately 800ms elapsed, the harness captured
Chrome's source pixels through CDP and the receiver's Niri window image. The
receiver's 90px toolbar was cropped before comparison. PSNR and mean absolute
error use RGB content pixels. Text-edge error selects edges in DOM-labeled
text/code/table regions. Final-frame scores do not measure quality throughout
motion. `null` PSNR in the lossless data means infinite PSNR (zero error).

## Evidence and interpretation limits

[results.json](results.json) retains the original unrounded per-case/per-scene
metrics, group means/ranges, renderer and limitations. [audit.json](audit.json)
records completed repeats, exact lossless captures and cleanup checks.
The `root` field in the original results identifies the scratch experiment;
it is provenance, not a required path for reading these committed results.
The workload is self-contained and needs no external images or downloads.

In the JSON, `rx_mbps`/`tx_mbps` are Mbit/s, `fps` is browser callbacks/second,
`decoded_buffer_fps` is decoded buffers/second, and `kb_per_browser_frame` is
**kilobits** received per browser callback. CPU fields are percentages of one
core; `rtt_max_ms` is the maximum sampled TCP RTT, while group `retrans_total`
is a sum across repeats. Group quality/rate/timing fields are arithmetic means.
The report bundle contains full source/receiver images and per-repeat views;
the repository retains a smaller representative crop sheet.

Viewer focus changed during the sweep, RTT varied, and requestAnimationFrame
is not physical presentation FPS or input latency. Two short repeats do not
establish statistical significance. The comparison supports the measured
quality/traffic tradeoff, not a definitive smoothness ranking or a claim that
network transport caused every earlier stall. Carbon's Intel decoding was
qualified separately at 250k; Carbon was not part of this bitrate sweep.

If artifacts remain troublesome, compare 500k with 1M on the affected real page
and display scale. If bandwidth or latency becomes limiting, repeat at 250k
and 500k on that path with controlled focus and matched scene/frame pacing.
For exact pixels, use the existing `--transport lossless` recovery option on
a fresh launch. Changing the policy later should retain the old canonical
bitrate in reset parsing and append a new dated result rather than rewriting
this study.
