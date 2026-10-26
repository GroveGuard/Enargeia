from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

from .config import Settings
from .quality import normalize_polarity, parse_confidence

log = logging.getLogger(__name__)

PROMPT_VERSION = "entity-extraction-v2-quality"
COMMUNITY_SUMMARY_PROMPT_VERSION = "community-summary-v1"
GLOBAL_ANSWER_PROMPT_VERSION = "global-search-answer-v1"

SYSTEM_PROMPT = """Du extrahierst einen vorsichtigen Knowledge Graph aus bereitgestelltem Text.
Erfinde nichts. Extrahiere nur explizit genannte oder eindeutig belegte Dinge.
Antworte ausschließlich als gültiges JSON-Objekt mit genau diesen Feldern:
{
  "entities": [{"name": "...", "type": "Person|Organisation|System|Projekt|Datei|Prozess|Ort|Produkt|Konzept|Sonstiges", "description": "kurze textnahe Beschreibung"}],
  "relations": [{"source": "Entitätsname", "type": "USES|DEPENDS_ON|PART_OF|IMPLEMENTS|REFERENCES|CALLS|RELATED_TO|LOCATED_IN|CREATED_BY|OWNED_BY", "target": "Entitätsname", "evidence": "wörtliches, zusammenhängendes Zitat aus dem Text", "confidence": 0.0, "polarity": "positive|negative|uncertain", "valid_from": "YYYY-MM-DD oder leer", "valid_until": "YYYY-MM-DD oder leer"}]
}
Maximal 20 Entitäten und 20 Beziehungen. Nur Entitäten verwenden, die im Text vorkommen.
Für jede Beziehung muss `evidence` ein wörtliches Zitat aus dem Eingabetext sein (keine Paraphrase).
`confidence` ist eine vorsichtige Einschätzung von 0 bis 1; sie ersetzt NICHT den Wortlautbeleg.
`polarity` ist negative, wenn der Text die Beziehung verneint oder widerlegt; uncertain bei Mehrdeutigkeit.
Zeitgrenzen nur nennen, wenn sie im Text explizit belegt sind. Gemeinsame Erwähnung allein ist keine Beziehung.
Wenn keine sicheren Ergebnisse vorliegen, gib leere Arrays zurück."""

CANDIDATE_REVIEW_PROMPT = """Du prüfst Beziehungskandidaten für einen Unternehmenswissensgraphen.
Die Kandidaten stammen aus wiederholter gemeinsamer Erwähnung oder struktureller Ähnlichkeit; das ist allein KEIN Beweis.
Nutze ausschließlich die beigefügten Originaltextausschnitte. Bestätige nur direkte, klar formulierte Beziehungen, deren Beleg ein wörtliches, zusammenhängendes Zitat aus einem der Ausschnitte ist und in dem beide Entitäten vorkommen.
Nicht bestätigen bei bloßer gemeinsamer Erwähnung, plausibler Vermutung, indirekter Kausalität, Widerspruch, unklarer Polarität oder fehlendem Beleg.
Antworte ausschließlich als JSON-Objekt: {"results":[{"candidate_id":"...","decision":"accept|reject|defer","predicate":"kurzes generisches Prädikat","evidence":"wörtliches Zitat oder leer","evidence_chunk_id":"ID der Quelle mit Zitat oder leer","confidence":0.0,"polarity":"positive|negative|uncertain","valid_from":"YYYY-MM-DD oder leer","valid_until":"YYYY-MM-DD oder leer","reason":"kurze Begründung"}]}
Gib für jeden übergebenen candidate_id genau ein Ergebnis zurück. Behalte candidate_id unverändert. Erfinde keine Zitatstellen oder Zeitgrenzen."""


