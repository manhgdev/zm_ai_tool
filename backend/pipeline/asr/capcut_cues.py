"""Clone caption grouping using CapCut word clocks, never estimated timing."""
from __future__ import annotations

import math
import re

from pipeline.export.srt import style_params


def validated_words(row: dict) -> list[dict]:
    """Return complete, ordered alignment or nothing; keep source text intact."""
    text = str(row.get("source") or row.get("text") or "")
    try:
        start, end = float(row["start"]), float(row["end"])
        words = [{"word": str(w["word"]), "start": float(w["start"]), "end": float(w["end"])}
                 for w in row.get("words", []) if str(w["word"]).strip()]
        if (not words or "".join("".join(w["word"].split()) for w in words) != "".join(text.split())
                or not all(math.isfinite(w["start"]) and math.isfinite(w["end"])
                           and start <= w["start"] < w["end"] <= end
                           and (i == 0 or w["start"] >= words[i - 1]["end"])
                           for i, w in enumerate(words))):
            return []
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
    return words


_SENTENCE_END = re.compile(r'''[.!?…。！？]["'”’)]*$''')
# ponytail: a small Vietnamese phrase rule, not a semantic model. It protects
# common multi-word addresses/time phrases and closes opening greetings. More
# complex syntax needs a language-aware boundary model, not guessed timestamps.
_GREETING = re.compile(r"(?:hê lô|hello|xin chào|chào)(?: tất cả)? (?:anh em|mọi người|các bạn)[,!！]?", re.I)
_PHRASES = {"anh em", "mọi người", "các bạn", "hôm nay", "ngày mai", "hôm qua"}


def regroup_capcut_segments(segments: list[dict], *, source_lang: str = "auto") -> list[dict]:
    """Group words across provider display cues, falling back to whole cues.

    A foreign translation has no word alignment: keep its source/target pair
    atomic instead of redistributing translated words using source durations.
    """
    params = style_params("hard")
    limit = params.max_chars
    vi = source_lang.lower().split("-")[0] in {"vi", "auto"}
    units: list[dict] = []
    for index, row in enumerate(segments):
        text = " ".join(str(row.get("source") or "").split())
        translation = " ".join(str(row.get("translation") or "").split())
        words = validated_words(row)
        if not words or (translation and translation != text):
            units.append({"row": index, "source": text, "translation": translation,
                          "start": row["start"], "end": row["end"], "words": [], "aligned": False})
            continue
        # Slice the supplied text to retain spaces/CJK punctuation exactly.
        positions = [i for i, char in enumerate(text) if not char.isspace()]
        offset = cursor = 0
        for word in words:
            offset += len("".join(word["word"].split()))
            stop = positions[offset - 1] + 1
            piece = text[cursor:stop]
            units.append({"row": index, "source": piece,
                          "translation": piece if translation else "",
                          "start": word["start"], "end": word["end"],
                          "words": [word], "aligned": True})
            cursor = stop

    groups: list[list[dict]] = []
    for unit in units:
        if not groups:
            groups.append([unit])
            continue
        group = groups[-1]
        previous = group[-1]
        base, next_base = segments[previous["row"]], segments[unit["row"]]
        separator = " " if previous["row"] != unit["row"] else ""
        current_text = _join_units(group, "source")
        candidate = current_text + separator + unit["source"]
        gap = round(float(unit["start"]) - float(previous["end"]), 3)
        elapsed = round(float(unit["end"]) - float(group[0]["start"]), 3)
        phrase = (previous["source"].strip() + " " + unit["source"].strip()).casefold()
        protected = vi and previous["aligned"] and unit["aligned"] and phrase in _PHRASES and gap <= 0.8
        same_metadata = all(base.get(k) == next_base.get(k) for k in ("speaker", "voice"))
        can_merge = (same_metadata and previous["aligned"] == unit["aligned"]
                     and bool(previous["translation"]) == bool(unit["translation"])
                     and gap >= 0 and elapsed <= 6
                     and not _SENTENCE_END.search(current_text)
                     and not (vi and _GREETING.fullmatch(current_text))
                     and previous["aligned"] and unit["aligned"]
                     and (gap <= 0.35 or protected))
        if can_merge:
            # Provider cues are soft boundary hints, not mandatory cuts. Only
            # revisit them when the short-cue limit is reached; never cut on
            # either side of a one-word provider fragment such as "thuyết".
            if len(candidate) > limit or len(candidate.split()) > params.max_words:
                cut = None
                for i in range(len(group) - 1, 0, -1):
                    left, right = group[i - 1], group[i]
                    if left["row"] == right["row"]:
                        continue
                    if vi and (left["source"].strip() + " " + right["source"].strip()).casefold() in _PHRASES:
                        continue
                    if any(len(str(segments[item["row"]].get("source") or "").split()) <= 1
                           for item in (left, right)):
                        continue
                    if len(_join_units(group[:i], "source")) >= 12:
                        cut = i
                        break
                if cut is None:
                    groups.append([unit])
                    continue
                tail = group[cut:]
                del group[cut:]
                groups.append(tail)
                group = groups[-1]
            group.append(unit)
        else:
            groups.append([unit])

    result = []
    for group in groups:
        row = dict(segments[group[0]["row"]])
        row.update(start=group[0]["start"], end=group[-1]["end"],
                   source=_join_units(group, "source"), translation=_join_units(group, "translation"))
        row.pop("words", None)
        if group[0]["aligned"]:
            row["words"] = [w for unit in group for w in unit["words"]]
        result.append(row)
    return result


def _join_units(units: list[dict], field: str) -> str:
    parts: list[str] = []
    for i, unit in enumerate(units):
        if i and unit["row"] != units[i - 1]["row"]:
            parts.append(" ")
        parts.append(unit[field])
    return "".join(parts).strip()
