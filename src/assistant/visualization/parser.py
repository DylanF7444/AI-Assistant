from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from assistant.router.normalization import normalize_text, spoken_number

VisualRequestKind = Literal["chart", "flowchart", "timeline", "table"]


@dataclass(frozen=True)
class ParsedVisualRequest:
    """A visual request parsed into the stable skill contract."""

    kind: VisualRequestKind
    title: str
    spec: dict[str, Any] | None = None
    clarification: str | None = None


_KIND_PATTERNS: tuple[tuple[VisualRequestKind, re.Pattern[str]], ...] = (
    (
        "flowchart",
        re.compile(
            r"\b(?:flow\s*chart|process\s+(?:flow|map)|workflow(?:\s+diagram)?)\b",
            re.I,
        ),
    ),
    ("timeline", re.compile(r"\btimeline\b", re.I)),
    ("table", re.compile(r"\btable\b", re.I)),
    ("chart", re.compile(r"\b(?:chart|graph|plot)\b", re.I)),
    ("flowchart", re.compile(r"\bdiagram\b", re.I)),
)
_REQUEST_ACTION = re.compile(
    r"\b(?:build|chart|convert|create|display|draw|generate|give|graph|make|plot|"
    r"present|put|render|show|turn|visuali[sz]e)\b",
    re.I,
)
_REQUEST_DESIRE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\b|"
    r"(?:can|could)\s+i\s+(?:get|have|see)\b|"
    r"i(?:'d|\s+would)\s+like\b|i\s+want\b)",
    re.I,
)
_EXPLANATORY_START = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:explain|"
    r"show\s+me\s+how|tell\s+me\s+(?:about|how|what|why)|"
    r"how\s+(?:does|do|is|are|can|would|to)|what\s+(?:does|do|is|are|chart|diagram|"
    r"flowchart|table|timeline)|why\s+(?:does|do|is|are))\b",
    re.I,
)
_MEDIA_REFERENCE = re.compile(
    r"(?:\b[a-z]:[\\/]|https?://|file://|\.(?:avif|bmp|gif|jpe?g|m4a|mkv|mov|mp3|mp4|"
    r"ogg|png|svg|wav|webm|webp)\b)",
    re.I,
)
_POLITE_PREFIX = re.compile(
    r"^\s*(?:(?:please|kindly)\s+)?(?:"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"(?:can|could)\s+i\s+(?:get|have|see)\s+|"
    r"i(?:'d|\s+would)\s+like\s+(?:(?:you\s+)?to\s+)?(?:see\s+)?|"
    r"i\s+want\s+(?:(?:you\s+)?to\s+)?(?:see\s+)?)?",
    re.I,
)
_NUMBER_WORDS = {
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
}
_SMALL_NUMBER_WORDS = {
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
}
_TENS_NUMBER_WORDS = {
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
}
_PERIOD_LABELS = {
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "december",
    "mon",
    "monday",
    "tue",
    "tuesday",
    "wed",
    "wednesday",
    "thu",
    "thursday",
    "fri",
    "friday",
    "sat",
    "saturday",
    "sun",
    "sunday",
    "q1",
    "q2",
    "q3",
    "q4",
}
_TOKEN_RE = re.compile(r"-?\d+(?:\.\d+)?|[A-Za-z]+(?:-[A-Za-z]+)?|q[1-4]", re.I)
_SEPARATOR_WORDS = {
    "and",
    "are",
    "at",
    "equal",
    "equals",
    "is",
    "was",
    "were",
    "value",
    "values",
}
_VALUE_UNIT_WORDS = {
    "degree",
    "degrees",
    "dollar",
    "dollars",
    "percent",
    "percentage",
    "points",
    "units",
}


