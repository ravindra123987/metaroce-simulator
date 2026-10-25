# Emulator vs simulator calibration (2026-10-16)

Goal: make the simulator's MetaRoCE model behave like libsoftmetaroce in the
emulator, so simulator results (and MetaRoCE-vs-MRC comparisons) can be
trusted. The emulator runs used here were all on the repaired fabric (see
`docs/findings/lsmr.md` #1), and predate the runner's switch to
`--libsoftmetaroce_slow_receiver=false` (lsmr.md #5) and the link-up fix
(lsmr.md #6).

## The `lsmr` profile

`sim: {profile: lsmr, time_scale: 1.0}` sets what the emulator measured:

| Setting | Value | Evidence |
|---|---|---|
| per-QP rate limit | 2 Gbit/s | LSMR is CPU-bound at about 1.9 Gbit/s per QP, and during plane loss plane 0 alone carries it |
| min / max RTO | 10 ms / 200 ms | LSMR defaults (`--libsoftmetaroce_min_rto_us`, `max_rto_us`) |
| re-steer after | 2 RTOs | `--libsoftmetaroce_rtos_before_repath` default |
| timeout-driven port failover | off | LSMR never logged a failover; traffic left plane 1 because the window shifted |

## Model fix found by calibrating

At first the simulator opened only **one** flow at a paced 2 Gbit/s
(fast start ended on the first ACK, and the activation rule never added
more at that window), so plane-1 loss stalled the whole QP. The spec
requires fast start to "begin a flow on every available port" (§6.1.3).
Fast start now does that, and those per-port flows stay active afterwards.

## Results (single QP h00 -> h07, 2p-2x4)

| Scenario | Metric | Emulator | Simulator |
|---|---|---|---|
| plane-loss | goodput, whole run | 1.74 Gbit/s | 2.00 |
| | goodput during the fault | 1.97 | 2.00 |
| | plane 1 back after clear | ~0.5 s | ~0 s |
| | retransmissions (by timeout) | 160 (119) | 411 (410) |
| link-flap | goodput during the fault | 2.04 | 2.00 |
| | recovery | 6.0 s (2 s outage at link-up) | 0 s |
| | retransmissions | 156 | 3 |
| incast (4 -> 1, ECN) | goodput | 1.84 | 2.00 |
| | CE-marked packets | 220k | 141k |

## What it says

- **Plane shift and recovery now agree:** one flow per plane, plane 0
  takes over within a bin, and plane 1 is used again right after the
  clear. The emulator's lower whole-run goodput is the occasional ~2 s
  stall, caused by LSMR's default 2 s RTO cap (lsmr.md #5). The
  simulator reproduces that kind of stall only with
  `sim.completion: inorder` (see `docs/experiments/alt-plane-retry.md`).
- **Retransmissions: within LSMR's run-to-run spread** *(revised
  2026-10-17)*. The "2.5x" came from comparing against a single emulator
  run. LSMR's `TxFlow::prepareRetxPacketsIfAny` turns out to resend the
  whole window on RTO too, and it skips re-queueing while the previous
  RTO's packets are still queued (now in the model as well). Over several
  runs of plane-loss:

  | | timeout retransmissions | re-steers |
  |---|---|---|
  | emulator, 3 runs | 119, 429, 816 (mean 455) | 55, 69, 226 |
  | simulator, 5 seeds | 272, 321, 324, 365, 423 (mean 341) | 208–303 |

  The ranges overlap. The model re-steers somewhat more often, probably
  because LSMR's RTO backed off to 2 s in these runs (lsmr.md #5).
  Simulator seeds now reach the ports' loss and marking RNGs; before, every
  seed gave identical runs.
- **The link-flap outage does not reproduce in the simulator, and should
  not.** Under the spec's rules the transport has no reason to stall at
  link-up. The emulator's 2 s outage came from the emulated fabric's
  duplicate-address-detection and neighbor-discovery timers, now fixed
  (lsmr.md #6); with the fix, LSMR link-flap runs show no outage.
- **Incast:** throughput agrees; CE marking is within 1.6x.

Reproduce: `sim:` block `{profile: lsmr, time_scale: 1.0}` on the
bundled scenarios (about 25 s of wall time each), then
`metaroce_sim.analysis.compare.write(out, name, {"emu": <emu run>, "sim": <sim run>})`.
