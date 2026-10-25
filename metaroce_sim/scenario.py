"""Scenario files: traffic, transport settings, fabric settings, timed faults.

Shared by the emulator (`mrsim emu run`) and the simulator (`mrsim sim run`).
The format is described in README.md.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

OPS = {"write", "write_imm", "send_recv", "read", "atomic_fetch_add", "atomic_cmp_swap"}
FAULT_ACTIONS = {"link-down", "link-up", "netem", "clear"}

_DUR = re.compile(r"^\s*([0-9.]+)\s*(ns|us|ms|s)?\s*$")
_SIZE = re.compile(r"^\s*([0-9.]+)\s*(B|KiB|MiB|GiB|KB|MB|GB)?\s*$")
_DUR_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, None: 1.0}
_SIZE_UNITS = {None: 1, "B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30,
               "KB": 10**3, "MB": 10**6, "GB": 10**9}


class ScenarioError(ValueError):
    pass


def seconds(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ScenarioError(f"bad duration {v!r}")
    return float(m.group(1)) * _DUR_UNITS[m.group(2)]


def size_bytes(v) -> int:
    if isinstance(v, int):
        return v
    m = _SIZE.match(str(v))
    if not m:
        raise ScenarioError(f"bad size {v!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2)])


@dataclass
class Traffic:
    pairs: list[tuple[int, int]]
    op: str = "write"
    msg_size: int = 1 << 20
    tags: int = 1
    paths: int = 4
    qps: int = 1


@dataclass
class Fault:
    at: float
    action: str
    target: dict
    spec: str = ""


@dataclass
class Scenario:
    name: str
    topology: str
    duration: float
    traffic: list[Traffic]
    faults: list[Fault] = field(default_factory=list)
    transport: dict = field(default_factory=dict)
    fabric: dict = field(default_factory=dict)
    capture: dict = field(default_factory=lambda: {"snaplen": 128})
    sim: dict = field(default_factory=dict)  # simulator-only knobs (metaroce_sim.sim.run)


def _pairs(spec, num_hosts: int) -> list[tuple[int, int]]:
    if spec == "all-to-all":
        return [(a, b) for a in range(num_hosts) for b in range(num_hosts) if a != b]
    if spec == "ring":
        return [(a, (a + 1) % num_hosts) for a in range(num_hosts)]
    if isinstance(spec, dict) and "shift" in spec:  # every host sends to host + k
        k = int(spec["shift"]) % num_hosts
        if not k:
            raise ScenarioError("shift must not be a multiple of the host count")
        return [(a, (a + k) % num_hosts) for a in range(num_hosts)]
    if isinstance(spec, dict) and "permutation" in spec:  # seeded random derangement
        rng = random.Random(spec["permutation"])
        while True:
            dst = list(range(num_hosts))
            rng.shuffle(dst)
            if all(a != b for a, b in enumerate(dst)):
                return list(enumerate(dst))
    if isinstance(spec, dict) and "incast" in spec:
        dst, n = spec["incast"], spec.get("senders", num_hosts - 1)
        srcs = [h for h in range(num_hosts) if h != dst][:n]
        return [(s, dst) for s in srcs]
    if isinstance(spec, list):
        out = []
        for p in spec:
            a, b = (p["src"], p["dst"]) if isinstance(p, dict) else p
            out.append((_host(a), _host(b)))
        return out
    raise ScenarioError(f"bad pairs {spec!r}")


def _host(v) -> int:
    if isinstance(v, int):
        return v
    m = re.fullmatch(r"h(\d+)", str(v))
    if not m:
        raise ScenarioError(f"bad host {v!r} (use 3 or 'h03')")
    return int(m.group(1))


def parse(d: dict, num_hosts: int) -> Scenario:
    try:
        traffic = []
        for t in d["traffic"]:
            op = t.get("op", "write")
            if op not in OPS:
                raise ScenarioError(f"op must be one of {sorted(OPS)}, got {op!r}")
            pairs = _pairs(t.get("pairs", "ring"), num_hosts)
            for a, b in pairs:
                if not (0 <= a < num_hosts and 0 <= b < num_hosts) or a == b:
                    raise ScenarioError(f"pair {a}->{b} invalid for {num_hosts} hosts")
            traffic.append(Traffic(
                pairs=pairs, op=op, msg_size=size_bytes(t.get("msg_size", "1MiB")),
                tags=int(t.get("tags", 1)), paths=int(t.get("paths", 4)),
                qps=int(t.get("qps", 1)),
            ))
        faults = []
        for f in d.get("faults") or []:
            if f["action"] not in FAULT_ACTIONS:
                raise ScenarioError(f"fault action must be one of {sorted(FAULT_ACTIONS)}")
            if f["action"] == "netem" and not f.get("spec"):
                raise ScenarioError("netem fault needs a spec, e.g. 'loss 30%'")
            faults.append(Fault(seconds(f["at"]), f["action"], dict(f["target"]),
                                f.get("spec", "")))
        sc = Scenario(
            name=d["name"], topology=d["topology"], duration=seconds(d.get("duration", "20s")),
            traffic=traffic, faults=sorted(faults, key=lambda f: f.at),
            transport=d.get("transport") or {}, fabric=d.get("fabric") or {},
            capture={"snaplen": 128, **(d.get("capture") or {})},
            sim=d.get("sim") or {},
        )
    except KeyError as e:
        raise ScenarioError(f"missing key {e}") from None
    for f in sc.faults:
        if f.at >= sc.duration:
            raise ScenarioError(f"fault at {f.at}s is after the {sc.duration}s run")
    return sc


def load(path: str | Path, num_hosts: int) -> Scenario:
    return parse(yaml.safe_load(Path(path).read_text()), num_hosts)