def visual_request_kind(text: str) -> VisualRequestKind | None:
    """Return the requested visual family without claiming explanatory/media requests."""

    normalized = normalize_text(text)
    if _EXPLANATORY_START.search(normalized) or _MEDIA_REFERENCE.search(normalized):
        return None
    if re.search(r"\b(?:draw|map|sketch)\b.*\bflow\b", normalized):
        return "flowchart"
    for kind, pattern in _KIND_PATTERNS:
        match = pattern.search(normalized)
        if not match:
            continue
        before = normalized[: match.start()]
        after = normalized[match.end() :]
        if (
            _REQUEST_ACTION.search(normalized)
            or _REQUEST_DESIRE.search(normalized)
            or not before.strip()
            or re.search(r"\b(?:as|in|into)\s+(?:a|an|the)?\s*$", before)
            or re.match(r"^\s*(?:of|showing|with)\b", after)
        ):
            return kind
    return None


def parse_visual_request(
    text: str,
    *,
    kind: VisualRequestKind | None = None,
) -> ParsedVisualRequest | None:
    """Parse typed or dictated visual language into a local typed specification."""

    selected_kind = kind or visual_request_kind(text)
    if selected_kind is None:
        return None
    body = _normalize_spoken_visual_syntax(_request_body(text, selected_kind))
    if selected_kind == "chart":
        return _parse_chart(text, body)
    if selected_kind == "flowchart":
        return _parse_flowchart(body)
    if selected_kind == "timeline":
        return _parse_timeline(body)
    return _parse_table(body)


def _request_body(text: str, kind: VisualRequestKind) -> str:
    value = _POLITE_PREFIX.sub("", text, count=1).strip()
    noun = {
        "chart": r"(?:(?:bar|line|trend)\s+)?(?:chart|graph|plot)",
        "flowchart": (
            r"(?:flow\s*chart|diagram|process\s+(?:flow|map)|workflow(?:\s+diagram)?|"
            r"(?:[A-Za-z]+\s+)?flow)"
        ),
        "timeline": r"timeline",
        "table": r"table",
    }[kind]
    transform = re.match(
        rf"^\s*(?:convert|put|show|transform|turn|visuali[sz]e)\s+(.+?)\s+"
        rf"(?:as|in|into|to)\s+(?:a|an|the)?\s*{noun}\s*[.!?]*$",
        value,
        flags=re.I,
    )
    if transform:
        return transform.group(1).strip(" \t:;,.!?")
    prefix = re.match(
        rf"^\s*(?:(?:build|create|display|draw|generate|give|make|present|render|show)"
        rf"(?:\s+(?:me|us))?(?:\s+(?:a|an|the))?\s+{noun}|"
        rf"(?:visuali[sz]e)(?:\s+(?:a|an|the))?\s+{noun}|"
        rf"(?:(?:a|an|the)\s+)?{noun}|"
        rf"(?:plot|graph)\b)",
        value,
        flags=re.I,
    )
    if prefix:
        value = value[prefix.end() :]
    value = re.sub(
        r"^\s*(?:(?:that\s+)?show(?:s|ing)?|containing|from|of|using|with)\b",
        "",
        value,
        count=1,
        flags=re.I,
    )
    return value.strip(" \t:;,.!?")


