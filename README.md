# Neo4j Company Knowledge Graph

Ein ausführbares Python-Projekt, das Windows-Verzeichnisse rekursiv inventarisiert, lesbare Inhalte extrahiert und mehrere Quellen zu einem nachvollziehbaren Unternehmenswissensgraphen in Neo4j verbindet. Es bietet inkrementelle Synchronisierung, Hybrid Search (Vektor + Volltext), Quellenpfade, optionale LLM-Entity-Extraktion und einen MCP-Server für Agents wie Claude Code, Hermes oder andere MCP-Clients.

## Funktionen

- Rekursiver Scan und inkrementelle Aktualisierung: unveränderte Dateien werden übersprungen; geänderte Dateien erhalten neue Chunks; gelöschte Dateien werden bei einem vollständigen Scan entfernt.
- Strukturgraph: `KGRoot → KGFolder → KGFile → KGChunk`.
- Inhalte und Quellenprovenienz: Chunk-Text, Originalpfad, relativer Pfad, SHA-256 für verarbeitete Inhalte, Änderungszeit, Position im Dokument sowie erkannte PDF-Seite/Überschrift.
- Hybrid Search: Neo4j-Vektorsuche plus Volltextsuche und Dateinamen-Treffer, mit Reciprocal Rank Fusion statt direktem Vergleich unterschiedlicher Scores. Aus den Top-Treffern erweitert eine begrenzte Cypher-Graphsuche über 1–2 Beziehungsschritte die Suche um weitere quellengebundene Abschnitte; das benötigt kein LLM.
- Optionale semantische Graph-Erweiterung: `KGEntity`, `KG_MENTIONS`, quellenbelegte `KGAssertion`-Knoten und `KG_RELATED_TO`-Kanten. Die Extraktion kann über die GUI oder `--extract-entities` aktiviert werden. Aussagen werden nur dann direkt aktiviert, wenn ein wörtlicher Quellbeleg, beide Entitätsnamen, eine bekannte Polarität, ein gültiger Zeitraum und die Mindestkonfidenz geprüft wurden. Nur aktive, konsolidierte `KG_ASSERTED_LINK`-Kanten werden für Graphsuche und Entitäts-GDS-Analysen verwendet.
- Neo4j-first Graph Intelligence: persistente Analyse-Läufe, Findings, exakte Inhaltsidentitäten, prüfbare Beziehungskandidaten, Qualitätsentscheidungen, Lernprofile und konsolidierte Aussagen werden direkt in Neo4j gespeichert. Der automatische Cleanup hält pro Root die letzten 20 Analyse-Läufe vollständig und verdichtet ältere Läufe in einem einzigen historischen Zusammenfassungsknoten.
- **Community Summaries + Global Search:** Nach der GDS-Community-Erkennung kann das LLM quellengebundene Zusammenfassungen geeigneter Communities als stabile `KGInsight`-Knoten speichern. Unveränderte Zusammenfassungen werden anhand eines Inhaltsfingerprints wiederverwendet; pro Analyse werden höchstens acht geänderte Communities in maximal vier Batches zusammengefasst (Standardwerte). Die Global Search kombiniert diese Verdichtungen mit konkreten Suchtreffern und gibt Originalquellenpfade zurück. Summaries sind abgeleitetes Wissen und kein eigenständiger Beweis.
- Autonomer Qualitätszyklus: Graphanalysen prüfen offene Kandidaten in kleinen LLM-Batches. Jede Annahme wird gegen die aktuelle Originalquelle erneut deterministisch verifiziert, bevor eine bestätigte Aussage gespeichert wird. Unsichere Fälle bleiben offen/vertagt; automatische Entscheidungen werden protokolliert und nach Quellenänderungen erneut prüfbar. Manuelle Entscheidungen bleiben als optionale Overrides möglich.
- Neo4j-gestützter LLM-Extraktionscache: erfolgreiche Ergebnisse werden nach Inhaltshash, Chat-Modell-Endpunkt und Prompt-Version in `KGExtractionCache` gespeichert. Identische Chunks können über Dateien und Roots hinweg wiederverwendet werden. Cache-Treffer, Cache-Misses und LLM-Aufrufe werden separat gezählt; der Cache enthält keine API-Schlüssel.
- Optionale Neo4j Graph Data Science (GDS)-Analyse: Community Detection (Leiden oder Louvain), Weakly Connected Components, PageRank und Node Similarity, sofern die jeweilige Prozedur verfügbar ist. Node Similarity erzeugt strukturelle Beziehungskandidaten auf Basis ähnlicher Graphnachbarschaften. Communities, Komponenten und Scores sind erneuerbare Ableitungen; es wird keine zusätzliche persistente Datenbank verwendet. `GDS_MAX_ENTITY_NODES` begrenzt die In-Memory-Projektion.
- MCP-Tools: `search_knowledge`, `global_search_knowledge`, `get_file_context`, `find_related_files`, `get_graph_stats`, `get_graph_capabilities`, `analyze_knowledge_graph`, `list_graph_assertions`, `get_graph_insights`, `list_graph_link_candidates`, `review_graph_link_candidate`, `get_files_needing_deep_analysis`.
- Flet-GUI mit Dashboard, Scan-Steuerung, Live-Protokoll, Hybrid Search, Graph-Explorer und Verbindungsprüfung. Im Dashboard lassen sich indexierte Roots nach Sicherheitsabfrage gezielt aus Neo4j entfernen; Quelldateien auf dem Dateisystem werden dabei nicht gelöscht.
- Fehler einer einzelnen Datei brechen den Gesamtimport nicht ab. Wenn der Scan auf nicht lesbare Ordner stößt, wird die Löschbereinigung vorsichtshalber ausgelassen.
- Ein Scan kann in der GUI angefordert abgebrochen/pausiert werden. Bereits gespeicherte Dateien bleiben erhalten; ein erneuter normaler Scan desselben Roots setzt inkrementell fort.
- `MAX_CHUNKS_PER_FILE` begrenzt, wie viele Textabschnitte pro Datei im Graph gespeichert und eingebettet werden. Standard: 500. Größere Dateien werden am Ende des Limits abgeschnitten, ohne die Originaldatei zu verändern. Die `KGFile`-Metadaten speichern Gesamtanzahl, indexierte/ausgelassene Chunks, Zeichenanzahl und `needs_deep_analysis=true`. Diese Dateien erscheinen im Dashboard als „Analyse erforderlich“, in Suchtreffern/Graph-Explorer mit Warnhinweis und können per MCP-Tool `get_files_needing_deep_analysis` für eine spätere Vollanalyse gefunden werden. Dateien oberhalb `MAX_FILE_SIZE_MB` werden ebenfalls mit `needs_deep_analysis=true` markiert; ihr Inhalt wird nicht gelesen, sondern nur mit Metadaten gespeichert. Das MCP-Tool listet beide Fälle inklusive Status, Größe und Grund. Sie lassen sich außerdem direkt in Neo4j abfragen:

