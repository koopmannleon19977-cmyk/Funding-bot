# SuperClaude Quick Reference - funding-bot

> Schnellübersicht der wichtigsten Commands für dieses Projekt

---

## 🎯 Top 10 Commands für funding-bot

| Command | Wann nutzen | Beispiel |
|---------|-------------|----------|
| `/save` | Nach wichtigen Änderungen | `/save pre-deployment` |
| `/load` | Session wiederherstellen | `/load pre-deployment` |
| `/implement` | Feature implementieren | `/implement new risk check` |
| `/test` | Tests generieren/ausführen | `/test` |
| `/analyze` | Code-Qualität prüfen | `/analyze src/exchanges/` |
| `/research` | Recherche (Web) | `/research "funding rate API best practices"` |
| `/troubleshoot` | Debugging | `/troubleshoot PnL calculation error` |
| `/design` | Architektur planen | `/design new exchange adapter` |
| `/cleanup` | Refactoring | `/cleanup old order handlers` |
| `/agent security` | Security Review | `/agent security` |

---

## 📋 Command Categories

### 🧠 Planning (4 Commands)
```bash
/brainstorm           # Strukturiertes Brainstorming
/design               # System-Architektur
/estimate             # Zeit-/Aufwandsschätzung
/spec-panel           # Spezifikations-Analyse
```

### 💻 Development (5 Commands)
```bash
/implement            # Code-Implementierung
/build                # Build-Workflows
/improve              # Code-Verbesserungen
/cleanup              # Refactoring
/explain              # Code-Erklärung
```

### 🧪 Testing & Quality (4 Commands)
```bash
/test                 # Test-Generierung
/analyze              # Code-Analyse
/troubleshoot         # Debugging
/reflect              # Retrospektiven
```

### 📚 Documentation (2 Commands)
```bash
/document             # Doku-Generierung
/help                 # Command-Hilfe
```

### 🔧 Version Control (1 Command)
```bash
/git                  # Git-Operationen
```

### 📊 Project Management (3 Commands)
```bash
/pm                   # Projektmanagement
/task                 # Task-Tracking
/workflow             # Workflow-Automatisierung
```

### 🔍 Research & Analysis (2 Commands)
```bash
/research             # Deep Web Research
/business-panel       # Business-Analyse
```

### 🎯 Utilities (9 Commands)
```bash
/agent                # KI-Agenten spawnen
/index-repo           # Repository-Indizierung
/index                # Indizierungs-Alias
/recommend            # Command-Empfehlungen
/select-tool          # Tool-Auswahl
/spawn                # Parallele Tasks
/load                 # Sessions laden
/save                 # Sessions speichern
/sc                   # Alle Commands anzeigen
```

---

## 🤖 Specialized Agents

### Verfügbare Agents (via `/agent`)
```bash
/agent security              # Security Engineer
/agent pm                    # Project Manager
/agent frontend              # Frontend Architect
/agent backend               # Backend Engineer
/agent devops                # DevOps Engineer
/agent qa                    # QA Specialist
/agent research              # Deep Research
```

**Beispiele:**
```bash
/agent security              # Full security audit
/agent research "API rate limits for derivatives exchanges"
```

---

## 🎨 Behavioral Modes (Automatisch)

SuperClaude aktiviert automatisch passende Modi:

| Mode | Wann aktiv | Effekt |
|------|------------|--------|
| **Brainstorming** | `/brainstorm` | Stellt gezielte Fragen |
| **Deep Research** | `/research` | Autonome Web-Recherche |
| **Token-Efficiency** | Komplexe Tasks | 30-50% weniger Tokens |
| **Task Management** | `/task`, `/pm` | Systematische Organisation |
| **Orchestration** | Multi-step tasks | Effiziente Tool-Koordination |

---

## 🔧 MCP Server Integration

SuperClaude nutzt automatisch deine konfigurierten MCP Server:

| Command | Nutzt MCP Server |
|---------|------------------|
| `/research` | **Tavily** (Web-Suche) |
| Code-Analyse | **Serena** (Code-Kontext) |
| Komplexe Logik | **Sequential-Thinking** |
| Doku-Lookup | **Context7** |
| Session Memory | **Serena** |

---

## ⚡ Power-User Tips

### 1. Session Management
```bash
# Vor riskanten Änderungen
/save before-major-refactor

# Nach erfolgreicher Implementierung
/save feature-funding-hedge-complete

# Session wiederherstellen
/load feature-funding-hedge-complete
```

### 2. Parallel Execution
```bash
# Mehrere Tasks parallel
/spawn "analyze exchange adapters" "review test coverage" "check security"
```

### 3. Deep Research
```bash
# Ausführliche Recherche (mit Tavily MCP)
/research "best practices for handling funding rate intervals"

# Business-Analyse
/business-panel "neue Exchange-Integration: Binance vs OKX"
```

### 4. Workflow-Chaining
```bash
# Kompletter Feature-Workflow
/design new-feature
# → Implementierung
/implement new-feature
# → Tests
/test
# → Review
/analyze
# → Session speichern
/save new-feature-complete
```

---

## 🚫 Was SuperClaude NICHT macht

- Keine automatischen Git-Commits ohne Bestätigung
- Keine Änderungen an Live-Trading ohne `live_trading=true`
- Keine Commits von Secrets/API-Keys
- Keine force-push zu main/master
- Keine destruktiven Git-Operationen

---

## 📖 Hilfe & Support

```bash
/help                 # SuperClaude Hilfe
/sc                   # Alle Commands auflisten
/recommend            # Command-Empfehlungen basierend auf Kontext
```

### Dokumentation
- **Projekt**: `AGENTS.md`, `PLANNING.md`, `TASK.md`, `KNOWLEDGE.md`
- **SuperClaude**: `.claude/SUPERCLAUDE_SETUP.md`
- **Online**: https://superclaude.netlify.app/docs

---

## 🎉 Häufige Workflows

### Feature-Entwicklung
```bash
1. /brainstorm "new feature ideas"
2. /design architecture
3. /implement feature
4. /test
5. /analyze code quality
6. /save feature-complete
```

### Bug-Fixing
```bash
1. /troubleshoot "describe bug"
2. /analyze affected code
3. /implement fix
4. /test
5. /reflect "what went wrong"
```

### Code Review
```bash
1. /analyze src/
2. /agent security
3. /cleanup
4. /document changes
```

### Research
```bash
1. /research "topic"
2. /business-panel "strategic implications"
3. /design implementation
```

---

*Für Details siehe: `.claude/SUPERCLAUDE_SETUP.md`*
*Projekt-Kontext: `AGENTS.md`, `PLANNING.md`, `TASK.md`, `KNOWLEDGE.md`*
