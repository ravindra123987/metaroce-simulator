"""containerlab lab control and fabric checks (run on the containerlab host)."""

from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path

from metaroce_sim.topo import gen
from metaroce_sim.topo.model import Topology


def container(t: Topology, node: str) -> str:
    return f"clab-metaroce-{t.name}-{node}"


def sh(cmd: list[str], *, check: bool = True, capture: bool = True) -> str:
    r = subprocess.run(cmd, check=check, capture_output=capture, text=True)
    return r.stdout if capture else ""


def dexec(t: Topology, node: str, script: str, *, check: bool = True) -> str:
    return sh(["sudo", "docker", "exec", container(t, node), "sh", "-c", script], check=check)


def up(t: Topology, workdir: Path) -> Path:
    clab_file = gen.write(t, workdir)
    sh(["sudo", "containerlab", "deploy", "-t", str(clab_file), "--reconfigure"], capture=False)
    return clab_file


def down(t: Topology, workdir: Path) -> None:
    clab_file = workdir / f"{t.name}.clab.yaml"
    sh(["sudo", "containerlab", "destroy", "-t", str(clab_file), "--cleanup"], capture=False)


# ------------------------------------------------------------------ ECMP check

SPRAY = """
import socket, sys
dst, first, count, reps = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
for port in range(first, first + count):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.bind(("::", port))
    for _ in range(reps):
        s.sendto(b"x" * 64, (dst, 2766))
    s.close()
"""


def _rx_packets(t: Topology, node: str, iface: str) -> int:
    return int(dexec(t, node, f"cat /sys/class/net/{iface}/statistics/rx_packets").strip())


def chi2_pvalue(counts: list[int]) -> float:
    """p-value that `counts` come from a uniform distribution (chi-square test)."""
    n, k = sum(counts), len(counts)
    if k < 2 or n == 0:
        return 1.0
    exp = n / k
    x = sum((c - exp) ** 2 / exp for c in counts)
    return _chi2_sf(x, k - 1)


def _chi2_sf(x: float, df: int) -> float:
    """Upper regularized incomplete gamma Q(df/2, x/2).

    Series for z < a + 1, Lentz continued fraction otherwise (Numerical
    Recipes `gammq`).
    """
    a, z = df / 2.0, x / 2.0
    if z <= 0:
        return 1.0
    log_pre = -z + a * math.log(z) - math.lgamma(a)
    if z < a + 1:
        term = total = 1.0 / a
        for i in range(1, 1000):
            term *= z / (a + i)
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return max(0.0, 1.0 - total * math.exp(log_pre))
    tiny = 1e-300
    b = z + 1.0 - a
    c, d = 1.0 / tiny, 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return min(1.0, math.exp(log_pre) * h)


def ecmp_check(
    t: Topology, src: int, dst: int, plane: int, ports: int = 256, reps: int = 4,
    first_port: int = 49152,
) -> dict:
    """Send UDP from `ports` source ports and count packets per spine."""
    src_leaf, _ = t.host_leaf(src)
    dst_leaf, _ = t.host_leaf(dst)
    if src_leaf == dst_leaf:
        raise ValueError("src and dst hosts sit on the same leaf; pick hosts on different leaves")
    spines = [t.spine(plane, s) for s in range(t.spines)]
    iface = t.spine_port(src_leaf)
    before = [_rx_packets(t, s, iface) for s in spines]
    dexec(t, t.host(src),
          f"python3 -c '{SPRAY}' {t.host_addr(dst, plane)} {first_port} {ports} {reps}")
    after = [_rx_packets(t, s, iface) for s in spines]
    counts = [a - b for a, b in zip(after, before)]
    return {
        "plane": plane, "src": t.host(src), "dst": t.host(dst),
        "sent": ports * reps, "per_spine": dict(zip(spines, counts)),
        "p_value": chi2_pvalue(counts),
    }


# ------------------------------------------------------------- delivery check

RECV = """
import socket, sys, time
s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
s.bind((sys.argv[1], int(sys.argv[2])))
s.settimeout(float(sys.argv[3]))
n = 0
try:
    while True:
        s.recv(64)
        n += 1
except OSError:
    pass
print(n)
"""

SEND1 = """
import socket, sys
dst, port, first, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
for p in range(first, first + count):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.bind(("::", p))
    s.sendto(b"x", (dst, port))
    s.close()
"""


def delivery(t: Topology, src: int, dst: int, plane: int, ports: int = 256,
             port: int = 9999) -> int:
    """Packets received at dst out of `ports`, one per UDP source port."""
    addr = t.host_addr(dst, plane)
    rx = subprocess.Popen(
        ["sudo", "docker", "exec", container(t, t.host(dst)), "python3", "-c", RECV,
         addr, str(port), "1.5"], stdout=subprocess.PIPE, text=True)
    time.sleep(0.4)
    dexec(t, t.host(src), f"python3 -c '{SEND1}' {addr} {port} 50000 {ports}")
    out, _ = rx.communicate(timeout=30)
    return int(out.strip() or 0)


def health(t: Topology, pairs, ports: int = 256) -> list[dict]:
    """Delivery for every (src, dst) both ways on every plane. Empty list = healthy."""
    bad = []
    for a, b in sorted({tuple(sorted(p)) for p in pairs}):
        for p in range(t.planes):
            for s_, d_ in ((a, b), (b, a)):
                # Warm up neighbor discovery: Linux queues only a few packets per
                # unresolved neighbor and drops the rest, which is not a fault.
                delivery(t, s_, d_, p, 4)
                got = delivery(t, s_, d_, p, ports)
                if got < ports:
                    bad.append({"src": t.host(s_), "dst": t.host(d_), "plane": p,
                                "received": got, "sent": ports})
    return bad
