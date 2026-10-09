from collections import Counter


def word_count(text: str) -> int:
    return len(text.split())


def char_freq(text: str) -> dict[str, int]:
    return dict(Counter(text))