def _normalize_spoken_visual_syntax(text: str) -> str:
    """Translate punctuation words commonly emitted by speech recognition."""

    value = re.sub(r"\b([ap])\s+\.?m\.?\b", r"\1m", text, flags=re.I)
    replacements = (
        (r"\b(?:vertical\s+bar|pipe)\b", "|"),
        (r"\bsemi[ -]?colon\b", ";"),
        (r"\bcolon\b", ":"),
        (r"\b(?:right\s+)?arrow\b", "->"),
        (r"\bequals?\s+sign\b", "="),
        (r"\b(?:new\s+row|new\s+line)\b", ";"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.I)
    return value


def _parse_chart(original: str, body: str) -> ParsedVisualRequest:
    explicit_title, values_text = _split_title(body, "chart")
    tokens = _tokens(values_text)
    spans = _number_spans(tokens)
    if not spans:
        return _clarification(
            "chart",
            "What values should the chart show? You can type or say labels and values, "
            "such as January twelve, February eighteen, March fifteen.",
        )

    values = [span[2] for span in spans]
    raw_labels: list[list[str]] = []
    previous_end = 0
    for start, end, _ in spans:
        label = _clean_words(token[0] for token in tokens[previous_end:start])
        while label and label[0].casefold() in _VALUE_UNIT_WORDS:
            label.pop(0)
        raw_labels.append(label)
        previous_end = end
    trailing = _clean_words(token[0] for token in tokens[previous_end:])
    if trailing and all(word.casefold() in _VALUE_UNIT_WORDS for word in trailing):
        trailing = []
    if trailing:
        return _clarification(
            "chart",
            f'I could not tell which value belongs to "{" ".join(trailing)}". '
            "Please put each label before its value when typing or speaking.",
        )

    inferred_title: str | None = None
    if raw_labels and raw_labels[0]:
        inferred_title, raw_labels[0] = _separate_chart_title(raw_labels)
    if all(not label for label in raw_labels):
        labels = [str(index) for index in range(1, len(values) + 1)]
    elif all(label for label in raw_labels):
        labels = [" ".join(label).strip() for label in raw_labels]
    else:
        return _clarification(
            "chart",
            "I found some unlabeled values. Please give every value a label, or provide only "
            "the values and I will number them.",
        )

    chart_type = "line" if re.search(r"\b(?:line|trend)\b", original, re.I) else "bar"
    title = explicit_title or inferred_title or ("Line chart" if chart_type == "line" else "Chart")
    return ParsedVisualRequest(
        kind="chart",
        title=title,
        spec={
            "kind": "chart",
            "chart_type": chart_type,
            "labels": tuple(labels),
            "values": tuple(values),
        },
    )


def _separate_chart_title(labels: list[list[str]]) -> tuple[str | None, list[str]]:
    first = labels[0]
    lowered = [word.casefold() for word in first]
    if "for" in lowered:
        split = len(lowered) - 1 - lowered[::-1].index("for")
        if split and split < len(first) - 1:
            return " ".join(first[:split]), first[split + 1 :]
    if "in" in lowered:
        split = len(lowered) - 1 - lowered[::-1].index("in")
        if split and split < len(first) - 1 and first[-1].casefold() in _PERIOD_LABELS:
            return " ".join(first[:split]), first[split + 1 :]
    if len(first) > 1 and first[-1].casefold() in _PERIOD_LABELS:
        return " ".join(first[:-1]), first[-1:]
    later_lengths = [len(label) for label in labels[1:] if label]
    if later_lengths and len(set(later_lengths)) == 1 and len(first) > later_lengths[0]:
        width = later_lengths[0]
        return " ".join(first[:-width]), first[-width:]
    return None, first


def _parse_flowchart(body: str) -> ParsedVisualRequest:
    title, steps_text = _split_title(body, "flowchart")
    steps_text = re.sub(r"^\s*(?:steps?|stages?|process)\b", "", steps_text, flags=re.I).strip()
    branch = _branching_flowchart(title, steps_text)
    if branch is not None:
        return branch
    labels = [
        item.strip(" \t\"'")
        for item in re.split(
            r"\s*(?:->|→|;|,|\bthen\b|\bnext\b|\bafter\s+that\b|\bfollowed\s+by\b)\s*",
            steps_text,
            flags=re.I,
        )
        if item.strip(" \t\"'")
    ]
    if len(labels) < 2:
        words = [token[0] for token in _tokens(steps_text)]
        if 2 <= len(words) <= 12 and all(_is_simple_step(word) for word in words):
            labels = words
    if len(labels) < 2:
        return _clarification(
            "flowchart",
            "What steps should the diagram connect? You can say, for example, plan then "
            "build then test then deploy.",
        )
    nodes = tuple(
        {"node_id": f"node-{index}", "label": label} for index, label in enumerate(labels, 1)
    )
    edges = tuple(
        {"source": f"node-{index}", "target": f"node-{index + 1}"} for index in range(1, len(nodes))
    )
    return ParsedVisualRequest(
        kind="flowchart",
        title=title or "Flowchart",
        spec={"kind": "flowchart", "nodes": nodes, "edges": edges},
    )


def _branching_flowchart(title: str | None, text: str) -> ParsedVisualRequest | None:
    match = re.match(
        r"^(?P<prefix>.*?)\bif\s+(?P<decision>.+?)\s+then\s+"
        r"(?P<yes>.+?)\s+(?:otherwise|else)\s+(?P<no>.+)$",
        text,
        flags=re.I,
    )
    if not match:
        return None
    prefix = match.group("prefix").strip(" \t,;:-")
    prefix_labels = [
        item.strip(" \t\"'")
        for item in re.split(
            r"\s*(?:->|→|;|,|\bthen\b|\bnext\b)\s*",
            prefix,
            flags=re.I,
        )
        if item.strip(" \t\"'")
    ]
    decision = match.group("decision").strip(" \t\"'?,")
    yes_label = match.group("yes").strip(" \t\"',")
    no_label = match.group("no").strip(" \t\"',")
    labels = [*prefix_labels, f"{decision}?", yes_label, no_label]
    nodes = tuple(
        {"node_id": f"node-{index}", "label": label} for index, label in enumerate(labels, 1)
    )
    decision_index = len(prefix_labels) + 1
    edges = [
        {"source": f"node-{index}", "target": f"node-{index + 1}"}
        for index in range(1, decision_index)
    ]
    edges.extend(
        (
            {
                "source": f"node-{decision_index}",
                "target": f"node-{decision_index + 1}",
                "label": "Yes",
            },
            {
                "source": f"node-{decision_index}",
                "target": f"node-{decision_index + 2}",
                "label": "No",
            },
        )
    )
    return ParsedVisualRequest(
        kind="flowchart",
        title=title or "Flowchart",
        spec={"kind": "flowchart", "nodes": nodes, "edges": tuple(edges)},
    )


def _parse_timeline(body: str) -> ParsedVisualRequest:
    title, events_text = _split_title(body, "timeline")
    markers = list(_timeline_markers(events_text))
    events: list[dict[str, str]] = []
    if markers:
        prefix = events_text[: markers[0].start()].strip(" \t:;,-")
        if prefix and title is None:
            title = re.sub(r"^(?:for|of)\s+", "", prefix, flags=re.I).strip()
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(events_text)
            label = events_text[marker.end() : end]
            label = re.sub(r"^\s*(?:-|–|—|:|at\b|then\b|and\b)+", "", label, flags=re.I)
            label = re.sub(r"\s*(?:;|,|then|and)\s*$", "", label, flags=re.I).strip()
            if not label:
                return _clarification(
                    "timeline",
                    f'What happened at "{marker.group(0).strip()}"?',
                )
            events.append({"when": marker.group(0).strip(), "label": label})
    else:
        pieces = [
            item.strip(" \t\"'")
            for item in re.split(r"\s*(?:;|\||\bthen\b|\bnext\b)\s*", events_text, flags=re.I)
            if item.strip(" \t\"'")
        ]
        if len(pieces) > 1:
            events = [{"when": str(index), "label": item} for index, item in enumerate(pieces, 1)]
    if not events:
        return _clarification(
            "timeline",
            "What events belong on the timeline? You can say, for example, nine AM standup, "
            "ten AM build, two PM review.",
        )
    return ParsedVisualRequest(
        kind="timeline",
        title=title or "Timeline",
        spec={"kind": "timeline", "events": tuple(events)},
    )


def _parse_table(body: str) -> ParsedVisualRequest:
    title, table_text = _split_title(body, "table")
    punctuated = _punctuated_table(table_text)
    if punctuated is not None:
        columns, rows = punctuated
        return _table_result(title, columns, rows)

    keyword_match = re.search(r"\bcolumns?\b(.+?)\brows?\b(.+)$", table_text, flags=re.I)
    if keyword_match:
        columns = _field_words(keyword_match.group(1))
        row_text = keyword_match.group(2)
        rows = _voice_rows(row_text, len(columns))
        if columns and rows:
            return _table_result(title, columns, rows)

    return _clarification(
        "table",
        "I need enough information to identify the columns and rows. You can say, for "
        "example, columns name and status, rows Alice ready, Bob waiting.",
    )


def _punctuated_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    if "|" not in text and ";" not in text and "\n" not in text:
        return None
    raw_rows = [item.strip() for item in re.split(r"\s*(?:;|\n)\s*", text) if item.strip()]
    rows: list[list[str]] = []
    for row in raw_rows:
        separator = r"\s*\|\s*" if "|" in row else r"\s*,\s*"
        cells = [cell.strip() for cell in re.split(separator, row) if cell.strip()]
        rows.append(cells)
    if len(rows) < 1 or len(rows[0]) < 1 or any(len(row) != len(rows[0]) for row in rows):
        return None
    return rows[0], rows[1:]


def _table_result(
    title: str | None,
    columns: list[str],
    rows: list[list[str]],
) -> ParsedVisualRequest:
    if not columns or not rows or any(len(row) != len(columns) for row in rows):
        return _clarification(
            "table",
            "The rows do not match the number of columns. Please repeat the column names and "
            "then the row values.",
        )
    return ParsedVisualRequest(
        kind="table",
        title=title or "Table",
        spec={
            "kind": "table",
            "columns": tuple(columns),
            "rows": tuple(tuple(row) for row in rows),
        },
    )


def _voice_rows(text: str, width: int) -> list[list[str]]:
    if width < 1:
        return []
    explicit = [
        _field_words(part)
        for part in re.split(r"\b(?:next\s+row|row|then)\b|;", text, flags=re.I)
        if _field_words(part)
    ]
    if len(explicit) > 1:
        return explicit if all(len(row) == width for row in explicit) else []
    return _chunks(_field_words(text), width)


def _field_words(text: str) -> list[str]:
    value = re.sub(r"\b(?:and|are|is)\b", " ", text, flags=re.I)
    return [item.strip(" \t\"'") for item in re.split(r"[,|\s]+", value) if item.strip(" \t\"'")]


def _chunks(items: list[str], width: int) -> list[list[str]]:
    if width < 1 or len(items) % width:
        return []
    return [items[index : index + width] for index in range(0, len(items), width)]


def _split_title(text: str, kind: VisualRequestKind) -> tuple[str | None, str]:
    for match in re.finditer(":", text):
        before = text[: match.start()].strip(" \t\"'")
        after = text[match.end() :].strip()
        if not before or not after:
            continue
        if kind == "chart":
            after_tokens = _tokens(after)
            starts_with_value = bool(after_tokens and _number_spans(after_tokens[:1]))
            if len(_tokens(before)) < 2 or starts_with_value:
                continue
        if kind == "timeline" and re.search(r"\d$", before) and re.match(r"\d", after):
            continue
        return before, after
    return None, text.strip()


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(match.group(0), match.start(), match.end()) for match in _TOKEN_RE.finditer(text)]