```cypher
MATCH (f:KGFile {needs_deep_analysis: true})
RETURN f.path, f.status, f.size, f.total_chunks, f.indexed_chunks, f.omitted_chunks, f.truncation_reason
ORDER BY CASE WHEN f.status = 'too_large' THEN 0 ELSE 1 END, f.omitted_chunks DESC
```
- Der Fortschritt protokolliert regelmäßig Laufzeit, Durchschnittsgeschwindigkeit und eine grobe Restzeit. Datei und Chunks werden innerhalb einer Neo4j-Transaktion gespeichert.
- Das Programm führt keine Schreiboperationen in den gescannten Quelldateien aus.

## Architektur

```text
Windows-Dateisystem
    ↓ Scanner + Änderungsvergleich
Dateiformat-Extraktion
    ↓ Chunking + optional Embeddings
Neo4j (alle persistenten Daten)
    ├── KGRoot / KGFolder / KGFile / KGChunk
    ├── Neo4j-Volltext- und Vektorindizes
    ├── optional KGEntity + belegte Beziehungen
    ├── KGAnalysisRun / KGInsight / KGLinkCandidate
    ├── KGAssertion / KGConsolidatedFact / KGQualityDecision / KGQualityProfile
    ├── KGContentIdentity / KGCommunity / KGComponent + GDS-Ergebnisse
    └── KGAnalysisHistorySummary (kompakte, kumulierte Archivstatistik)
    └── Graphprojektionen (GDS In-Memory, temporär)
    ↓
Cypher + optional GDS → Hybrid Search / Graph Intelligence → Flet / CLI / MCP / LLM
```

## Graph Intelligence und selbstverbessernder Graph

Die Funktion **Graph Intelligence** ist in der GUI verfügbar. Die strukturellen Basisanalysen brauchen kein LLM. Wenn `AUTONOMOUS_QUALITY_ENABLED=true` ist, wird zusätzlich eine begrenzte Zahl offener Beziehungskandidaten geprüft; `COMMUNITY_SUMMARIES_ENABLED=true` erstellt bei vorhandener GDS-Unterstützung außerdem Zusammenfassungen geänderter, ausreichend großer Communities.

```powershell
# Tatsächlich verfügbare Neo4j-/GDS-Funktionen anzeigen
.\.venv\Scripts\win-kg.exe graph-capabilities

# Den gesamten Graphen analysieren; GDS ausführen, falls installiert und innerhalb des Limits
.\.venv\Scripts\win-kg.exe analyze-graph

# Ohne GDS; die automatische Kandidatenprüfung kann weiterhin das konfigurierte LLM nutzen
.\.venv\Scripts\win-kg.exe analyze-graph --no-gds

# Kandidaten und deren Prüfstatus anzeigen
.\.venv\Scripts\win-kg.exe link-candidates --status proposed

# Optionaler manueller Override; die automatische Prüfung bleibt davon unabhängig
.\.venv\Scripts\win-kg.exe review-link-candidate <KANDIDAT_ID> rejected --note "Nur zufällige gemeinsame Erwähnung"
```

