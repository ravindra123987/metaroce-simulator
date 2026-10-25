"""Congestion control against spec §6 and §7.4."""

import itertools

from metaroce_sim.sim.metaroce.cc import QpCC
from metaroce_sim.sim.metaroce.config import Config
from metaroce_sim.sim.metaroce.flow import TxFlow


def make(n=8, **kw):
    cfg = Config(**kw)
    flows = [TxFlow(flow_id=i, entropy=0, plane=0) for i in range(n)]
    counter = itertools.count(49152)
    return QpCC(cfg, flows, lambda: next(counter)), flows


def invariant(cc):
    return sum(f.fwnd for f in cc.flows if f.active) == max(cc.cfg.qwnd_min, int(cc.qwnd))


def test_additive_increase_about_epsilon_per_window():
    cc, flows = make()
    flows[0].active, flows[0].fwnd, cc.qwnd = True, 10, 10.0
    for _ in range(10):  # one full window acked, all window limited, no CE
        cc.on_ack(flows[0], acked=1, ce=0, lost=0, window_limited=True)
    assert 10.9 < cc.qwnd < 11.1
    assert invariant(cc)


def test_ce_cuts_beta_per_marked_packet_from_marked_flow():
    cc, flows = make()
    for f in flows[:2]:
        f.active, f.fwnd = True, 10
    cc.qwnd = 20.0
    cc.on_ack(flows[1], acked=4, ce=4, lost=0, window_limited=False)
    assert cc.qwnd == 18.0
    assert (flows[0].fwnd, flows[1].fwnd) == (10, 8)  # load shifts off the marked flow
    assert invariant(cc)


def test_every_packet_marked_halves_per_rtt():
    cc, flows = make()
    flows[0].active, flows[0].fwnd, cc.qwnd = True, 16, 16.0
    cc.on_ack(flows[0], acked=16, ce=16, lost=0, window_limited=False)
    assert cc.qwnd == 8.0


def test_loss_cuts_lambda_per_packet():
    cc, flows = make()
    flows[0].active, flows[0].fwnd, cc.qwnd = True, 16, 16.0
    cc.on_ack(flows[0], acked=0, ce=0, lost=3, window_limited=False)
    assert cc.qwnd == 13.0


def test_flow_activation_rule_example():
    """§7.4.2: QWND 50 with average_window 12 needs at least four flows."""
    cc, flows = make(average_window=12, epsilon=1.0)
    flows[0].active, flows[0].fwnd, cc.qwnd = True, 1, 1.0
    while cc.qwnd < 50:  # grow through clean, window-limited ACKs
        f = max(cc.active(), key=lambda f: f.fwnd)
        cc.on_ack(f, acked=1, ce=0, lost=0, window_limited=True)
    assert len(cc.active()) == 4  # 12 * (4 + 1) = 60 > 50 stops at four
    assert all(f.fwnd >= cc.cfg.fast_start_burst for f in cc.active()[:3])  # 4th still bootstrapping
    assert invariant(cc)


def test_new_flow_gets_new_entropy_and_bootstrap_burst():
    cc, flows = make(average_window=4, fast_start_burst=4)
    flows[0].active, flows[0].fwnd, cc.qwnd = True, 9, 9.0
    cc._rebalance(None, None)  # 4 * 2 = 8 < 9 -> claim flow 1
    assert flows[1].active and flows[1].entropy != 0
    cc.qwnd = 13.0
    cc._rebalance(grow=flows[0], shrink=None)
    assert flows[1].fwnd == 4  # increments credited to the chosen flow first


def test_fast_start_grows_as_it_sends_and_spreads_flows():
    cc, _flows = make(fast_start_burst=4)
    cc.start_fast_start(now=0)
    used = [cc.fast_start_flow_for_send().flow_id for _ in range(10)]
    assert used == [0, 0, 0, 0, 1, 1, 1, 1, 2, 2]
    assert cc.qwnd == 10
    cc.maybe_exit_fast_start(now=1, ack_ce=False, pending=True, est_rtt=1e9)
    assert not cc.fast_start


def test_rcn_cap_and_zones():
    cc, flows = make(mtu=4096)
    cc.on_rate_hint(gbps=10, rtt_ns=32_768)  # 10 Gbit/s * 32.768 us = 40 KiB = 10 packets
    assert abs(cc.qwnd_max - 10.0) < 1e-9
    flows[0].active, flows[0].fwnd, cc.qwnd = True, 30, 30.0  # > 2x cap: waiting zone
    cc.on_ack(flows[0], acked=4, ce=0, lost=0, window_limited=True)
    assert cc.qwnd == 26.0  # -1 per acked packet
    cc.qwnd = 15.0  # between 1x and 2x: rate halving
    cc.on_ack(flows[0], acked=4, ce=0, lost=0, window_limited=True)
    assert cc.qwnd == 13.0  # -1/2 per acked packet


def test_clean_acks_move_window_back_to_a_starved_flow():
    cc, flows = make()
    for f, w in zip(flows[:2], (20, 2)):
        f.active, f.fwnd = True, w
    cc.qwnd = cc.qwnd_max = 22.0  # at the RCN cap: no growth possible
    for _ in range(30):
        cc.on_ack(flows[1], acked=1, ce=0, lost=0, window_limited=True)
    assert abs(flows[0].fwnd - flows[1].fwnd) <= 1
    assert invariant(cc)
