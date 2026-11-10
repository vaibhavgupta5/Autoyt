import random

import pytest


def make_words(n_sentences=60, words_per=14, wps=2.6, start=0.0):
    """Synthetic transcript: steady speech, one sentence per `words_per` words."""
    words, t, k = [], start, 0
    step = 1 / wps
    for si in range(n_sentences):
        for j in range(words_per):
            w = f"word{k}" + ("." if j == words_per - 1 else "")
            words.append({"w": w, "s": round(t, 3), "e": round(t + step * 0.8, 3)})
            t += step
            k += 1
    return words


@pytest.fixture
def words():
    return make_words()


@pytest.fixture
def rng():
    return random.Random(7)
