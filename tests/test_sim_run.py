"""End-to-end simulator runs of the bundled scenarios (small time scale)."""

import json
from pathlib import Path

import yaml

from metaroce_sim import scenario
from metaroce_sim.sim import run as simrun
from metaroce_sim.topo import model

T = model.load("topologies/2p-2x4.yaml")


def _run(tmp_path, name, scale=0.003):
    raw = yaml.safe_load(Path(f"scenarios/{name}.yaml").read_text())
    raw.setdefault("sim", {})["time_scale"] = scale
    out = simrun.run(T, scenario.parse(raw, T.num_hosts), tmp_path)
    return json.loads((out / "report.json").read_text())


def _mean(series, t0, t1, bin_s=0.1):
    v = series[int(t0 / bin_s):int(t1 / bin_s)]
    return sum(v) / len(v)


def test_baseline_uses_both_planes_without_loss(tmp_path):
    d = _run(tmp_path, "baseline")
    g = d["goodput_gbps"]
    assert _mean(g["plane 0"], 2, 12) > 1 and _mean(g["plane 1"], 2, 12) > 1
    assert d["sim"]["msgs_done"] > 0
    assert sum(f["retx"] for f in d["flows"].values()) < 50
    qps = d["qp_gbps"]
    assert len(qps) == 2 and abs(sum(qps.values()) - _mean(g["total"], 1, 15)) < 0.5


def test_plane_loss_moves_traffic_and_plane_comes_back(tmp_path):
    d = _run(tmp_path, "plane-loss")
    g = d["goodput_gbps"]
    before, during = _mean(g["plane 1"], 2, 5), _mean(g["plane 1"], 6, 10)
    assert during < 0.2 * before                      # plane 1 abandoned under 30% loss
    assert _mean(g["plane 0"], 6, 10) > _mean(g["plane 0"], 2, 5)  # plane 0 takes over
    assert _mean(g["plane 1"], 13, 15) > 0.5 * before  # and plane 1 is used again
    assert any(len(f["sports"]) > 1 for f in d["flows"].values())  # timeouts re-steered


def test_incast_with_ecn_marks_and_fills_shaped_downlinks(tmp_path):
    d = _run(tmp_path, "incast")
    g = d["goodput_gbps"]
    assert sum(sum(v) for v in d["ce_per_s"].values()) > 0
    assert _mean(g["total"], 2, 12) > 1.6  # 2 x 1 Gbit/s shaped downlinks into h07


def test_mrc_sprays_all_evs_and_compare_writes_page(tmp_path):
    from metaroce_sim.analysis import compare

    raw = yaml.safe_load(Path("scenarios/baseline.yaml").read_text())
    raw.setdefault("sim", {})["time_scale"] = 0.003
    sc = scenario.parse(raw, T.num_hosts)
    dirs = {tp: simrun.run(T, sc, tmp_path, run_id=tp, transport=tp) for tp in ("metaroce", "mrc")}
    mrc = json.loads((dirs["mrc"] / "report.json").read_text())
    used = [f for f in mrc["flows"].values() if f["pkts"] > 0]
    assert len(used) == 2 * T.planes * T.spines  # 2 QPs x every (plane, spine) EV
    html = compare.write(tmp_path / "cmp", sc.name, dirs)
    assert "MetaRoCE" in html.read_text() and "MRC" in html.read_text()
    m = json.loads((tmp_path / "cmp" / "compare.json").read_text())
    assert m["mrc"]["goodput_gbps"] > 0 and m["metaroce"]["goodput_gbps"] > 0


def test_lsmr_profile_sets_measured_parameters():
    raw = yaml.safe_load(Path("scenarios/baseline.yaml").read_text())
    raw["sim"] = {"profile": "lsmr"}
    cfg = simrun.build_config(scenario.parse(raw, T.num_hosts))
    assert (cfg.min_rto_ns, cfg.max_rto_ns, cfg.max_gbps) == (10_000_000, 200_000_000, 2.0)
    assert cfg.rtos_before_port_failover == 0


def test_inorder_completion_holds_slots_behind_an_open_message():
    from metaroce_sim.sim.metaroce.endpoint import SenderQp

    q = SenderQp.__new__(SenderQp)
    q.running, q.max_msgs, q.next_posn, q.next_msn = True, 4, 0, 10
    q.open_msgs = {6: [0, 0]}          # msn 6 still missing a packet; 7..9 done
    q.inorder = False
    assert q._has_new_data()           # any-order: one slot in use
    q.inorder = True
    assert not q._has_new_data()       # in-order: 6..9 all wait on msn 6
    q.open_msgs = {7: [0, 0]}
    assert q._has_new_data()
