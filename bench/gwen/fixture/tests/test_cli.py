from minilib.cli import main


def test_cli_stats(tmp_path, capsys):
    p = tmp_path / "test.txt"
    p.write_text("hello world")
    assert main(["stats", str(p)]) == 0
    out, _ = capsys.readouterr()
    assert "words: 2" in out
    assert "chars: 11" in out


def test_cli_inventory(tmp_path, capsys):
    p = tmp_path / "items.csv"
    p.write_text("name,qty,price\nA,2,1.5\nB,10,5.0\n")
    assert main(["inventory", str(p)]) == 0
    out, _ = capsys.readouterr()
    assert "A 2" in out
    assert "B 10" not in out
