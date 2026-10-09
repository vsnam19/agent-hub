import json

from minilib.cli import main


def test_hidden_stats_json(tmp_path, capsys):
    p = tmp_path / "test.txt"
    p.write_text("a b")
    assert main(["stats", "--json", str(p)]) == 0
    out, _ = capsys.readouterr()
    assert json.loads(out) == {"words": 2, "chars": 3}


def test_hidden_stats_plain(tmp_path, capsys):
    p = tmp_path / "test.txt"
    p.write_text("a b")
    assert main(["stats", str(p)]) == 0
    out, _ = capsys.readouterr()
    assert "words: 2" in out
    assert "chars: 3" in out
