from pathlib import Path

import yaml

from metaroce_sim import scenario
from metaroce_sim.emu import runner
from metaroce_sim.topo import model

T = model.load("topologies/2p-2x4.yaml")


def test_bundled_scenarios_parse():
    for path in sorted(Path("scenarios").glob("*.yaml")):
        topo = yaml.safe_load(path.read_text())["topology"]
        scenario.load(path, model.load(topo).num_hosts)


def test_rdma_gen_commands_use_one_gid_per_plane():
    sc = scenario.load("scenarios/plane-loss.yaml", T.num_hosts)
    tr = sc.traffic[0]
    srv = runner.server_cmd(T, sc, 7, 1, tr, 18515)
    cli = runner.client_cmd(T, sc, 0, 7, tr, 18515)
    assert "--gid_addresses=fd00:0:3:1::2,fd00:1:3:1::2" in srv
    assert "--gid_addresses=fd00:0:0:0::2,fd00:1:0:0::2" in cli
    assert "--rc_servers=fd00:0:3:1::2#1" in cli and "--libsoftmetaroce_txflows=8" in cli
    assert "--verbs_mode=libsoftmetaroce" in cli and "--num_messages=1000000000" in cli


def test_transport_block_maps_to_lsmr_flags():
    raw = scenario.yaml.safe_load(Path("scenarios/plane-loss.yaml").read_text())
    raw["transport"] = {"cc": "static:4", "min_rto_us": 2000, "flags": ["--foo=1"]}
    sc = scenario.parse(raw, T.num_hosts)
    cli = runner.client_cmd(T, sc, 0, 7, sc.traffic[0], 18515)
    assert "--libsoftmetaroce_cc_algorithm=static:4" in cli
    assert "--libsoftmetaroce_min_rto_us=2000" in cli and cli.endswith("--foo=1")
