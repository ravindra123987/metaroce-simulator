# Retry on another plane: how much of the plane-loss stall does it remove?

2026-10-21. Simulator only. Script: [alt_plane_retry.py](alt_plane_retry.py).

## Question

With `slow_receiver=false`, LSMR still stalls a whole QP for up to 0.7 s in
the emulator's plane-loss scenario (30% loss on plane 1 for 5 s;
[findings #5](../findings/lsmr.md)). LSMR re-steers a timed-out flow to new
entropy on the *same* plane. The spec also allows moving it to another port
(§4.8, §7.5.4). How much of the stall does that remove?

## First, the simulator had to reproduce the stall

The `lsmr` profile showed no stall on any of 5 seeds. The simulator
completed messages in any order: one message stuck behind a lost packet held
one of the 8 WQE slots, and the other 7 kept the link busy. RDMA completes
work requests in order, so in LSMR a stuck message holds its slot *and*
every slot posted after it. The sender runs out of WQEs and stops.

`sim.completion: inorder` models that: a slot is freed only when every
earlier message is done. With it, the simulator reproduces the stall. It is
off by default so earlier results stay comparable.

## Result

`lsmr` profile, `completion: inorder`, plane-loss, time_scale 1.0.

| Variant | Seeds | Longest whole-QP stall | Goodput during fault (cap 2.0 Gbit/s) |
|---|---|---|---|
| Any-order completion (old model) | 5 | 0 on all | 2.0 |
| In order, re-steer within the plane only (LSMR) | 7 | 0.7–1.9 s (median 1.4) | 0.40–1.03 (mean 0.73) |
| In order, 32 WQEs instead of 8 | 3 | 0.8–1.4 s | 1.19–1.39 |
| In order, **move to another plane after 2 RTOs** | 7 | **0 on all** | 2.0 |
| In order, move to another plane after 1 RTO | 3 | 0 on all | 1.99–2.0 |
| *Emulator, LSMR, `slow_receiver=false`* | 3 | *0, 0.7 s, 0.6 s* | — |

Retrying on the other plane removes the stall completely in the simulator.
The flow leaves the lossy plane on its second timeout, after 10 + 20 ms
instead of backing off to the 200 ms cap. The no-fault and link-flap
scenarios are unchanged by it (same goodput, 0 stalls; link-flap logs 12
RTOs instead of 3).

The simulator is more pessimistic than the emulator: 0.7–1.9 s against
0–0.7 s. Treat the absolute numbers as an upper bound. The finding is the
direction: the remaining stall is head-of-line blocking on a flow stuck
re-sending over the lossy plane, and leaving the plane removes it.

## Caveats

- In the simulator a flow that fails over comes back after 20 ms
  (`port_restore_ns`) and may fail over again while the plane is still bad.
  LSMR has no port failover to compare against.
- Loss is 30% on every host-facing port of one plane. A loss pattern that
  hits both planes would not be helped by this.
