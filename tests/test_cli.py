from metaroce_sim.cli import main


def test_cli_runs_without_args():
    assert main([]) == 0