def _number_spans(tokens: list[tuple[str, int, int]]) -> list[tuple[int, int, float]]:
    spans: list[tuple[int, int, float]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index][0].casefold()
        try:
            if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
                spans.append((index, index + 1, float(token)))
                index += 1
                continue
            consumed = _consume_spoken_number(tokens, index)
            if consumed:
                end, phrase = consumed
                spans.append((index, end, _spoken_value(phrase)))
                index = end
                continue
        except ValueError:
            pass
        index += 1
    return spans


def _consume_spoken_number(
    tokens: list[tuple[str, int, int]],
    start: int,
) -> tuple[int, str] | None:
    index = start
    words: list[str] = []
    if index < len(tokens) and tokens[index][0].casefold() in {"minus", "negative"}:
        words.append(tokens[index][0].casefold())
        index += 1
    under_end = _consume_under_thousand(tokens, index)
    if under_end == index:
        return None
    words.extend(token[0].casefold() for token in tokens[index:under_end])
    index = under_end
    if index < len(tokens) and tokens[index][0].casefold() == "thousand":
        words.append("thousand")
        index += 1
        if index < len(tokens) and tokens[index][0].casefold() == "and":
            words.append("and")
            index += 1
        remainder_end = _consume_under_thousand(tokens, index)
        words.extend(token[0].casefold() for token in tokens[index:remainder_end])
        index = remainder_end
    if index < len(tokens) and tokens[index][0].casefold() == "point":
        decimal_start = index
        words.append("point")
        index += 1
        while index < len(tokens) and tokens[index][0].casefold() in _SMALL_NUMBER_WORDS:
            words.append(tokens[index][0].casefold())
            index += 1
        if index == decimal_start + 1:
            index = decimal_start
            words.pop()
    return index, " ".join(words)


