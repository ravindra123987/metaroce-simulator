# libsoftmetaroce findings

Observed with `rdma_gen` + libsoftmetaroce at metaroce `98686f59`, on the
2p-2x4 emulated fabric (h00 -> h07, both planes, one address per plane).
Findings #2 and #5 were re-checked on metaroce `a04f884` (2026-10-21) in a
minimal network-namespace setup and still hold there.

## 1. SIGSEGV in `Qp::processWatchdogExpiry()` when a QP stalls

When a QP makes no ACK progress for the watchdog interval, LSMR's expiry
path segfaults (exit 139, stack `Qp::processWatchdogExpiry() <-
processWatchdogExpiryAsync()`). It should put the QP and its WQEs into
error. Nothing is logged first, even with `--logbufsecs=0`.

**Correction (2026-10-16).** Most crashes we first attributed to LSMR came
from an emulator bug. Our link-down fault (`ip link set down`) made Linux
drop the interface's IPv6 addresses and their static routes, and link-up
did not restore them. One plane-0 spine path stayed a blackhole (122 of
256 UDP source ports delivered), flows hashed onto it stalled, and the
watchdog fired. The "end-of-run race" explanation was wrong. After the fix
(idempotent setup, `keep_addr_on_down=1`, re-setup on link-up, and a
pre-run delivery check):

| Scenario on a healthy fabric | Runs | Crashed |
|---|---|---|
| no faults | 3 | 0 |
| plane-loss (30% on plane 1, 5 s) | 3 | 0 |
| link-flap | 3 | 0 |
| incast with RED ECN | 1 | 0 |
| incast with drop-tail (42k retransmissions) | 1 | 2 processes |

The segfault is still real, but it takes sustained heavy loss to hit.
`--use_completion_barrier=true` remains on in the runner.

## 2. Repath can pair one plane's source with another plane's destination

`TxFlow` re-steers a flow after `--libsoftmetaroce_rtos_before_repath` RTOs
by calling `Sender::getAlternateSocket()`. That function round-robins over
**every** socket the sender owns, whatever local address it is bound to. So
a flow on the (plane 1 local, plane 1 remote) port pair can move to a
socket bound to the plane-0 address while still sending to the plane-1
remote:

    fd00::2 -> fd00:1:3:1::2   (seen on h00 eth2 during plane-loss)

Spec §7.5 multi-plane wiring has no such port pair. Our fabric routes by
destination, so the packets still arrive, but a fabric doing source-based
plane routing or uRPF would drop them. Re-steering should pick a socket
bound to the same local port (spec §4.8 changes the entropy, not the
port, on the first re-steer).

Reported upstream as [metaroce#3](https://github.com/facebookincubator/metaroce/issues/3).

## 3. Smaller notes

- `rdma_gen` sends exactly `--num_messages`, which defaults to 0, so a
  client without it sends nothing and exits 0.
- glog writes to `/tmp/rdma_gen.*` unless `--log_dir` is set, and it
  buffers, so set `--logbufsecs=0` to keep the lines before a crash.
- LSMR asks for 16 MB socket buffers and warns when
  `net.core.{r,w}mem_max` caps them.
- Send-with-Invalidate (opcode `0xc3`, spec §8.8.4) is not in LSMR's
  opcode enum.

## 4. Incast with ECN, and the limits of RCN at software rates

`scenarios/incast.yaml`: 4 senders to h07, every leaf-to-host port shaped
to 1 Gbit/s, RED ECN (min 30 KB, max 90 KB, p 0.1). The same run with
drop-tail queues is `incast-noecn.yaml`.

| Plane-1 flows at h07 | RED + ECN | drop-tail |
|---|---|---|
| CE-marked data | 10–15% | 0 |
| CNP in ACKs | equals CE count | 0 |
| retransmissions per flow | 0–2 | 440–4,281 (87–1,387 RTO) |

LSMR echoes every CE mark in the ACK CNP field and backs off without
loss. The receiver's RCN rate hint is in whole Gbit/s (spec §8.5.8,
minimum 1), so with `nic_rate_gbps=1` and 4 senders the fair share of
0.25 is clamped to 1. RCN cannot be tested below about 1 Gbit/s per
sender, so the emulator can't exercise it at software speeds; the DES
covers RCN instead.

