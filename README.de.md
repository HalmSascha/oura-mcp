<div align="center">
  <img src="docs/logo.svg" alt="oura-mcp" width="120" height="120">
  <h1>oura-mcp</h1>
  <p><strong>MCP-Server für Oura-Ring-Daten — ausschließlich OAuth2, selbst gehostet.</strong></p>
  <p><a href="README.md">English version</a></p>
</div>

---

> **Spiegel auf zwei Forges.** Das maßgebliche Repository liegt auf [GitHub](https://github.com/HalmSascha/oura-mcp); ein identischer Spiegel liegt auf [Codeberg](https://codeberg.org/saschahalm/oura-mcp).
> Issues und Pull Requests laufen über GitHub und sind auf dem Spiegel abgeschaltet.

Schlaf, Readiness, Aktivität, Workouts, HRV und Ruhepuls aus dem Oura Ring, für
beliebige MCP-Clients verfügbar. Läuft per stdio für lokale Clients oder per
Streamable HTTP hinter einem Shared Secret im Netzbetrieb.

Kein offizielles Projekt von Ōura Health Oy und in keiner Weise mit dem
Unternehmen verbunden.

## Warum noch einer

Oura hat **Personal Access Tokens im Dezember 2025 abgeschafft**. Neue lassen
sich nicht mehr erzeugen, womit die meisten vorhandenen Oura-MCP-Server für eine
Neueinrichtung unbrauchbar geworden sind — sie authentifizieren sich per PAT.
Dieser hier nutzt ausschließlich OAuth2.

Dazu kommen drei Fallen, die jeweils echte Debugging-Zeit gekostet haben:

**Oura betreibt zwei Token-Endpunkte.** Die veröffentlichte Dokumentation nennt
nur `api.ouraring.com/oauth/token`, faktisch bedient Ouras Infrastruktur heute
aber `moi.ouraring.com/oauth/v2/ext/oauth-token`. Die Reihenfolge ist
entscheidend: Authorization Codes sind einmalig verwendbar
([RFC 6749 §4.1.2](https://datatracker.ietf.org/doc/html/rfc6749#section-4.1.2)),
ein abgelehnter Versuch am falschen Endpunkt verbrennt den Code und macht jeden
Fallback wertlos. Dieser Server fragt `moi` zuerst.

**Refresh-Tokens sind ebenfalls einmalig.** Jeder Refresh liefert ein neues und
entwertet das alte. Geht die Antwort verloren, ist die Autorisierung tot und
muss von Hand neu aufgebaut werden. Tokens werden deshalb atomar geschrieben,
bevor sie herausgegeben werden, und parallele Refreshes laufen hinter einer
Sperre.

**Zeiträume sind hier inklusive.** Ouras eigenes `end_date` ist exklusiv, eine
naive Ein-Tages-Abfrage kommt also leer zurück. Diese Falle wird einmal an einer
Stelle geschlossen, statt sie jedem Aufrufer zu überlassen.

## Tools

| Tool | Zweck |
|---|---|
| `get_sleep` | Tägliche Schlaf-Scores mit Teilbewertungen |
| `get_sleep_detail` | Schlafphasen pro Session, HRV, niedrigste Herzfrequenz |
| `get_readiness` | Readiness, Temperaturabweichung, Erholungsindex |
| `get_activity` | Schritte, Kalorien, Ziele, Inaktivität |
| `get_workouts` | Erkannte und manuelle Workouts mit Distanz und Intensität |
| `get_sessions` | Geführte und freie App-Sessions |
| `get_tags` | Selbst gesetzte Tags (erweitertes Format) |
| `get_stress` | Tägliche Stress- und Erholungsdauer |
| `get_resilience` | Resilienz-Level und Beitragsfaktoren |
| `get_spo2` | Nächtliche Sauerstoffsättigung, Atemstörungsindex |
| `get_heart_health` | Kardiovaskuläres Alter und VO2max |
| `get_heartrate` | Herzfrequenz-Zeitreihe, automatisch in 30-Tage-Fenster geteilt |
| `get_personal_info` | Stammdaten des Kontos |
| `get_ring_configuration` | Ringgeneration, Größe, Farbe |
| `get_status` | Autorisierungsdiagnose, ohne Gesundheitsdaten anzufassen |
| `get_event_window` | **Erholungsverlauf um ein Ereignis, in einem Aufruf** |

### `get_event_window`

Das ist das Werkzeug, wegen dem es sich lohnt. Ereignisdatum und Fenster rein,
tagweiser Verlauf raus — Schlaf-Score, Readiness, HRV, Ruhepuls, Schritte und
Workouts, ausgerichtet auf einer `offset_days`-Achse: negativ vor dem Ereignis,
null am Tag selbst, positiv danach.

```
Tag          Offset  Schlaf  Ready   HRV   RHF  Schritte
2026-09-17       -2      78     84    28    53     2.697
2026-09-18       -1      68     71    26    56     2.439
2026-09-19       +0      58     73    23    52     1.942   <- Ereignis
2026-09-20       +1      57     78    30    52     1.782
2026-09-21       +2      83     85    25    52     2.110
```

Die Frage „wie hart hat mich das getroffen und wie lange hat die Erholung
gedauert" ist damit ein Aufruf statt sechs plus Handarbeit beim Zusammenführen.
Gebaut für den Abgleich harter Tage — ein Ultramarathon, eine lange Wanderung,
ein Flug, eine schlechte Nacht — gegen das, was der Ring danach aufgezeichnet
hat.

Endpunkte, die eine Oura-Mitgliedschaft oder neuere Hardware voraussetzen,
degradieren sauber: Sie antworten mit `{"available": false, "reason": "..."}`
statt den Aufruf scheitern zu lassen.

## Einrichtung

### 1. Oura-Application registrieren

Auf **https://developer.ouraring.com/applications** gehen — das ist das richtige
Portal, und das falsche zu erwischen ist der mit Abstand häufigste Fehler bei
der Einrichtung. Diese Redirect-URI hinterlegen:

```
http://localhost:8765/callback
```

Client ID und Client Secret notieren.

> Wer parallel die [Home-Assistant-Integration](https://github.com/louispires/Oura-Home-Assistant-Integration)
> nutzt, kommt mit einer einzigen Application aus — einfach
> `https://my.home-assistant.io/redirect/oauth` als zweite Redirect-URI
> ergänzen.

### 2. Einmalig autorisieren

Der OAuth-Flow braucht einen Browser und kann deshalb nicht headless im
Container laufen:

```bash
export OURA_CLIENT_ID=...
export OURA_CLIENT_SECRET=...
uvx --from oura-mcp oura-mcp --token-store ./tokens.json auth
```

Das schreibt `tokens.json` mit Rechten `0600`.

> **Nur eine Kopie im Einsatz halten.** Oura entwertet das Refresh-Token bei
> jeder Nutzung, zwei Kopien derselben Datei schießen sich beim ersten Refresh
> gegenseitig ab. Nach dem Kopieren auf einen Server die lokale Kopie löschen.

### 3a. Lokal per stdio

```json
{
  "mcpServers": {
    "oura": {
      "command": "uvx",
      "args": ["--from", "oura-mcp", "oura-mcp", "--token-store", "/pfad/zu/tokens.json", "serve"],
      "env": {
        "OURA_CLIENT_ID": "...",
        "OURA_CLIENT_SECRET": "..."
      }
    }
  }
}
```

### 3b. Per HTTP in Docker

Multi-Arch-Images (`amd64` und `arm64`) liegen auf GHCR:

```bash
docker pull ghcr.io/halmsascha/oura-mcp:latest
```

`docker-compose.example.yaml` nach `docker-compose.yaml` kopieren, Werte
eintragen, dann:

```bash
mkdir -p data && cp /pfad/zu/tokens.json data/ && chmod 600 data/tokens.json
docker compose up -d
```

Clients authentifizieren sich mit `Authorization: Bearer $OURA_MCP_TOKEN`.

## Konfiguration

| Variable | Default | Bedeutung |
|---|---|---|
| `OURA_CLIENT_ID` | — | Pflicht |
| `OURA_CLIENT_SECRET` | — | Pflicht |
| `OURA_TOKEN_STORE` | `/data/tokens.json` | Ablage der Tokens |
| `OURA_MCP_TRANSPORT` | `stdio` | `stdio` oder `http` |
| `OURA_MCP_TOKEN` | — | Shared Secret, **Pflicht** bei `http` |
| `OURA_MCP_HOST` | `0.0.0.0` | Bind-Adresse |
| `OURA_MCP_PORT` | `8000` | Bind-Port |
| `OURA_MCP_ALLOWED_HOSTS` | — | Erlaubte `Host`-Header, kommagetrennt |
| `OURA_LOG_LEVEL` | `INFO` | Logstufe |

`OURA_MCP_ALLOWED_HOSTS` ist nötig, sobald der Server unter etwas anderem als
localhost angesprochen wird. Das MCP-SDK prüft den `Host`-Header gegen
DNS-Rebinding und antwortet sonst mit `421`, was sich leicht als
Netzwerkproblem fehldeuten lässt.

`/healthz` antwortet ohne Token, damit Container-Healthchecks das Secret nicht
kennen müssen. Alles andere liefert ohne gültiges Bearer-Token `401`.

## Sicherheit

- `tokens.json` enthält Access- und Refresh-Token im Klartext. Die Datei steht
  in `.gitignore` und wird mit `0600` geschrieben. Nicht committen.
- Abgelehnte Token-Anfragen loggen nur Endpunkt-Host, Grant-Type und
  HTTP-Status. Response-Bodies werden nie geloggt, weil sie Tokens enthalten
  können. Ein Test sichert das ab.
- **Der HTTP-Transport kennt keine Authentifizierung pro Nutzer.** Das Shared
  Secret ist die einzige Hürde. Das ist bewusst ein Einzelnutzer-Entwurf: den
  Port nicht ohne vorgelagerten Schutz ins Internet stellen.

## Fehlersuche

Immer mit `get_status` anfangen. Es meldet, ob Zugangsdaten gesetzt sind, ob der
Token-Store existiert und ob die Autorisierung tatsächlich funktioniert — ohne
Gesundheitsdaten zu lesen.

| Symptom | Wahrscheinliche Ursache |
|---|---|
| `404` nach der Zustimmung bei Oura | Redirect-URI nicht hinterlegt oder im falschen Portal |
| `401` beim Token-Tausch | Falsches Portal, abweichende Redirect-URI oder neu erzeugtes Client Secret |
| Token-Anfrage beim Refresh abgelehnt | Das Refresh-Token wurde bereits benutzt. `auth --force` neu durchlaufen |
| `421` bei jedem HTTP-Aufruf | `OURA_MCP_ALLOWED_HOSTS` enthält den vom Client genutzten Host nicht |
| Manche Werte dauerhaft `available: false` | Diese Endpunkte brauchen eine Mitgliedschaft oder neuere Hardware |

## Entwicklung

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

41 Tests decken die inklusiv/exklusiv-Datumsgrenze ab, Paginierung,
Heartrate-Fenster, Rate-Limit-Retry samt Obergrenze, das Degradieren optionaler
Endpunkte, die Token-Endpunkt-Reihenfolge, atomare Persistenz, parallele
Refreshes, die Weitergabe von Fehlermeldungen an den Client und eine
Regressionssperre, die sicherstellt, dass kein Geheimnis im Log landet.

## KI-unterstützte Entwicklung

Dieses Projekt ist mit KI-Unterstützung entstanden (Claude / Claude Code), unter
Steuerung und Review durch [Sascha Halm](https://github.com/HalmSascha). Die
Architekturentscheidungen, die Wahl reiner OAuth-Authentifizierung und die
Verifikation des undokumentierten Oura-Token-Endpunkts wurden vor dem Commit von
einem Menschen geprüft.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