LLM-extrahierte Quellbeziehungen werden mit `KGAssertion` und `KG_RELATED_TO` gespeichert. Eine bestätigte Aussage wird in `KGConsolidatedFact` konsolidiert und kann über eine generische `KG_ASSERTED_LINK`-Kante genutzt werden. Der Prädikatsraum bleibt flexibel; es ist kein fester Beziehungstyp pro fachlicher Aussage erforderlich. Entitäten mit exakt identischem normalisiertem Namen und Typ behalten ihre root-lokalen `KGEntity`-Knoten, erhalten aber dieselbe deterministische `canonical_entity_id` und werden über `SAME_ENTITY` verbunden. Das ist absichtlich konservativ; semantisch ähnliche Namen oder unbekannte Aliasse werden nicht automatisch blind zusammengeführt.

Die deterministische Analyse findet exakte Duplikate über SHA-256, Dateien ohne gespeicherte Text-Chunks, isolierte Entitäten und wiederholt gemeinsam erwähnte Paare. Solche Paare bleiben zunächst `KGLinkCandidate`-Hypothesen.

### Autonomer Qualitätszyklus

Nach der Graphanalyse kann die Software bis zu `QUALITY_MAX_CANDIDATES_PER_RUN` offene Kandidaten in Batches (`QUALITY_BATCH_SIZE`) an das konfigurierte Chat-LLM geben. Standardmäßig sind das höchstens 20 Kandidaten in Gruppen von 5, also maximal vier LLM-Aufrufe pro Analyse. Nur Kandidaten mit noch existierenden Quell-Chunks werden geprüft. Das LLM muss eine konkrete Beziehung, Polarität, Konfidenz, optionalen Gültigkeitszeitraum und eine wörtliche Belegstelle aus genau einem Quellen-Chunk liefern.

Vor der Speicherung liest Neo4j die Kandidaten und Originaltexte erneut. Die Software kontrolliert, dass die zitierte Passage wörtlich vorkommt, beide Entitätsnamen als vollständige Namens-Treffer im Text stehen, die Polarität bekannt ist, der Zeitraum aktuell ist und die Konfidenz den Schwellenwert erreicht. Fehlt eine Prüfung, wird die Relation nicht aktiviert. Unsichere Fälle bleiben vertagt oder vorgeschlagen. Strukturelle GDS-Ähnlichkeit oder wiederholte gemeinsame Erwähnung allein reicht ausdrücklich nicht als Beleg.

`KGConsolidatedFact` führt assertions mit exakt gleichen kanonischen Subjekt-/Prädikat-/Objekt-Schlüsseln zusammen und speichert unterstützende Assertions, Anzahl der Beleg-Chunks sowie unabhängige Quelldateien. Positive und negative Assertions werden verglichen: Bei gleichzeitig aktiven widersprüchlichen Belegen markiert das System den Fakt als `conflict` und entfernt die aktive Kurzschlusskante, bis der Konflikt durch veränderte oder zeitlich gültige Belege geklärt ist. `KGQualityDecision` protokolliert automatisch akzeptierte, abgelehnte und vertagte Kandidaten; `KGQualityProfile` erfasst deterministische Validierungsergebnisse. Wenn nach jeweils zehn Prüfungen die Fehlerquote mindestens 20 % beträgt, wird die betreffende Konfidenzschwelle vorsichtig um 0,02 erhöht, höchstens auf 0,95. Die Schwelle wird durch diesen Mechanismus nie automatisch abgesenkt. Das System trainiert kein Modell und schreibt keine Prompts automatisch um.

Automatische Entscheidungen können neu bewertet werden, wenn sich ihre Quellbasis ändert. Manuelle `accepted`-/`rejected`-Entscheidungen bleiben als bewusste Overrides erhalten und werden durch diesen Prozess nicht überschrieben.

### Community Summaries und Global Search

Die GDS-Community-ID ist kein laufbezogener Integer mehr: `KGCommunity.id` wird aus den sortierten IDs der Mitgliedsentitäten deterministisch abgeleitet. So kann die Zusammenfassung bei unveränderter Mitglieder- und Beleggrundlage wiederverwendet werden. Pro geeigneter Community wird ein `KGInsight` mit `kind='community_summary'` gespeichert und mit `KG_SUMMARIZES_COMMUNITY` an den Community-Knoten gehängt. Die Details umfassen den Quell-Fingerprint, Modell-/Promptversion, Mitglieder, bestätigte Relationsanzahl, Quellenpfade sowie Key Points, Caveats und mögliche Konflikte.

Nur Communities mit mindestens `COMMUNITY_SUMMARY_MIN_MEMBERS` Mitgliedern und mindestens einer aktiven, bestätigten internen Beziehung werden zusammengefasst. Neu berechnet werden höchstens `COMMUNITY_SUMMARY_MAX_PER_RUN` geänderte Communities pro Analyse; mehrere Communities werden in Batches der Größe `COMMUNITY_SUMMARY_BATCH_SIZE` verarbeitet. Unveränderte Eingabefingerprints werden ohne LLM-Aufruf reaktiviert. Nicht mehr aktuelle Communities und Zusammenfassungen werden als stale markiert und von Global Search ausgeschlossen. GDS selbst kann weiterhin sehr kleine Communities erkennen, aber diese werden nicht automatisch zusammengefasst.

