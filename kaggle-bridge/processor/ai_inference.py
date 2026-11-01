"""Gate C — clip selection with the production prompts and output contract.

**Nothing here is a reinterpretation of ClipWright's selection logic.** The
system prompt, the task prompt, the timestamp format and the output contract are
copied verbatim from ``backend/src/ai.py`` at commit ``a73607d``:

* ``transcript_analysis_system_prompt`` → :data:`TRANSCRIPT_ANALYSIS_SYSTEM_PROMPT`
* ``build_transcript_analysis_prompt``  → :func:`build_transcript_user_prompt`
* ``TranscriptAnalysis`` / ``TranscriptSegment`` / ``ViralityAnalysis``
  → :func:`validate_analysis` (a pydantic-free mirror of the same rules)

What the bridge deliberately does **not** port is the windowed orchestration
(``split_transcript_into_windows`` → per-window analysis → ``rerank_candidates``
→ ``_validate_segments`` grounding/snapping). Phase 1 runs one short source in a
single window, so those stages have nothing to do yet; they stay in production.
This difference is recorded in ``docs/COMPATIBILITY-NOTES.md`` rather than
silently assumed.

Two inference backends are provided because Kaggle's GPU makes both plausible:

``ollama``
    Same runtime as production (an OpenAI-ish local HTTP daemon), highest
    fidelity. Needs the daemon installed and the model pulled inside the kernel.
``transformers``
    ``transformers`` + ``accelerate`` loading the same 3B instruction model in
    fp16 on the T4. No daemon, no model-pull step, but the chat template and
    sampling are then ours, not Ollama's.

The caller picks; the report records which one ran.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# --- contract constants, mirrored from backend/src/ai.py ---------------------

IDEAL_CLIP_MIN_SECONDS = 25
IDEAL_CLIP_MAX_SECONDS = 50
MIN_ACCEPTED_CLIP_SECONDS = 15
MAX_ACCEPTED_CLIP_SECONDS = 60
HOOK_TITLE_MAX_CHARS = 64
HOOK_TITLE_MAX_WORDS = 10
HOOK_TYPES = ("question", "statement", "statistic", "story", "contrast", "none")

TRANSCRIPT_SPAN_RE = re.compile(
    r"^\[(?P<start>\d{1,3}:\d{2}(?::\d{2})?)\s*-\s*"
    r"(?P<end>\d{1,3}:\d{2}(?::\d{2})?)\]\s*(?P<text>.*)$"
)

#: Default GGUF/instruction model. Production defaults to ``ollama:llama3.2:3b``.
DEFAULT_MODEL = "llama3.2:3b"

#: Ungated mirror of the same weights, used by the transformers backend so the
#: kernel never needs a Hugging Face token.
DEFAULT_TRANSFORMERS_MODEL = "unsloth/Llama-3.2-3B-Instruct"


class AnalysisError(RuntimeError):
    """Raised when the model could not produce a usable analysis."""


# --- prompts (verbatim, with bound placeholders) ----------------------------

TRANSCRIPT_ANALYSIS_SYSTEM_PROMPT = """You are an expert transcript analyst for short-form video editing.

Your job is extraction and ranking, not creative rewriting. You must stay fully grounded in the transcript and choose the best clip candidates that already exist in the source material.

OUTPUT CONTRACT:
- Return valid JSON only. Do not output Markdown, headings, bullets, prose, code fences, explanations, or commentary outside the JSON object.
- The top-level JSON object must include: "most_relevant_segments", "summary", and "key_topics".
- Each item in "most_relevant_segments" must include: "start_time", "end_time", "text", "relevance_score", "reasoning", "virality", and "hook_title".
- Do not use "segment" as an output field. Use "text".
- "virality" must include: "hook_score", "engagement_score", "value_score", "shareability_score", "total_score", "hook_type", and "virality_reasoning".
- Every returned segment must be {minimum}-{maximum} seconds long. Prefer {ideal_minimum}-{ideal_maximum} seconds.

CORE OBJECTIVES:
1. Identify segments that would be compelling on social media platforms
2. Focus on complete thoughts, insights, or entertaining moments
3. Prioritize content with hooks, emotional moments, or valuable information
4. Each segment should be engaging and worth watching
5. Score each segment's viral potential with detailed breakdown