def _consume_under_thousand(tokens: list[tuple[str, int, int]], start: int) -> int:
    index = _consume_under_hundred(tokens, start)
    if index == start:
        return start
    if index < len(tokens) and tokens[index][0].casefold() == "hundred":
        index += 1
        if index < len(tokens) and tokens[index][0].casefold() == "and":
            index += 1
        remainder = _consume_under_hundred(tokens, index)
        if remainder != index:
            index = remainder
    return index


def _consume_under_hundred(tokens: list[tuple[str, int, int]], start: int) -> int:
    if start >= len(tokens):
        return start
    word = tokens[start][0].casefold()
    if word not in _NUMBER_WORDS:
        return start
    index = start + 1
    if (
        word in _TENS_NUMBER_WORDS
        and index < len(tokens)
        and tokens[index][0].casefold() in _SMALL_NUMBER_WORDS
    ):
        index += 1
    return index


def _spoken_value(phrase: str) -> float:
    sign = -1.0 if phrase.startswith(("minus ", "negative ")) else 1.0
    unsigned = re.sub(r"^(?:minus|negative)\s+", "", phrase)
    if " point " not in unsigned:
        return sign * spoken_number(unsigned)
    whole, decimal = unsigned.split(" point ", 1)
    digits = "".join(str(int(spoken_number(word))) for word in decimal.split())
    return sign * float(f"{int(spoken_number(whole))}.{digits}")