In both incast runs every rdma_gen process (4 clients and the server) hit
finding #1 at teardown, so throughput has to come from the captures.

## 5. Resolved: `slow_receiver=true` (the default) stalls a QP for up to 2 s

Reported upstream as [metaroce#2](https://github.com/facebookincubator/metaroce/issues/2):
`--libsoftmetaroce_max_rto_us` has no effect while `slow_receiver` is true.

**Symptom.** Under 30% loss on one plane, the whole QP sometimes sent
nothing for 1-2 s: no data on either flow and no ACKs, then a burst of
timeout retransmissions on the lossy flow, and everything resumed.

**Packet evidence** (run 2026-10-17 09:16): flow 1 retransmitted FSN 2687 at
6.613 s and that copy was lost; flow 1 then sent nothing for 2.75 s until
9.367 s, when the same FSN was resent and ACKed 0.2 ms later. Healthy flow 0
kept sending until every outstanding 1 MiB message was waiting on a flow-1
packet; then rdma_gen had nothing new to post and the QP went silent.

**RTO instrumentation** (`images/host/patches/rtodbg.patch`: one log line
per RTO fire and re-arm): `fire flow 1 consec 12`, then `rearm flow 1
rto_us 2000000`. A 2 s RTO, not the 200 ms `max_rto_us`.

**Cause.** `--libsoftmetaroce_slow_receiver` defaults to **true**. It selects
`kSlowReceiverMaxRtoNs` (2 s) in `TxFlow::computeRtoIntervalUs()`, ignoring
`--libsoftmetaroce_max_rto_us`, and seeds the first RTT estimate at 250 ms.
That cap is outside the spec's 1-100 ms range for the RTO upper bound
(§4.5.2). On a lossy plane, retransmissions keep failing, the backoff reaches
the cap, and because every message has packets on that flow, the whole QP
waits.

| plane-loss, 3 runs each | longest whole-QP stall |
|---|---|
| LSMR defaults | 0, 1.4 s, 2.1 s |
| `--libsoftmetaroce_slow_receiver=false` | 0, 0.7 s, 0.6 s |

The emulator runner now passes `slow_receiver=false` by default. The
remaining stalls of up to 0.7 s are the same head-of-line effect at the
200 ms cap. The spec's alternate-path retransmission (§4.8: move a timed-out
flow to another port) would shorten them further; LSMR re-steers within the
same plane, and only across planes through finding #2.

The simulator, with in-order completion, reproduces the stall (0.7–1.9 s on
7 seeds) and removes it on all 7 when a flow moves to another plane after
two RTOs: [experiments/alt-plane-retry.md](../experiments/alt-plane-retry.md).

## 6. Resolved: the 2 s outage at link-up was the emulated fabric (2026-10-17)

Without LSMR, UDP from 64 source ports across a `p0-spine0:eth4` flap stayed
blackholed for ~2.3 s after link-up. Two Linux defaults caused it.
containerlab creates interfaces before our setup runs, so the per-interface
values win over `all`/`default`:

- `accept_dad=1`: re-upped addresses went tentative for duplicate address
  detection;
- `neigh.<if>.retrans_time_ms=1000`: the flushed neighbor was only
  re-solicited after one second.

With per-interface `accept_dad=0`, `dad_transmits=0` and `retrans_time_ms=100`
in the generated setup, traffic is back within 0.3 s of link-up, and the LSMR
link-flap runs show no outage (156 -> 33-40 retransmissions).

This fix did not remove the plane-loss stall (a netem fault never takes a
link down), which pointed the search at LSMR itself and led to #5.