In der GUI aktiviert das Kontrollkästchen **Global Search** eine übergreifende Antwort. Sie verbindet bis zu drei passende Community-Zusammenfassungen mit den besten Hybrid-Search-Quellabschnitten. Das Ergebnis nennt die verwendeten Community-Kontexte und zeigt die Originalquellen separat an. Die normale Suche bleibt unverändert und ruft kein zusätzliches Antwort-LLM auf. Der MCP-Modus `global_search_knowledge` stellt die gleiche Funktion für Agenten bereit.

`.env`-Einstellungen: `COMMUNITY_SUMMARIES_ENABLED=true`, `COMMUNITY_SUMMARY_MIN_MEMBERS=3`, `COMMUNITY_SUMMARY_MAX_PER_RUN=8`, `COMMUNITY_SUMMARY_BATCH_SIZE=2` und `GLOBAL_SEARCH_ENABLED=true`. Bei Standardwerten erzeugt die Summary-Erstellung höchstens vier LLM-Aufrufe pro Analyse; unveränderte Communities benötigen keinen Aufruf. Das LLM für Global Search wird erst beim ausdrücklichen Aufruf dieser Suchart verwendet. Die Funktion benötigt GDS für neue Community-Erkennung, das vorhandene Chat-LLM für geänderte Zusammenfassungen sowie Antwortgenerierung und die bereits vorhandenen Quell- und Embedding-Suchfunktionen.

Einstellungen in `.env`: `AUTONOMOUS_QUALITY_ENABLED=true`, `QUALITY_MAX_CANDIDATES_PER_RUN=20`, `QUALITY_BATCH_SIZE=5` und `QUALITY_MIN_CONFIDENCE=0.82`. Mit `AUTONOMOUS_QUALITY_ENABLED=false` wird nur die automatische Kandidatenprüfung abgeschaltet; die deterministische Belegprüfung beim optionalen Entity-Extraction-Aufruf bleibt erhalten. Das Review-LLM wird nur aufgerufen, wenn offene, belegbare Kandidaten gefunden werden.

### Automatischer Graph-Cleanup

Nach einer erfolgreichen Indexierung mit Graphänderungen und nach einer abgeschlossenen Graphanalyse wird automatisch eine Wartung ausgeführt. Unbeendete Analyseläufe, die nach 24 Stunden noch als `running` markiert sind, werden als verwaiste/abgebrochene Läufe auf `failed` gesetzt, damit auch sie in die Retention fallen. Pro Root bleiben die letzten 20 nicht laufenden `KGAnalysisRun`-Knoten erhalten; ältere Läufe werden in `KGAnalysisHistorySummary` zu kompakten Gesamtzählern (unter anderem Läufe, Findings und Kandidaten nach Status/Analyseart) verdichtet und anschließend als Detailknoten gelöscht. Die Zusammenfassung wird als einzelner Neo4j-Knoten fortgeschrieben, nicht als neuer Knoten pro Wartungslauf. Verwaiste `KGInsight`-, `KGContentIdentity`-, Extraktions-Assertion-, Community- und Komponenten-Knoten werden bereinigt. Stale GDS-Properties und semantische Ähnlichkeitskanten ohne gültige Embeddings werden ebenfalls entfernt. Der Cleanup wird bei Fehlern protokolliert und ist best-effort; ein bereits erfolgreicher Dateiimport oder Analyselauf bleibt erfolgreich.

Wenn Neo4j GDS verfügbar ist, entfernt die Analyse zuerst redundante inaktive `KG_ASSERTED_LINK`-Kanten (die Review-Historie bleibt auf `KGAssertion` erhalten) und kann dann in einer temporären GDS-Projektion ausschließlich auf dem aktiven `KG_ASSERTED_LINK`-Beziehungsgraphen Community Detection (Leiden, falls verfügbar, andernfalls Louvain), Weakly Connected Components (WCC), PageRank und Node Similarity ausführen. Die separate Chunk-KNN-Projektion lädt die `embedding`-Eigenschaft ausdrücklich in den GDS-Speicher. Die WCC-Analyse erkennt getrennte Wissenskomponenten; Community-Zuordnungen, Komponenten und Zentralitätsscores werden zurück in Neo4j geschrieben. Node Similarity schlägt Entitäten mit ähnlichen Graphnachbarschaften als überprüfbare Kandidaten vor. Vor GDS prüft die Anwendung `GDS_MAX_ENTITY_NODES` (Standard: 50000); bei Überschreitung wird die GDS-Analyse übersprungen, während die Cypher-Analyse weiter nutzbar bleibt. Node Similarity besitzt zusätzlich ein niedrigeres Limit `GDS_MAX_NODE_SIMILARITY_NODES` (Standard: 15000), weil die Paarvergleichsberechnung erheblich teurer sein kann. GDS wird nicht automatisch beim normalen Datei-Scan ausgeführt.

