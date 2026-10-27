"""Deterministic safeguards shared by source extraction and autonomous review."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Normalize whitespace and case for conservative exact-substring checks."""
    return _SPACE_RE.sub(" ", str(value or "")).strip().casefold()


def quote_is_supported(text: str, evidence: str, *, min_chars: int = 12) -> bool:
    evidence_norm = normalize_text(evidence)
    text_norm = normalize_text(text)
    return len(evidence_norm) >= min_chars and evidence_norm in text_norm


def entity_is_mentioned(text: str, name: str) -> bool:
    """Require a whole entity-name match to avoid false positives like SAP/SAPUI5."""
    norm_name = normalize_text(name)
    if len(norm_name) < 2:
        return False
    # Whitespace has already been normalized; word boundaries prevent matching a
    # short entity name inside a longer identifier.
    pattern = r"(?<!\w)" + re.escape(norm_name) + r"(?!\w)"
    return re.search(pattern, normalize_text(text), flags=re.UNICODE) is not None


def _entity_name_is_too_generic(name: str) -> bool:
    normalized = normalize_text(name)
    return len(normalized) < 3 or re.fullmatch(r"[\d.\-\s]+", normalized) is not None


def _entities_are_near_evidence(text: str, source: str, target: str, evidence: str) -> bool:
    if entity_is_mentioned(evidence, source) and entity_is_mentioned(evidence, target):
        return True

    evidence_words = str(evidence or "").split()
    if not evidence_words:
        return False
    pattern = r"\s+".join(re.escape(word) for word in evidence_words)
    match = re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE)
    if match is None:
        return False

    nearby = text[max(0, match.start() - 300):min(len(text), match.end() + 300)]
    lines = re.split(r"[\r\n]+", nearby)
    contexts = lines + [
        sentence
        for line in lines
        for sentence in re.split(r"(?<=[.!?])\s+", line)
    ]
    return any(
        entity_is_mentioned(context, source) and entity_is_mentioned(context, target)
        for context in contexts
    )


def parse_confidence(value: Any, default: float = 0.0) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    if confidence != confidence:  # NaN
        return default
    return min(1.0, max(0.0, confidence))


def normalize_polarity(value: Any) -> str:
    raw = str(value or "").strip().casefold().replace("-", "_")
    if raw in {"positive", "affirmed", "affirmative", "supports", "true", "yes", "bejaht", "positiv"}:
        return "positive"
    if raw in {"negative", "refutes", "refuted", "negated", "false", "no", "verneint", "negativ", "contradicts", "contradicted"}:
        return "negative"
    return "unknown"


def temporal_validity(valid_from: Any = None, valid_until: Any = None, *, today: date | None = None) -> tuple[bool, str]:
    """Check ISO dates; no dates means unbounded validity."""
    today = today or date.today()
    try:
        starts = date.fromisoformat(str(valid_from)[:10]) if valid_from else None
        ends = date.fromisoformat(str(valid_until)[:10]) if valid_until else None
    except (TypeError, ValueError):
        return False, "invalid_validity_date"
    if starts and starts > today:
        return False, "not_yet_valid"
    if ends and ends < today:
        return False, "expired"
    if starts and ends and starts > ends:
        return False, "invalid_validity_range"
    return True, "valid_period"


def validate_relation_evidence(
    *, text: str, source: str, target: str, evidence: str,
    confidence: Any, polarity: Any, valid_from: Any = None,
    valid_until: Any = None, min_confidence: float = 0.82,
    relation_type: str | None = None,
) -> dict[str, Any]:
    """Return deterministic validation decision; LLM judgement alone is not enough."""
    conf = parse_confidence(confidence)
    pol = normalize_polarity(polarity)
    if not quote_is_supported(text, evidence):
        return {"accepted": False, "reason": "evidence_not_verbatim", "confidence": conf, "polarity": pol}
    if _entity_name_is_too_generic(source) or _entity_name_is_too_generic(target):
        return {"accepted": False, "reason": "entity_name_too_generic", "confidence": conf, "polarity": pol}
    if str(relation_type or "").strip().upper() == "RELATED_TO":
        return {"accepted": False, "reason": "relation_type_too_generic", "confidence": conf, "polarity": pol}
    if not _entities_are_near_evidence(text, source, target, evidence):
        return {"accepted": False, "reason": "entities_not_near_evidence", "confidence": conf, "polarity": pol}
    if pol == "unknown":
        return {"accepted": False, "reason": "polarity_uncertain", "confidence": conf, "polarity": pol}
    if conf < min_confidence:
        return {"accepted": False, "reason": "confidence_below_threshold", "confidence": conf, "polarity": pol}
    valid, reason = temporal_validity(valid_from, valid_until)
    if not valid:
        return {"accepted": False, "reason": reason, "confidence": conf, "polarity": pol}
    return {"accepted": True, "reason": "evidence_validated", "confidence": conf, "polarity": pol}