GROUNDING RULES:
1. Use only the provided transcript lines and timestamps
2. Never invent facts, tone, context, or transitions that are not present
3. Treat this as span selection over a timestamped transcript, not open-ended summarization
4. Each selected segment must map to one contiguous range in the transcript
5. segment.text must match the chosen span closely and must not include content from outside the chosen range
6. Do not stitch together distant moments into one clip
7. If a speaker label appears, use it only if it is part of the spoken content and helps clarity

CONTENT NEUTRALITY RULES:
1. This is clipping software for legitimate editing workflows
2. Do not judge, moralize, or downgrade a segment just because the topic is controversial, sensitive, adult, political, criminal, medical, or otherwise intense
3. Evaluate segments only on clip quality: clarity, self-contained value, hook strength, emotional impact, specificity, and shareability
4. Do not refuse analysis just because the speaker describes risky, offensive, or uncomfortable subject matter
5. Only downgrade a segment when the transcript itself is weak, confusing, repetitive, unusable, or a poor standalone clip

SEGMENT SELECTION CRITERIA:
1. STRONG HOOKS: Attention-grabbing opening lines
2. VALUABLE CONTENT: Tips, insights, interesting facts, stories
3. EMOTIONAL MOMENTS: Excitement, surprise, humor, inspiration
4. COMPLETE THOUGHTS: Self-contained ideas that make sense alone
5. ENTERTAINING: Content people would want to share
6. HIGH SIGNAL: Prefer specific, concrete language over vague discussion
7. LOW FILLER: Avoid greetings, sponsor reads, repeated setup, throat-clearing, and housekeeping unless they are unusually compelling

WHAT A GOOD CLIP FEELS LIKE:
- A viewer should understand and care without the original title, thumbnail, or previous context
- Prefer a complete mini-story or argument: setup, tension or claim, specific detail, and payoff
- Expand a great short moment to nearby contiguous lines when that adds needed setup, stakes, or payoff
- Strong picks include contrarian claims, mistakes or lessons, concrete examples, before/after moments, frameworks, surprising results, emotionally charged reactions, and complete answers to interesting questions
- Bad picks include intros, sponsor or CTA sections, vague setup, contextless quote fragments, repeated points, definitions without payoff, meandering background, and answer fragments that require unseen context

VIRALITY SCORING (0-100 total, from four 0-25 subscores):
For each segment, provide a detailed virality breakdown:

1. HOOK STRENGTH (0-25):
   - 20-25: Immediately grabs attention (surprising fact, bold claim, intriguing question)
   - 15-19: Good opener that creates curiosity
   - 10-14: Decent start but could be stronger
   - 0-9: Weak or no hook

2. ENGAGEMENT (0-25):
   - 20-25: Highly entertaining, emotional, or dramatic
   - 15-19: Interesting and holds attention
   - 10-14: Moderately engaging
   - 0-9: Flat or boring delivery

3. VALUE (0-25):
   - 20-25: Actionable insights, unique knowledge, or transformative ideas
   - 15-19: Useful information most people don't know
   - 10-14: Somewhat informative
   - 0-9: Common knowledge or filler content

4. SHAREABILITY (0-25):
   - 20-25: "I need to send this to someone" content
   - 15-19: Content worth bookmarking
   - 10-14: Nice but not share-worthy
   - 0-9: Generic content

HOOK TITLES ("hook_title" per segment):
- Write a short on-screen headline (3-9 words) that is burned into the top of the clip
- It must make a scrolling viewer stop: a bold claim, curiosity gap, number, or stakes taken directly from the segment
- Stay grounded: only promise what the clip actually delivers; never invent facts or numbers
- Do not simply repeat the first spoken words verbatim; reframe them as a headline
- Plain text only: no hashtags, no emojis, no quotes around the title
- Write it in the same language as the transcript
- Good examples: "The $40k mistake I keep seeing", "Why nobody tells you this about VC", "Do this before your next interview"

