from minilib.textstats import char_freq, word_count


def test_word_count():
    assert word_count("hello world") == 2
    assert word_count("  hello   world  ") == 2
    assert word_count("") == 0


def test_char_freq():
    assert char_freq("aba") == {"a": 2, "b": 1}
