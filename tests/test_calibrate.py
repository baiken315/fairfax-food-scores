from datetime import date

from ffxfood.calibrate import auc, calibrate, spearman
from ffxfood.db import connect
from test_scoring import _seed


def test_auc_and_spearman():
    assert auc([1, 2, 3, 4], [False, False, True, True]) == 1.0
    assert auc([4, 3, 2, 1], [False, False, True, True]) == 0.0
    assert auc([1, 1, 1, 1], [False, True, False, True]) == 0.5        # ties count half
    assert auc([1, 2], [True, True]) is None
    assert abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1.0) < 1e-12
    assert abs(spearman([1, 2, 3, 4], [4, 3, 2, 1]) + 1.0) < 1e-12


def test_persistent_offenders_are_predicted(tmp_path):
    con = connect(tmp_path / "t.sqlite")
    for i in range(20):      # clean places stay clean, dirty places stay dirty
        _seed(con, f"C{i}", "Fast Food Restaurant", [("2025-03-01", []), ("2026-03-01", [])])
        _seed(con, f"D{i}", "Fast Food Restaurant", [("2025-03-01", [(8, "priority")]), ("2026-03-01", [(15, "priority")])])
    con.commit()
    rep = calibrate(con, prior_strengths=(2.0,))
    assert rep["n_pairs"] == 40
    for name, res in rep["sets"].items():
        assert res["next_priority_rf@ps=2"][0] == 1.0, name
