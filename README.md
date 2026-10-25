# metaroce-simulator

An emulator and a discrete-event simulator for
[MetaRoCE](https://github.com/facebookincubator/metaroce), Meta's RDMA
transport for lossy, multi-plane Ethernet fabrics (spec 1.0).

The two engines share one topology format and one scenario format, and
write the same reports, so a scenario can be run against the real
implementation and against the model and compared directly.

- **`emu`** runs Meta's `libsoftmetaroce` and `rdma_gen`, unmodified, in
  containerlab hosts. The hosts sit on a multi-plane Clos of lightweight
  Linux switches with kernel ECMP, `tc` shaping, and RED/ECN. Faults (link
  down, loss, delay) are injected while traffic runs, and every result is
  decoded from packet captures.
- **`sim`** is a pure-Python discrete-event simulator with a MetaRoCE
  endpoint model built from the spec: flows, SACK and the retransmission
  cursor, RTO and re-steering, AIMD, fast start, RCN zones, and port
  failover. It runs at nanosecond resolution, scales past what containers
  can, and includes a small MRC model for side-by-side comparison.

Also included:

- a MetaRoCE packet codec and pcap decoder (all headers, mCRC), validated
  byte for byte against `libsoftmetaroce` captures;
- HTML and JSON reports per run: goodput per plane and per QP,
  retransmissions, ECN marks and CNPs, flow re-steers, recovery time after
  each fault, and switch link utilization in the simulator.

## Install

Requires Python 3.10 or later.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
make check        # lint + unit tests (no containers needed)
```

## Simulator

```sh
# One scenario, written to results-sim/<run>/report.{json,html}
mrsim sim run scenarios/plane-loss.yaml --results results-sim

# MetaRoCE and MRC side by side, with compare.html
mrsim sim compare scenarios/baseline.yaml --results results-sim
```

`--time-scale` sets how many simulated seconds stand for one scenario
second (default 0.02), which trades fidelity for speed. Report times are
always in scenario seconds, so simulator and emulator charts line up.

## Emulator

The emulator needs a Linux host with Docker and
[containerlab](https://containerlab.dev), run as root.

1. Build the switch image and the host image. The host image contains
   `libsoftmetaroce` and `rdma_gen`, built with Meta's getdeps. The first
   build compiles folly and fbthrift and takes a few hours. A failed build
   resumes where it stopped.

   ```sh
   docker build -t metaroce-switch:latest images/switch
   mkdir -p ~/metaroce-build && cp images/host/build-lsmr.sh ~/metaroce-build/
   docker run --rm -v ~/metaroce-build:/w \
     -e METAROCE_SHA=98686f59a2fbf16844cea9beb8e2101b36ecee02 -e JOBS=2 \
     ubuntu:24.04 bash /w/build-lsmr.sh
   docker build -f images/host/Dockerfile -t metaroce-host:latest ~/metaroce-build
   ```

2. Deploy a fabric, check it, run a scenario, and tear it down:

   ```sh
   mrsim emu up topologies/2p-2x4.yaml
   mrsim emu health topologies/2p-2x4.yaml       # UDP delivery on every plane
   mrsim emu ecmp-check topologies/2p-2x4.yaml   # source ports spread over spines
   mrsim emu run scenarios/plane-loss.yaml --results results
   mrsim emu down topologies/2p-2x4.yaml
   ```

`mrsim report <run-dir>...` rebuilds reports from existing run directories.
`mrsim pcap summarize <file.pcap>` prints a per-flow table of any MetaRoCE
capture.

## Topologies

Each plane is an independent leaf-spine fabric, and each host has one NIC
per plane (the spec's multi-plane mode).

```yaml
name: 2p-2x4
planes: 2
spines: 2           # per plane
leaves: 4           # per plane
hosts_per_leaf: 2
link:
  mtu: 4200
```

Bundled: `2p-2x4` (8 hosts), `4p-4x8` (16 hosts, 2:1 uplink headroom) and
`4p-4x8-h4` (32 hosts, 1:1).

## Scenarios

```yaml
name: plane-loss
topology: topologies/2p-2x4.yaml
duration: 15s
traffic:
  - pairs: [{src: h00, dst: h07}]   # or all-to-all, ring, {incast: 7, senders: 4},
                                    #    {shift: 16}, {permutation: <seed>}
    op: write                       # write, write_imm, send_recv, read,
                                    #    atomic_fetch_add, atomic_cmp_swap
    msg_size: 1MiB
    paths: 8                        # flows per QP
    qps: 1
transport:                          # passed to libsoftmetaroce
  cc: static:4
  max_rto_us: 20000
fabric:
  shape: {ports: downlinks, rate: 1gbit}          # downlinks, uplinks, spines, all
  ecn: {min_kb: 30, max_kb: 90, prob: 0.1, limit_kb: 400}
faults:
  - {at: 5s, action: netem, target: {plane: 1}, spec: "loss 30%"}
  - {at: 10s, action: clear, target: {plane: 1}}
sim:                                # simulator-only settings
  profile: lsmr                     # timers and rate measured from libsoftmetaroce
  completion: inorder               # free a WQE slot only after earlier messages complete
  seed: 1
```

Fault actions are `link-down`, `link-up`, `netem` and `clear`. Targets are
a link (`{link: "p0-spine0:eth4"}`), a node, or a whole plane.

## Results and findings

- [docs/findings/lsmr.md](docs/findings/lsmr.md): behavior of
  `libsoftmetaroce` observed in the emulator, including two issues
  reported upstream
  ([#2](https://github.com/facebookincubator/metaroce/issues/2),
  [#3](https://github.com/facebookincubator/metaroce/issues/3)).
- [docs/calibration.md](docs/calibration.md): how the simulator's `lsmr`
  profile was matched to emulator runs.
- [docs/experiments/alt-plane-retry.md](docs/experiments/alt-plane-retry.md):
  how much of a plane-loss stall is removed by retrying on another plane.
- [docs/experiments/scale-4p.md](docs/experiments/scale-4p.md): the cost of
  ECMP hash collisions on a 4-plane fabric, MetaRoCE against MRC.

## Layout

```
metaroce_sim/
  codec/      MetaRoCE headers, mCRC, pcap reader
  topo/       topology model and containerlab generator
  emu/        lab control, faults, QoS, scenario runner
  sim/        discrete-event engine, fabric, MetaRoCE and MRC endpoints
  analysis/   timelines, reports, comparisons
images/       switch and host container images
scenarios/    bundled scenarios
topologies/   bundled topologies
tests/        unit and fixture tests; tests/lab needs a deployed lab
docs/         findings, calibration, experiments
```

## License

Apache-2.0. See [LICENSE](LICENSE).
