# Scaling to 4 planes: ECMP hash collisions

2026-10-21. Simulator only. Script: [scale_matrix.py](scale_matrix.py), jobs:
[scale_jobs.txt](scale_jobs.txt).

## Setup

- `4p-4x8`: 4 planes × (4 spines, 8 leaves), 2 hosts per leaf. 16 hosts.
  Leaf uplinks have 2:1 headroom.
- `4p-4x8-h4`: the same with 4 hosts per leaf. 32 hosts. 1:1, so a link that
  two flows hash onto has no spare capacity.
- Traffic: every host writes 1 MiB messages to one other host. `shift`
  sends to the host half the fabric away, so all traffic crosses the spines.
  `permutation` is a seeded random derangement, re-drawn per seed. One QP
  per host, 10 Gbit/s links, 6 s at time_scale 0.01, 3 seeds.
- MetaRoCE at γ = 1 (spec default) and γ = 2, and MRC.
- New in the report: `qp_gbps` (per-QP goodput after the first second) and
  `link_util` (every switch egress port).

## Result

Capacity is hosts × 4 planes × 10 Gbit/s. Jain index 1.0 is perfectly fair.
"Idle" leaf uplinks carry less than 5% load; "full" carry more than 95%.

| Fabric | Traffic | Transport | Of capacity | Worst QP (Gbit/s) | Jain | Leaf uplinks idle / full |
|---|---|---|---|---|---|---|
| 2:1 | shift | MetaRoCE γ1 | 78% | 28.4 | 0.997 | 50% / 0% |
| 2:1 | shift | MetaRoCE γ2 | 99% | 39.7 | 1.0 | 50% / 50% |
| 2:1 | shift | MRC | 93% | 37.3 | 1.0 | 0% / 0% |
| 2:1 | permutation | MetaRoCE γ1 | 65% | 19.3 | 0.981 | 57% / 6% |
| 2:1 | permutation | MetaRoCE γ2 | 73% | 16.6 | 0.956 | 57% / 26% |
| 2:1 | permutation | MRC | 93% | 37.0 | 1.0 | 0% / 0% |
| 1:1 | shift | MetaRoCE γ1 | 64% | 18.7 | 0.957 | 16% / 16% |
| 1:1 | shift | MetaRoCE γ2 | 75% | 18.4 | 0.911 | 16% / 64% |
| 1:1 | shift | MRC | 93% | 37.1 | 1.0 | 0% / 62% |
| 1:1 | permutation | MetaRoCE γ1 | 55% | 11.3 | 0.933 | 37% / 17% |
| 1:1 | permutation | MetaRoCE γ2 | 60% | 9.9 | 0.91 | 37% / 31% |
| 1:1 | permutation | MRC | 93% | 36.5 | 1.0 | 0% / 32% |

## Reading it

- **γ = 1 is a cap of its own.** On 2:1 shift, no link carries two flows.
  MetaRoCE γ1 still gets only 78% there, the same ceiling as the 2-plane
  baseline (27 of 40 Gbit/s). So compare collisions using γ2, which reaches
  99% when nothing collides.
- **Collisions cost MetaRoCE 25–40% at 1:1.** MetaRoCE γ2 falls from 99%
  (2:1 shift) to 75% (1:1 shift) and 60% (1:1 permutation). 16–37% of leaf
  uplinks sit idle while others are full, and the worst QP gets 10–18 Gbit/s
  of its 40. Random permutations hurt even at 2:1 (73%, worst QP 16.6),
  where two flows to the same destination leaf can meet on a spine downlink.
- **MRC is flat.** It sprays every packet over all (plane, spine) paths, so
  no uplink is idle. Every QP gets ~37 Gbit/s in every case. Its 93% is the
  same on every fabric and pattern, so it is set by MRC itself, not by
  collisions.
- MetaRoCE's 8 flows per QP do not spread load here, because CC keeps about
  one flow per port active. On 2:1, half the uplinks stay idle simply
  because each leaf sends only 2 flows per plane.

## Caveats

- Shift traffic is identical across seeds (the hash inputs do not change),
  so its three runs agree exactly. Permutation results vary by seed.
- The simulator hashes entropy the same way for every switch. Real switches
  use per-switch seeds and may spread better or worse.
- No emulator comparison: 32 hosts × 4 planes is beyond a single-host containerlab setup.