def _clean_words(words) -> list[str]:
    result = [word.strip(" \t\"'") for word in words if word.strip(" \t\"'")]
    while result and result[0].casefold() in _SEPARATOR_WORDS:
        result.pop(0)
    while result and result[-1].casefold() in _SEPARATOR_WORDS:
        result.pop()
    return result


def _is_simple_step(word: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z-]*", word)) and word.casefold() not in {
        "a",
        "an",
        "and",
        "the",
    }


_CLOCK_MARKER = (
    r"(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"(?:[ -](?:fifteen|thirty|forty[ -]five))?\s+(?:a\.?m\.?|p\.?m\.?)"
)
_MONTH_MARKER = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"(?:\s+(?:\d{1,2}|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
    r"tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    r"eighteenth|nineteenth|twentieth|twenty[ -](?:first|second|third|fourth|fifth|"
    r"sixth|seventh|eighth|ninth)|thirtieth|thirty[ -]first))?(?:,?\s+\d{4})?"
)
_TIMELINE_MARKER_RE = re.compile(
    rf"\b(?:{_CLOCK_MARKER}|\d{{4}}-\d{{2}}-\d{{2}}|{_MONTH_MARKER}|\d{{4}})\b",
    re.I,
)


def _timeline_markers(text: str):
    return _TIMELINE_MARKER_RE.finditer(text)


def _clarification(kind: VisualRequestKind, message: str) -> ParsedVisualRequest:
    return ParsedVisualRequest(
        kind=kind,
        title={
            "chart": "Chart",
            "flowchart": "Flowchart",
            "timeline": "Timeline",
            "table": "Table",
        }[kind],
        clarification=message,
    )
