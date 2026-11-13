"""Ask Gemini for clip candidates, window by window, then hand them to the validator."""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import Settings
from .errors import ActionNeeded
from .sentences import fmt_time

log = logging.getLogger("autoyt.select")

SYSTEM = """You are an editor who cuts viral YouTube Shorts from long English streams and podcasts.
You are given a numbered transcript. Pick moments that work as stand-alone {lo}-{hi} second clips.

Every clip must be one of three types, and anything else must be skipped:
- funny: a real joke, bit, roast or chaotic moment, with its payoff inside the clip
- informative: a specific fact, explanation or practical insight the viewer learns from
- deep: an honest personal story, a thoughtful idea, or a perspective that makes people reflect

Rules:
- The clip must make sense without the rest of the video.
- The first 3 seconds must hook the viewer: start ON the claim, question or punchline setup, never on
  filler ("so", "um", "anyway", "like I was saying"). The ending must complete the thought.
- Prefer endings that loop: a last line that makes the first line hit again scores a high loop_score.
- Skip ads, sponsor reads, intros, outros, chat reading, housekeeping and long silences.
- The heading is a short on-screen hook of at most 6 words. It must be true to what is said.
  No invented quotes, no outrage bait, no claims the speaker did not make.
- Avoid medical or financial advice, accusations against named private people, tragedy and self-harm.
- A clip is about {wlo}-{whi} spoken words. Choose start and end by sentence number.
- Return exactly the number of clips requested. Ordinary conversation still contains usable stand-alone
  moments: a surprising claim, a story, a punchline, a strong opinion, a useful tip, an honest admission.
  Rank them and return the best ones. Only return fewer if the window has no speech worth clipping at all.
- Be honest in the scores so the editor can filter: a weak but usable clip is 5+5, a good one 7+7,
  a great one 9+9. Never inflate scores to hide a weak clip.

The transcript may have no punctuation (auto-captions); each numbered line is one short phrase.

Return JSON only, in this shape:
{"clips":[{"start_sentence":int,"end_sentence":int,"heading":str,"title":str,"description":str,
"context_line":str,"tags":[str],"category":"funny|informative|deep","hook_score":1-10,
"standalone_score":1-10,"loop_score":1-10,"comment_question":str,"reason":str}]}
"title" is the YouTube title (max 80 characters, no hashtags). "description" is one or two plain lines.
"context_line" is one short line telling a new viewer what is going on.
"tags" is 5 to 8 short lowercase search keywords for this clip: the topic, the people discussed, the
subject area. No hashtags and no links. Never put a URL or web address anywhere in the output.
"title" must sell the payoff or the open question (what the viewer gets), not just name the topic, and
must not invent facts. "comment_question" is one short question a viewer would want to answer in the
comments about this clip (an opinion or a choice, no yes/no trivia)."""

GTA_TALK = """

THIS CHANNEL'S GTA 6 FEED. The transcript is from a video about Grand Theft Auto VI (GTA 6).
- Only pick moments that are about GTA 6: news, confirmed features, the map, characters, release timing,
  price and editions, gameplay impressions, comparisons with GTA 5, tips, strong opinions and hot takes.
- Skip any moment where a trailer, a song or other music is playing, and skip moments that describe or
  react to a trailer or another creator's video. Speech only.
- Never present a rumour or a leak as fact. Say "rumoured" when the speaker does. Skip leaked footage talk.
- "title" must contain the exact words "GTA 6" within its first 40 characters, plus the concrete subject
  (for example "GTA 6 map is bigger than you think"). These are the words people search for.
- Prefer category "informative" for news and facts, "funny" for jokes, "deep" for opinions.
{phase}"""

PHASES = {
    "pre": ("Right now GTA 6 is {days} days from launch (19 Nov 2026). People search for: release time in "
            "their region, preload, price and editions, map size, what is confirmed, what to expect on day one."),
    "launch": ("GTA 6 launched {ago} days ago. People search for: first impressions, is it worth it, tips for "
               "beginners, hidden details, best missions, money tips, comparisons with GTA 5."),
    "post": "GTA 6 is out. People search for: tips, secrets, best moments, updates, GTA 6 Online news.",
}


def system_for(kind: str, clip: dict | None = None, today_iso: str | None = None,
               launch_date: str = "2026-11-19", hint: str = "") -> str:
    """The selection prompt for a kind, with its clip length and word budget filled in."""
    from datetime import date

    clip = clip or {"min_seconds": 50, "max_seconds": 55}
    lo, hi = int(clip["min_seconds"]), int(clip["max_seconds"])
    # str.replace, not format(): the prompt contains literal JSON braces
    text = (SYSTEM.replace("{lo}", str(lo)).replace("{hi}", str(hi))
            .replace("{wlo}", str(int(lo * 2.5))).replace("{whi}", str(int(hi * 2.55))))
    if kind == "gta6_talk":
        days = (date.fromisoformat(launch_date) - date.fromisoformat(today_iso or date.today().isoformat())).days
        if days > 0:
            phase = PHASES["pre"].format(days=days)
        elif days > -21:
            phase = PHASES["launch"].format(ago=-days)
        else:
            phase = PHASES["post"]
        text += GTA_TALK.replace("{phase}", "- " + phase)
    if hint:
        text += "\n\n" + hint
    return text