CLIP BOUNDARIES — OPENING AND ENDING:
- start_time is the first word of a sentence a new viewer can understand with no prior context
- Do not start mid-sentence, on a filler (like, dude, um, well), on a bare agreement (yes, yeah, right, exactly, totally), or on a pronoun that points at someone the viewer has not met yet (he, she, they)
- Do not start on a back-reference ("as I said", "the thing we mentioned", "that point", "before we started recording") unless the clip also contains the thing being referenced
- If the moment only makes sense with the previous sentence's noun, claim, or question, move start_time back to include that sentence
- end_time is the last word of the sentence that lands the point. Do not stop mid-clause, and do not keep the opening words of the next unrelated thought
- The clip's real first sentence is the hook. Do not choose a weak opening because a later line could be replayed in front of it
- Judge the first spoken seconds on their own, separately from hook_title: a viewer who hears only them must know the subject or claim
- Skip lead-in banter before the point (calling someone over, "No. No? Ah!", greetings, reactions to something off-screen) and start on the sentence that delivers it
- Do not start on a one-word answer to a question the clip leaves out ("Catharsis."), or on a conjunction that continues an earlier point ("But the main reason...", "And then...", "So that's why..."); start where the thought stands alone, or include the question or claim it answers
- Lower hook_score for a segment whose opening still depends on missing context

VISUAL SUITABILITY:
- The clip is a video. Avoid segments where the speech says the picture is interrupted, unavailable or broken: a technical problem, "these images cannot be shown", "we'll be right back", a frozen or lost stream, a test or placeholder screen
- Those passages usually play over a still slate or black, so they are not ready-to-post highlights no matter how strong the words are

COLD-OPEN HOOK ("hook_start_time"/"hook_end_time" per segment, optional):
- Prefer null. The clip is played from start_time; a later line is not replayed in front of it unless an operator explicitly enables that
- Only set a range when a 2-5 second punchline later in the segment stands completely alone AND the real opening is already a full setup
- Use timestamps from the transcript lines inside the segment, at least 3 seconds after start_time
- Set both to null when the clip already opens on its strongest line, or when replaying the line would just repeat it

HOOK TYPES to identify:
- "question": Opens with a question that creates curiosity
- "statement": Bold claim or surprising statement
- "statistic": Uses compelling numbers or data
- "story": Starts with narrative/anecdote
- "contrast": Before/after or problem/solution framing
- "none": No clear hook pattern

TIMING GUIDELINES:
- Target {ideal_minimum}-{ideal_maximum} seconds for most clips
- Shorter picks within the requested range must still be self-contained and complete
- CRITICAL: start_time MUST be different from end_time (minimum {minimum} seconds apart)
- Focus on natural content boundaries rather than arbitrary time limits
- Include enough context for the segment to be understandable
- Stay within the requested duration range
- Start at the hook or the minimum setup needed to make the hook land, and end after the payoff
- If a highlight is only one good line, expand to include the surrounding setup and payoff rather than returning a tiny fragment
- Stop expanding when the topic drifts, the speaker repeats the same point, or the clip loses momentum

TIMESTAMP REQUIREMENTS - EXTREMELY IMPORTANT:
- Use EXACT timestamps as they appear in the transcript
- Never modify timestamp format (keep MM:SS structure)
- start_time MUST be LESS THAN end_time (start_time < end_time)
- MINIMUM segment duration: {minimum} seconds (end_time - start_time >= {minimum} seconds)
- MAXIMUM segment duration: {maximum} seconds
- IDEAL segment duration: {ideal_minimum}-{ideal_maximum} seconds
- Look at transcript ranges like [02:25 - 02:35] and use different start/end times
- NEVER use the same timestamp for both start_time and end_time
- Example: start_time: "02:25", end_time: "02:35" (NOT "02:25" and "02:25")

SCORING AND OUTPUT RULES:
- relevance_score should reflect how well the segment works as a standalone short clip, not just whether the topic is generally important
- Penalize clips that are only quotable but not self-contained, too generic, missing setup, missing payoff, or padded with filler
- virality_reasoning and reasoning should cite what is actually present in the chosen span
- summary and key_topics must also stay grounded in the transcript and should not add outside interpretation

