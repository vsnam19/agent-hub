from minilib.textstats import top_words


def test_top_words_hidden():
    assert top_words("a b c a", 3) == [("a", 2), ("b", 1), ("c", 1)]
    assert top_words("Hello, hello! What's up?", 10) == [("hello", 2), ("up", 1), ("what's", 1)]
    assert top_words("A a B b", 2) == [("a", 2), ("b", 2)]
    assert top_words("x y", 0) == []
    assert top_words("x y", -1) == []
    assert top_words("a", 5) == [("a", 1)]