def make_windows(sentences: list[dict], window_s: float, overlap_s: float) -> list[list[dict]]:
    if not sentences:
        return []
    end_t = sentences[-1]["end"]
    windows, t = [], 0.0
    while t < end_t:
        w = [x for x in sentences if x["start"] >= t - overlap_s and x["start"] < t + window_s]
        if len(w) > 10:
            windows.append(w)
        t += window_s
    return windows


def window_prompt(window: list[dict], title: str, credit: str, n: int) -> str:
    lines = [f"[{x['i']}] {fmt_time(x['start'])} {x['text']}" for x in window]
    return (f"Video: {title}\nChannel: {credit}\n"
            f"Return exactly {n} clips from the transcript below (sentence numbers are in brackets).\n\n"
            + "\n".join(lines))


def _parse_json(text: str):
    """Tolerant parse: strips code fences and ignores anything after the first complete JSON value."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise
        data, _ = json.JSONDecoder().raw_decode(text[start:])  # raises if still broken -> caller retries
    if isinstance(data, dict):
        data = data.get("clips", [])
    return data if isinstance(data, list) else []


def _clip_schema() -> dict:
    """Structured-output schema: Gemini then cannot emit broken JSON or an invalid category."""
    str_ = {"type": "STRING"}
    int_ = {"type": "INTEGER"}
    return {"type": "OBJECT", "required": ["clips"], "properties": {"clips": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "required": ["start_sentence", "end_sentence", "heading", "title", "category", "hook_score",
                     "standalone_score"],
        "properties": {
            "start_sentence": int_, "end_sentence": int_, "heading": str_, "title": str_,
            "description": str_, "context_line": str_, "reason": str_,
            "tags": {"type": "ARRAY", "items": str_},
            "category": {"type": "STRING", "enum": ["funny", "informative", "deep"]},
            "hook_score": int_, "standalone_score": int_, "loop_score": int_, "comment_question": str_,
        }}}}}


def call_gemini(s: Settings, prompt: str, retries: int = 5, system: str | None = None) -> list[dict]:
    if not s.gemini_api_key:
        raise ActionNeeded("GEMINI_API_KEY is not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system or system_for("podcast")}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": _clip_schema(),
                             "temperature": 0.5},
    }
    delay = 5.0
    last = ""
    for attempt in range(retries):
        try:
            r = requests.post(url, json=body, headers={"x-goog-api-key": s.gemini_api_key}, timeout=180)
        except requests.RequestException as e:
            last = str(e)
        else:
            if r.status_code == 200:
                try:
                    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return _parse_json(text)
                except (KeyError, IndexError, ValueError) as e:
                    last = f"bad response: {e}"
            elif r.status_code == 400 and "responseSchema" in r.text and "responseSchema" in body["generationConfig"]:
                log.warning("Gemini rejected the response schema; continuing without it")
                body["generationConfig"].pop("responseSchema")  # prompt still asks for the same JSON
                last = "schema rejected"
            elif r.status_code in (400, 401, 403) and ("API key" in r.text or "API_KEY" in r.text
                                                      or r.status_code in (401, 403)):
                raise ActionNeeded(f"Gemini rejected the API key or request ({r.status_code}): {r.text[:200]}")
            elif r.status_code == 404:
                raise ActionNeeded(f"Gemini model {s.gemini_model!r} not found: {r.text[:200]}")
            else:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
        log.warning("gemini attempt %d failed: %s", attempt + 1, last)
        time.sleep(delay)
        delay = min(delay * 3, 120)
    raise RuntimeError(f"Gemini failed after {retries} tries: {last}")


def collect_candidates(s: Settings, sentences: list[dict], title: str, credit: str,
                       per_window: int | None = None, kind: str = "podcast", clip: dict | None = None,
                       hint: str = "", today_iso: str | None = None) -> list[dict]:
    clip_cfg = clip or s.cfg["clip"]
    n = per_window or clip_cfg["candidates_per_window"]
    windows = make_windows(sentences, clip_cfg["window_minutes"] * 60, clip_cfg["window_overlap_seconds"])
    system = system_for(kind, clip_cfg, today_iso, str(s.cfg["gta6"].get("launch_date", "2026-11-19")), hint)

    def one(k: int):
        return call_gemini(s, window_prompt(windows[k], title, credit, n), system=system)

    out: list[dict] = []
    # windows are independent, so ask for several at once (each call is about 2 s of waiting)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(one, k) for k in range(len(windows))]
        for k, fut in enumerate(futures):
            try:
                got = fut.result()
            except ActionNeeded:
                raise
            except RuntimeError as e:
                log.warning("window %d skipped: %s", k, e)
                continue
            log.info("window %d/%d: %d candidates", k + 1, len(windows), len(got))
            out.extend(c for c in got if isinstance(c, dict))
    return out