COMMUNITY_SUMMARY_PROMPT = """Du verdichtest einen kleinen Ausschnitt eines quellengebundenen Unternehmenswissensgraphen.
Erfinde keine Fakten, Ursachen, Abhängigkeiten oder Architekturentscheidungen. Nutze ausschließlich Mitglieder, bestätigte Beziehungen und Belege aus dem Input.
Eine Community ist nur eine algorithmisch erkannte Strukturgruppe, keine Garantie für ein zusammenhängendes Thema. Formuliere diese Unsicherheit transparent, wenn die Belege dünn sind.
Unterscheide bestätigte Beziehungen von bloßen Entitätsbeschreibungen. Negative oder widersprüchliche Belege müssen in conflicts/caveats erscheinen und dürfen nicht zu einer eindeutigen Behauptung zusammengezogen werden.
Keine neuen Entitäten oder Beziehungen vorschlagen. Keine internen IDs in den Text übernehmen.
Antworte ausschließlich als gültiges JSON-Objekt mit dem Feld results. Für jede community_id genau ein Objekt:
{"results":[{"community_id":"unverändert","title":"kurzer präziser Titel","summary":"Zusammenfassung mit 2-5 Sätzen","key_points":["belegte Kernaussage"],"conflicts":["Widerspruch oder Unsicherheit"],"caveats":["Begrenzung der Aussage"]}]}
summary maximal 900 Zeichen, title maximal 120 Zeichen, key_points maximal 5, conflicts/caveats maximal 4. Wenn kein belastbarer fachlicher Zusammenhang erkennbar ist, sage dies offen und bleibe beschreibend."""

GLOBAL_ANSWER_PROMPT = """Du beantwortest eine Nutzerfrage mit einer Kombination aus Community-Zusammenfassungen und Originaltextabschnitten eines Unternehmenswissensgraphen.
Nutze ausschließlich den bereitgestellten Kontext. Erfinde keine Fakten oder Beziehungen. Community summaries sind abgeleitete Verdichtungen und keine eigenen Beweise; stütze konkrete Aussagen bevorzugt auf Quellen [S1], [S2] usw. Community-Kontext darf mit [C1], [C2] usw. referenziert werden.
Behaupte keine Einigkeit, wenn Quellen oder Zusammenfassungen Widersprüche/Caveats enthalten. Wenn der Kontext die Frage nicht ausreichend beantwortet, benenne die Lücke klar.
Schreibe auf Deutsch, antworte direkt und strukturiert, maximal ca. 700 Wörter. Verwende die verfügbaren Quellenmarker wörtlich und erfinde keine Marker.
Antworte ausschließlich als JSON-Objekt: {"answer":"Antworttext inklusive Quellenmarkern","used_sources":["S1"],"used_communities":["C1"],"uncertainties":["offene Frage oder Konflikt"]}."""



def _parse_json_response(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content)

    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        start = max(0, exc.pos - 200)
        end = min(len(content), exc.pos + 200)
        log.warning(
            "LLM-JSON-Fehler: %s | Antwortausschnitt: %r",
            exc, content[start:end]
        )

        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise

        candidate = match.group(0)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as retry_exc:
            start = max(0, retry_exc.pos - 200)
            end = min(len(candidate), retry_exc.pos + 200)
            log.warning(
                "LLM-JSON-Fallback fehlgeschlagen: %s | Ausschnitt: %r",
                retry_exc, candidate[start:end]
            )
            raise

    if not isinstance(value, dict):
        raise ValueError("LLM-Antwort ist kein JSON-Objekt.")
    value.setdefault("entities", [])
    value.setdefault("relations", [])
    return value


class EntityExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()

    @property
    def cache_signature(self) -> str:
        """Stable signature for safe cache reuse without including API secrets."""
        endpoint = self.settings.llm_base_url.strip().rstrip("/").casefold()
        model = self.settings.llm_model.strip()
        return f"{endpoint}|{model}|{PROMPT_VERSION}"

    def extract(self, text: str) -> dict[str, list[dict[str, str]]]:
        if not self.settings.llm_base_url or not self.settings.llm_model:
            raise RuntimeError("Für Entity-Extraktion müssen LLM_BASE_URL und LLM_MODEL konfiguriert sein.")
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        started = time.monotonic()
        response = self.session.post(
            f"{self.settings.llm_base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.settings.llm_model,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text[:10000]},
                ],
            },
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        log.info(
            "LLM-Antwortzeit Entity-Extraktion: %.1f s (Modell %s)",
            time.monotonic() - started,
            self.settings.llm_model,
        )
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        result = _parse_json_response(content)

        allowed_types = {"Person", "Organisation", "System", "Projekt", "Datei", "Prozess", "Ort", "Produkt", "Konzept", "Sonstiges"}
        entities: list[dict[str, str]] = []
        by_name: dict[str, str] = {}
        for item in result.get("entities", [])[:20]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()[:200]
            if not name:
                continue
            entity_type = str(item.get("type", "Sonstiges")).strip()
            if entity_type not in allowed_types:
                entity_type = "Sonstiges"
            description = str(item.get("description", "")).strip()[:700]
            entities.append({"name": name, "type": entity_type, "description": description})
            by_name[name.casefold()] = name

        relations: list[dict[str, Any]] = []
        allowed_relations = {"USES", "DEPENDS_ON", "PART_OF", "IMPLEMENTS", "REFERENCES", "CALLS", "RELATED_TO", "LOCATED_IN", "CREATED_BY", "OWNED_BY"}
        for item in result.get("relations", [])[:20]:
            if not isinstance(item, dict):
                continue
            source = by_name.get(str(item.get("source", "")).strip().casefold())
            target = by_name.get(str(item.get("target", "")).strip().casefold())
            relation_type = str(item.get("type", "RELATED_TO")).strip().upper()
            evidence = str(item.get("evidence", "")).strip()[:500]
            if source and target and source != target and evidence:
                try:
                    confidence = float(item.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                relations.append({
                    "source": source,
                    "target": target,
                    "type": relation_type if relation_type in allowed_relations else "RELATED_TO",
                    "evidence": evidence,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "polarity": normalize_polarity(item.get("polarity")),
                    "valid_from": str(item.get("valid_from", "")).strip()[:10],
                    "valid_until": str(item.get("valid_until", "")).strip()[:10],
                })
        return {"entities": entities, "relations": relations}

    def _chat_json(self, system_prompt: str, user_payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        if not self.settings.llm_base_url or not self.settings.llm_model:
            raise RuntimeError("LLM_BASE_URL und LLM_MODEL müssen konfiguriert sein.")
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        response = self.session.post(
            f"{self.settings.llm_base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.settings.llm_model,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            },
            timeout=timeout or self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _parse_json_response(str(content))

    def summarize_communities(self, communities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create bounded, source-grounded descriptions for a small batch of communities."""
        payload: list[dict[str, Any]] = []
        for community in communities[:8]:
            members = []
            for member in (community.get("members") or [])[:10]:
                members.append({
                    "name": str(member.get("name") or "")[:120],
                    "type": str(member.get("type") or "")[:40],
                    "description": str(member.get("description") or "")[:150],
                })
            relations = []
            for relation in (community.get("relations") or [])[:6]:
                relations.append({
                    "source": str(relation.get("source") or "")[:120],
                    "predicate": str(relation.get("predicate") or "RELATED_TO")[:60],
                    "target": str(relation.get("target") or "")[:120],
                    "evidence": str(relation.get("evidence") or "")[:180],
                    "source_path": str(relation.get("source_path") or "")[:160],
                })
            payload.append({
                "community_id": str(community.get("id") or ""),
                "member_count": int(community.get("member_count") or len(members)),
                "members": members,
                "confirmed_relations": relations,
            })
        if not payload:
            return []
        result = self._chat_json(COMMUNITY_SUMMARY_PROMPT, {"communities": payload})
        rows = result.get("results", [])
        if not isinstance(rows, list):
            return []
        allowed_ids = {item["community_id"] for item in payload}
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("community_id") or "")
            if cid not in allowed_ids or cid in seen:
                continue
            seen.add(cid)
            title = str(item.get("title") or "Thematische Wissensgruppe").strip()[:120]
            summary = str(item.get("summary") or "").strip()[:900]
            if len(summary) < 40:
                continue
            def clean_list(name: str, maximum: int, max_chars: int) -> list[str]:
                raw = item.get(name, [])
                if not isinstance(raw, list):
                    return []
                return [str(value).strip()[:max_chars] for value in raw[:maximum] if str(value).strip()]
            cleaned.append({
                "community_id": cid,
                "title": title or "Thematische Wissensgruppe",
                "summary": summary,
                "key_points": clean_list("key_points", 5, 400),
                "conflicts": clean_list("conflicts", 4, 300),
                "caveats": clean_list("caveats", 4, 300),
            })
        return cleaned

    def answer_global_query(
        self,
        query: str,
        communities: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Synthesize an answer over community summaries and locally retrieved sources."""
        community_context = []
        for index, item in enumerate(communities[:3], start=1):
            details = item.get("details") or {}
            community_context.append({
                "citation": f"C{index}",
                "title": str(item.get("title") or "")[:120],
                "summary": str(details.get("summary") or item.get("description") or "")[:500],
                "key_points": [str(x)[:160] for x in (details.get("key_points") or [])[:3]],
                "conflicts": [str(x)[:140] for x in (details.get("conflicts") or [])[:2]],
                "caveats": [str(x)[:140] for x in (details.get("caveats") or [])[:2]],
                "members": [str(x)[:80] for x in (details.get("member_names") or [])[:8]],
            })
        source_context = []
        for index, item in enumerate(sources[:4], start=1):
            source_context.append({
                "citation": f"S{index}",
                "path": str(item.get("path") or item.get("filename") or "")[:220],
                "section": str(item.get("section") or "")[:100],
                "text": str(item.get("text") or "")[:650],
            })
        if not community_context and not source_context:
            return {
                "answer": "Für diese Frage wurden weder passende Community-Zusammenfassungen noch passende Quellabschnitte gefunden.",
                "used_sources": [], "used_communities": [],
                "uncertainties": ["Der indexierte Graph enthält für diese Frage keinen ausreichenden Kontext."],
            }
        result = self._chat_json(
            GLOBAL_ANSWER_PROMPT,
            {"question": query[:1000], "community_summaries": community_context, "source_sections": source_context},
        )
        allowed_sources = {item["citation"] for item in source_context}
        allowed_communities = {item["citation"] for item in community_context}
        def valid_ids(name: str, allowed: set[str]) -> list[str]:
            value = result.get(name, [])
            if not isinstance(value, list):
                return []
            return list(dict.fromkeys(str(item) for item in value if str(item) in allowed))
        answer = str(result.get("answer") or "").strip()
        if not answer:
            answer = "Das Modell lieferte keine verwertbare Antwort. Prüfe die unten aufgeführten Originalquellen."
        return {
            "answer": answer[:12000],
            "used_sources": valid_ids("used_sources", allowed_sources),
            "used_communities": valid_ids("used_communities", allowed_communities),
            "uncertainties": [str(x)[:400] for x in result.get("uncertainties", [])[:8]]
            if isinstance(result.get("uncertainties", []), list) else [],
        }

    def review_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Review a small batch of graph candidates in one LLM call.

        This method only asks for a decision. The caller must independently verify
        that every quoted passage exists verbatim in the original source chunk.
        """
        if not candidates:
            return []
        if not self.settings.llm_base_url or not self.settings.llm_model:
            raise RuntimeError("Für die autonome Qualitätsprüfung müssen LLM_BASE_URL und LLM_MODEL konfiguriert sein.")
        payload_candidates = []
        for candidate in candidates:
            sources = []
            for source in candidate.get("sources", [])[:5]:
                sources.append({
                    "chunk_id": str(source.get("id", "")),
                    "path": str(source.get("path", "")),
                    "text": str(source.get("text", ""))[:1800],
                })
            payload_candidates.append({
                "candidate_id": str(candidate.get("id", "")),
                "subject": str(candidate.get("subject_name", "")),
                "subject_type": str(candidate.get("subject_type", "")),
                "object": str(candidate.get("object_name", "")),
                "object_type": str(candidate.get("object_type", "")),
                "candidate_method": str(candidate.get("method", "")),
                "original_reason": str(candidate.get("reason", ""))[:500],
                "sources": sources,
            })
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        response = self.session.post(
            f"{self.settings.llm_base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.settings.llm_model,
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": CANDIDATE_REVIEW_PROMPT},
                    {"role": "user", "content": json.dumps({"candidates": payload_candidates}, ensure_ascii=False)},
                ],
            },
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        result = _parse_json_response(response.json()["choices"][0]["message"]["content"])
        rows = result.get("results", [])
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
