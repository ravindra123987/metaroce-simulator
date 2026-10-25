"""Per-flow reliability against the spec's own examples."""

from metaroce_sim.sim.metaroce.flow import Ack, TxFlow


def _flow(sent: int) -> TxFlow:
    f = TxFlow(flow_id=0, entropy=49152, plane=0, fwnd=64)
    for _ in range(sent):
        f.take_fsn(None)
    return f


def _bits(cum: int, bitstr: str) -> frozenset:
    # SelAckBitVector as printed in the spec: rightmost bit is FSN cum+1.
    return frozenset(cum + 1 + i for i, b in enumerate(reversed(bitstr)) if b == "1")


def test_figure_4_8_cursor_sequence():
    """Spec Figure 4.8: FSNs 0-9 in flight; 2, 5, 6 lost."""
    f = _flow(10)
    rows = [
        # (CumFSN, SelAckBitVector, MaxFSN, new data sent, una, cur, nxt, retransmitted)
        (1, "", 1, 2, 2, 2, 12, []),
        (1, "10", 3, 0, 2, 3, 12, [2]),
        (1, "110", 4, 1, 2, 3, 13, []),
        (1, "100110", 7, 1, 2, 6, 14, [5]),
        (1, "1100110", 8, 0, 2, 7, 14, [6]),
        (4, "11100", 9, 2, 5, 7, 16, []),
    ]
    for cum, bits, mx, new, una, cur, nxt, retx in rows:
        _, resend = f.on_ack(Ack(cum_fsn=cum, max_fsn=mx, sacked=_bits(cum, bits)))
        for _ in range(new):
            f.take_fsn(None)
        assert resend == retx, (cum, bits)
        assert (f.snd_una, f.snd_cur, f.snd_nxt) == (una, cur, nxt), (cum, bits)


def test_duplicate_sack_does_not_resend():
    f = _flow(6)
    _, r1 = f.on_ack(Ack(cum_fsn=0, max_fsn=3, sacked=frozenset({2, 3})))
    _, r2 = f.on_ack(Ack(cum_fsn=0, max_fsn=3, sacked=frozenset({2, 3})))
    assert r1 == [1] and r2 == []


def test_sack_mode_never_leaves_recovery_to_rto():
    f = _flow(4)
    _, r = f.on_ack(Ack(cum_fsn=0, max_fsn=3, sacked=frozenset({3})), sack_mode="never")
    assert r == []
    assert f.on_rto() == [1, 2]


def test_rto_resets_cursor_and_backs_off():
    f = _flow(5)
    f.on_ack(Ack(cum_fsn=1, max_fsn=1))
    assert f.on_rto() == [2, 3, 4]
    f.rtt_sample(100_000, 0.25)
    one = f.rto_ns(10_000, 10_000_000)
    f.rtos_without_ack += 1
    assert f.rto_ns(10_000, 10_000_000) == 2 * one
    assert f.rto_ns(10_000, 150_000) == 150_000  # capped by the upper bound


def test_rto_formula_matches_spec():
    f = _flow(0)
    f.rtt_sample(10_000, 0.25)  # first sample: srtt = 10 us, mdev = 5 us
    assert f.rto_ns(1_000, 10_000_000) == 10_000 + 4 * 5_000
    assert f.rto_ns(50_000, 10_000_000) == 50_000  # lower bound
    assert f.rto_ns(1_000, 10_000_000, last_rtt=80_000) == 80_000  # latest RTT is a floor
