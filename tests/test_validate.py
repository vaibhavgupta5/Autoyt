from autoyt.config import DEFAULTS
from autoyt.sentences import build_sentences
from autoyt.validate import build_clip, fit_window, validate_and_pick

CFG = DEFAULTS["clip"]
BANNED = DEFAULTS["banned_words"]


def cand(s, e, cat="funny", heading="He said the quiet part", **kw):
    d = {"start_sentence": s, "end_sentence": e, "heading": heading, "title": "A title", "description": "d",
         "context_line": "c", "category": cat, "hook_score": 8, "standalone_score": 8}
    d.update(kw)
    return d


def test_sentences_split_on_punctuation(words):
    sents = build_sentences(words)
    assert len(sents) == 60
    assert sents[0]["w0"] == 0 and sents[0]["w1"] == 13


def test_clip_duration_always_in_window(words):
    sents = build_sentences(words)
    for s, e in [(3, 6), (3, 20), (10, 14), (0, 9), (30, 39)]:
        c = build_clip(sents, words, cand(s, e), CFG, BANNED)
        assert c is not None, (s, e)
        assert 50.0 <= c["duration"] <= 55.0, (s, e, c["duration"])


def test_prefers_sentence_boundary(words):
    sents = build_sentences(words)
    fit = fit_window(sents, words, 3, 12, CFG)
    assert fit and fit["clean"]
    assert words[fit["w1"]]["w"].endswith(".")


def test_rejects_bad_candidates(words):
    sents = build_sentences(words)
    assert build_clip(sents, words, cand(3, 12, cat="rant"), CFG, BANNED) is None
    assert build_clip(sents, words, cand(3, 12, heading=""), CFG, BANNED) is None
    assert build_clip(sents, words, cand(3, 9999), CFG, BANNED) is None
    assert build_clip(sents, words, cand(3, 12, heading="about suicide today"), CFG, BANNED) is None
    assert build_clip(sents, words, {"start_sentence": "x"}, CFG, BANNED) is None


def test_rejects_dead_air(words):
    sents = build_sentences(words)
    for w in words[60:]:
        w["s"] += 10
        w["e"] += 10
    sents = build_sentences(words)
    # a clip crossing the 10 s hole must fail the gap rule
    assert build_clip(sents, words, cand(2, 10), CFG, BANNED) is None


def test_pick_best_no_overlap_unique_headings_mixed_categories(words):
    sents = build_sentences(words)
    cands = [
        cand(3, 12, "funny", "Heading one", hook_score=10, standalone_score=10),
        cand(5, 14, "funny", "Heading two", hook_score=9, standalone_score=9),    # overlaps the first
        cand(20, 29, "funny", "Heading three"),
        cand(40, 49, "informative", "Heading four", hook_score=5, standalone_score=5),
        cand(50, 59, "deep", "Heading one"),                                       # duplicate heading
    ]
    picked = validate_and_pick(sents, words, cands, 3, CFG, BANNED)
    assert len(picked) == 3
    spans = sorted((c["start"], c["end"]) for c in picked)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), "clips overlap"
    assert len({c["heading"].lower() for c in picked}) == len(picked)
    assert len({c["category"] for c in picked}) >= 2


def test_weak_scores_are_filtered_in_code(words):
    sents = build_sentences(words)
    weak = build_clip(sents, words, cand(3, 12, hook_score=4, standalone_score=4), CFG, BANNED)  # 8 < min 10
    assert weak is None
    ok = build_clip(sents, words, cand(3, 12, hook_score=5, standalone_score=5), CFG, BANNED)    # exactly 10
    assert ok is not None


def test_heading_truncated_to_eight_words(words):
    sents = build_sentences(words)
    c = build_clip(sents, words, cand(3, 12, heading="one two three four five six seven eight nine ten"), CFG, BANNED)
    assert c and len(c["heading"].split()) == 8