Die Feedbackschleife ist bewusst konservativ: `accepted`, `rejected` und `deferred` werden gespeichert und beeinflussen, welche Kandidaten erneut vorgeschlagen werden. Akzeptierte Beziehungen werden als generische `KG_ASSERTED_LINK`-Kanten mit `predicate`-Eigenschaft in Graphabfragen und GDS-Projektionen nutzbar; die zugehörige `KGAssertion` bewahrt Herkunft und Prüfverfahren. Quelländerungen markieren offene Kandidaten und automatisch geprüfte Entscheidungen als veraltet, wenn die betroffene Quellbasis geändert wurde. Automatisch bestätigte Fakten ohne lebende Belege werden aus dem aktiven Graphen genommen; manuelle Overrides werden nicht stillschweigend überschrieben. Die Kandidatenliste priorisiert Vorschlagsverfahren anhand ihrer Akzeptanzhistorie, wenn mindestens fünf Entscheidungen vorliegen. Davon getrennt passt `KGQualityProfile` nach jeweils zehn deterministischen Validierungen mit einer Fehlerquote von mindestens 20 % die Konfidenzschwelle um 0,02 nach oben an (bis maximal 0,95). Prompts und Modellparameter werden nicht automatisch geändert.

## Unterstützte Dateien

Mit den normalen Abhängigkeiten: PDF, DOCX, XLSX/XLSM, PPTX, EML, TXT, Markdown, HTML, CSV/TSV, JSON/JSONL, YAML, XML/XSD/XSL, Jupyter-Notebooks und zahlreiche Quellcode-/Konfigurationsformate (Python, JavaScript/TypeScript, Java, C#, Go, Rust, SQL, PowerShell, Batch, Shell, INI, TOML usw.).

Unbekannte und binäre Dateien werden als Dateiknoten mit Metadaten erfasst, jedoch nicht als Text interpretiert. Bilder werden standardmäßig nur als Metadaten erfasst; sie werden erst mit aktivierter Docling-/OCR-Integration verarbeitet. Die alten Binärformate DOC/XLS/PPT, RTF und OpenDocument werden ebenfalls erst mit der optionalen Docling-Integration verarbeitet. Für gescannte PDFs oder Bilder kann zusätzlicher OCR-Aufwand nötig sein.

## 1. Voraussetzungen

- Windows 10/11 und Python 3.10 oder neuer
- Neo4j 5.x oder neuer mit Vektorindex-Unterstützung, lokal oder Neo4j Aura
- Ollama oder eine OpenAI-kompatible Embedding-API (für semantische Suche)
- Optional ein OpenAI-kompatibler Chat-Completions-Endpunkt für die Extraktion semantischer Entitäten

Der offizielle Neo4j Python Driver wird verwendet. Die Vektorsuche nutzt `db.index.vector.queryNodes`, das über viele Neo4j-5.x-Versionen hinweg kompatibel ist; neuere Neo4j-Versionen empfehlen für Vektorsuche inzwischen die Cypher-`SEARCH`-Syntax. Referenzen: [Neo4j Python Driver](https://neo4j.com/docs/python-manual/current/) · [Neo4j Vector Indexes](https://neo4j.com/docs/cypher-manual/current/indexes/search-indexes/vector-indexes/) · [Ollama Embeddings](https://docs.ollama.com/capabilities/embeddings).

## 2. Windows-Installation

Entpacke das Projekt in einen dauerhaften Ordner, öffne dort PowerShell und führe aus:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup_windows.bat
```

Alternativ manuell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,test]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

**Wichtig:** Wenn bereits eine `.env` existiert, behalte sie und überschreibe sie nicht. Ergänze neue Einstellungen bei Bedarf manuell aus `.env.example`. Bearbeite `.env` und setze mindestens `NEO4J_PASSWORD`. `NEO4J_URI`, `NEO4J_USERNAME` und `NEO4J_DATABASE` müssen auf deine Datenbank zeigen. Die `.env`-Datei ist durch `.gitignore` ausgeschlossen.

### Grafische Oberfläche (GUI)

Das Setup installiert auch Flet. Starte die GUI mit Doppelklick auf `run_gui.vbs` (empfohlen) oder `run_gui.bat`. Die GUI startet ohne dauerhaft sichtbares Terminalfenster als Desktop-Anwendung und benötigt keinen Browser. Das rotierende Diagnoseprotokoll liegt unter `logs/winkg-gui.log` (bis zu drei ältere 5-MB-Dateien werden aufbewahrt). Startausgaben und unerwartete Startfehler werden zusätzlich in `logs/winkg-gui-console.log` protokolliert; Hinweise des Launchers stehen in `logs/winkg-gui-startup.log`.

Die GUI enthält:

- **Dashboard:** Gesamtzahlen des Graphen sowie indexierte Root-Verzeichnisse. Bei jedem Root gibt es **„Aus Index löschen“**. Die App fragt vor der Löschung nach und entfernt nur die Graphdaten dieses Roots aus Neo4j; Quelldateien und Ordner bleiben unangetastet. Während eines laufenden Scans ist die Löschung gesperrt. Der entfernte Root kann später erneut indexiert werden.
- **Scan & Import:** Ordnerauswahl, inkrementeller Scan, Abbrechen/Pausieren und späteres Fortsetzen, vollständiger Neuaufbau, optionale Embeddings, Entity-Extraktion und Live-Protokoll. Ein Root-Neuaufbau verlangt eine zusätzliche Bestätigung, weil die bisherigen Graphdaten dieses Roots gelöscht werden.
- **Knowledge Search:** Hybrid-Suche mit Quellenpfad, Textausschnitt, Abschnitt/Seite und optional erkannten Entitäten.
- **Graph Explorer:** Datei, übergeordneter Ordner, Textabschnitte, extrahierte Entitäten und über gemeinsame Entitäten verbundene Dateien erkunden.
- **Verbindung:** Neo4j- und Embedding-Test sowie Anzeige relevanter `.env`-Konfiguration ohne Passwortanzeige.

Der Ordnerdialog greift auf das Dateisystem des Rechners zu, auf dem die GUI ausgeführt wird. Die Anwendung sollte daher lokal auf dem Windows-Rechner gestartet werden, dessen Dateien indexiert werden sollen.

### Ollama für Embeddings

```powershell
ollama pull nomic-embed-text
```

Stelle sicher, dass Ollama lokal erreichbar ist. Standardmäßig ruft das Projekt `http://localhost:11434/api/embed` auf. In `.env` stehen die Parameter:

```dotenv
EMBEDDING_PROVIDER=ollama
EMBEDDING_BASE_URL=http://localhost:11434
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_BATCH_SIZE=32
MAX_CHUNKS_PER_FILE=500
```

Für CPU-only-Systeme ist `EMBEDDING_BATCH_SIZE=32` der aktuelle Startwert. Die Batchgröße wird pro Datei in den Logs mit Dauer, Durchsatz und geschätzter Restzeit protokolliert. Größere Batches sind nicht automatisch schneller; auf der Zielhardware mit echten Dokumenten testen. Ollama wird bei Embedding-Aufrufen mit `keep_alive=30m` gebeten, das Modell zwischen den Aufrufen im Speicher zu halten.

Alternativ können `EMBEDDING_PROVIDER=openai`, `EMBEDDING_BASE_URL` (Basis-URL einschließlich `/v1`, z. B. `http://localhost:1234/v1`), `EMBEDDING_MODEL` und optional `EMBEDDING_API_KEY` gesetzt werden.

## 3. Verbindung testen

Im Projektverzeichnis:

```powershell
.\.venv\Scripts\win-kg.exe doctor --embedding
```

Oder starte die grafische Oberfläche per Doppelklick auf `run_gui.vbs` (alternativ `run_gui.bat`).

Der Befehl prüft Neo4j und optional die Embedding-API. Wenn Neo4j nicht erreichbar ist, zuerst Verbindung, URI, Datenbankname und Passwort korrigieren.

## 4. Erstimport

Beispiel mit einem Projektordner:

```powershell
.\.venv\Scripts\win-kg.exe scan "C:\MS_VSCode_Workspace\Fletapps_HA"
```

Oder mit der Batch-Datei:

```powershell
.\run_scan.bat "C:\MS_VSCode_Workspace\Fletapps_HA"
```

Für die erste große Verzeichnisstruktur empfehle ich, zunächst **ohne Entity-Extraktion** zu importieren. So werden Struktur, Text, Volltextsuche und Embeddings zuerst verfügbar:

```powershell
.\.venv\Scripts\win-kg.exe scan "D:\Wissensbasis"
```

Danach kannst du semantische Entitäten und Beziehungen mit einem konfigurierten Chat-Modell ergänzen:

```powershell
.\.venv\Scripts\win-kg.exe scan "D:\Wissensbasis" --force --extract-entities
```

`--force` verarbeitet alle Dateien erneut, damit die Entitäten für bereits vorhandene Chunks ebenfalls extrahiert werden. Das kann abhängig von Dateianzahl, Modell und Textmenge viele LLM-Aufrufe verursachen.


### Scan pausieren und fortsetzen

- In der GUI **„Scan abbrechen / pausieren“** drücken. Die aktuell bearbeitete Datei wird noch fertig gespeichert; danach stoppt der Scan.
- Die bereits erfolgreich gespeicherten Dateien bleiben in Neo4j erhalten. Ein unvollständiger Lauf führt keine Bereinigung vermeintlich gelöschter Dateien durch.
- Zum Fortsetzen denselben Root mit **„Inkrementell indexieren“** erneut starten. Bereits unveränderte Dateien werden übersprungen; fehlende oder unvollständige Dateien werden verarbeitet.
- Der Abbruch unterbricht keine laufende externe Anfrage mitten im Aufruf. Bei Embeddings/LLM wartet die Anwendung deshalb auf das Ende bzw. Timeout des aktuellen Aufrufs und schließt anschließend die aktuelle Datei sicher ab.
- **Achtung bei „Root neu aufbauen“:** Diese Funktion löscht den bisherigen Graphen dieses Roots am Anfang. Ein Abbruch kann daher einen nur teilweise neu aufgebauten Graphen hinterlassen. Für normale Unterbrechungen immer den inkrementellen Scan verwenden.

### Sichere Scan-Defaults

- Verzeichnisse wie `.git`, `.venv`, `node_modules`, `__pycache__`, `build` und `dist` werden standardmäßig ausgeschlossen. Weitere Verzeichnisnamen können über `EXTRA_EXCLUDED_DIRS` (durch Semikolon getrennt) ergänzt werden.
- Typische Geheimnisdateien wie `.env*`, Schlüsseldateien, `credentials`- und `token`-Dateien werden standardmäßig übersprungen. Nur wenn diese Inhalte ausdrücklich indexiert werden sollen, kann `--include-sensitive` bzw. `INCLUDE_SENSITIVE_FILES=true` genutzt werden.
- Symlinks/Junctions werden nicht verfolgt, um Endlosschleifen und unbeabsichtigte Laufwerks-/Netzwerkzugriffe zu vermeiden.
- `--include-ignored` schließt die standardmäßig ausgeschlossenen Verzeichnisnamen mit ein. Nicht wahllos auf `C:\` ohne gezielte Excludes anwenden.
- Bei Dateien oberhalb `MAX_FILE_SIZE_MB` werden standardmäßig nur Metadaten gespeichert und `needs_deep_analysis=true` gesetzt. Sie werden im Dashboard und per MCP-Tool als analysebedürftig aufgeführt. Den Grenzwert nur erhöhen, wenn RAM und Laufzeit das erlauben.
- Bei Dateien mit mehr als `MAX_CHUNKS_PER_FILE` werden nur die ersten N Chunks indexiert. Der Eintrag wird ebenfalls mit `needs_deep_analysis=true` markiert; die vollständige Quelldatei bleibt unverändert.
- Wenn der Chunk-Limit-Wert gesenkt wird, erkennt der nächste normale Scan bereits importierte Dateien, deren gespeicherte Chunks über dem neuen Limit liegen, und verarbeitet sie erneut. Für die sichere Begrenzung bereits vorhandener Einträge kann zusätzlich `--force` verwendet werden.

Wenn ein Dateizugriff fehlschlägt, werden Scanfehler protokolliert. Die Bereinigung alter Knoten wird für einen Lauf mit Traversierungsfehlern ausgelassen, damit nicht versehentlich Daten entfernt werden.

## 5. Suche und Abfragen

```powershell
# Hybrid Search mit Quellenbelegen
.\.venv\Scripts\win-kg.exe search "Welche Module schreiben in die SQLite-Datenbank?" --limit 8

# Suche nur in einem Root
.\.venv\Scripts\win-kg.exe search "Fakturasperre Retourenauftrag" --root "D:\Wissensbasis"

# JSON-Ausgabe für Skripte
.\.venv\Scripts\win-kg.exe search "Neo4j MCP" --json

# Einzeldatei und gespeicherte Chunks
.\.venv\Scripts\win-kg.exe file "src\main.py" --root "C:\MS_VSCode_Workspace\Fletapps_HA"

# Über gemeinsame Entitäten verbundene Dateien (braucht Entity-Extraktion)
.\.venv\Scripts\win-kg.exe related "README.md" --root "D:\Wissensbasis"

# Graph-Zähler
.\.venv\Scripts\win-kg.exe stats
```

Die normale Suche kombiniert semantische Ähnlichkeit und exakte Texttreffer. Jeder Treffer enthält Pfad, Ausschnitt und – soweit von der Extraktion ermittelt – Seite/Abschnitt. Eine Dateistruktur alleine erzeugt noch keine semantischen Beziehungen; dafür muss `--extract-entities` aktiviert sein. Bei Verwendung eines Cloud-LLM-Endpunkts werden die jeweiligen Textabschnitte an diesen Anbieter übertragen; für vollständig lokale Verarbeitung muss der konfigurierte Embedding- und Chat-Endpunkt lokal laufen.

## 6. MCP-Server für LLM-Agenten

Das Paket installiert den MCP-SDK mit. Starte im Projektverzeichnis testweise:

```powershell
.\.venv\Scripts\python.exe -m winkg.mcp_server
```

Der Server verwendet **stdio** (kein offener Netzwerk-Port). Der MCP-Client muss auf das Projektverzeichnis bzw. die richtige Python-Umgebung zeigen. Allgemeines Beispiel für eine MCP-Client-Konfiguration:

```json
{
  "mcpServers": {
    "windows-knowledge-graph": {
      "command": "C:\\Pfad\\neo4j_windows_knowledge_graph\\.venv\\Scripts\\python.exe",
      "args": ["-m", "winkg.mcp_server"]
    }
  }
}
```

Folgende Tools werden registriert:

- `search_knowledge(query, limit, root?)`
- `get_file_context(path, root?)`
- `find_related_files(path, limit, root?)`
- `get_graph_stats(root?)`
- `get_files_needing_deep_analysis(limit, root?)`

Der MCP-Server lädt seine Einstellungen ausschließlich aus der `.env` im Projektordner. Stelle sicher, dass der Server mit der Python-Umgebung dieses Projekts gestartet wird; Windows-Umgebungsvariablen sollen die projektlokale Konfiguration nicht ersetzen.

## 7. Optional: semantische Entity-Extraktion

In `.env` konfigurieren:

```dotenv
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=ollama
ENTITY_EXTRACTION_ENABLED=false
```

Die Chat-Completions-URL muss unter `{LLM_BASE_URL}/chat/completions` erreichbar sein. Die Entity-Extraktion selbst bleibt standardmäßig deaktiviert. Mit `--extract-entities` werden Entitäten und ihre quellenbelegten Beziehungen ermittelt. Nur Relationen, deren wörtliche Evidenz, Entitätsnamen, Polarität, Gültigkeitszeitraum und Konfidenz die lokalen Prüfungen bestehen, werden als Assertion übernommen; alle anderen bleiben vorgeschlagen. Daneben kann die Graphanalyse vorhandene Kandidaten in kleinen Batches automatisch prüfen. Jede automatische Annahme wird vor dem Speichern anhand aktueller Neo4j-Quelldaten erneut geprüft. Bei kritischen Anwendungen bleiben Stichproben und ein manueller Override sinnvoll.

## 8. Docling optional aktivieren

Die leichte Standardinstallation deckt bereits viele gebräuchliche Dateitypen ab. Für zusätzliche Office-/OpenDocument-Formate oder bessere Layoutbehandlung kann Docling installiert werden:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[docling,mcp]"
```

Anschließend in `.env` `USE_DOCLING=true` setzen. Je nach Dokumenttyp können Download, Modellinitialisierung und CPU-/RAM-Aufwand höher sein. Die Dateien werden lokal verarbeitet; überprüfe die Datenschutzanforderungen deiner Umgebung.

## 9. Datenmodell in Neo4j

Labels:

- `KGRoot`: ein importiertes Quellverzeichnis
- `KGFolder`: Ordner in der Verzeichnisstruktur
- `KGFile`: Datei-Metadaten und Indexierungsstatus
- `KGChunk`: Textabschnitt mit optionalem Vektor und Quellenposition
- `KGEntity`: optional extrahierte Entität

Kanten:

- `KG_CONTAINS`: Root/Ordner enthält Ordner oder Datei
- `KG_HAS_CHUNK`: Datei hat Textabschnitt
- `KG_MENTIONS`: Textabschnitt erwähnt Entität
- `KG_RELATED_TO`: aus einem Chunk extrahierte Beziehung samt Beleg, Konfidenz, Polarität und Status
- `KG_ASSERTED_LINK`: generische aktive Beziehung mit freiem `predicate`-Wert; nur bestätigte, konsolidierte Beziehungen werden traversiert und für Entitäts-GDS-Analysen projiziert
- `SAME_ENTITY`: Verbindung root-lokaler Entitätsknoten mit exakt gleichem normalisiertem Namen und Typ
- `KGConsolidatedFact`: sammelt Assertions für dieselbe kanonische Subjekt-/Prädikat-/Objekt-Aussage, einschließlich unabhängiger Belegquellen und Konfliktstatus
- `KGQualityDecision` / `KGQualityProfile`: Entscheidungen zur autonomen Kandidatenprüfung und kompakte Kennzahlen der deterministischen Validierung
- `KGAssertion`: quellengebundene Aussage mit offenem Prädikat, Status, Methode und Beleg-Chunks
- `KGExtractionCache`: erfolgreiche LLM-Extraktion, keyed by Content-Hash, Modell-Endpunkt und Prompt-Version
- `KGComponent`: temporäre GDS-WCC-Ergebnisse als nachvollziehbare Komponentenknoten; bei Änderungen als veraltet markiert

Die verwendeten Labels und Beziehungstypen sind bewusst mit `KG` präfixiert, damit vorhandene Graphdaten nicht mit generischen Labels wie `File` oder `Entity` kollidieren.

## 10. Aktualisieren, Neuaufbau und Troubleshooting

- Normaler Scan ist inkrementell: `win-kg scan "D:\Wissensbasis"`.
- `--force` extrahiert vorhandene Dateien erneut, überschreibt ihre Chunks und kann für Entity-Extraktion gebraucht werden.
- `--rebuild` löscht **nur den Graphen dieses Roots** und importiert ihn neu. Vorher sicherstellen, dass die `root`-Pfadangabe exakt dem bisherigen Root entspricht.
- In derselben Neo4j-Datenbank müssen alle Roots dasselbe Embedding-Modell verwenden. Ein Modellwechsel erzeugt einen Dimensions-/Vektorraummix und wird erkannt. Für einen bewussten Wechsel `--rebuild` verwenden und zuerst andere Roots mit abweichendem Modell entfernen oder eine getrennte Datenbank nutzen.
- `--no-embeddings` legt nur Volltext und Graph ab; die semantische Suche ist dann aus. Der Root ist damit als `none` registriert; zum späteren Wechsel auf Vektoren `--rebuild` mit aktivierten Embeddings ausführen.
- Wenn Neo4j einen Vektorindex nicht erstellen kann, zuerst die Neo4j-Version und die Logs kontrollieren. Volltextindexierung bleibt getrennt von der Vektorsuche.
- `status=embedding_missing` bedeutet: Inhalt wurde gerettet und kann per Volltext gefunden werden, aber die Embedding-API oder der Vektorindex ist fehlgeschlagen. Ein weiterer normaler Scan versucht diese Dateien erneut.
- Große, unlesbare oder nicht unterstützte Dateien bleiben als Metadatenknoten vorhanden, sofern der Scan sie sehen kann.

## 11. Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Die Tests decken Chunking, Inhaltsextraktion, Graph-Cleanup, Quellbeleg-Validierung, adaptive Schwellen und die Batch-Steuerung des autonomen Qualitätszyklus ab. Die Testsuite führt keine echten Neo4j-/LLM-Integrationstests aus; diese benötigen laufende Dienste und müssen separat ausgeführt werden.