Find the number of compelling segments requested in the task (usually 2-5) that would work well as standalone clips. Quality over quantity: choose fewer stronger segments over filling a quota. Every selected segment must be accurate, self-contained, have proper time ranges, and score high on virality metrics."""


@dataclass(frozen=True)
class ClipBounds:
    minimum: int = MIN_ACCEPTED_CLIP_SECONDS
    maximum: int = MAX_ACCEPTED_CLIP_SECONDS
    ideal_minimum: int = IDEAL_CLIP_MIN_SECONDS
    ideal_maximum: int = IDEAL_CLIP_MAX_SECONDS


def build_system_prompt(bounds: ClipBounds = ClipBounds()) -> str:
    return TRANSCRIPT_ANALYSIS_SYSTEM_PROMPT.format(
        minimum=bounds.minimum,
        maximum=bounds.maximum,
        ideal_minimum=bounds.ideal_minimum,
        ideal_maximum=bounds.ideal_maximum,
    )


def build_transcript_user_prompt(
    transcript: str,
    *,
    window: Optional[Tuple[int, int]] = None,
    clip_signals: Optional[str] = None,
    performance_context: Optional[str] = None,
    bounds: ClipBounds = ClipBounds(),
    segment_target: str = "2-5",
) -> str:
    """Verbatim port of ``build_transcript_analysis_prompt`` (same sections, same order)."""
    window_note = ""
    if window:
        window_note = (
            f"\nThis transcript is one window ({_format_minutes_label(window[0])} - "
            f"{_format_minutes_label(window[1])}) of a longer video. Other windows are "
            "analysed separately, so pick the best clips that exist inside this window only.\n"
        )
    performance_section = _performance_history_section(performance_context)
    signal_section = ""
    if clip_signals:
        signal_section = (
            "\n\nAdditional deterministic signals from transcript/audio analysis:\n"
            f"{clip_signals}\n\n"
            "Use these as hints only. They should influence ranking, but every final segment "
            "must still be a coherent contiguous transcript range."
        )

    return f"""Analyze this video transcript and identify the most engaging segments for short-form content.
{window_note}
The transcript is formatted as one line per timestamped span, for example:
[00:12 - 00:21] Spoken text here
[00:21 - 00:35] More spoken text here

Follow this workflow:
1. Read the transcript as a sequence of timestamped spans.
2. Select only contiguous ranges that already exist in the transcript.
3. Prefer moments with a strong hook, clear payoff, emotional charge, or concrete value.
4. For each chosen segment, use the earliest timestamp in the selected range as start_time and the latest timestamp in the selected range as end_time.
5. start_time must be the first word of a self-contained sentence: never a mid-thought fragment, a filler, a bare agreement, or a reference to something this clip does not contain. end_time must finish that thought's last sentence and must not include the next thought's opening words.

Selection target:
- Choose {segment_target} segments total.
- Most selected clips should be {bounds.ideal_minimum}-{bounds.ideal_maximum} seconds.
- Every clip must contain a full setup and payoff within {bounds.minimum}-{bounds.maximum} seconds.
- If a strong moment is shorter than {bounds.ideal_minimum} seconds, first try expanding to nearby contiguous transcript lines that add useful context.
- Skip weak standalone picks: intros, sponsor reads, CTAs, contextless quotes, repeated points, vague setup, and answer fragments that require prior context.
- Before returning a segment, ask whether a viewer would understand and care without seeing the rest of the source video.

Critical accuracy requirements:
- Do not fabricate or embellish content.
- Do not use timestamps that are not present in the transcript.
- Do not merge separate non-contiguous moments into one segment.
- segment.text must reflect only the spoken content inside the selected time range.
- If a span lacks enough context to stand alone, expand to nearby contiguous lines rather than guessing.
- If there is a tradeoff between "viral" and "accurate", choose accuracy.
- Do not reject or penalize a segment simply because of the subject matter; stay content-neutral and assess clip quality only.
{signal_section}{performance_section}

JSON-only output requirements:
- Return one valid JSON object and nothing else.
- No Markdown, headings, bullets, code fences, or explanatory text outside JSON.
- Top-level keys: "most_relevant_segments", "summary", "key_topics".
- Segment keys: "start_time", "end_time", "text", "relevance_score", "reasoning", "virality", "hook_title".
- Optional segment keys "hook_start_time"/"hook_end_time": null unless a later 2-5 second line stands alone and the real opening is already a complete setup. The clip is not replayed from that line by default.
- "hook_title" is a 3-9 word plain-text headline for the clip, in the transcript's language, grounded in the segment (no hashtags, emojis, or quotes).
- Virality keys: "hook_score", "engagement_score", "value_score", "shareability_score", "total_score", "hook_type", "virality_reasoning".
- Do not return segments shorter than {bounds.minimum} seconds or longer than {bounds.maximum} seconds.

