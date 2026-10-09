import json

from minilib.cli import main
from minilib.report import summarize


def test_hidden_summarize_empty():
    assert summarize([]) == {"count": 0, "total_qty": 0, "total_value": 0.0, "low_stock": []}


def test_hidden_cli_report(tmp_path, capsys):
    p = tmp_path / "items.csv"
    p.write_text("name,qty,price\nZ,1,1.111\nY,1,1.111\n")
    assert main(["report", str(p)]) == 0
    out, _ = capsys.readouterr()
    res = json.loads(out)
    assert res == {"count": 2, "total_qty": 2, "total_value": 2.22, "low_stock": ["Y", "Z"]}