Transcript:
{transcript}"""


def _performance_history_section(performance_context: Optional[str]) -> str:
    """Same shape as the production helper: an optional, clearly-labelled section."""
    if not performance_context:
        return ""
    return (
        "\n\nCreator performance history (measured audience response to past clips):\n"
        f"{performance_context}\n\n"
        "Use it to bias ranking toward what this audience actually watched, but never "
        "at the cost of staying grounded in this transcript."
    )


def _format_minutes_label(seconds: int) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    return f"{minutes:02d}:{remainder:02d}"


# --- transcript formatting (mirrors production) -----------------------------


def format_ms_to_timestamp(ms: int) -> str:
    """``MM:SS`` with unbounded minutes — identical to the production helper."""
    seconds = int(ms) // 1000
    minutes = seconds // 60
    return f"{minutes:02d}:{seconds % 60:02d}"


def format_transcript_lines(transcript: Mapping[str, Any]) -> List[str]:
    """Turn a ClipWright transcript cache into prompt lines.

    Production formats one line per utterance (``[MM:SS - MM:SS] text``), with a
    speaker prefix only when a speaker is known. Whisper output has no speaker,
    so the prefix never appears here.
    """
    lines: List[str] = []
    for utterance in transcript.get("utterances") or []:
        text = str(utterance.get("text") or "").strip()
        if not text:
            continue
        start = format_ms_to_timestamp(int(utterance.get("start") or 0))
        end = format_ms_to_timestamp(int(utterance.get("end") or 0))
        speaker = utterance.get("speaker")
        prefix = f"Speaker {speaker}: " if speaker else ""
        lines.append(f"[{start} - {end}] {prefix}{text}")
    return lines


def parse_transcript_timestamp_seconds(timestamp: str) -> int:
    parts = [int(part) for part in str(timestamp).split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported timestamp format: {timestamp}")


# --- response parsing and validation (mirrors the pydantic models) ----------


def extract_json_object(text: str) -> Dict[str, Any]:
    """Pull the first balanced JSON object out of a model reply.

    Small local models wrap JSON in code fences or add a sentence before it often
    enough that a strict ``json.loads`` is not enough. Brace matching skips over
    strings, so braces inside transcript text do not break it.
    """
    if not text:
        raise AnalysisError("model returned an empty reply")

    cleaned = text.strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, Mapping):
                        return dict(parsed)
                    break
        start = cleaned.find("{", start + 1)

    raise AnalysisError("model reply did not contain a parsable JSON object")


def sanitize_hook_title(raw: Optional[str]) -> Optional[str]:
    """Port of ``sanitize_hook_title``: strip quotes/markdown/hashtags, cap the length."""
    if not raw:
        return None
    title = str(raw).strip().strip("\"'`“”‘’").strip()
    title = re.sub(r"#\w+", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip(".,;:-–— ").strip()
    if not title:
        return None
    words = title.split()
    if len(words) > HOOK_TITLE_MAX_WORDS:
        title = " ".join(words[:HOOK_TITLE_MAX_WORDS])
    if len(title) > HOOK_TITLE_MAX_CHARS:
        clipped = title[: HOOK_TITLE_MAX_CHARS + 1]
        cut = clipped.rfind(" ")
        title = (clipped[:cut] if cut > 20 else title[:HOOK_TITLE_MAX_CHARS]).rstrip(".,;:-–— ")
    return title or None


def _default_virality() -> Dict[str, Any]:
    return {
        "hook_score": 15,
        "engagement_score": 15,
        "value_score": 15,
        "shareability_score": 15,
        "total_score": 60,
        "hook_type": "none",
        "virality_reasoning": "The model did not provide a detailed virality breakdown.",
    }


@dataclass
class ValidatedAnalysis:
    segments: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    key_topics: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "most_relevant_segments": self.segments,
            "summary": self.summary,
            "key_topics": self.key_topics,
            "rejected_segments": self.rejected,
            "warnings": self.warnings,
        }


def _coerce_score(value: Any, *, low: float, high: float, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, numeric))


def validate_analysis(
    payload: Mapping[str, Any],
    *,
    bounds: ClipBounds = ClipBounds(),
    source_duration_seconds: Optional[float] = None,
    max_segments: Optional[int] = None,
) -> ValidatedAnalysis:
    """Validate a model reply against the production contract.

    Mirrors the pydantic model's defaults and coercions (missing virality
    sub-scores default, ``relevance_score`` given as 0-100 is divided by 100,
    ``text`` accepts the ``segment`` alias) and additionally enforces the clip
    duration bounds the production ``_validate_segments`` stage would repair.
    Segments that cannot be repaired are rejected with a reason instead of
    silently dropped.
    """
    result = ValidatedAnalysis()
    if not isinstance(payload, Mapping):
        raise AnalysisError("analysis payload is not a JSON object")

    raw_segments = payload.get("most_relevant_segments")
    if raw_segments is None:
        # Tolerate the alternate key some small models emit.
        raw_segments = payload.get("segments")
        if raw_segments is not None:
            result.warnings.append("model used 'segments' instead of 'most_relevant_segments'")
    if not isinstance(raw_segments, list):
        raise AnalysisError("analysis payload is missing a 'most_relevant_segments' array")

    summary = payload.get("summary")
    result.summary = summary.strip() if isinstance(summary, str) else ""
    topics = payload.get("key_topics")
    if isinstance(topics, list):
        result.key_topics = [str(topic).strip() for topic in topics if str(topic).strip()]

    for index, raw in enumerate(raw_segments):
        reason = _validate_one_segment(
            raw, index, bounds, source_duration_seconds, result
        )
        if reason:
            result.rejected.append({"index": index, "reason": reason})

    if max_segments is not None and len(result.segments) > max_segments:
        result.segments = sorted(
            result.segments, key=lambda s: s["virality"]["total_score"], reverse=True
        )[:max_segments]
        result.warnings.append(f"trimmed selection to {max_segments} segments")

    return result


def _validate_one_segment(
    raw: Any,
    index: int,
    bounds: ClipBounds,
    source_duration_seconds: Optional[float],
    result: ValidatedAnalysis,
) -> Optional[str]:
    if not isinstance(raw, Mapping):
        return "segment is not an object"

    start_raw = raw.get("start_time")
    end_raw = raw.get("end_time")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        return "missing start_time/end_time"
    try:
        start_seconds = parse_transcript_timestamp_seconds(start_raw)
        end_seconds = parse_transcript_timestamp_seconds(end_raw)
    except (ValueError, TypeError):
        return f"unparsable timestamps {start_raw!r}-{end_raw!r}"

    if end_seconds <= start_seconds:
        return f"end_time {end_raw} is not after start_time {start_raw}"

    duration = end_seconds - start_seconds
    if duration < bounds.minimum:
        return f"segment is {duration}s, shorter than the {bounds.minimum}s minimum"
    if duration > bounds.maximum:
        return f"segment is {duration}s, longer than the {bounds.maximum}s maximum"
    if source_duration_seconds and end_seconds > source_duration_seconds + 1:
        return f"segment ends at {end_seconds}s, past the {source_duration_seconds:.0f}s source"

    text = raw.get("text", raw.get("segment"))
    if not isinstance(text, str) or not text.strip():
        return "missing text"

    virality_raw = raw.get("virality")
    virality = _default_virality()
    if isinstance(virality_raw, Mapping):
        for key in ("hook_score", "engagement_score", "value_score", "shareability_score"):
            virality[key] = int(_coerce_score(virality_raw.get(key), low=0, high=25, default=virality[key]))
        virality["total_score"] = int(
            _coerce_score(
                virality_raw.get("total_score"),
                low=0,
                high=100,
                default=sum(virality[k] for k in ("hook_score", "engagement_score", "value_score", "shareability_score")),
            )
        )
        hook_type = virality_raw.get("hook_type")
        virality["hook_type"] = hook_type if hook_type in HOOK_TYPES else "none"
        reasoning = virality_raw.get("virality_reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            virality["virality_reasoning"] = reasoning.strip()
    elif virality_raw is not None:
        result.warnings.append(f"segment {index}: 'virality' was not an object; defaults used")

    relevance = _coerce_score(raw.get("relevance_score"), low=0.0, high=1.0, default=0.75)
    if isinstance(raw.get("relevance_score"), (int, float)) and 1 < float(raw["relevance_score"]) <= 100:
        relevance = float(raw["relevance_score"]) / 100.0

    reasoning_text = raw.get("reasoning")
    hook_title = sanitize_hook_title(raw.get("hook_title", raw.get("title", raw.get("headline"))))

    segment = {
        "start_time": _format_seconds_as_transcript(start_seconds),
        "end_time": _format_seconds_as_transcript(end_seconds),
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "duration_seconds": duration,
        "text": text.strip(),
        "relevance_score": round(relevance, 3),
        "reasoning": reasoning_text.strip() if isinstance(reasoning_text, str) else "",
        "virality": virality,
        "hook_title": hook_title,
        "hook_start_time": raw.get("hook_start_time"),
        "hook_end_time": raw.get("hook_end_time"),
    }
    result.segments.append(segment)
    return None


def _format_seconds_as_transcript(seconds: int) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# --- backends ---------------------------------------------------------------


@dataclass
class InferenceResult:
    text: str
    backend: str
    model: str
    duration_seconds: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class OllamaBackend:
    """Talks to a local Ollama daemon — the same runtime production uses."""

    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = "http://127.0.0.1:11434",
        *,
        num_ctx: int = 8192,
        timeout_seconds: float = 900.0,
        temperature: Optional[float] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    def available(self) -> Tuple[bool, str]:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            return False, f"ollama is not reachable at {self.base_url}: {error}"
        names = [str(m.get("name", "")) for m in payload.get("models") or []]
        if not any(name == self.model or name.startswith(f"{self.model}:") for name in names):
            return False, f"model {self.model!r} is not installed (present: {', '.join(names) or 'none'})"
        return True, f"ollama ready with {self.model}"

    def generate(self, system: str, user: str) -> InferenceResult:
        import urllib.error
        import urllib.request

        options: Dict[str, Any] = {"num_ctx": self.num_ctx}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": options,
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            raise AnalysisError(f"ollama request failed: {error}") from error
        elapsed = time.perf_counter() - started

        message = payload.get("message") or {}
        text = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise AnalysisError("ollama returned an empty message")

        metadata = {
            "num_ctx": self.num_ctx,
            "temperature": self.temperature,
            "eval_count": payload.get("eval_count"),
            "prompt_eval_count": payload.get("prompt_eval_count"),
            "total_duration_ns": payload.get("total_duration"),
        }
        if isinstance(metadata.get("eval_count"), int) and elapsed > 0:
            metadata["tokens_per_second"] = round(metadata["eval_count"] / elapsed, 2)
        return InferenceResult(text=text, backend=self.name, model=self.model, duration_seconds=elapsed, metadata=metadata)


class TransformersBackend:
    """Loads the same 3B instruction model directly on the GPU."""

    name = "transformers"

    def __init__(
        self,
        model_id: str = DEFAULT_TRANSFORMERS_MODEL,
        *,
        max_new_tokens: int = 2048,
        device: str = "cuda",
        temperature: Optional[float] = None,
    ) -> None:
        self.model_id = model_id
        self.max_new_tokens = max_new_tokens
        self.device = device
        self.temperature = temperature
        self._pipeline = None

    def available(self) -> Tuple[bool, str]:
        try:
            import transformers  # noqa: F401
            import torch
        except ImportError as error:
            return False, f"transformers stack unavailable: {error}"
        if self.device == "cuda" and not torch.cuda.is_available():
            return False, "CUDA is not available to torch"
        return True, f"transformers ready with {self.model_id}"

    def _load(self):
        if self._pipeline is not None:
            return self._pipeline
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

        tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
            device_map="auto" if self.device == "cuda" else None,
        )
        self._pipeline = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            device_map="auto" if self.device == "cuda" else None,
        )
        return self._pipeline

    def generate(self, system: str, user: str) -> InferenceResult:
        generator = self._load()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        started = time.perf_counter()
        output = generator(
            messages,
            max_new_tokens=self.max_new_tokens,
            do_sample=self.temperature is not None,
            temperature=self.temperature if self.temperature is not None else None,
            return_full_text=False,
        )
        elapsed = time.perf_counter() - started
        text = ""
        if isinstance(output, list) and output:
            first = output[0]
            if isinstance(first, Mapping):
                generated = first.get("generated_text")
                if isinstance(generated, list) and generated:
                    text = str(generated[-1].get("content", ""))
                elif isinstance(generated, str):
                    text = generated
        if not text.strip():
            raise AnalysisError("transformers backend returned an empty generation")
        return InferenceResult(
            text=text,
            backend=self.name,
            model=self.model_id,
            duration_seconds=elapsed,
            metadata={"max_new_tokens": self.max_new_tokens, "device": self.device},
        )


def build_backend(
    *,
    backend: str = "auto",
    model: str = DEFAULT_MODEL,
    base_url: str = "http://127.0.0.1:11434",
    transformers_model: str = DEFAULT_TRANSFORMERS_MODEL,
    device: str = "cuda",
    num_ctx: int = 8192,
    timeout_seconds: float = 900.0,
    temperature: Optional[float] = None,
):
    """Return a usable backend.

    ``auto`` prefers Ollama (parity with production) and falls back to
    transformers, reporting which one it chose and why.
    """
    notes: List[str] = []
    candidates = []
    if backend in ("auto", "ollama"):
        candidates.append(
            OllamaBackend(model, base_url, num_ctx=num_ctx, timeout_seconds=timeout_seconds, temperature=temperature)
        )
    if backend in ("auto", "transformers"):
        candidates.append(
            TransformersBackend(
                transformers_model, device=device, temperature=temperature
            )
        )

    for candidate in candidates:
        ok, detail = candidate.available()
        notes.append(f"{candidate.name}: {detail}")
        if ok:
            return candidate, notes
    raise AnalysisError("no usable local inference backend — " + " | ".join(notes))


def run_analysis(
    transcript: Mapping[str, Any],
    *,
    backend,
    bounds: ClipBounds = ClipBounds(),
    source_duration_seconds: Optional[float] = None,
    max_segments: Optional[int] = None,
    window: Optional[Tuple[int, int]] = None,
    clip_signals: Optional[str] = None,
    performance_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one single-window analysis and validate the reply.

    One repair attempt is made when the reply cannot be parsed, using the same
    prompts plus an explicit correction instruction; after that the failure is
    reported rather than retried indefinitely.
    """
    lines = format_transcript_lines(transcript)
    if not lines:
        raise AnalysisError("transcript is empty; nothing to analyse")
    formatted = "\n".join(lines)

    system = build_system_prompt(bounds)
    user = build_transcript_user_prompt(
        formatted,
        window=window,
        clip_signals=clip_signals,
        performance_context=performance_context,
        bounds=bounds,
    )

    attempts: List[Dict[str, Any]] = []
    inference = backend.generate(system, user)
    attempts.append({"attempt": 1, "seconds": round(inference.duration_seconds, 3), "chars": len(inference.text)})

    try:
        payload = extract_json_object(inference.text)
        analysis = validate_analysis(
            payload, bounds=bounds, source_duration_seconds=source_duration_seconds, max_segments=max_segments
        )
    except AnalysisError as first_error:
        repair_instruction = (
            "\n\nYour previous reply could not be parsed as the required JSON object. "
            "Return ONLY the JSON object with the keys described above — no prose, no code fences."
        )
        inference = backend.generate(system, user + repair_instruction)
        attempts.append({"attempt": 2, "seconds": round(inference.duration_seconds, 3), "chars": len(inference.text)})
        try:
            payload = extract_json_object(inference.text)
            analysis = validate_analysis(
                payload, bounds=bounds, source_duration_seconds=source_duration_seconds, max_segments=max_segments
            )
            analysis.warnings.append(f"required a repair attempt after: {first_error}")
        except AnalysisError as second_error:
            raise AnalysisError(
                f"model reply was unusable after a repair attempt: {second_error}"
            ) from second_error

    return {
        "backend": inference.backend,
        "model": inference.model,
        "inference_metadata": inference.metadata,
        "attempts": attempts,
        "transcript_lines": len(lines),
        "prompt_chars": len(system) + len(user),
        "analysis": analysis.as_dict(),
        "segments": analysis.segments,
        "rejected_segments": analysis.rejected,
        "summary": analysis.summary,
        "key_topics": analysis.key_topics,
        "bounds": {
            "minimum": bounds.minimum,
            "maximum": bounds.maximum,
            "ideal_minimum": bounds.ideal_minimum,
            "ideal_maximum": bounds.ideal_maximum,
        },
    }
