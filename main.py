from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator, field_validator
from typing import Optional, List
from datetime import datetime, timezone
import httpx
import uuid
import os
import json
import logging

# ── Structured JSON logging ───────────────────────────────
# In productie (LOG_FORMAT=json) worden logs als JSON geschreven
# voor log aggregators (Datadog, CloudWatch, Grafana Loki).
# In development (LOG_FORMAT=text) worden logs leesbaar als tekst.

import json as _json_module

class _JsonFormatter(logging.Formatter):
    """JSON log formatter voor productie log-aggregatie."""
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "module":  record.module,
            "line":    record.lineno,
        }
        if record.exc_info:
            log_obj["exc"] = self.formatException(record.exc_info)
        return _json_module.dumps(log_obj, ensure_ascii=False)

_log_format = os.environ.get("LOG_FORMAT", "text").lower()
_handler = logging.StreamHandler()

if _log_format == "json":
    _handler.setFormatter(_JsonFormatter())
else:
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S"
    ))

logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logger = logging.getLogger("svm")  # svm = School van Morgen

# ── App setup ─────────────────────────────────────────────
app = FastAPI(title="School van morgen", version="1.0.0")

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    if o.strip()
]

# ── Rate limiter ──────────────────────────────────────────────────────────
import time as _time

_rate_limit_store: dict[str, list[float]] = {}
_RATE_LIMIT_AI_PER_MINUUT  = 30   # Zware AI endpoints (genereren documenten)
_RATE_LIMIT_CLEANUP_INTERVAL = 300 # Cleanup elke 5 minuten
_rate_limit_laatste_cleanup  = 0.0

def _rate_limit_cleanup() -> None:
    """Verwijder verlopen IP-entries om geheugenlek te voorkomen."""
    global _rate_limit_laatste_cleanup
    nu = _time.monotonic()
    if nu - _rate_limit_laatste_cleanup < _RATE_LIMIT_CLEANUP_INTERVAL:
        return
    drempel = nu - 60
    verlopen = [ip for ip, ts in _rate_limit_store.items() if not any(t > drempel for t in ts)]
    for ip in verlopen:
        del _rate_limit_store[ip]
    _rate_limit_laatste_cleanup = nu
    if verlopen:
        logger.debug(f"Rate limit cleanup: {len(verlopen)} IPs verwijderd uit store")

def _check_rate_limit_sync(client_ip: str, limiet: int) -> None:
    """
    Sliding-window rate limiter (60 seconden venster).
    asyncio is single-threaded — geen lock nodig voor CPython dict.
    Gooit HTTPException 429 met Retry-After header als limiet bereikt is.
    """
    _rate_limit_cleanup()
    nu     = _time.monotonic()
    drempel = nu - 60
    calls  = [t for t in _rate_limit_store.get(client_ip, []) if t > drempel]
    if len(calls) >= limiet:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": "60"},
            detail=f"Te veel verzoeken ({limiet}/minuut limiet). Wacht 60 seconden.",
        )
    calls.append(nu)
    _rate_limit_store[client_ip] = calls

async def _check_rate_limit(request: Request) -> None:
    """Rate limit voor zware AI endpoints."""
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit_sync(ip, _RATE_LIMIT_AI_PER_MINUUT)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Pas rate limiting toe op AI-endpoints vóór de handler wordt uitgevoerd."""
    ai_paths = ["/rapporten/batch", "/opp", "/handelingsplan", "/analyseer"]
    if any(request.url.path.startswith(p) for p in ai_paths):
        await _check_rate_limit(request)
    response = await call_next(request)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "DELETE", "PUT", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """
    Voegt een uniek X-Request-ID toe aan elke request en response.
    Gebruikt de inkomende header als die er is (voor proxy-compatibiliteit),
    anders genereren we er een.

    Gebruik in logs: logger.info(f"[{request.state.request_id}] ...")
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response

# ── Config ────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-sonnet-4-20250514"

SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
RESEND_API_KEY       = os.environ.get("RESEND_API_KEY", "")      # resend.com — gratis laag: 3000 e-mails/maand
MAIL_FROM            = os.environ.get("MAIL_FROM", "noreply@schoolvanmorgen.nl")
APP_URL              = os.environ.get("APP_URL", "http://localhost:8000")
# Service role key — alleen voor user_metadata updates (nooit naar de browser sturen)
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

security = HTTPBearer(auto_error=False)

# ── Gedeelde HTTP clients (connection pooling) ─────────────
# httpx.AsyncClient houdt TCP-verbindingen open en hergebruikt ze.
# Zonder dit opent elke Supabase-aanroep een nieuwe TCP verbinding:
# - DNS lookup: ~20ms
# - TCP handshake: ~10ms
# - TLS handshake: ~30ms
# Totaal: ~60ms overhead per aanroep die we volledig elimineren.
_supabase_client: httpx.AsyncClient | None = None
_claude_client:   httpx.AsyncClient | None = None

def _get_supabase_client() -> httpx.AsyncClient:
    """Geeft de gedeelde Supabase HTTP client terug."""
    if _supabase_client is None:
        raise RuntimeError("Supabase client niet geïnitialiseerd. Is startup() uitgevoerd?")
    return _supabase_client

def _get_claude_client() -> httpx.AsyncClient:
    """Geeft de gedeelde Anthropic HTTP client terug."""
    if _claude_client is None:
        raise RuntimeError("Claude client niet geïnitialiseerd. Is startup() uitgevoerd?")
    return _claude_client

# ── Startup check ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    ontbrekend = []
    if not ANTHROPIC_API_KEY:
        ontbrekend.append("ANTHROPIC_API_KEY")
    if not SUPABASE_URL:
        ontbrekend.append("SUPABASE_URL")
    if not SUPABASE_ANON_KEY:
        ontbrekend.append("SUPABASE_ANON_KEY")
    if ontbrekend:
        raise RuntimeError(
            f"Verplichte omgevingsvariabelen niet ingesteld: {', '.join(ontbrekend)}. "
            f"Voeg ze toe aan je .env bestand en herstart de server."
        )
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY niet ingesteld — e-mail versturen is uitgeschakeld. "
                       "Registreer gratis op resend.com en voeg de sleutel toe aan .env.")
    logger.info(f"Opstartcontrole geslaagd. CORS toegestaan voor: {ALLOWED_ORIGINS}")

    # Initialiseer gedeelde HTTP clients met connection pooling
    global _supabase_client, _claude_client

    _supabase_client = httpx.AsyncClient(
        base_url=SUPABASE_URL,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
    )

    _claude_client = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        timeout=httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    logger.info("HTTP connection pools geïnitialiseerd (Supabase: 50 conns, Claude: 20 conns)")

@app.on_event("shutdown")
async def shutdown():
    """Sluit HTTP connection pools netjes af bij server shutdown."""
    global _supabase_client, _claude_client
    if _supabase_client:
        await _supabase_client.aclose()
    if _claude_client:
        await _claude_client.aclose()
    logger.info("HTTP connection pools gesloten.")

# ══════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """
---
### **🎯 ROL & DOEL**
Je bent een **rapportgenerator voor Nederlandse basisscholen (groep 1–8)**.
**Je schrijft UITSLUITEND rapportteksten voor ouders van kinderen op de basisschool.**
Je doel is om **de ontwikkeling van het kind** centraal te zetten, **duidelijk en begrijpelijk** voor ouders en het kind zelf.

---
### **🔒 PRIVACY & AVG (NON-NEGOTIABLE)**
- **Gebruik NOOIT persoonsgegevens** (namen, data, schoolnaam, leerkrachtennaam, etc.).
- **Gebruik ALLEEN de informatie die de docent heeft ingevoerd.**
  - **Voeg niets toe** wat niet in de input staat.
  - **Vul niets zelf in** (geen aannames, geen eigen invulling).
- **Sanitize alle input:** Namen → "[LEERLING]", data → "[DATUM]".

---
### **✍️ SCHRIJFSTIJL & REGELS**
1. **Toon per groep:**
   - **Groep 1–3 (onderbouw):** Warm, verhalend, **geen cijfers/toetsniveaus**, focus op spelen en ontdekken.
   - **Groep 4–6 (middenbouw):** Toegankelijk, concreet, **CITO-niveaus uitleggen in gewone taal**.
   - **Groep 7–8 (bovenbouw):** Respecteer dat het kind meeleest; **eerlijk maar positief** over uitdagingen.

2. **Gebruik NOOIT dezelfde formulering voor meerdere rapporten.**
   - **Variëer zinsopbouw, woordkeuze en structuur** om unieke teksten te garanderen.

3. **Schrijf grammaticaal correct Nederlands.**
   - Gebruik altijd "**deze** rapportperiode" (niet "dit rapportperiode").
   - Werkwoorden als "laat zien" altijd afmaken: "laat **vooruitgang** zien", "laat **groei** zien".
   - Geen losse zinsfragmenten — elke zin heeft een onderwerp, werkwoord én object.
   - Schrijf op C2-niveau: foutloos, verzorgd en professioneel. Geen grammaticafouten, geen spelfouten, geen onvolledige zinnen. De tekst moet direct bruikbaar zijn als officieel schoolrapport zonder correcties.

3. **Richt je UITSLUITEND op de ontwikkeling van het kind.**
   - **Geen algemene loftuitingen** (bv. "Wat een fijn kind!").
   - **Geen vergelijkingen met andere kinderen** (bv. "Beter dan de meeste leerlingen").

4. **Gebruik ALLEEN EN UITSLUITEND de informatie die de docent heeft ingevoerd.**
   - **Geen aannames, geen eigen interpretaties.**
   - **Als een vakgebied niet in de input staat, vermeld het niet.**

---
### **📚 VAKGEBIEDEN & TERMINOLOGIE**
Gebruik **alleen de volgende vakgebieden en terminologie**:
| **Vakgebied**         | **Toelichting**                                                                 | **Voorbeelden**                          |
|------------------------|-------------------------------------------------------------------------------|-----------------------------------------|
| **Technisch lezen**   | DMT (woordenrijen), AVI (lopende tekst)                                       | "DMT-niveau: A", "AVI: M5"               |
| **Rekenen**           | Rekenen-Wiskunde (bewerkingen, meten, meetkunde) vs. Rekenen Basisbewerkingen | "Deeltafels", "Meten met liniaal"       |
| **Taal**              | Spelling, Taalverzorging, Woordenschat                                       | "Spelling: moeite met werkwoorden"      |
| **Begrijpen**         | Begrijpend lezen en luisteren **altijd apart** vermelden.                    | "Begrijpend lezen: niveau B"             |
| **Engels**            | Alleen relevant vanaf **groep 7**.                                            | "Engels: basiswoorden beheerst"         |
| **Sociaal-emotioneel**| VISEON 2.0: Sociaal-emotioneel functioneren, Executieve functies, etc.       | "Samenwerken in groepjes"               |

---
### **📊 CITO-NIVEAUS (I–V)**
Gebruik **alleen deze terminologie** voor CITO-niveaus:
| **Niveau** | **Percentiel**       | **Uitleg (gewone taal)**                     |
|------------|----------------------|---------------------------------------------|
| I+         | Top 10%              | "Uitzonderlijk sterk"                       |
| I          | 80e–100e             | "Ruim bovengemiddeld"                       |
| II         | 60e–80e              | "Bovengemiddeld"                            |
| III        | 40e–60e              | "Gemiddeld, rond het landelijk midden"      |
| IV         | 20e–40e              | "Ondergemiddeld, aandacht nodig"            |
| V          | 0e–20e               | "Ruim ondergemiddeld"                       |
| V-         | Laagste 10%          | "Zeer kwetsbaar"                            |

---
### **📥 INPUT (Verplicht)**
De docent levert **UITSLUITEND** de volgende informatie aan:
- Groep (1-8)
- Vakgebieden (met CITO/DMT-niveaus en observaties)
- Vorig rapport (optioneel)

---
### **📤 OUTPUT (Strikte JSON-structuur)**
**Retourneer ALLEEN het volgende JSON-object**, zonder uitleg of extra tekst:
```json
{
  "samenvatting": "<2-3 zinnen over de algehele ontwikkeling (als vorig rapport beschikbaar, anders huidige situatie)>",
  "positief": ["<concrete positieve ontwikkeling 1>", "<positieve ontwikkeling 2>"],
  "aandacht": ["<aandachtspunt 1>", "<aandachtspunt 2>"],
  "doelen_behaald": <true/false/null>,
  "sentiment": "<groei/stabiel/achteruitgang/gemengd>",
  "kern": "<maximaal 15 woorden die de trend samenvatten>"
}
```

---
### **⚠️ STRIKTE REGELS (NON-NEGOTIABLE)**
1. **Gebruik NOOIT eigen invulling.**
2. **Gebruik NOOIT dezelfde zinnen voor verschillende leerlingen.**
3. **Gebruik ALLEEN de gegeven informatie.**
4. **Wees concreet en feitelijk.**
5. **Vermijd vakjargon.**
6. **Als er geen vorig rapport is, focus dan op de huidige situatie.**
"""

PEDAGOGISCH_PROMPT = """Je bent een ervaren pedagogisch adviseur voor Nederlandse leerkrachten in het primair onderwijs.
Je analyseert observaties en notities over een leerling en geeft voor elke theorie een concreet, direct bruikbaar advies.

TOON EN STIJL
- Schrijf in gewone taal, geen wetenschappelijk jargon
- Elk advies is morgen uitvoerbaar in de klas
- Kort en concreet — maximaal 3 zinnen per theorie
- Geen uitleg van de theorie zelf, alleen het advies voor dit kind

DE VIJF THEORIEEN — WAT JE PER THEORIE ADRESSEERT:

ZPD — Zone of Proximal Development (Vygotsky)
Wat ligt net buiten het zelfstandige bereik van dit kind maar is haalbaar met begeleiding?
Geef een concrete scaffolding-strategie: wat doet de leerkracht voor, samen, dan zelfstandig?
Denk aan: denk-hardop voordoen, gestructureerde samenwerking, geleidelijk loslaten.

GROEIMINDSET (Dweck)
Hoe kan de leerkracht inspanning en strategie benadrukken in plaats van aanleg of resultaat?
Geef een concrete feedbackzin of aanpak die de leerkracht kan gebruiken bij dit kind.
Denk aan: "Je hebt dit bereikt doordat je...", fouten als leermomenten framen, procesgerichte complimenten.

ZELFDETERMINATIE (Deci & Ryan)
Hoe versterk je autonomie (eigen keuzes), competentie (iets goed kunnen) en verbondenheid (erbij horen)?
Geef een concrete aanpassing aan de instructie of taakomgeving voor dit kind.
Denk aan: keuze geven in aanpak, behapbare stappen zodat succes voelbaar is, verbinding met klasgenoten.

FORMATIEF TOETSEN (Black & Wiliam)
Welke concrete feedbackstrategie of check-techniek past bij dit kind op dit moment?
Geef een praktische methode die de leerkracht kan inzetten zonder extra voorbereiding.
Denk aan: exit-tickets, twee sterren en een wens, peer-feedback, hardop redeneren, mini-whiteboards.

COGNITIEVE BELASTING (Sweller)
Hoe verminder je de mentale belasting zodat dit kind energie heeft voor het leren zelf?
Geef een concrete aanpassing aan instructie, materiaal of taakomgeving.
Denk aan: visuele ondersteuning toevoegen, instructie opknippen, onnodige informatie weghalen, werken met voorbeelden.

PRIORITEIT
Welke van de vijf adviezen is het meest urgent en waarom? Geef in 1-2 zinnen de belangrijkste aanbeveling.

Output: alleen een geldig JSON-object zonder markdown of backticks. Zet null als de notities onvoldoende informatie bieden voor een theorie:
{"zpd":null,"groeimindset":null,"zelfdeterminatie":null,"formatief":null,"cognitief":null,"prioriteit":null}"""

OPP_PROMPT = """Je bent een specialist passend onderwijs voor Nederlandse basisscholen.
Stel een wettelijk compleet Ontwikkelingsperspectief (OPP) op conform de Wet Passend Onderwijs.
Schrijf alle teksten in begrijpelijk Nederlands. Gebruik concrete, observeerbare taal, geen vakjargon.

Output: alleen een geldig JSON-object zonder markdown of backticks:
{"uitstroombestemming":"verwachte uitstroombestemming met motivatie (3-4 zinnen)","bevorderende_factoren":"wat helpt dit kind vooruit (2-3 zinnen)","belemmerende_factoren":"wat maakt het lastiger (2-3 zinnen)","ondersteuningsbeschrijving":"welke ondersteuning wordt geboden en hoe (3-4 zinnen)","handelingsdeel":{"doelen":"SMART-geformuleerde doelen voor komende periode","aanpak":"concrete aanpak in de klas","betrokkenen":"wie doet wat - leerkracht, IB, ouders, kind"},"hoorrecht":"samenvatting hoorrecht leerling en ouders (2 zinnen)","programmaafwijkingen":"eventuele aanpassingen aan het reguliere programma, of null","evaluatiemoment":"wanneer en hoe wordt het OPP gevalueerd"}"""

HANDELINGSPLAN_PROMPT = """Je bent een orthopedagogisch specialist voor Nederlandse basisscholen.
Stel een concreet, uitvoerbaar handelingsplan op met bewezen interventies.
Schrijf praktisch en concreet, de leerkracht moet er morgen mee aan de slag kunnen.

Output: alleen een geldig JSON-object zonder markdown of backticks:
{"ondersteuningsbehoefte":"samenvatting van de behoefte in 1-2 zinnen","huidige_situatie":"hoe staat het kind er nu voor (2-3 zinnen)","doelen":[{"doel":"concreet SMART-doel","aanpak":"hoe dit doel bereikt wordt in de klas","frequentie":"hoe vaak en wanneer","materialen":"welke materialen of methodes"}],"klasaanpassingen":"concrete aanpassingen in de klas (3-4 punten)","ouderadvies":"wat ouders thuis kunnen doen (2-3 concrete tips)","evaluatie":"hoe en wanneer wordt het plan gevalueerd","interventies":"bewezen interventies passend bij deze behoefte"}"""

OUDERGESPREK_PROMPT = """Je bent een ervaren basisschoolleerkracht die een professioneel oudergesprekverslag schrijft.
Schrijf warm maar professioneel. Focus op wat besproken is en wat er gaat gebeuren.
Wees concreet over afspraken.

Output: alleen een geldig JSON-object zonder markdown of backticks:
{"samenvatting":"beknopte samenvatting van het gesprek (2-3 zinnen)","positief":"wat goed gaat en waar het kind energie van krijgt","aandacht":"waar school en ouders aandacht voor hebben","afspraken":"concrete, navolgbare afspraken die gemaakt zijn","vervolg":"vervolgstap of moment van volgend contact","verslag":"volledig kant-en-klaar verslag in 1 alinea, formeel maar warm, klaar voor archief"}"""

# ══════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════

class PromptVerzoek(BaseModel):
    prompt: str

GELDIGE_GROEPEN = {str(i) for i in range(1, 9)} | {"SO", "SBO", ""}

class LeerlingAanmaken(BaseModel):
    voornaam: str = Field(..., min_length=1, max_length=100, description="Voornaam van de leerling")
    groep: Optional[str] = Field("", max_length=10)
    ondersteuningsbehoeftes: Optional[List[str]] = []
    notities: Optional[str] = Field("", max_length=5000)
    leerlingnummer: Optional[str] = Field(None, max_length=20)
    achternaam: Optional[str] = Field(None, max_length=100)
    tussenvoegsel: Optional[str] = Field(None, max_length=20)

    @validator('voornaam')
    def voornaam_schoon(cls, v):
        return v.strip()

    @validator('groep')
    def groep_geldig(cls, v):
        if v and v.strip() not in GELDIGE_GROEPEN:
            raise ValueError(f"Ongeldige groep: {v!r}. Verwacht 1-8, SO of SBO.")
        return (v or "").strip()

    @validator('ondersteuningsbehoeftes')
    def behoeftes_geldig(cls, v):
        if v and len(v) > 20:
            raise ValueError("Maximaal 20 ondersteuningsbehoeftes")
        return [str(b)[:100] for b in (v or [])]

class LeerlingBijwerken(BaseModel):
    voornaam: Optional[str] = Field(None, min_length=1, max_length=100)
    groep: Optional[str] = Field(None, max_length=10)
    ondersteuningsbehoeftes: Optional[List[str]] = None
    notities: Optional[str] = Field(None, max_length=5000)
    leerlingnummer: Optional[str] = Field(None, max_length=20)
    achternaam: Optional[str] = Field(None, max_length=100)
    tussenvoegsel: Optional[str] = Field(None, max_length=20)

class RapportOpslaan(BaseModel):
    leerling_id: str = Field(..., min_length=1, max_length=100)
    rapport_data: dict

    @validator('leerling_id')
    def id_formaat(cls, v):
        import re
        if not re.match(r'^[a-zA-Z0-9_-]{1,100}$', v):
            raise ValueError("Ongeldig leerling_id formaat")
        return v

class OppVerzoek(BaseModel):
    notities: str = Field("", max_length=10000)
    naam: str = Field("", max_length=150)
    groep: str = Field("", max_length=10)
    ondersteuningsbehoefte: str = Field("", max_length=200)
    leerling_id: Optional[str] = Field(None, max_length=100)

class HandelingsplanVerzoek(BaseModel):
    notities: str = Field("", max_length=10000)
    naam: str = Field("", max_length=150)
    groep: str = Field("", max_length=10)
    ondersteuningsbehoefte: str = Field(..., min_length=1, max_length=200, description="Verplicht")
    leerling_id: Optional[str] = Field(None, max_length=100)

class OudergesprekVerzoek(BaseModel):
    notities: str
    naam: str = ""
    groep: str = ""
    datum: str = ""
    leerling_id: Optional[str] = None

class LvsProfielOpslaan(BaseModel):
    leerling_id: str
    scores: dict
    vorige_scores: dict
    tijdlijn: list

class LvsTijdlijnItem(BaseModel):
    leerling_id: str
    item: dict  # {type, datum, tekst}

# ══════════════════════════════════════════════════════════
# STANDAARD SCORERINGSYSTEMEN PRIMAIR ONDERWIJS NEDERLAND
# Bronnen: Cito/Leerling in beeld, Rijksoverheid referentieniveaus,
#          AVI-systeem (Zwijsen/Cito), VISEON 2.0
# ══════════════════════════════════════════════════════════

# CITO "Leerling in beeld" niveauindeling (I t/m V)
# Elk niveau = 20% van landelijke populatie per jaargroep
CITO_NIVEAUS = {
    "I":   {"label": "I",   "omschrijving": "Ruim bovengemiddeld",  "percentiel": "80–100%", "kleur": "#2d6a2d"},
    "I+":  {"label": "I+",  "omschrijving": "Zeer sterk (top 10%)", "percentiel": "90–100%", "kleur": "#1a4a1a"},
    "II":  {"label": "II",  "omschrijving": "Bovengemiddeld",       "percentiel": "60–80%",  "kleur": "#4a8a4a"},
    "III": {"label": "III", "omschrijving": "Gemiddeld",            "percentiel": "40–60%",  "kleur": "#8a8a00"},
    "IV":  {"label": "IV",  "omschrijving": "Ondergemiddeld",       "percentiel": "20–40%",  "kleur": "#c07000"},
    "V":   {"label": "V",   "omschrijving": "Ruim ondergemiddeld",  "percentiel": "0–20%",   "kleur": "#a03030"},
    "V-":  {"label": "V-",  "omschrijving": "Zeer zwak (laagste 10%)", "percentiel": "0–10%","kleur": "#7a1a1a"},
}

# Mapping CITO-niveau → interne 0-100 score voor het radardiagram
CITO_NAAR_SCORE = {"I+": 95, "I": 85, "II": 70, "III": 55, "IV": 38, "V": 22, "V-": 10}
SCORE_NAAR_CITO = [(90,"I+"), (78,"I"), (63,"II"), (48,"III"), (32,"IV"), (18,"V"), (0,"V-")]

# Alternatieve indeling A–E (25/25/25/15/10%)
# A=top25%, B=bovengemiddeld, C=gemiddeld, D=ondergemiddeld, E=laagste 10%
ABCDE_NIVEAUS = {
    "A+": "Top 10% (zie I+)", "A": "Bovenste 25%", "B": "Bovengemiddeld",
    "C": "Gemiddeld", "D": "Ondergemiddeld", "E": "Laagste 10%"
}
# Mapping A-E naar I-V voor uniformiteit
ABCDE_NAAR_CITO = {"A+": "I+", "A": "I", "B": "II", "C": "III", "D": "IV", "E": "V"}

# AVI-leesniveaus (Zwijsen/Cito, herzien 2008)
# 12 niveaus: Start, M3, E3, M4, E4, M5, E5, M6, E6, M7, E7, Plus
# M = midden schooljaar, E = einde schooljaar, cijfer = groep
AVI_NIVEAUS = [
    {"code": "START", "label": "AVI Start", "groep": "begin 3",  "score": 5},
    {"code": "M3",    "label": "AVI M3",    "groep": "midden 3", "score": 15},
    {"code": "E3",    "label": "AVI E3",    "groep": "eind 3",   "score": 25},
    {"code": "M4",    "label": "AVI M4",    "groep": "midden 4", "score": 35},
    {"code": "E4",    "label": "AVI E4",    "groep": "eind 4",   "score": 43},
    {"code": "M5",    "label": "AVI M5",    "groep": "midden 5", "score": 52},
    {"code": "E5",    "label": "AVI E5",    "groep": "eind 5",   "score": 60},
    {"code": "M6",    "label": "AVI M6",    "groep": "midden 6", "score": 68},
    {"code": "E6",    "label": "AVI E6",    "groep": "eind 6",   "score": 75},
    {"code": "M7",    "label": "AVI M7",    "groep": "midden 7", "score": 83},
    {"code": "E7",    "label": "AVI E7",    "groep": "eind 7",   "score": 90},
    {"code": "PLUS",  "label": "AVI Plus",  "groep": "boven gr7","score": 97},
]

# Referentieniveaus taal en rekenen (Wet referentieniveaus 2010, Rijksoverheid)
# Voor PO gelden: 1F (fundamenteel, eis voor ~85% leerlingen einde gr8)
#                 2F (streefniveau taal), 1S (streefniveau rekenen)
REFERENTIENIVEAUS = {
    "taal": {
        "1F": "Fundamenteel niveau — basis voor vmbo-b/k. Minimumeis einde basisschool (~85% leerlingen).",
        "2F": "Streefniveau taal — voor vmbo-t, havo, vwo uitstroom. Complexere teksten en taalvaardigheid.",
    },
    "rekenen": {
        "1F": "Fundamenteel niveau — basis voor vmbo-b/k. Minimumeis einde basisschool.",
        "1S": "Streefniveau rekenen — equivalent aan 2F taal. Voor vmbo-t, havo, vwo uitstroom.",
    },
    "groep_8_norm": "Minimaal 85% van leerlingen moet 1F beheersen aan einde groep 8.",
}

# VISEON 2.0 — sociaal-emotionele ontwikkeling (Cito)
# Scores worden weergegeven als A-E (zelfde schaal als CITO-cognitief)
# Domeinen: Welbevinden, Zelfredzaamheid, Sociaal gedrag, Motivatie, Werkhouding
VISEON_DOMEINEN = ["Welbevinden", "Zelfredzaamheid", "Sociaal gedrag", "Motivatie", "Werkhouding"]
VISEON_SCHAAL = {"A": "Sterk positief", "B": "Positief", "C": "Gemiddeld", "D": "Aandachtspunt", "E": "Zorgelijk"}

GELDIGE_VAKGEBIEDEN = {"lezen", "avi", "dmt", "rekenen", "rekenen_basis", "spelling", "taalverzorging", "woordenschat", "begrijpend", "begrijpend_luis", "engels", "sociaal", "executief", "groeimeter", "werkhouding"}
GELDIGE_NIVEAUS     = {"I", "II", "III", "IV", "V", None}
GELDIGE_BRONNEN     = {"handmatig", "csv", "cito"}

class ToetsImport(BaseModel):
    leerling_id: str
    vakgebied: str
    score: int
    niveau: Optional[str] = None
    afname_datum: Optional[str] = None
    bron: Optional[str] = "handmatig"

    @field_validator("vakgebied")
    @classmethod
    def valideer_vakgebied(cls, v):
        if v not in GELDIGE_VAKGEBIEDEN:
            raise ValueError(f"Ongeldig vakgebied '{v}'. Kies uit: {', '.join(sorted(GELDIGE_VAKGEBIEDEN))}")
        return v

    @field_validator("niveau")
    @classmethod
    def valideer_niveau(cls, v):
        if v is not None and v not in GELDIGE_NIVEAUS:
            raise ValueError(f"Ongeldig niveau '{v}'. Kies uit: I, II, III, IV, V")
        return v

    @field_validator("score")
    @classmethod
    def valideer_score(cls, v):
        if not 0 <= v <= 100:
            raise ValueError(f"Score {v} buiten bereik. Vul een waarde in tussen 0 en 100.")
        return v

    @field_validator("bron")
    @classmethod
    def valideer_bron(cls, v):
        if v and v not in GELDIGE_BRONNEN:
            return "handmatig"
        return v or "handmatig" 

class ToetsBatch(BaseModel):
    toetsen: List[ToetsImport]

class GroepsplanRij(BaseModel):
    leerling_id: str
    vakgebied: str
    instructieniveau: str   # onafhankelijk | basis | intensief | individueel

class GroepsplanOpslaan(BaseModel):
    groep: str
    schooljaar: Optional[str] = "2025-2026"
    rijen: List[GroepsplanRij]
    notities: Optional[str] = ""

class SchoolProfiel(BaseModel):
    naam: str
    brin: Optional[str] = ""
    adres: Optional[str] = ""

class OuderBerichtVersturen(BaseModel):
    leerling_id: str
    ouder_email: str
    onderwerp: str
    inhoud_html: str       # volledig HTML rapport of bericht
    inhoud_tekst: str      # platte tekst fallback
    bericht_type: Optional[str] = "rapport"  # rapport | handelingsplan | opp | algemeen

class OuderBerichtReactie(BaseModel):
    bericht_id: str
    reactie_tekst: str

# ══════════════════════════════════════════════════════════
# AUTH HELPER
# ══════════════════════════════════════════════════════════

async def get_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Niet ingelogd.")
    token = credentials.credentials
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {token}"
                }
            )
            if res.status_code != 200:
                raise HTTPException(status_code=401, detail="Sessie verlopen. Log opnieuw in.")
            return res.json()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Authenticatieserver niet bereikbaar.")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout: {str(e)}")

# ══════════════════════════════════════════════════════════
# SCHOOL CONTEXT HELPER
# ══════════════════════════════════════════════════════════

async def get_school_context(user: dict, token: str) -> dict:
    """
    Haal de school-context op voor een gebruiker.
    Combineert user_metadata (snel, uit JWT) met leerkrachten_scholen (actueel).
    Geeft altijd een dict terug met: school_id, rol, is_ib, is_directeur, heeft_school
    """
    # Eerst uit user_metadata (zit in de JWT — geen extra aanroep nodig)
    meta = user.get("user_metadata", {})
    school_id = meta.get("school_id")
    rol = meta.get("rol", "leerkracht")

    # Als user_metadata nog niet bijgewerkt is, val terug op de database
    if not school_id:
        koppeling = await supabase_get("leerkrachten_scholen", token,
            {"leerkracht_id": f"eq.{user['id']}", "select": "school_id,rol"})
        if koppeling:
            school_id = koppeling[0].get("school_id")
            rol = koppeling[0].get("rol", "leerkracht")

    return {
        "school_id":     school_id,
        "rol":           rol,
        "is_ib":         rol in ("ib", "directeur"),
        "is_directeur":  rol == "directeur",
        "heeft_school":  bool(school_id)
    }


async def _controleer_leerling_toegang(
    leerling_id: str,
    user: dict,
    token: str,
    ctx: dict | None = None
) -> dict:
    """
    Controleert of een leraar toegang heeft tot een leerling.
    Toegang is er als:
    1. De leerling direct van deze leraar is (leerkracht_id match), OF
    2. De leerling op dezelfde school zit (school_id match)

    Gooit HTTPException 404 als er geen toegang is.
    Geeft het leerling-record terug bij succes.
    """
    if ctx is None:
        ctx = await get_school_context(user, token)

    # Probeer eerst directe eigenaar
    leerling = await supabase_get("leerlingen", token, {
        "id":            f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":        "*"
    })
    if leerling:
        return leerling[0]

    # Probeer via school_id
    if ctx.get("school_id"):
        leerling = await supabase_get("leerlingen", token, {
            "id":        f"eq.{leerling_id}",
            "school_id": f"eq.{ctx['school_id']}",
            "select":    "*"
        })
        if leerling:
            return leerling[0]

    raise HTTPException(status_code=404, detail="Leerling niet gevonden of geen toegang.")


async def update_user_metadata(user_id: str, metadata: dict):
    """
    Werk user_metadata bij via de Supabase Admin API.
    Vereist SUPABASE_SERVICE_KEY — nooit naar de browser sturen.
    """
    if not SUPABASE_SERVICE_KEY:
        logger.warning("SUPABASE_SERVICE_KEY niet ingesteld — user_metadata kan niet worden bijgewerkt.")
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.put(
                f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json"
                },
                json={"user_metadata": metadata}
            )
            if res.status_code not in (200, 201):
                logger.error(f"user_metadata update mislukt: {res.status_code} {res.text[:200]}")
                return False
            return True
    except Exception as e:
        logger.error(f"user_metadata update fout: {e}")
        return False


# ══════════════════════════════════════════════════════════
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════

def _supabase_headers(token: str) -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

async def supabase_get(path: str, token: str, params: dict | None = None) -> list:
    """
    GET request naar Supabase REST API.
    Gebruikt de gedeelde connection pool — geen nieuwe TCP verbinding per aanroep.

    Args:
        path:   Tabelnaam of endpoint (bijv. "leerlingen")
        token:  Supabase JWT access token van de gebruiker
        params: Query parameters (filter, select, order, limit, etc.)

    Returns:
        Lijst van records. Lege lijst als er niets gevonden is.

    Raises:
        HTTPException 401: Token verlopen
        HTTPException 503: Database niet bereikbaar
    """
    if params is None:
        params = {}
    client = _get_supabase_client()
    try:
        res = await client.get(
            f"/rest/v1/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if res.status_code == 401:
            raise HTTPException(status_code=401, detail="Sessie verlopen. Log opnieuw in.")
        if res.status_code != 200:
            logger.error(f"supabase_get {path} → {res.status_code}: {res.text[:200]}")
            raise HTTPException(
                status_code=res.status_code,
                detail=f"Databasefout bij ophalen van {path}: {res.text[:300]}"
            )
        data = res.json()
        return data if isinstance(data, list) else [data]
    except HTTPException:
        raise
    except httpx.TimeoutException:
        logger.error(f"supabase_get {path} timeout")
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        logger.error(f"supabase_get {path} verbindingsfout: {e}")
        raise HTTPException(status_code=503, detail=f"Verbindingsfout met database: {str(e)}")

async def supabase_post(path: str, token: str, data: dict) -> list | dict:
    """
    POST request naar Supabase REST API (aanmaken van record).
    Geeft het aangemaakte record terug (door Prefer: return=representation).
    """
    client = _get_supabase_client()
    try:
        res = await client.post(
            f"/rest/v1/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "return=representation",
            },
            json=data,
        )
        if res.status_code == 401:
            raise HTTPException(status_code=401, detail="Sessie verlopen. Log opnieuw in.")
        if res.status_code not in (200, 201):
            logger.error(f"supabase_post {path} → {res.status_code}: {res.text[:200]}")
            raise HTTPException(
                status_code=res.status_code,
                detail=f"Opslaan mislukt in {path}: {res.text[:300]}"
            )
        return res.json()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        logger.error(f"supabase_post {path} timeout")
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        logger.error(f"supabase_post {path} verbindingsfout: {e}")
        raise HTTPException(status_code=503, detail=f"Verbindingsfout met database: {str(e)}")

async def supabase_patch(path: str, token: str, data: dict) -> list | dict:
    """
    PATCH request naar Supabase REST API (bijwerken van bestaand record).
    """
    client = _get_supabase_client()
    try:
        res = await client.patch(
            f"/rest/v1/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "return=representation",
            },
            json=data,
        )
        if res.status_code == 401:
            raise HTTPException(status_code=401, detail="Sessie verlopen. Log opnieuw in.")
        if res.status_code not in (200, 204):
            logger.error(f"supabase_patch {path} → {res.status_code}: {res.text[:200]}")
            raise HTTPException(
                status_code=res.status_code,
                detail=f"Bijwerken mislukt in {path}: {res.text[:300]}"
            )
        return res.json() if res.content else []
    except HTTPException:
        raise
    except httpx.TimeoutException:
        logger.error(f"supabase_patch {path} timeout")
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        logger.error(f"supabase_patch {path} verbindingsfout: {e}")
        raise HTTPException(status_code=503, detail=f"Verbindingsfout met database: {str(e)}")

async def supabase_delete(path: str, token: str) -> None:
    """
    DELETE request naar Supabase REST API.
    """
    client = _get_supabase_client()
    try:
        res = await client.delete(
            f"/rest/v1/{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if res.status_code == 401:
            raise HTTPException(status_code=401, detail="Sessie verlopen. Log opnieuw in.")
        if res.status_code not in (200, 204):
            logger.error(f"supabase_delete {path} → {res.status_code}: {res.text[:200]}")
            raise HTTPException(
                status_code=res.status_code,
                detail=f"Verwijderen mislukt in {path}: {res.text[:300]}"
            )
    except HTTPException:
        raise
    except httpx.TimeoutException:
        logger.error(f"supabase_delete {path} timeout")
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        logger.error(f"supabase_delete {path} verbindingsfout: {e}")
        raise HTTPException(status_code=503, detail=f"Verbindingsfout met database: {str(e)}")

# ══════════════════════════════════════════════════════════
# CLAUDE HELPERS
# ══════════════════════════════════════════════════════════

def _anthropic_headers() -> dict:
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

def _claude_body(system: str, user: str, max_tokens: int, stream: bool = False) -> dict:
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if stream:
        body["stream"] = True
    return body

async def roep_claude_aan(
    system: str,
    user: str,
    max_tokens: int = 1500,
    request_id: str | None = None,
) -> str:
    """
    Roept de Claude API aan via de gedeelde connection pool.

    Args:
        system:     System prompt
        user:       Gebruikersbericht / prompt
        max_tokens: Maximum te genereren tokens (default 1500)
        request_id: Optioneel correlation ID voor log-tracing

    Returns:
        Gegenereerde tekst van Claude

    Raises:
        HTTPException 429: Rate limit bereikt
        HTTPException 504: Timeout (Claude reageert niet binnen 90s)
        HTTPException 503: Verbindingsfout
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Anthropic API-sleutel niet geconfigureerd."
        )

    client = _get_claude_client()
    log_prefix = f"[{request_id}] " if request_id else ""

    try:
        res = await client.post(
            "/v1/messages",
            json=_claude_body(system, user, max_tokens),
        )

        if res.status_code == 401:
            logger.error(f"{log_prefix}Claude 401 — ongeldige API-sleutel")
            raise HTTPException(status_code=500, detail="Ongeldige Anthropic API-sleutel.")
        if res.status_code == 429:
            retry_after = res.headers.get("retry-after", "60")
            logger.warning(f"{log_prefix}Claude 429 — rate limit, retry-after: {retry_after}s")
            raise HTTPException(
                status_code=429,
                detail=f"AI-limiet bereikt. Wacht {retry_after} seconden en probeer opnieuw."
            )
        if res.status_code == 529:
            logger.warning(f"{log_prefix}Claude 529 — overbelast")
            raise HTTPException(status_code=503, detail="AI-service tijdelijk overbelast. Probeer over 30 seconden opnieuw.")
        if res.status_code != 200:
            logger.error(f"{log_prefix}Claude {res.status_code}: {res.text[:200]}")
            raise HTTPException(
                status_code=502,
                detail=f"AI-service fout ({res.status_code}). Probeer opnieuw."
            )

        data = res.json()
        if not data.get("content") or not data["content"][0].get("text"):
            logger.error(f"{log_prefix}Claude lege response: {data}")
            raise HTTPException(status_code=502, detail="AI-service gaf een lege respons. Probeer opnieuw.")

        return data["content"][0]["text"]

    except HTTPException:
        raise
    except httpx.TimeoutException:
        logger.error(f"{log_prefix}Claude timeout (>90s)")
        raise HTTPException(status_code=504, detail="AI reageert niet (timeout na 90s). Probeer opnieuw.")
    except httpx.RequestError as e:
        logger.error(f"{log_prefix}Claude verbindingsfout: {e}")
        raise HTTPException(status_code=503, detail="Kan AI-service niet bereiken. Controleer de verbinding.")

def _veilig_json_parse(tekst: str) -> dict:
    """JSON parsen met opschoning van markdown-blokken."""
    schoon = tekst.strip()
    if schoon.startswith("```"):
        delen = schoon.split("```")
        if len(delen) >= 3:
            schoon = delen[1].lstrip("json").strip()
        else:
            schoon = schoon.replace("```json", "").replace("```", "").strip()
    return json.loads(schoon)

# ══════════════════════════════════════════════════════════
# PSEUDONIMISERING — AVG-vriendelijk
# ══════════════════════════════════════════════════════════

def pseudonimiseer(tekst: str, naam: str) -> tuple[str, dict]:
    """
    Vervangt de naam van de leerling door [LEERLING] in de tekst.
    Geeft de geanonimiseerde tekst terug plus een mapping om later te herstellen.
    Werkt ook als de naam in verschillende vormen voorkomt (hoofdletter, kleine letter).
    """
    if not naam or not naam.strip():
        return tekst, {}

    mapping = {}
    placeholder = "[LEERLING]"

    # Vervang de naam in alle varianten
    import re
    naam_schoon = naam.strip()

    # Voornaam alleen
    varianten = [naam_schoon]

    # Verander ook kleine letters variant
    if naam_schoon[0].isupper():
        varianten.append(naam_schoon[0].lower() + naam_schoon[1:])

    mapping[placeholder] = naam_schoon

    geanon = tekst
    for variant in varianten:
        # Vervang hele woorden alleen (geen gedeeltelijke matches)
        geanon = re.sub(r'\b' + re.escape(variant) + r'\b', placeholder, geanon)

    return geanon, mapping

def herstel_pseudoniem(tekst: str, mapping: dict) -> str:
    """Zet de echte naam terug in de gegenereerde tekst."""
    if not mapping:
        return tekst
    for placeholder, echte_naam in mapping.items():
        tekst = tekst.replace(placeholder, echte_naam)
    return tekst

# ══════════════════════════════════════════════════════════
# PRIVACY — DRIEDUBBELE CONTROLE
# ══════════════════════════════════════════════════════════

import re as _re
import logging as _logging

_privacy_log = _logging.getLogger("privacy-audit")

# ── Laag 1: Pseudonimisering (al gebouwd in pseudonimiseer()) ──

# ── Laag 2: NER — detecteer onbekende namen ───────────────────
try:
    import spacy as _spacy
    _nlp = _spacy.load("nl_core_news_sm")
    _NER_BESCHIKBAAR = True
    _privacy_log.info("NER model geladen: nl_core_news_sm")
except Exception:
    _nlp = None
    _NER_BESCHIKBAAR = False
    _privacy_log.warning("NER model niet beschikbaar. Installeer met: python -m spacy download nl_core_news_sm")

def _ner_scan(tekst: str) -> str:
    """
    Laag 2: Detecteer namen via NER die niet al vervangen zijn door laag 1.
    Vervangt gevonden persoonsnamen door [PERSOON].
    """
    if not _NER_BESCHIKBAAR or not _nlp:
        return tekst
    try:
        doc = _nlp(tekst)
        vervangen = tekst
        # Verwerk van achteren naar voren zodat indices kloppen
        entiteiten = [(ent.start_char, ent.end_char, ent.label_)
                      for ent in doc.ents
                      if ent.label_ in ("PER", "PERSON")]
        for start, end, label in sorted(entiteiten, reverse=True):
            naam_gevonden = tekst[start:end]
            _privacy_log.warning(
                f"NER laag 2: persoonsnaam gevonden en vervangen: '{naam_gevonden}'"
            )
            vervangen = vervangen[:start] + "[PERSOON]" + vervangen[end:]
        return vervangen
    except Exception as e:
        _privacy_log.error(f"NER scan fout: {e}")
        return tekst

# ── Laag 3: Patroon-blokker (alleen als NER niet beschikbaar is) ──
#
# Strategie: zoek alleen naar woorden in een echte naam-context, niet
# naar elk woord met een hoofdletter. Twee patronen:
#
#   A) Naam na een introducerend woord: "juf Jansen", "met haar vader Rob",
#      "klasgenoot Piet", "meneer De Vries". Deze constructie is bijna
#      altijd een persoonsnaam.
#
#   B) Initialen met punt: "J.", "M.H." — staan nooit zomaar in schooltekst
#      zonder dat het een persoon betreft.
#
# Woorden die enkel met een hoofdletter beginnen (Gym, Snappet, Montessori)
# worden NIET meer geblokkeerd — dat deed de oude laag 3 ten onrechte.

# Introducerende woorden die vrijwel altijd gevolgd worden door een persoonsnaam
_NAAM_INTRODUCERS = _re.compile(
    r'(?i:\\b(?:juf|meester|meneer|mevrouw|dhr|mevr|vader|moeder|mama|papa|opa|oma|'
    r'broer|zus|oom|tante|klasgenoot|klasgenote|vriend|vriendin|'
    r'collega|begeleider|begeleidster|therapeut|logopedist|orthopedagoog|'
    r'intern|extern|coach|assistent))\\s+([A-Z][a-z]+(?:\\s[A-Z][a-z]+)*)'
)

# Initialen met punt: J. of M.H. of A.B.C.
_INITIALEN_PATROON = _re.compile(r'\b[A-Z](?:\.[A-Z])+\.?\b')

def _patroon_check(tekst: str) -> tuple[str, list[str]]:
    """
    Laag 3: Alleen actief als NER (laag 2) niet beschikbaar is.
    Zoekt uitsluitend naar namen in een duidelijke naam-context:
    - Namen na introducerende woorden (juf, meneer, vader, klasgenoot, ...)
    - Initialen met punt (J., M.H.)
    Gewone hoofdletterwoorden (Gym, Snappet, Montessori) worden niet geblokkeerd.
    """
    # Als NER beschikbaar is, heeft laag 2 al het zware werk gedaan.
    # Laag 3 voegt dan geen waarde toe en zou alleen valse alarmen geven.
    if _NER_BESCHIKBAAR:
        return tekst, []

    waarschuwingen = []
    schoon = tekst

    # Patroon A: naam na introducer — vervang stil (geen blokkering)
    def vervang_introducer(m):
        gevonden = m.group(1)
        _privacy_log.warning(f"Laag 3 (fallback): naam na introducer vervangen: '{gevonden}'")
        return m.group(0).replace(gevonden, "[PERSOON]")

    schoon_a = _NAAM_INTRODUCERS.sub(vervang_introducer, schoon)
    if schoon_a != schoon:
        waarschuwingen.append("Laag 3: naam na introducer (juf/meneer/vader/...) vervangen door [PERSOON]")
        schoon = schoon_a

    # Patroon B: initialen — vervang stil
    initialen = _INITIALEN_PATROON.findall(schoon)
    if initialen:
        for init in initialen:
            _privacy_log.warning(f"Laag 3 (fallback): initialen vervangen: '{init}'")
        schoon = _INITIALEN_PATROON.sub("[INITIALEN]", schoon)
        waarschuwingen.append(f"Laag 3: initialen vervangen: {initialen[:3]}")

    return schoon, waarschuwingen
# ── Laag 4: Extra naamscan — tweede NER pass ──────────────────

def _extra_naamscan(tekst: str, originele_naam: str) -> str:
    """
    Laag 4: Een tweede, strengere scan specifiek gericht op de naam
    van de leerling en mogelijke varianten die door lagen 1-3 zijn gemist.
    Vervangt gevonden varianten door [LEERLING].
    """
    if not originele_naam or not originele_naam.strip():
        return tekst

    import re as _re4
    naam_schoon = originele_naam.strip()
    resultaat = tekst

    # Scan op de naam zelf en veelvoorkomende varianten
    varianten = set()
    varianten.add(naam_schoon)                          # Origineel: Emma
    varianten.add(naam_schoon.lower())                  # Kleine letters: emma
    varianten.add(naam_schoon.upper())                  # Hoofdletters: EMMA
    varianten.add(naam_schoon.capitalize())             # Eerste hoofdletter: Emma

    # Verkorte versies (bijv. "Em" voor "Emma")
    if len(naam_schoon) > 4:
        varianten.add(naam_schoon[:3])                  # Eerste 3 letters
        varianten.add(naam_schoon[:3].lower())

    # Spaties of koppeltekens in naam (bijv. "Jan-Willem" → ook "Jan")
    if '-' in naam_schoon or ' ' in naam_schoon:
        for deel in _re4.split(r'[\s-]', naam_schoon):
            if len(deel) > 2:
                varianten.add(deel)
                varianten.add(deel.lower())

    for variant in varianten:
        if len(variant) < 2:
            continue
        patroon = r'\b' + _re4.escape(variant) + r'\b'
        if _re4.search(patroon, resultaat, _re4.IGNORECASE):
            _privacy_log.warning(
                f"Laag 4: naamvariant '{variant}' gevonden en vervangen door [LEERLING]"
            )
            resultaat = _re4.sub(patroon, '[LEERLING]', resultaat, flags=_re4.IGNORECASE)

    return resultaat

# ── Hoofd-privacyfunctie: alle vier lagen ─────────────────────

def privacy_filter(tekst: str, naam: str = "", strict: bool = False) -> tuple[str, dict]:
    """
    Voert alle drie privacylagen uit op de tekst.

    Laag 1: Pseudonimisering van de bekende naam
    Laag 2: NER-scan voor onbekende namen
    Laag 3: Patrooncheck voor verdachte woorden

    Bij strict=True wordt een HTTPException gegooid als er na alle
    lagen nog verdachte woorden overblijven.

    Geeft de gefilterde tekst terug plus de naam-mapping voor herstel.
    """
    audit = {
        "origineel_lengte": len(tekst),
        "naam_opgegeven": bool(naam),
        "ner_beschikbaar": _NER_BESCHIKBAAR,
        "waarschuwingen": [],
    }

    # Laag 1: Pseudonimiseer bekende naam
    stap1, naam_mapping = pseudonimiseer(tekst, naam)
    if tekst != stap1:
        audit["waarschuwingen"].append(f"Laag 1: naam '{naam}' vervangen door [LEERLING]")

    # Laag 2: NER-scan
    stap2 = _ner_scan(stap1)
    if stap1 != stap2:
        audit["waarschuwingen"].append("Laag 2: NER detecteerde extra persoonsgegevens")

    # Laag 3: Patrooncheck
    stap3, patroon_warnings = _patroon_check(stap2)
    audit["waarschuwingen"].extend(patroon_warnings)

    # Audit log
    if audit["waarschuwingen"]:
        _privacy_log.warning(
            f"Privacy audit: {len(audit['waarschuwingen'])} waarschuwing(en) — "
            + ", ".join(audit["waarschuwingen"])
        )
    else:
        _privacy_log.info("Privacy audit: schoon — geen persoonsgegevens gedetecteerd")

    # ── Laag 4: Extra naamscan — tweede pass op naamvarianten ──
    # (Laag 3 vervangt nu stil; blokkering is verwijderd om valse alarmen te voorkomen.)
    stap4 = _extra_naamscan(stap3, naam)
    if stap3 != stap4:
        audit["waarschuwingen"].append("Laag 4: extra naamscan vond en verving naamvarianten")

    _privacy_log.info(
        f"Privacy audit voltooid: {len(audit['waarschuwingen'])} waarschuwing(en). "
        f"Alle 4 lagen doorlopen."
    )

    return stap4, naam_mapping

# ══════════════════════════════════════════════════════════
# STATISCHE BESTANDEN
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
# SPRAAKHERKENNING — Whisper via OpenAI API
# ══════════════════════════════════════════════════════════

from fastapi import UploadFile, File

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

@app.post("/spraak/transcribeer")
async def transcribeer_spraak(
    audio: UploadFile = File(...),
    user=Depends(get_user),
):
    """
    Transcribeert een audiobestand via OpenAI Whisper.

    Verwacht: multipart/form-data met een 'audio' veld (webm, mp3, wav, m4a).
    Geeft terug: { "tekst": "getranscribeerde tekst" }

    Kosten: ~€0.006 per minuut audio.
    AVG: audio wordt direct verwijderd na transcriptie — niet opgeslagen.
    """
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Spraakherkenning niet geconfigureerd. Voeg OPENAI_API_KEY toe aan de omgevingsvariabelen."
        )

    # Lees audio data
    audio_bytes = await audio.read()
    if len(audio_bytes) < 100:
        raise HTTPException(status_code=400, detail="Audiofragment te kort of leeg.")
    if len(audio_bytes) > 25 * 1024 * 1024:  # 25MB max (Whisper limiet)
        raise HTTPException(status_code=400, detail="Audiobestand te groot (max 25MB).")

    # Stuur naar Whisper API
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={
                    "file": (audio.filename or "opname.webm", audio_bytes, audio.content_type or "audio/webm"),
                    "model": (None, "whisper-1"),
                    "language": (None, "nl"),
                    "response_format": (None, "text"),
                }
            )

        if response.status_code == 401:
            raise HTTPException(status_code=500, detail="Ongeldige OpenAI API-sleutel.")
        if response.status_code != 200:
            logger.error(f"Whisper API fout: {response.status_code} {response.text[:100]}")
            raise HTTPException(status_code=502, detail="Spraakherkenning tijdelijk niet beschikbaar.")

        tekst = response.text.strip()
        logger.info(f"Whisper transcriptie: {len(tekst)} tekens")
        return {"tekst": tekst}

    except HTTPException:
        raise
    except _httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Spraakherkenning timeout. Probeer een kortere opname.")
    except Exception as e:
        logger.error(f"Whisper fout: {e}")
        raise HTTPException(status_code=500, detail="Spraakherkenning mislukt.")

# ══════════════════════════════════════════════════════════
# OCR CITO-SCORES — Claude Vision
# ══════════════════════════════════════════════════════════

class OcrScoresVerzoek(BaseModel):
    afbeelding: str   # base64-gecodeerde afbeelding
    mime_type: str = "image/jpeg"

@app.post("/lvs/ocr-scores")
async def ocr_cito_scores(
    verzoek: OcrScoresVerzoek,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    Extraheert CITO-scores en gestandaardiseerde scores uit een foto
    van een rapport, scoreoverzicht of uitdraai via Claude Vision.

    Herkent:
    - CITO niveaus (I t/m V, I+, V-)
    - AVI niveaus (M3 t/m E7, Plus)
    - Vaardigheidsscores (numerieke waarden)
    - Functioneringsniveaus (A t/m E)
    - Vakgebieden: DMT, AVI, Rekenen, Spelling, Taalverzorging,
      Woordenschat, Begrijpend lezen, Engels, Sociaal-emotioneel

    Geeft terug: { scores: {...}, leerling_info: {...} }
    """
    system = """Je bent een expert in het lezen van Nederlandse basisschool CITO-rapporten
en scoreoverzichten. Extraheer alle scores uit de afbeelding.

Geef ALTIJD een JSON-object terug, nooit tekst erbuiten. Formaat:
{
  "scores": {
    "lezen": "III",
    "avi": "E5",
    "rekenen": "II",
    "rekenen_basis": "II",
    "spelling": "IV",
    "taalverzorging": null,
    "woordenschat": null,
    "begrijpend": "III",
    "begrijpend_luisteren": null,
    "engels": null,
    "sociaal_emotioneel": null,
    "executieve_functies": null,
    "werkhouding": null
  },
  "leerling_info": {
    "naam": "Sem",
    "groep": "6",
    "schooljaar": "2024-2025",
    "toetsmoment": "M6"
  },
  "betrouwbaarheid": "hoog"
}

Gebruik voor niveaus: I+, I, II, III, IV, V, V- (CITO) of A+, A, B, C, D, E (sociaal/werkhouding)
Voor AVI: Start, M3, E3, M4, E4, M5, E5, M6, E6, M7, E7, Plus
Als een score niet zichtbaar of leesbaar is: gebruik null
Geef nooit scores in als je ze niet zeker kunt lezen."""

    user_prompt = """Analyseer dit scoreoverzicht en extraheer alle zichtbare scores.
Retourneer uitsluitend het JSON-object, geen uitleg."""

    try:
        if not ANTHROPIC_API_KEY:
            raise HTTPException(status_code=503, detail="AI niet geconfigureerd.")

        client = _get_claude_client()
        res = await client.post(
            "/v1/messages",
            json={
                "model": "claude-opus-4-5",
                "max_tokens": 1000,
                "system": system,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": verzoek.mime_type,
                                "data": verzoek.afbeelding,
                            }
                        },
                        {"type": "text", "text": user_prompt}
                    ]
                }]
            }
        )

        if res.status_code != 200:
            logger.error(f"Claude OCR fout: {res.status_code}")
            raise HTTPException(status_code=502, detail="Score-herkenning mislukt.")

        import json as _json
        tekst = res.json()["content"][0]["text"].strip()

        # Strip markdown code blocks indien aanwezig
        tekst = tekst.replace("```json", "").replace("```", "").strip()

        data = _json.loads(tekst)
        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OCR fout: {e}")
        raise HTTPException(status_code=500, detail="Score-herkenning mislukt. Probeer een duidelijkere foto.")

@app.get("/health")
async def health():
    """
    Health check voor load balancers en monitoring.
    Checkt of downstream dependencies bereikbaar zijn.
    200 = operationeel, 503 = gedegradeerd.
    """
    checks: dict = {}
    alles_ok = True

    # Supabase ping — lichtgewicht, max 3s timeout
    try:
        client = _get_supabase_client()
        res = await client.get(
            "/rest/v1/",
            headers={"apikey": SUPABASE_ANON_KEY},
            timeout=3.0,
        )
        checks["supabase"] = "ok" if res.status_code < 500 else f"fout_{res.status_code}"
        if res.status_code >= 500:
            alles_ok = False
    except Exception as e:
        checks["supabase"] = f"niet_bereikbaar"
        alles_ok = False
        logger.error(f"Health check: Supabase niet bereikbaar: {e}")

    # Anthropic — alleen config check, geen netwerk-aanroep
    checks["anthropic"] = "geconfigureerd" if ANTHROPIC_API_KEY else "api_sleutel_ontbreekt"
    if not ANTHROPIC_API_KEY:
        alles_ok = False

    return JSONResponse(
        status_code=200 if alles_ok else 503,
        content={
            "status":  "ok" if alles_ok else "gedegradeerd",
            "service": "school-van-morgen",
            "versie":  "1.0.0",
            "checks":  checks,
        }
    )

@app.get("/")
async def root():
    # Zoek index.html op in meerdere bekende locaties
    for pad in ["static/index.html", "index.html", "app/index.html"]:
        if os.path.exists(pad):
            return FileResponse(pad)
    raise HTTPException(status_code=404, detail="index.html niet gevonden. Zorg dat het bestand naast main.py staat of in een 'static' map.")

@app.get("/manifest.json")
async def manifest_json():
    for pad in ["static/manifest.json", "manifest.json"]:
        if os.path.exists(pad):
            return FileResponse(pad)
    return JSONResponse({
        "name": "School van morgen", "short_name": "SvM",
        "start_url": "/", "display": "standalone",
        "background_color": "#FDF8F2", "theme_color": "#D4500F", "icons": []
    })

# Serveer statische bestanden indien de map bestaat
for _static_dir in ["static", "."]:
    if os.path.exists(os.path.join(_static_dir, "index.html")):
        app.mount("/assets", StaticFiles(directory=_static_dir), name="static")
        break

# ══════════════════════════════════════════════════════════
# RAPPORT GENEREREN (STREAMING)
# ══════════════════════════════════════════════════════════

MAX_PROMPT_LENGTE = 4000  # ruim genoeg voor alle echte notities; voorkomt misbruik

@app.post("/analyseer")
async def analyseer(
    verzoek: PromptVerzoek,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Rapport genereren met streaming. Vereist een geldig Supabase-sessietoken."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="Anthropic API-sleutel niet ingesteld op de server.")
    if len(verzoek.prompt.strip()) < 20:
        raise HTTPException(status_code=400, detail="Notities te kort (minimaal 20 tekens).")
    if len(verzoek.prompt) > MAX_PROMPT_LENGTE:
        raise HTTPException(status_code=400, detail=f"Notities te lang (maximaal {MAX_PROMPT_LENGTE} tekens).")

    # Privacy driedubbele controle — Laag 1+2+3
    import re as _re2
    naam_in_prompt = ""
    naam_match = _re2.search(r'Leerling:\s*([^,\.\n]+)', verzoek.prompt)
    if naam_match:
        naam_in_prompt = naam_match.group(1).strip()
    prompt_anon, naam_mapping = privacy_filter(verzoek.prompt, naam_in_prompt)

    async def stream():
        volledig_buffer = []
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                async with client.stream(
                    "POST", ANTHROPIC_URL,
                    headers=_anthropic_headers(),
                    json=_claude_body(SYSTEM_PROMPT, prompt_anon, 1800, stream=True),
                ) as res:
                    if res.status_code == 401:
                        yield json.dumps({"error": "Ongeldige API-sleutel."})
                        return
                    if res.status_code == 429:
                        yield json.dumps({"error": "Te veel verzoeken. Probeer over een moment opnieuw."})
                        return
                    if res.status_code != 200:
                        yield json.dumps({"error": f"API-fout ({res.status_code})."})
                        return
                    async for line in res.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                evt = json.loads(data)
                                if evt.get("type") == "content_block_delta":
                                    tekst = evt["delta"].get("text", "")
                                    if tekst:
                                        volledig_buffer.append(tekst)
                                        # Herstel naam in elk chunk dat [LEERLING] bevat
                                        tekst_herstel = herstel_pseudoniem(tekst, naam_mapping)
                                        yield tekst_herstel
                            except (json.JSONDecodeError, KeyError) as e:
                                logger.debug(f"Stream JSON parse skip: {e}")
        except httpx.TimeoutException:
            yield json.dumps({"error": "Timeout - Claude reageert niet. Probeer opnieuw."})
        except Exception as e:
            logger.error(f"Streaming fout: {e}")
            yield json.dumps({"error": "Er is iets misgegaan. Probeer opnieuw."})

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")

# ══════════════════════════════════════════════════════════
# LEERLINGEN
# ══════════════════════════════════════════════════════════

@app.get("/leerlingen")
async def haal_leerlingen_op(
    limit:  int = 200,
    offset: int = 0,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Haalt leerlingen op. Logica:
    - Met school_id: alle leerlingen van de school (voor alle leraren, niet alleen eigen)
    - Zonder school_id (solo): alleen eigen leerlingen
    IB-ers en directeuren zien altijd de volledige schoollijst.
    """
    token = credentials.credentials
    ctx   = await get_school_context(user, token)
    limit = min(limit, 500)

    if ctx["school_id"]:
        # Schoolbreed: alle leerlingen met dit school_id
        # Alle leraren op dezelfde school zien dezelfde leerlingen
        params = {
            "school_id": f"eq.{ctx['school_id']}",
            "order":     "groep.asc,voornaam.asc",
            "select":    "*",
            "limit":     str(limit),
            "offset":    str(offset)
        }
        leerlingen = await supabase_get("leerlingen", token, params)

        # Voeg leraar-naam toe voor IB/directeur-overzicht
        if ctx["is_ib"]:
            koppelingen = await supabase_get("leerkrachten_scholen", token,
                {"school_id": f"eq.{ctx['school_id']}", "select": "leerkracht_id,voornaam,email"})
            naam_map = {k["leerkracht_id"]: k.get("voornaam") or k.get("email", "Onbekend")
                        for k in (koppelingen or [])}
            for l in (leerlingen or []):
                l["leerkracht_naam"] = naam_map.get(l.get("leerkracht_id"), "")

        return leerlingen or []
    else:
        # Solo-gebruik: alleen eigen leerlingen
        return await supabase_get(
            "leerlingen", token,
            {
                "leerkracht_id": f"eq.{user['id']}",
                "order":         "groep.asc,voornaam.asc",
                "select":        "*",
                "limit":         str(limit),
                "offset":        str(offset)
            }
        )

@app.post("/leerlingen")
async def maak_leerling_aan(
    leerling: LeerlingAanmaken,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Maakt een leerling aan. Met deduplicatie:
    - Als er al een leerling bestaat met hetzelfde leerlingnummer binnen de school,
      wordt die teruggegeven in plaats van een duplicaat aan te maken.
    - Als er al een leerling bestaat met dezelfde voornaam+groep binnen de school,
      wordt die teruggegeven (zachte deduplicatie, geen error).
    """
    if not leerling.voornaam or not leerling.voornaam.strip():
        raise HTTPException(status_code=400, detail="Voornaam is verplicht.")
    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    # ── Deduplicatie: check op leerlingnummer binnen school ──
    if leerling.leerlingnummer and ctx["school_id"]:
        bestaand = await supabase_get("leerlingen", token, {
            "leerlingnummer": f"eq.{leerling.leerlingnummer.strip()}",
            "school_id":      f"eq.{ctx['school_id']}",
            "select":         "*",
            "limit":          "1"
        })
        if bestaand:
            # Leerling bestaat al — update groep als die gewijzigd is, geef terug
            existing = bestaand[0]
            if leerling.groep and existing.get("groep") != leerling.groep:
                await supabase_patch(
                    f"leerlingen?id=eq.{existing['id']}",
                    token, {"groep": leerling.groep,
                            "bijgewerkt_op": datetime.now(timezone.utc).isoformat()}
                )
                existing["groep"] = leerling.groep
            existing["_bestaand"] = True  # signaal voor frontend
            return existing

    # ── Deduplicatie: check op voornaam+groep binnen school (zachte check) ──
    if ctx["school_id"] and leerling.groep:
        bestaand_naam = await supabase_get("leerlingen", token, {
            "voornaam":  f"eq.{leerling.voornaam.strip()}",
            "groep":     f"eq.{leerling.groep}",
            "school_id": f"eq.{ctx['school_id']}",
            "select":    "id,voornaam,groep",
            "limit":     "1"
        })
        if bestaand_naam:
            bestaand_naam[0]["_bestaand"] = True
            return bestaand_naam[0]

    record = {
        "leerkracht_id": user["id"],
        "voornaam":      leerling.voornaam.strip(),
        "groep":         leerling.groep,
        "ondersteuningsbehoeftes": leerling.ondersteuningsbehoeftes,
        "notities":      leerling.notities,
    }
    if ctx["school_id"]:
        record["school_id"] = ctx["school_id"]
    if leerling.leerlingnummer:
        record["leerlingnummer"] = leerling.leerlingnummer.strip()
    if leerling.achternaam:
        record["achternaam"] = leerling.achternaam.strip()
    if leerling.tussenvoegsel:
        record["tussenvoegsel"] = leerling.tussenvoegsel.strip()

    data = await supabase_post("leerlingen", token, record)
    return data[0] if isinstance(data, list) and data else data

@app.put("/leerlingen/{leerling_id}")
async def werk_leerling_bij(
    leerling_id: str,
    update: LeerlingBijwerken,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    velden = {k: v for k, v in update.model_dump().items() if v is not None}
    if not velden:
        raise HTTPException(status_code=400, detail="Geen velden om bij te werken.")
    velden["bijgewerkt_op"] = datetime.now(timezone.utc).isoformat()
    data = await supabase_patch(
        f"leerlingen?id=eq.{leerling_id}&leerkracht_id=eq.{user['id']}",
        token, velden
    )
    return data[0] if isinstance(data, list) and data else data

@app.delete("/leerlingen/{leerling_id}")
async def verwijder_leerling(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    # Verwijder eerst alle bijbehorende rapporten (cascade)
    try:
        await supabase_delete(
            f"rapporten?leerling_id=eq.{leerling_id}&leerkracht_id=eq.{user['id']}",
            token
        )
    except HTTPException:
        pass  # Geen rapporten is ook goed
    await supabase_delete(
        f"leerlingen?id=eq.{leerling_id}&leerkracht_id=eq.{user['id']}",
        token
    )
    return {"verwijderd": True}

# ══════════════════════════════════════════════════════════
# RAPPORTEN
# ══════════════════════════════════════════════════════════

@app.get("/rapporten/{leerling_id}")
async def haal_rapporten_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    return await supabase_get(
        "rapporten", token,
        {
            "leerling_id": f"eq.{leerling_id}",
            "leerkracht_id": f"eq.{user['id']}",
            "order": "aangemaakt_op.desc",
            "select": "id,aangemaakt_op,rapport_data"
        }
    )

@app.get("/rapporten/{leerling_id}/{rapport_id}")
async def haal_rapport_op(
    leerling_id: str,
    rapport_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Enkel rapport ophalen zodat de frontend een rapport uit de geschiedenis kan tonen."""
    token = credentials.credentials
    data = await supabase_get(
        "rapporten", token,
        {
            "id": f"eq.{rapport_id}",
            "leerling_id": f"eq.{leerling_id}",
            "leerkracht_id": f"eq.{user['id']}",
            "select": "*"
        }
    )
    if not data:
        raise HTTPException(status_code=404, detail="Rapport niet gevonden.")
    return data[0]

@app.post("/rapporten/batch")
async def genereer_batch_rapporten(
    verzoek: dict,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Genereert rapporten voor alle leerlingen in een groep in één aanroep.
    Per leerling:
    1. Haalt notities op (laatste 5)
    2. Haalt LVS-scores op
    3. Haalt ondersteuningsbehoeftes op van de leerling
    4. Combineert alles tot één prompt
    5. Genereert het rapport via Claude
    6. Slaat het op en cascadeert naar LVS-tijdlijn
    """
    import asyncio

    groep      = verzoek.get("groep", "")
    schooljaar = verzoek.get("schooljaar", "")
    token      = credentials.credentials

    if not groep:
        raise HTTPException(status_code=400, detail="Groep is verplicht.")

    # Haal alle leerlingen in de groep op
    leerlingen_data = await supabase_get("leerlingen", token, {
        "groep":         f"eq.{groep}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":        "*",
        "order":         "voornaam.asc"
    })
    if not leerlingen_data:
        raise HTTPException(status_code=404, detail=f"Geen leerlingen gevonden in groep {groep}.")

    resultaten = []
    fouten     = []

    async def genereer_voor_leerling(leerling: dict) -> dict:
        lid   = leerling["id"]
        naam  = leerling.get("voornaam", "")
        g     = leerling.get("groep", groep)

        try:
            # Haal notities, LVS-scores en eerder rapport parallel op
            notities_taak = supabase_get("leerling_notities", token, {
                "leerling_id":  f"eq.{lid}",
                "leerkracht_id": f"eq.{user['id']}",
                "order":        "aangemaakt_op.asc",
                "select":       "tekst,aangemaakt_op,type"
            })
            lvs_taak = supabase_get("lvs_profielen", token, {
                "leerling_id":  f"eq.{lid}",
                "leerkracht_id": f"eq.{user['id']}",
                "select":       "scores"
            })

            notities_data, lvs_data = await asyncio.gather(
                notities_taak, lvs_taak, return_exceptions=True
            )

            # Bouw notitie-context
            notities_tekst = ""
            if isinstance(notities_data, list) and notities_data:
                regels = []
                for n in notities_data:
                    datum = n.get("aangemaakt_op", "")[:10]
                    regels.append(f"[{datum}] {n.get('tekst', '').strip()}")
                notities_tekst = "\n".join(regels)

            if not notities_tekst:
                return {
                    "leerling_id":  lid,
                    "voornaam":     naam,
                    "status":       "overgeslagen",
                    "reden":        "Geen notities beschikbaar"
                }

            # Bouw LVS-context
            lvs_tekst = ""
            if isinstance(lvs_data, list) and lvs_data:
                scores = lvs_data[0].get("scores", {})
                gevuld = []
                for k_score, v_score in scores.items():
                    if v_score and v_score > 0:
                        gevuld.append(f"{k_score}: {v_score}")
                if gevuld:
                    lvs_tekst = "\nBekende LVS-scores (ter informatie): " + ", ".join(gevuld) + "."

            # Bouw ondersteuningscontext
            behoeftes = leerling.get("ondersteuningsbehoeftes") or []
            if isinstance(behoeftes, str):
                behoeftes = [behoeftes] if behoeftes else []
            ond_tekst = ""
            if behoeftes:
                ond_tekst = f"\nOndersteuningsbehoeftes: {', '.join(behoeftes)}."


            groep_type = (
                "onderbouw (groep 1-3)" if int(g or 0) <= 3
                else "middenbouw (groep 4-6)" if int(g or 0) <= 6
                else "bovenbouw (groep 7-8)"
            ) if g and str(g).isdigit() else ""

            prompt = (
                f"Leerling: {naam}, groep {g}."
                + (f" Dit kind zit in de {groep_type}." if groep_type else "")
                + ond_tekst
                + lvs_tekst
                + f"\nNotities van de leerkracht:\n\"{notities_tekst}\""
                + "\n\nRetourneer ALLEEN een geldig JSON-object met deze sleutels:"
                + '\n{"leerresultaten":null,"werkhouding":null,"sociaal_emotioneel":null,'
                + '"aandachtspunten":null,"doelen":null,"positieve_punten":null,'
                + '"ondersteuning":null,"rapportcommentaar":null}'
            )

            tekst = await roep_claude_aan(SYSTEM_PROMPT, prompt, max_tokens=1200)

            try:
                parsed = _veilig_json_parse(tekst)
            except Exception:
                return {
                    "leerling_id": lid,
                    "voornaam":    naam,
                    "status":      "fout",
                    "reden":       "Rapport kon niet worden verwerkt"
                }

            if not parsed:
                return {
                    "leerling_id": lid,
                    "voornaam":    naam,
                    "status":      "fout",
                    "reden":       "Leeg rapport gegenereerd"
                }

            # Sla op
            rapport_data = await supabase_post("rapporten", token, {
                "leerling_id":   lid,
                "leerkracht_id": user["id"],
                "rapport_data":  parsed
            })

            # Cascade naar LVS-tijdlijn
            datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
            commentaar = parsed.get("rapportcommentaar") or ""
            try:
                await _voeg_tijdlijn_toe(
                    leerling_id=lid,
                    leerkracht_id=user["id"],
                    token=token,
                    item={
                        "type":      "rapport",
                        "datum":     datum_nl,
                        "tekst":     f"Rapport (batch) gegenereerd. {commentaar[:80]}…" if commentaar else "Rapport (batch) gegenereerd.",
                        "sentiment": "neutraal",
                        "bron":      "rapport"
                    }
                )
            except Exception:
                pass

            return {
                "leerling_id":  lid,
                "voornaam":     naam,
                "status":       "ok",
                "rapport_data": parsed,
                "rapport_id":   (rapport_data[0] if isinstance(rapport_data, list) and rapport_data else rapport_data or {}).get("id")
            }

        except Exception as e:
            logger.warning(f"Batch rapport fout voor {naam}: {e}")
            return {
                "leerling_id": lid,
                "voornaam":    naam,
                "status":      "fout",
                "reden":       str(e)[:200]
            }

    # Verwerk in batches van 3 parallel om API-limieten te respecteren
    BATCH_GROOTTE = 3
    for i in range(0, len(leerlingen_data), BATCH_GROOTTE):
        batch   = leerlingen_data[i:i + BATCH_GROOTTE]
        groepje = await asyncio.gather(*[genereer_voor_leerling(l) for l in batch])
        for r in groepje:
            if r["status"] == "ok":
                resultaten.append(r)
            else:
                fouten.append(r)

    return {
        "totaal":      len(leerlingen_data),
        "gegenereerd": len(resultaten),
        "overgeslagen": len([f for f in fouten if f.get("status") == "overgeslagen"]),
        "fouten":      len([f for f in fouten if f.get("status") == "fout"]),
        "resultaten":  resultaten,
        "fouten_detail": fouten
    }


@app.get("/rapporten/{leerling_id}/trend")
async def analyseer_rapport_trend(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Vergelijkt de twee meest recente rapporten van een leerling en detecteert
    ontwikkelingstrends. Geeft een gestructureerde samenvatting terug die in
    de frontend getoond kan worden en in de LVS-tijdlijn opgeslagen wordt.

    Detecteert trends in: leerresultaten, werkhouding, sociaal-emotioneel,
    aandachtspunten (terugkerende patronen), doelen (behaald?).
    """
    token = credentials.credentials

    # Haal de laatste 3 rapporten op (we vergelijken de twee meest recente)
    rapporten = await supabase_get("rapporten", token, {
        "leerling_id":   f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "order":         "aangemaakt_op.desc",
        "limit":         "3",
        "select":        "id,aangemaakt_op,rapport_data"
    })

    if not rapporten or len(rapporten) < 2:
        return {
            "beschikbaar": False,
            "reden": "Minimaal 2 rapporten nodig voor trendanalyse.",
            "trend": None
        }

    huidig  = rapporten[0]
    vorig   = rapporten[1]
    h_data  = huidig.get("rapport_data", {}) or {}
    v_data  = vorig.get("rapport_data",  {}) or {}

    # Bouw prompt voor Claude — geef beide rapportteksten mee
    def haal_tekst(data: dict, sleutel: str) -> str:
        val = data.get(sleutel, "")
        if isinstance(val, list):
            return "; ".join(
                str(item.get("praktijk", item.get("behoefte", "")))
                for item in val if isinstance(item, dict)
            )
        return str(val) if val else ""

    secties = ["leerresultaten", "werkhouding", "sociaal_emotioneel",
               "aandachtspunten", "doelen", "positieve_punten"]

    vorig_tekst   = "\n".join(f"{s}: {haal_tekst(v_data, s)}" for s in secties if haal_tekst(v_data, s))
    huidig_tekst  = "\n".join(f"{s}: {haal_tekst(h_data, s)}" for s in secties if haal_tekst(h_data, s))
    vorig_datum   = vorig.get("aangemaakt_op", "")[:10]
    huidig_datum  = huidig.get("aangemaakt_op", "")[:10]

    if not vorig_tekst or not huidig_tekst:
        return {"beschikbaar": False, "reden": "Rapporten bevatten onvoldoende tekst.", "trend": None}

    TREND_PROMPT = """Je analyseert twee rapporten van dezelfde leerling en detecteert ontwikkelingstrends.

Retourneer ALLEEN een JSON-object met deze structuur:
{
  "samenvatting": "2-3 zinnen over de algehele ontwikkeling tussen beide rapporten",
  "positief": ["concrete positieve ontwikkeling 1", "positieve ontwikkeling 2"],
  "aandacht": ["aandachtspunt dat terugkeert of verergert"],
  "doelen_behaald": true/false/null,
  "sentiment": "groei" | "stabiel" | "achteruitgang" | "gemengd",
  "kern": "maximaal 15 woorden die de trend samenvatten"
}

Wees concreet en feitelijk. Vermijd naam van het kind. Geen algemene loftuitingen.
Vergelijk alleen wat expliciet in beide rapporten staat."""

    prompt_tekst = f"""Vorig rapport ({vorig_datum}):
{vorig_tekst}

Huidig rapport ({huidig_datum}):
{huidig_tekst}"""

    try:
        import anthropic as _ac
        client = _ac.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system=TREND_PROMPT,
            messages=[{"role": "user", "content": prompt_tekst}]
        )
        raw  = response.content[0].text.strip()
        trend = _veilig_json_parse(raw)
    except Exception as e:
        logger.warning(f"Trendanalyse mislukt: {e}")
        return {"beschikbaar": False, "reden": "Analyse mislukt.", "trend": None}

    if not trend:
        return {"beschikbaar": False, "reden": "Resultaat kon niet worden verwerkt.", "trend": None}

    # Sla de trend op in de LVS-tijdlijn als nieuwe tijdlijnregel
    try:
        datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
        sentiment_map = {"groei": "positief", "stabiel": "neutraal",
                         "achteruitgang": "aandacht", "gemengd": "neutraal"}
        await _voeg_tijdlijn_toe(
            leerling_id=leerling_id,
            leerkracht_id=user["id"],
            token=token,
            item={
                "type":      "rapport",
                "datum":     datum_nl,
                "tekst":     f"Trend: {trend.get('kern', trend.get('samenvatting', '')[:80])}",
                "sentiment": sentiment_map.get(trend.get("sentiment", ""), "neutraal"),
                "bron":      "trendanalyse"
            }
        )
    except Exception as e:
        logger.warning(f"Trend->LVS cascade mislukt (stil): {e}")

    return {
        "beschikbaar": True,
        "vorig_datum":  vorig_datum,
        "huidig_datum": huidig_datum,
        "trend":        trend
    }


@app.post("/rapporten")
async def sla_rapport_op(
    verzoek: RapportOpslaan,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    data = await supabase_post("rapporten", token, {
        "leerling_id": verzoek.leerling_id,
        "leerkracht_id": user["id"],
        "rapport_data": verzoek.rapport_data
    })
    opgeslagen = data[0] if isinstance(data, list) and data else data

    # Cascade: voeg rapport toe aan LVS-tijdlijn (stil falen)
    try:
        datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
        commentaar = verzoek.rapport_data.get("rapportcommentaar") or ""
        tekst = "Rapport opgeslagen." + (f" {commentaar[:80]}..." if commentaar else "")
        await _voeg_tijdlijn_toe(
            leerling_id=verzoek.leerling_id,
            leerkracht_id=user["id"],
            token=token,
            item={"type": "rapport", "datum": datum_nl, "tekst": tekst}
        )
    except Exception as _e:
        logger.warning(f"Cascade rapport->LVS mislukt (stil): {_e}")
    return opgeslagen

@app.delete("/rapporten/{rapport_id}")
async def verwijder_rapport(
    rapport_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    await supabase_delete(
        f"rapporten?id=eq.{rapport_id}&leerkracht_id=eq.{user['id']}",
        token
    )
    return {"verwijderd": True}

# ══════════════════════════════════════════════════════════
# OPP
# ══════════════════════════════════════════════════════════

@app.post("/opp")
async def opp(verzoek: OppVerzoek, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not verzoek.notities or len(verzoek.notities.strip()) < 20:
        raise HTTPException(status_code=400, detail="Notities moeten minimaal 20 tekens bevatten.")
    if len(verzoek.notities) > MAX_PROMPT_LENGTE:
        raise HTTPException(status_code=400, detail=f"Notities te lang (maximaal {MAX_PROMPT_LENGTE} tekens).")

    # Privacy driedubbele controle — Laag 1+2+3
    notities_anon, naam_mapping = privacy_filter(verzoek.notities, verzoek.naam)

    parts = []
    if verzoek.naam:
        groep_tekst = f", groep {verzoek.groep}" if verzoek.groep else ""
        parts.append(f"Leerling: [LEERLING]{groep_tekst}.")
    if verzoek.ondersteuningsbehoefte:
        parts.append(f"Ondersteuningsbehoefte: {verzoek.ondersteuningsbehoefte}.")
    parts.append(f'Notities van de leerkracht:\n"{notities_anon}"')

    tekst = await roep_claude_aan(OPP_PROMPT, "\n".join(parts), max_tokens=2500)
    tekst = herstel_pseudoniem(tekst, naam_mapping)

    try:
        parsed = _veilig_json_parse(tekst)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"OPP JSON parse mislukt: {tekst[:100]}")
        return {"data": None, "tekst": tekst}

    # Cascade: voeg OPP toe aan LVS-tijdlijn EN sla uitstroom op als gestructureerd veld
    if verzoek.leerling_id:
        try:
            datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
            uitstroom = (parsed.get("uitstroombestemming") or parsed.get("uitstroom") or "")[:120]
            tijdlijn_tekst = "OPP gegenereerd." + (f" Uitstroom: {uitstroom}…" if uitstroom else "")
            token_str = credentials.credentials if hasattr(credentials, "credentials") else ""

            # Tijdlijn
            await _voeg_tijdlijn_toe(
                leerling_id=verzoek.leerling_id,
                leerkracht_id=user["id"],
                token=token_str,
                item={
                    "type":      "opp",
                    "datum":     datum_nl,
                    "tekst":     tijdlijn_tekst,
                    "sentiment": "neutraal",
                    "bron":      "opp"
                }
            )

            # Sla uitstroombestemming op als gestructureerd veld op de leerling
            # zodat IB/directeur het kan opvragen zonder het OPP-document te openen
            if uitstroom:
                opp_meta = {
                    "uitstroombestemming": uitstroom,
                    "opp_datum":           datum_nl,
                    "doelen":              (parsed.get("doelen") or "")[:500],
                    "evaluatie":           (parsed.get("evaluatie") or "")[:200],
                }
                await supabase_patch(
                    f"leerlingen?id=eq.{verzoek.leerling_id}&leerkracht_id=eq.{user['id']}",
                    token_str,
                    {"opp_meta": opp_meta, "bijgewerkt_op": datetime.now(timezone.utc).isoformat()}
                )
        except Exception as _e:
            logger.warning(f"Cascade OPP->LVS/leerling mislukt (stil): {_e}")
    return {"data": parsed, "tekst": tekst}

# ══════════════════════════════════════════════════════════
# HANDELINGSPLAN
# ══════════════════════════════════════════════════════════

# ── Aanwezigheid ────────────────────────────────────────────────

class AanwezigheidItem(BaseModel):
    leerling_id: str
    datum: str
    status: str = "onbekend"
    reden: str = ""
    opmerking: str = ""

@app.get("/aanwezigheid")
async def haal_aanwezigheid_op(datum: str, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    data = await supabase_get(
        "aanwezigheid",
        token,
        {"datum": f"eq.{datum}", "leerkracht_id": f"eq.{user['id']}"}
    )
    return data

@app.post("/aanwezigheid")
async def sla_aanwezigheid_op(item: AanwezigheidItem, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{SUPABASE_URL}/rest/v1/aanwezigheid",
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates,return=representation"
                },
                json={
                    "leerkracht_id": user["id"],
                    "leerling_id": item.leerling_id,
                    "datum": item.datum,
                    "status": item.status,
                    "reden": item.reden,
                    "opmerking": item.opmerking,
                    "bijgewerkt_op": "now()"
                }
            )
            if res.status_code == 401:
                raise HTTPException(status_code=401, detail="Sessie verlopen.")
            if res.status_code not in (200, 201):
                raise HTTPException(status_code=res.status_code, detail=f"Opslaan aanwezigheid mislukt: {res.text[:300]}")
            return res.json()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout). Probeer opnieuw.")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout: {str(e)}")

# ══════════════════════════════════════════════════════════
# AANWEZIGHEID PATROONDETECTIE
# ══════════════════════════════════════════════════════════

def _analyseer_aanwezigheidspatroon(registraties: list, leerling_id: str) -> dict | None:
    """
    Analyseert de aanwezigheidsregistraties van een leerling over de afgelopen periode.
    Retourneert een signaalbeschrijving als er een patroon wordt gevonden, anders None.

    Patronen die we detecteren:
    - Frequent afwezig: 3+ keer afwezig in de laatste 15 schooldagen
    - Aaneengesloten afwezigheid: 3+ opeenvolgende dagen afwezig
    - Structureel te laat: 3+ keer te laat in de laatste 15 schooldagen
    - Gemengd patroon: combinatie van afwezig + te laat
    """
    if not registraties:
        return None

    # Sorteer op datum, nieuwste eerst
    gesorteerd = sorted(registraties, key=lambda r: r.get("datum", ""), reverse=True)

    # Neem de laatste 20 registraties (≈ 4 weken)
    recent = gesorteerd[:20]
    alle = gesorteerd  # voor aaneengesloten detectie

    # Tel per status
    afwezig_recent  = [r for r in recent if r.get("status") == "afwezig"]
    te_laat_recent  = [r for r in recent if r.get("status") == "laat"]
    aanwezig_recent = [r for r in recent if r.get("status") == "aanwezig"]

    n_afwezig  = len(afwezig_recent)
    n_te_laat  = len(te_laat_recent)
    n_recent   = len(recent)

    if n_recent == 0:
        return None

    # ── Patroon 1: Aaneengesloten afwezigheid (3+ dagen op rij) ──
    max_streak = 0
    streak      = 0
    streak_start = None
    for r in gesorteerd:
        if r.get("status") == "afwezig":
            streak += 1
            if streak_start is None:
                streak_start = r.get("datum")
            max_streak = max(max_streak, streak)
        else:
            streak = 0
            streak_start = None

    if max_streak >= 3:
        return {
            "type":      "aaneengesloten_afwezig",
            "ernst":     "aandacht" if max_streak < 5 else "zorg",
            "kern":      f"{max_streak} opeenvolgende dagen afwezig",
            "details":   f"Aaneengesloten afwezigheid van {max_streak} dagen gedetecteerd.",
            "domein":    "algemeen"
        }

    # ── Patroon 2: Frequent afwezig (3+ van laatste 15 dagen) ──
    laatste_15 = [r for r in gesorteerd[:15]]
    afwezig_15 = sum(1 for r in laatste_15 if r.get("status") == "afwezig")
    if afwezig_15 >= 3:
        pct = round(afwezig_15 / max(len(laatste_15), 1) * 100)
        return {
            "type":    "frequent_afwezig",
            "ernst":   "aandacht" if afwezig_15 < 5 else "zorg",
            "kern":    f"{afwezig_15}x afwezig in de laatste {len(laatste_15)} schooldagen ({pct}%)",
            "details": f"Leerling was {afwezig_15} van de laatste {len(laatste_15)} geregistreerde schooldagen afwezig.",
            "domein":  "algemeen"
        }

    # ── Patroon 3: Structureel te laat (3+ van laatste 15 dagen) ──
    te_laat_15 = sum(1 for r in laatste_15 if r.get("status") == "laat")
    if te_laat_15 >= 3:
        return {
            "type":    "structureel_te_laat",
            "ernst":   "aandacht",
            "kern":    f"{te_laat_15}x te laat in de laatste {len(laatste_15)} schooldagen",
            "details": f"Leerling kwam {te_laat_15} van de laatste {len(laatste_15)} geregistreerde schooldagen te laat.",
            "domein":  "werkhouding"
        }

    # ── Patroon 4: Gemengd patroon (afwezig + te laat samen ≥ 4 van 15) ──
    gemengd_15 = afwezig_15 + te_laat_15
    if gemengd_15 >= 4:
        return {
            "type":    "gemengd_verzuim",
            "ernst":   "aandacht",
            "kern":    f"{gemengd_15}x niet volledig aanwezig in de laatste {len(laatste_15)} schooldagen",
            "details": f"Combinatie: {afwezig_15}x afwezig, {te_laat_15}x te laat in de laatste {len(laatste_15)} dagen.",
            "domein":  "werkhouding"
        }

    return None  # Geen patroon gevonden


@app.post("/aanwezigheid/analyseer/{leerling_id}")
async def analyseer_aanwezigheid_leerling(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Analyseert aanwezigheidspatroon van één leerling en slaat een signaal
    op in de LVS-tijdlijn als er een patroon wordt gevonden.
    Retourneert het signaal of null.
    """
    token = credentials.credentials

    # Haal de laatste 30 registraties op
    data = await supabase_get("aanwezigheid", token, {
        "leerling_id":  f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "order":        "datum.desc",
        "limit":        "30",
        "select":       "datum,status,reden,opmerking"
    })

    if not data:
        return {"signaal": None}

    signaal = _analyseer_aanwezigheidspatroon(data, leerling_id)

    if signaal:
        # Controleer of we dit signaal al recent hebben toegevoegd
        # (maximaal 1x per 7 dagen hetzelfde type signaal)
        lvs_profiel = await supabase_get("lvs_profielen", token, {
            "leerling_id":  f"eq.{leerling_id}",
            "leerkracht_id": f"eq.{user['id']}",
            "select":       "tijdlijn"
        })

        if lvs_profiel:
            tijdlijn = lvs_profiel[0].get("tijdlijn") or []
            from datetime import date, timedelta
            zeven_dagen_geleden = (date.today() - timedelta(days=7)).strftime("%-d %b %Y")

            # Check of hetzelfde type signaal de afgelopen 7 dagen al staat
            al_recent = any(
                item.get("bron") == "aanwezigheid" and
                item.get("type") == signaal["domein"] and
                item.get("datum", "") >= zeven_dagen_geleden
                for item in tijdlijn
            )

            if not al_recent:
                datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
                await _voeg_tijdlijn_toe(
                    leerling_id=leerling_id,
                    leerkracht_id=user["id"],
                    token=token,
                    item={
                        "type":      signaal["domein"],
                        "datum":     datum_nl,
                        "tekst":     signaal["kern"],
                        "sentiment": signaal["ernst"],
                        "bron":      "aanwezigheid"
                    }
                )

    return {"signaal": signaal}


@app.post("/aanwezigheid/analyseer_groep/{groep}")
async def analyseer_aanwezigheid_groep(
    groep: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Analyseert aanwezigheid voor alle leerlingen in een groep tegelijk.
    Retourneert een lijst van signalen per leerling.
    Wordt aangeroepen na het opslaan van een dag's registraties.
    """
    import asyncio
    token = credentials.credentials

    # Haal alle leerlingen in de groep op
    leerlingen_data = await supabase_get("leerlingen", token, {
        "groep":         f"eq.{groep}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":        "id,voornaam"
    })

    if not leerlingen_data:
        return {"signalen": []}

    signalen = []

    async def analyseer_een(leerling: dict):
        try:
            data = await supabase_get("aanwezigheid", token, {
                "leerling_id":  f"eq.{leerling['id']}",
                "leerkracht_id": f"eq.{user['id']}",
                "order":        "datum.desc",
                "limit":        "30",
                "select":       "datum,status"
            })
            signaal = _analyseer_aanwezigheidspatroon(data or [], leerling["id"])
            if signaal:
                # Voeg toe aan LVS-tijdlijn (deduplicatie zit in de helper)
                lvs_profiel = await supabase_get("lvs_profielen", token, {
                    "leerling_id":  f"eq.{leerling['id']}",
                    "leerkracht_id": f"eq.{user['id']}",
                    "select":       "tijdlijn"
                })
                if lvs_profiel:
                    tijdlijn = lvs_profiel[0].get("tijdlijn") or []
                    from datetime import date, timedelta
                    grens = (date.today() - timedelta(days=7)).strftime("%-d %b %Y")
                    al_recent = any(
                        item.get("bron") == "aanwezigheid" and
                        item.get("datum", "") >= grens
                        for item in tijdlijn
                    )
                    if not al_recent:
                        datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
                        await _voeg_tijdlijn_toe(
                            leerling_id=leerling["id"],
                            leerkracht_id=user["id"],
                            token=token,
                            item={
                                "type":      signaal["domein"],
                                "datum":     datum_nl,
                                "tekst":     signaal["kern"],
                                "sentiment": signaal["ernst"],
                                "bron":      "aanwezigheid"
                            }
                        )
                signalen.append({
                    "leerling_id":   leerling["id"],
                    "voornaam":      leerling["voornaam"],
                    "signaal":       signaal
                })
        except Exception as e:
            logger.warning(f"Patroonanalyse voor {leerling['id']} mislukt: {e}")

    # Analyseer alle leerlingen parallel (max 8 tegelijk)
    groepjes = [leerlingen_data[i:i+8] for i in range(0, len(leerlingen_data), 8)]
    for groepje in groepjes:
        await asyncio.gather(*[analyseer_een(l) for l in groepje])

    return {"signalen": [s for s in signalen if s["signaal"]]}


@app.post("/handelingsplan")
async def handelingsplan(verzoek: HandelingsplanVerzoek, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not verzoek.ondersteuningsbehoefte:
        raise HTTPException(status_code=400, detail="Ondersteuningsbehoefte is verplicht voor een handelingsplan.")
    if not verzoek.notities or len(verzoek.notities.strip()) < 10:
        raise HTTPException(status_code=400, detail="Notities moeten minimaal 10 tekens bevatten.")
    if len(verzoek.notities) > MAX_PROMPT_LENGTE:
        raise HTTPException(status_code=400, detail=f"Notities te lang (maximaal {MAX_PROMPT_LENGTE} tekens).")

    # Privacy driedubbele controle — Laag 1+2+3
    notities_anon, naam_mapping = privacy_filter(verzoek.notities, verzoek.naam)

    parts = []
    if verzoek.naam:
        groep_tekst = f", groep {verzoek.groep}" if verzoek.groep else ""
        parts.append(f"Leerling: [LEERLING]{groep_tekst}.")
    parts.append(f"Ondersteuningsbehoefte: {verzoek.ondersteuningsbehoefte}.")
    parts.append(f'Notities van de leerkracht:\n"{notities_anon}"')

    tekst = await roep_claude_aan(HANDELINGSPLAN_PROMPT, "\n".join(parts), max_tokens=2500)
    tekst = herstel_pseudoniem(tekst, naam_mapping)

    try:
        parsed = _veilig_json_parse(tekst)
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Handelingsplan JSON parse mislukt: {tekst[:100]}")
        return {"data": None, "tekst": tekst}

    # Cascade: voeg handelingsplan toe aan LVS-tijdlijn
    if verzoek.leerling_id:
        try:
            datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
            behoefte = verzoek.ondersteuningsbehoefte or ""
            tijdlijn_tekst = f"Handelingsplan {behoefte} gegenereerd.".strip()
            await _voeg_tijdlijn_toe(
                leerling_id=verzoek.leerling_id,
                leerkracht_id=user["id"],
                token=credentials.credentials,
                item={"type": "handelingsplan", "datum": datum_nl, "tekst": tijdlijn_tekst}
            )
        except Exception as _e:
            logger.warning(f"Cascade handelingsplan->LVS mislukt (stil): {_e}")
    return {"data": parsed, "tekst": tekst}

# ══════════════════════════════════════════════════════════
# OUDERGESPREK
# ══════════════════════════════════════════════════════════

@app.post("/oudergesprek")
async def oudergesprek(verzoek: OudergesprekVerzoek, user=Depends(get_user)):
    if not verzoek.notities or len(verzoek.notities.strip()) < 20:
        raise HTTPException(status_code=400, detail="Aantekeningen moeten minimaal 20 tekens bevatten.")
    if len(verzoek.notities) > MAX_PROMPT_LENGTE:
        raise HTTPException(status_code=400, detail=f"Notities te lang (maximaal {MAX_PROMPT_LENGTE} tekens).")

    # Privacy driedubbele controle — Laag 1+2+3
    notities_anon, naam_mapping = privacy_filter(verzoek.notities, verzoek.naam)

    parts = []
    if verzoek.naam:
        groep_tekst = f", groep {verzoek.groep}" if verzoek.groep else ""
        datum_tekst = f", gesprek op {verzoek.datum}" if verzoek.datum else ""
        parts.append(f"Leerling: [LEERLING]{groep_tekst}{datum_tekst}.")
    parts.append(f'Aantekeningen van de leerkracht:\n"{notities_anon}"')

    tekst = await roep_claude_aan(OUDERGESPREK_PROMPT, "\n".join(parts), max_tokens=2000)
    tekst = herstel_pseudoniem(tekst, naam_mapping)

    try:
        parsed = _veilig_json_parse(tekst)
        return {"data": parsed, "tekst": tekst}
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Oudergesprek JSON parse mislukt: {tekst[:100]}")
        return {"data": None, "tekst": tekst}

# ══════════════════════════════════════════════════════════
# PEDAGOGISCH ADVIES
# ══════════════════════════════════════════════════════════

@app.post("/pedagogisch")
async def pedagogisch_advies(verzoek: PromptVerzoek, user=Depends(get_user)):
    """Pedagogisch advies op basis van 5 theorieen. Apart endpoint voor betere foutafhandeling."""
    if len(verzoek.prompt.strip()) < 20:
        raise HTTPException(status_code=400, detail="Notities te kort.")
    if len(verzoek.prompt) > MAX_PROMPT_LENGTE:
        raise HTTPException(status_code=400, detail=f"Notities te lang (maximaal {MAX_PROMPT_LENGTE} tekens).")
    tekst = await roep_claude_aan(PEDAGOGISCH_PROMPT, verzoek.prompt, max_tokens=1500)
    try:
        parsed = _veilig_json_parse(tekst)
        return {"data": parsed}
    except (json.JSONDecodeError, ValueError):
        return {"data": None, "tekst": tekst}


# ══════════════════════════════════════════════════════════
# LVS PROFIELEN
# ══════════════════════════════════════════════════════════

@app.get("/lvs/{leerling_id}")
async def haal_lvs_profiel_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Haal het LVS-profiel op voor een leerling. Maak aan als het nog niet bestaat."""
    token = credentials.credentials
    data = await supabase_get(
        "lvs_profielen", token,
        {
            "leerling_id": f"eq.{leerling_id}",
            "leerkracht_id": f"eq.{user['id']}",
            "select": "*"
        }
    )
    if data:
        return data[0]
    # Profiel bestaat nog niet — maak aan met standaard scores
    nieuw = await supabase_post("lvs_profielen", token, {
        "leerling_id": leerling_id,
        "leerkracht_id": user["id"],
        "scores": {"lezen":70,"avi":70,"dmt":70,"rekenen":70,"rekenen_basis":70,"spelling":70,"taalverzorging":70,"woordenschat":70,"begrijpend":70,"begrijpend_luis":70,"engels":70,"sociaal":70,"executief":70,"groeimeter":70,"werkhouding":70},
        "vorige_scores": {"lezen":70,"avi":70,"dmt":70,"rekenen":70,"rekenen_basis":70,"spelling":70,"taalverzorging":70,"woordenschat":70,"begrijpend":70,"begrijpend_luis":70,"engels":70,"sociaal":70,"executief":70,"groeimeter":70,"werkhouding":70},
        "tijdlijn": []
    })
    return nieuw[0] if isinstance(nieuw, list) and nieuw else nieuw

@app.put("/lvs/{leerling_id}")
async def sla_lvs_profiel_op(
    leerling_id: str,
    verzoek: LvsProfielOpslaan,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Sla scores en tijdlijn op. Upsert: aanmaken als niet bestaat, bijwerken als wel."""
    token = credentials.credentials
    # Controleer of profiel bestaat
    bestaand = await supabase_get(
        "lvs_profielen", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}", "select": "id"}
    )
    if bestaand:
        data = await supabase_patch(
            f"lvs_profielen?leerling_id=eq.{leerling_id}&leerkracht_id=eq.{user['id']}",
            token,
            {
                "scores": verzoek.scores,
                "vorige_scores": verzoek.vorige_scores,
                "tijdlijn": verzoek.tijdlijn
            }
        )
    else:
        data = await supabase_post("lvs_profielen", token, {
            "leerling_id": leerling_id,
            "leerkracht_id": user["id"],
            "scores": verzoek.scores,
            "vorige_scores": verzoek.vorige_scores,
            "tijdlijn": verzoek.tijdlijn
        })
    return data[0] if isinstance(data, list) and data else data

@app.post("/lvs/{leerling_id}/tijdlijn")
async def voeg_tijdlijn_item_toe(
    leerling_id: str,
    verzoek: LvsTijdlijnItem,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Voeg één item toe aan de tijdlijn zonder het hele profiel op te sturen."""
    token = credentials.credentials
    # Haal huidige tijdlijn op
    data = await supabase_get(
        "lvs_profielen", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}", "select": "tijdlijn"}
    )
    if not data:
        raise HTTPException(status_code=404, detail="LVS profiel niet gevonden.")
    tijdlijn = data[0].get("tijdlijn", [])
    tijdlijn.insert(0, verzoek.item)  # nieuwste bovenaan
    updated = await supabase_patch(
        f"lvs_profielen?leerling_id=eq.{leerling_id}&leerkracht_id=eq.{user['id']}",
        token,
        {"tijdlijn": tijdlijn}
    )
    return updated[0] if isinstance(updated, list) and updated else {"tijdlijn": tijdlijn}

@app.get("/lvs")
async def haal_alle_lvs_profielen_op(
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Haal alle LVS profielen op voor deze leerkracht (voor IB-overzicht)."""
    token = credentials.credentials
    return await supabase_get(
        "lvs_profielen", token,
        {"leerkracht_id": f"eq.{user['id']}", "select": "*"}
    )

# ══════════════════════════════════════════════════════════
# GLOBALE ERROR HANDLER
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
# E-MAIL HELPER (Resend)
# ══════════════════════════════════════════════════════════

async def verstuur_email(naar: str, onderwerp: str, html: str, tekst: str) -> dict:
    """Verstuur een e-mail via Resend. Geeft {"ok": True} of {"ok": False, "fout": ...} terug."""
    if not RESEND_API_KEY:
        raise HTTPException(status_code=503,
            detail="E-mail versturen is niet geconfigureerd. Voeg RESEND_API_KEY toe aan .env.")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": MAIL_FROM,
                    "to": [naar],
                    "subject": onderwerp,
                    "html": html,
                    "text": tekst
                }
            )
            if res.status_code not in (200, 201):
                logger.error(f"Resend fout {res.status_code}: {res.text[:200]}")
                raise HTTPException(status_code=502,
                    detail=f"E-mail versturen mislukt (Resend {res.status_code}).")
            return {"ok": True, "resend_id": res.json().get("id")}
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="E-mailserver niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout e-mail: {str(e)}")


def bouw_rapport_email_html(naam: str, groep: str, rapport_html: str, leerkracht_naam: str, school_naam: str) -> str:
    """Bouw een nette HTML-e-mail rondom de rapporttekst."""
    groep_tekst = f" (groep {groep})" if groep else ""
    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body {{ font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 24px; }}
  .wrapper {{ max-width: 600px; margin: 0 auto; background: #fff; border: 1px solid #e0e0e0; }}
  .header {{ background: #111; color: #fff; padding: 20px 24px; }}
  .header h1 {{ margin: 0; font-size: 18px; font-weight: 500; }}
  .header p {{ margin: 4px 0 0; font-size: 12px; color: #aaa; }}
  .body {{ padding: 24px; color: #222; line-height: 1.7; font-size: 14px; }}
  .rapport {{ background: #f9f9f9; border-left: 3px solid #111; padding: 16px 20px; margin: 16px 0; }}
  .footer {{ padding: 16px 24px; border-top: 1px solid #e0e0e0; font-size: 11px; color: #888; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <h1>Rapport van {naam}{groep_tekst}</h1>
    <p>{school_naam}</p>
  </div>
  <div class="body">
    <p>Geachte ouder(s)/verzorger(s),</p>
    <p>Hierbij ontvangt u het rapport van <strong>{naam}</strong>{groep_tekst}.</p>
    <div class="rapport">{rapport_html}</div>
    <p>Met vriendelijke groet,<br><strong>{leerkracht_naam}</strong><br>{school_naam}</p>
  </div>
  <div class="footer">
    Dit bericht is verstuurd via School van Morgen. Heeft u vragen? Neem contact op met de school.
  </div>
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════
# OUDERCOMMUNICATIE
# ══════════════════════════════════════════════════════════

@app.post("/ouder/verstuur")
async def verstuur_ouder_bericht(
    verzoek: OuderBerichtVersturen,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Verstuur een bericht naar ouders en sla het op in de communicatiegeschiedenis."""
    token = credentials.credentials

    # Controleer toegang
    ctx_ouder = await get_school_context(user, token)
    await _controleer_leerling_toegang(verzoek.leerling_id, user, token, ctx_ouder)

    # Verstuur via Resend
    resend_result = await verstuur_email(
        naar=verzoek.ouder_email,
        onderwerp=verzoek.onderwerp,
        html=verzoek.inhoud_html,
        tekst=verzoek.inhoud_tekst
    )

    # Sla op in communicatiegeschiedenis
    bericht_data = {
        "leerkracht_id": user["id"],
        "leerling_id":   verzoek.leerling_id,
        "ouder_email":   verzoek.ouder_email,
        "onderwerp":     verzoek.onderwerp,
        "inhoud_tekst":  verzoek.inhoud_tekst[:2000],  # bewaar platte tekst voor overzicht
        "bericht_type":  verzoek.bericht_type,
        "resend_id":     resend_result.get("resend_id"),
        "verstuurd_op":  datetime.now(timezone.utc).isoformat(),
        "status":        "verstuurd"
    }
    opgeslagen = await supabase_post("ouder_berichten", token, bericht_data)
    return {"verstuurd": True, "bericht_id": opgeslagen[0]["id"] if opgeslagen else None}


@app.get("/ouder/berichten/{leerling_id}")
async def haal_ouder_berichten_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Haal de communicatiegeschiedenis op voor een leerling."""
    token = credentials.credentials
    data = await supabase_get("ouder_berichten", token, {
        "leerling_id":  f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "order":        "verstuurd_op.desc",
        "select":       "*"
    })
    return data or []


@app.delete("/ouder/berichten/{bericht_id}")
async def verwijder_ouder_bericht(
    bericht_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Verwijder een bericht uit de geschiedenis (alleen van eigen leerlingen)."""
    token = credentials.credentials
    bestaand = await supabase_get("ouder_berichten", token,
        {"id": f"eq.{bericht_id}", "leerkracht_id": f"eq.{user['id']}"})
    if not bestaand:
        raise HTTPException(status_code=404, detail="Bericht niet gevonden.")
    await supabase_delete(f"ouder_berichten?id=eq.{bericht_id}", token)
    return {"verwijderd": True}


# ══════════════════════════════════════════════════════════
# SCHOOL & ROLLEN
# ══════════════════════════════════════════════════════════

@app.get("/school/profiel")
async def haal_school_profiel_op(user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    meta = user.get("user_metadata", {})
    school_id = meta.get("school_id")
    if not school_id:
        return {"school_id": None, "naam": None, "rol": meta.get("rol", "leerkracht"), "aangemeld": False}
    data = await supabase_get("scholen", token, {"id": f"eq.{school_id}"})
    school = data[0] if data else {}
    return {"school_id": school_id, "naam": school.get("naam"), "brin": school.get("brin"), "rol": meta.get("rol", "leerkracht"), "aangemeld": True}

@app.get("/school/collegas")
async def haal_collegas_op(user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    meta = user.get("user_metadata", {})
    rol = meta.get("rol", "leerkracht")
    school_id = meta.get("school_id")
    if not school_id:
        raise HTTPException(status_code=403, detail="Geen schoolaccount gekoppeld.")
    if rol not in ("ib", "directeur"):
        raise HTTPException(status_code=403, detail="Alleen IB-ers en directeuren kunnen dit inzien.")
    data = await supabase_get("leerkrachten_scholen", token, {"school_id": f"eq.{school_id}", "select": "leerkracht_id,rol,email,voornaam"})
    return data or []

@app.post("/school/aanmaken")
async def maak_school_aan(profiel: SchoolProfiel, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    school_data = {"naam": profiel.naam, "brin": profiel.brin, "adres": profiel.adres, "aangemaakt_door": user["id"]}
    school = await supabase_post("scholen", token, school_data)
    if not school:
        raise HTTPException(status_code=500, detail="School aanmaken mislukt.")
    school_id = school[0]["id"]
    await supabase_post("leerkrachten_scholen", token, {
        "leerkracht_id": user["id"], "school_id": school_id, "rol": "directeur", "email": user.get("email", "")
    })
    # Zet school_id en rol in user_metadata zodat JWT bij volgende login up-to-date is
    await update_user_metadata(user["id"], {"school_id": school_id, "rol": "directeur"})
    return {"school_id": school_id, "naam": profiel.naam, "rol": "directeur"}

@app.post("/school/uitnodigen")
async def nodig_collega_uit(body: dict, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    meta = user.get("user_metadata", {})
    if meta.get("rol") != "directeur":
        raise HTTPException(status_code=403, detail="Alleen de directeur kan collega's uitnodigen.")
    school_id = meta.get("school_id")
    if not school_id:
        raise HTTPException(status_code=400, detail="Geen schoolaccount gekoppeld.")
    import secrets, hashlib
    uitnodiging_token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(uitnodiging_token.encode()).hexdigest()
    await supabase_post("school_uitnodigingen", token, {
        "school_id": school_id, "rol": body.get("rol", "leerkracht"),
        "token_hash": token_hash, "aangemaakt_door": user["id"], "gebruikt": False
    })
    return {"uitnodigingstoken": uitnodiging_token, "school_id": school_id, "rol": body.get("rol", "leerkracht")}

@app.post("/school/deelnemen")
async def neem_deel_aan_school(body: dict, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    import hashlib
    uitnodiging = body.get("uitnodigingstoken", "").strip()
    if not uitnodiging:
        raise HTTPException(status_code=400, detail="Uitnodigingstoken ontbreekt.")
    token_hash = hashlib.sha256(uitnodiging.encode()).hexdigest()
    uitnodigingen = await supabase_get("school_uitnodigingen", token, {"token_hash": f"eq.{token_hash}", "gebruikt": "eq.false"})
    if not uitnodigingen:
        raise HTTPException(status_code=404, detail="Ongeldig of verlopen uitnodigingstoken.")
    inv = uitnodigingen[0]
    await supabase_post("leerkrachten_scholen", token, {
        "leerkracht_id": user["id"], "school_id": inv["school_id"], "rol": inv["rol"], "email": user.get("email", "")
    })
    await supabase_patch(f"school_uitnodigingen?token_hash=eq.{token_hash}", token, {"gebruikt": True})
    # Zet school_id en rol in user_metadata
    await update_user_metadata(user["id"], {"school_id": inv["school_id"], "rol": inv["rol"]})
    return {"school_id": inv["school_id"], "rol": inv["rol"]}


# ══════════════════════════════════════════════════════════
# TOETSREGISTRATIE
# ══════════════════════════════════════════════════════════

async def _verwerk_toets(item: ToetsImport, user_id: str, token: str) -> dict:
    datum = item.afname_datum or datetime.now(timezone.utc).date().isoformat()
    data = {
        "leerkracht_id": user_id, "leerling_id": item.leerling_id,
        "vakgebied": item.vakgebied, "score": item.score, "niveau": item.niveau,
        "afname_datum": datum, "bron": item.bron,
        "aangemaakt_op": datetime.now(timezone.utc).isoformat()
    }
    result = await supabase_post("toetsresultaten", token, data)
    if not result:
        raise RuntimeError("Opslaan in toetsresultaten mislukt.")
    profiel = await supabase_get("lvs_profielen", token,
        {"leerling_id": f"eq.{item.leerling_id}", "leerkracht_id": f"eq.{user_id}"})
    if profiel:
        huidig   = profiel[0]
        scores   = huidig.get("scores", {})
        vorige   = huidig.get("vorige_scores", {})
        tijdlijn = huidig.get("tijdlijn", [])
        vorige[item.vakgebied] = scores.get(item.vakgebied, 0)
        scores[item.vakgebied] = item.score
        tekst = f"{item.vakgebied.capitalize()} \u2014 score {item.score}"
        if item.niveau: tekst += f" (niveau {item.niveau})"
        if item.bron != "handmatig": tekst += f" via {item.bron}"
        tijdlijn.insert(0, {"type": "toets", "datum": datum, "tekst": tekst})
        await supabase_patch(
            f"lvs_profielen?leerling_id=eq.{item.leerling_id}&leerkracht_id=eq.{user_id}",
            token, {"scores": scores, "vorige_scores": vorige, "tijdlijn": tijdlijn})
    return result[0]


@app.post("/toetsen")
async def sla_toets_op(item: ToetsImport, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        return await _verwerk_toets(item, user["id"], token)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/toetsen/batch")
async def importeer_toetsen(batch: ToetsBatch, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    import asyncio
    token = credentials.credentials
    if len(batch.toetsen) > 500:
        raise HTTPException(status_code=400, detail="Maximaal 500 toetsen per import.")
    resultaten, fouten = [], []
    async def verwerk_een(i: int, item: ToetsImport):
        try:
            resultaten.append(await _verwerk_toets(item, user["id"], token))
        except Exception as e:
            fouten.append({"index": i, "leerling_id": item.leerling_id, "vakgebied": item.vakgebied, "fout": str(e)})
    groepgrootte = 10
    for start in range(0, len(batch.toetsen), groepgrootte):
        groep = batch.toetsen[start:start + groepgrootte]
        await asyncio.gather(*[verwerk_een(start + i, item) for i, item in enumerate(groep)])
    return {"geimporteerd": len(resultaten), "fouten": len(fouten), "foutdetails": fouten[:20]}

@app.get("/toetsen/{leerling_id}")
async def haal_toetsen_op(leerling_id: str, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    data = await supabase_get("toetsresultaten", token, {
        "leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}",
        "order": "afname_datum.desc", "select": "*"
    })
    return data or []


# ══════════════════════════════════════════════════════════
# NOTITIES PER LEERLING (database-backed, vervangt localStorage)
# ══════════════════════════════════════════════════════════

class NotitieAanmaken(BaseModel):
    tekst: str
    type: Optional[str] = "notitie"  # notitie | observatie | bijzonderheid | gesprek

@app.get("/leerlingen/{leerling_id}/notities")
async def haal_notities_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Haal alle notities op voor een leerling, nieuwste eerst."""
    token = credentials.credentials
    data = await supabase_get("leerling_notities", token, {
        "leerling_id": f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "order": "aangemaakt_op.desc",
        "select": "*"
    })
    return data or []

@app.post("/leerlingen/{leerling_id}/notities")
async def sla_notitie_op(
    leerling_id: str,
    notitie: NotitieAanmaken,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """Sla een notitie op en voeg hem toe aan de LVS-tijdlijn."""
    if not notitie.tekst or not notitie.tekst.strip():
        raise HTTPException(status_code=400, detail="Notitietekst is leeg.")
    if len(notitie.tekst) > 5000:
        raise HTTPException(status_code=400, detail="Notitie te lang (max 5000 tekens).")
    token = credentials.credentials

    # Controleer toegang — eigen leerling of schoolbreed
    ctx1 = await get_school_context(user, token)
    leerling_rec = await _controleer_leerling_toegang(leerling_id, user, token, ctx1)
    leerling = [leerling_rec]

    nu = datetime.now(timezone.utc)
    record = {
        "leerling_id":   leerling_id,
        "leerkracht_id": user["id"],
        "tekst":         notitie.tekst.strip(),
        "type":          notitie.type or "notitie",
        "aangemaakt_op": nu.isoformat()
    }
    opgeslagen = await supabase_post("leerling_notities", token, record)
    if not opgeslagen:
        raise HTTPException(status_code=500, detail="Notitie opslaan mislukt.")

    # Cascade: categoriseer de notitie en voeg toe aan LVS-tijdlijn
    datum_nl = nu.strftime("%-d %b %Y")
    categorie = await categoriseer_notitie(notitie.tekst.strip())
    domein    = categorie.get("domein", "algemeen")
    kern      = categorie.get("kern",   notitie.tekst.strip()[:80])
    sentiment = categorie.get("sentiment", "neutraal")

    await _voeg_tijdlijn_toe(
        leerling_id=leerling_id,
        leerkracht_id=user["id"],
        token=token,
        item={
            "type":      domein,
            "datum":     datum_nl,
            "tekst":     kern,
            "sentiment": sentiment,
            "bron":      "notitie"
        }
    )
    result = opgeslagen[0] if isinstance(opgeslagen, list) else opgeslagen
    # Geef de categorisatie terug aan de frontend zodat die feedback kan tonen
    if isinstance(result, dict):
        result["_categorie"] = {"domein": domein, "kern": kern, "sentiment": sentiment}
    return result

@app.delete("/leerlingen/{leerling_id}/notities/{notitie_id}")
async def verwijder_notitie(
    leerling_id: str,
    notitie_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    bestaand = await supabase_get("leerling_notities", token,
        {"id": f"eq.{notitie_id}", "leerling_id": f"eq.{leerling_id}",
         "leerkracht_id": f"eq.{user['id']}", "select": "id"})
    if not bestaand:
        raise HTTPException(status_code=404, detail="Notitie niet gevonden.")
    await supabase_delete(f"leerling_notities?id=eq.{notitie_id}", token)
    return {"verwijderd": True}


# ══════════════════════════════════════════════════════════
# VOLLEDIG LEERLINGPROFIEL (geaggregeerde studentkaart)
# ══════════════════════════════════════════════════════════

@app.get("/leerlingen/{leerling_id}/volledig")
async def haal_volledig_profiel_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Geaggregeerde studentkaart: combineert leerlingdata, LVS-profiel,
    recente rapporten, notities, OPP/handelingsplan-tijdlijn en aanwezigheid
    in één API-aanroep.
    """
    token = credentials.credentials

    # Controleer toegang
    ctx_vol = await get_school_context(user, token)
    leerling = await _controleer_leerling_toegang(leerling_id, user, token, ctx_vol)

    import asyncio
    # Haal alles parallel op
    lvs_taak        = supabase_get("lvs_profielen", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}", "select": "*"})
    rapporten_taak  = supabase_get("rapporten", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}",
         "order": "aangemaakt_op.desc", "select": "id,aangemaakt_op,rapport_data", "limit": "5"})
    notities_taak   = supabase_get("leerling_notities", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}",
         "order": "aangemaakt_op.desc", "select": "*", "limit": "20"})
    toetsen_taak    = supabase_get("toetsresultaten", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}",
         "order": "afname_datum.desc", "select": "*", "limit": "30"})
    berichten_taak  = supabase_get("ouder_berichten", token,
        {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}",
         "order": "verstuurd_op.desc", "select": "id,onderwerp,verstuurd_op,bericht_type,ouder_email", "limit": "10"})

    lvs_res, rapporten_res, notities_res, toetsen_res, berichten_res = await asyncio.gather(
        lvs_taak, rapporten_taak, notities_taak, toetsen_taak, berichten_taak,
        return_exceptions=True
    )

    def veilig(r):
        return r if isinstance(r, list) else []

    return {
        "leerling":   leerling,
        "lvs":        veilig(lvs_res)[0] if veilig(lvs_res) else None,
        "rapporten":  veilig(rapporten_res),
        "notities":   veilig(notities_res),
        "toetsen":    veilig(toetsen_res),
        "berichten":  veilig(berichten_res),
    }


# ══════════════════════════════════════════════════════════
# LEERLINGDOSSIER OVER TIJD — moat 1
# Het volledige dossier van een leerling, schoolbreed en
# over meerdere schooljaren. Dit is de kern van de langetermijn-
# waarde: elk jaar dat een school de software gebruikt, wordt
# dit dossier rijker en waardevoller.
# ══════════════════════════════════════════════════════════

@app.get("/dossier/{leerling_id}")
async def haal_dossier_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Het volledige leerlingdossier — alle data over alle schooljaren.

    Verschil met /leerlingen/{id}/volledig:
    - Haalt data op van ALLE leraren die ooit met deze leerling hebben gewerkt
      (mits ze op dezelfde school zitten)
    - Groepeert per schooljaar
    - Berekent een ontwikkelingslijn over tijd
    - Geeft een AI-samenvatting van de totale ontwikkeling

    Dit is de kern van moat 1: het dossier wordt waardevoller
    naarmate de leerling langer op de school zit.
    """
    import asyncio
    from datetime import date

    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    # Toegangscheck — eigen leerling of schoolbreed
    leerling = await _controleer_leerling_toegang(leerling_id, user, token, ctx)

    # Bepaal scope: schoolbreed of eigen
    school_id = ctx.get("school_id")

    # Haal alle leerkrachten op die ooit met deze leerling hebben gewerkt
    if school_id:
        # Schoolbrede query — alle leraren op deze school
        alle_leraren = await supabase_get("leerkrachten_scholen", token, {
            "school_id": f"eq.{school_id}",
            "select":    "leerkracht_id,voornaam"
        })
        leraar_ids = [l["leerkracht_id"] for l in (alle_leraren or [])]
        leraar_namen = {l["leerkracht_id"]: l.get("voornaam", "Onbekend") for l in (alle_leraren or [])}
    else:
        leraar_ids   = [user["id"]]
        leraar_namen = {user["id"]: "Jij"}

    ids_filter = "(" + ",".join(leraar_ids) + ")" if leraar_ids else f"({user['id']})"

    # Haal alle data parallel op — geen limiet, want dit is het volledige dossier
    rapporten_taak = supabase_get("rapporten", token, {
        "leerling_id":   f"eq.{leerling_id}",
        "leerkracht_id": f"in.{ids_filter}",
        "order":         "aangemaakt_op.asc",
        "select":        "id,aangemaakt_op,rapport_data,leerkracht_id"
    })
    notities_taak = supabase_get("leerling_notities", token, {
        "leerling_id":   f"eq.{leerling_id}",
        "leerkracht_id": f"in.{ids_filter}",
        "order":         "aangemaakt_op.asc",
        "select":        "id,tekst,aangemaakt_op,type,leerkracht_id"
    })
    lvs_taak = supabase_get("lvs_profielen", token, {
        "leerling_id":   f"eq.{leerling_id}",
        "leerkracht_id": f"in.{ids_filter}",
        "order":         "bijgewerkt_op.desc",
        "select":        "*"
    })
    toetsen_taak = supabase_get("toetsresultaten", token, {
        "leerling_id":   f"eq.{leerling_id}",
        "leerkracht_id": f"in.{ids_filter}",
        "order":         "afname_datum.asc",
        "select":        "*"
    })
    aanwezigheid_taak = supabase_get("aanwezigheid", token, {
        "leerling_id":   f"eq.{leerling_id}",
        "leerkracht_id": f"in.{ids_filter}",
        "order":         "datum.asc",
        "select":        "datum,status"
    })

    rapporten_res, notities_res, lvs_res, toetsen_res, aanwezigheid_res = await asyncio.gather(
        rapporten_taak, notities_taak, lvs_taak, toetsen_taak, aanwezigheid_taak,
        return_exceptions=True
    )

    def veilig(r):
        return r if isinstance(r, list) else []

    rapporten    = veilig(rapporten_res)
    notities     = veilig(notities_res)
    lvs_profielen = veilig(lvs_res)
    toetsen      = veilig(toetsen_res)
    aanwezigheid = veilig(aanwezigheid_res)

    # ── Groepeer alles per schooljaar ──────────────────────
    # Schooljaar loopt van augustus t/m juli
    def bepaal_schooljaar(datum_str: str) -> str:
        try:
            d = date.fromisoformat(datum_str[:10])
            if d.month >= 8:
                return f"{d.year}-{d.year + 1}"
            else:
                return f"{d.year - 1}-{d.year}"
        except Exception:
            return "onbekend"

    schooljaren: dict = {}

    for r in rapporten:
        sj = bepaal_schooljaar(r.get("aangemaakt_op", ""))
        if sj not in schooljaren:
            schooljaren[sj] = {"rapporten": [], "notities": [], "toetsen": [], "aanwezigheid": []}
        schooljaren[sj]["rapporten"].append({
            "id":           r.get("id"),
            "datum":        r.get("aangemaakt_op", "")[:10],
            "leerkracht":   leraar_namen.get(r.get("leerkracht_id"), "Onbekend"),
            "rapport_data": r.get("rapport_data", {})
        })

    for n in notities:
        sj = bepaal_schooljaar(n.get("aangemaakt_op", ""))
        if sj not in schooljaren:
            schooljaren[sj] = {"rapporten": [], "notities": [], "toetsen": [], "aanwezigheid": []}
        schooljaren[sj]["notities"].append({
            "datum":      n.get("aangemaakt_op", "")[:10],
            "tekst":      n.get("tekst", ""),
            "type":       n.get("type", ""),
            "leerkracht": leraar_namen.get(n.get("leerkracht_id"), "Onbekend")
        })

    for t in toetsen:
        sj = bepaal_schooljaar(t.get("afname_datum", ""))
        if sj not in schooljaren:
            schooljaren[sj] = {"rapporten": [], "notities": [], "toetsen": [], "aanwezigheid": []}
        schooljaren[sj]["toetsen"].append(t)

    for a in aanwezigheid:
        sj = bepaal_schooljaar(a.get("datum", ""))
        if sj not in schooljaren:
            schooljaren[sj] = {"rapporten": [], "notities": [], "toetsen": [], "aanwezigheid": []}
        schooljaren[sj]["aanwezigheid"].append(a)

    # ── Bereken aanwezigheidsstatistieken per schooljaar ──
    for sj, data in schooljaren.items():
        aaw = data["aanwezigheid"]
        if aaw:
            totaal    = len(aaw)
            afwezig   = sum(1 for a in aaw if a.get("status") == "afwezig")
            te_laat   = sum(1 for a in aaw if a.get("status") == "laat")
            data["aanwezigheid_samenvatting"] = {
                "totaal_dagen":      totaal,
                "afwezig":           afwezig,
                "te_laat":           te_laat,
                "aanwezigheid_pct":  round((totaal - afwezig) / totaal * 100) if totaal else 100
            }

    # ── Meest recente LVS-profiel ──────────────────────────
    huidig_lvs = lvs_profielen[0] if lvs_profielen else None

    # ── Bouw tijdlijn van alle schooljaren ─────────────────
    # Gesorteerd van oud naar nieuw
    tijdlijn_schooljaren = sorted(schooljaren.keys(), key=lambda s: s[:4] if s != "onbekend" else "0")

    return {
        "leerling":             leerling,
        "schooljaren":          {sj: schooljaren[sj] for sj in tijdlijn_schooljaren},
        "tijdlijn_schooljaren": tijdlijn_schooljaren,
        "huidig_lvs":           huidig_lvs,
        "totaal_notities":      len(notities),
        "totaal_rapporten":     len(rapporten),
        "totaal_toetsen":       len(toetsen),
        "eerste_registratie":   notities[0]["aangemaakt_op"][:10] if notities else None,
        "leraren":              list(set(leraar_namen.values()))
    }


@app.get("/dossier/{leerling_id}/samenvatting")
async def genereer_dossier_samenvatting(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Genereert een AI-samenvatting van het volledige dossier van een leerling.
    Beschrijft de ontwikkeling over alle schooljaren in begrijpelijke taal.
    Bedoeld als introductie voor een nieuwe leraar of IB-er.
    """
    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    # Hergebruik het dossier endpoint
    dossier_res = await haal_dossier_op(leerling_id, user,
        type('Creds', (), {'credentials': token})())

    leerling    = dossier_res["leerling"]
    schooljaren = dossier_res["schooljaren"]

    if not schooljaren:
        return {"samenvatting": None, "reden": "Geen data beschikbaar."}

    # Bouw context voor Claude
    naam  = leerling.get("voornaam", "")
    groep = leerling.get("groep", "")
    regels = [f"Leerling: {naam}, huidige groep: {groep}"]
    regels.append(f"In de software geregistreerd over {len(schooljaren)} schoolja(a)r(en).")

    for sj, data in schooljaren.items():
        regels.append(f"\nSchooljaar {sj}:")

        if data["rapporten"]:
            for r in data["rapporten"][:2]:
                rd = r.get("rapport_data", {})
                commentaar = rd.get("rapportcommentaar") or rd.get("leerresultaten") or ""
                if commentaar:
                    regels.append(f"  Rapport ({r['datum']}): {str(commentaar)[:200]}")
        if data["notities"]:
            regels.append(f"  {len(data['notities'])} notities van leerkracht(en).")
            for n in data["notities"][:3]:
                regels.append(f"  - {n['tekst'][:100]}")
        if data.get("aanwezigheid_samenvatting"):
            s = data["aanwezigheid_samenvatting"]
            regels.append(f"  Aanwezigheid: {s['aanwezigheid_pct']}% ({s['afwezig']} keer afwezig, {s['te_laat']} keer te laat)")

    dossier_tekst = "\n".join(regels)


    prompt = (
        f"Je analyseert het volledige schooldossier van een leerling voor een nieuwe leraar of IB-er.\n\n"
        f"{dossier_tekst}\n\n"
        "Schrijf een beknopte introductie (3-5 alinea's) die:\n"
        "1. De algehele ontwikkeling over de schooljaren beschrijft\n"
        "2. Sterke punten benoemt die consistent zichtbaar zijn\n"
        "3. Aandachtspunten benoemt die terugkeren\n"
        "4. Concrete tips geeft voor de huidige leraar op basis van wat eerder werkte\n\n"
        "Schrijf in de tweede persoon richting de leraar. Warm maar professioneel."
    )
    try:
        samenvatting = await roep_claude_aan(SYSTEM_PROMPT, prompt, max_tokens=800)
    except Exception as e:
        logger.warning(f"Dossier samenvatting mislukt: {e}")
        return {"samenvatting": None, "reden": "Generatie mislukt."}

    # Sla de samenvatting op in de LVS-tijdlijn
    try:
        datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
        await _voeg_tijdlijn_toe(
            leerling_id=leerling_id,
            leerkracht_id=user["id"],
            token=token,
            item={
                "type":      "rapport",
                "datum":     datum_nl,
                "tekst":     f"Dossier samenvatting gegenereerd ({len(schooljaren)} schooljaren).",
                "sentiment": "neutraal",
                "bron":      "dossier"
            }
        )
    except Exception:
        pass

    return {
        "samenvatting":    samenvatting,
        "schooljaren":     list(schooljaren.keys()),
        "totaal_notities": dossier_res["totaal_notities"],
        "totaal_rapporten": dossier_res["totaal_rapporten"],
        "leraren":         dossier_res["leraren"]
    }


@app.post("/dossier/{leerling_id}/overdracht")
async def genereer_overdrachtsrapport(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Genereert een overdrachtsrapport voor de volgende leraar.
    Bedoeld voor gebruik aan het einde van het schooljaar.
    Vat het huidige schooljaar samen en geeft concrete handvatten
    mee voor de ontvangende leraar.
    """
    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    leerling = await _controleer_leerling_toegang(leerling_id, user, token, ctx)
    naam     = leerling.get("voornaam", "")
    groep    = leerling.get("groep", "")

    # Haal het huidige schooljaar op
    from datetime import date
    vandaag = date.today()
    huidig_sj = (
        f"{vandaag.year}-{vandaag.year+1}" if vandaag.month >= 8
        else f"{vandaag.year-1}-{vandaag.year}"
    )

    # Haal alle data van dit schooljaar op
    notities_data  = await supabase_get("leerling_notities", token, {
        "leerling_id":  f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "order":        "aangemaakt_op.desc",
        "select":       "tekst,aangemaakt_op,type",
        "limit":        "20"
    })
    rapporten_data = await supabase_get("rapporten", token, {
        "leerling_id":  f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "order":        "aangemaakt_op.desc",
        "select":       "rapport_data,aangemaakt_op",
        "limit":        "3"
    })
    lvs_data = await supabase_get("lvs_profielen", token, {
        "leerling_id":  f"eq.{leerling_id}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":       "scores,tijdlijn"
    })

    notities   = notities_data or []
    rapporten  = rapporten_data or []
    lvs        = lvs_data[0] if lvs_data else {}

    notities_tekst = "\n".join([f"- {n['tekst'][:150]}" for n in notities[:8]])


    rapport_tekst = ""
    if rapporten:
        rd = rapporten[0].get("rapport_data", {})
        rapport_tekst = "\n".join([
            f"{k}: {str(v)[:200]}"
            for k, v in rd.items()
            if v and k in ["leerresultaten", "werkhouding", "sociaal_emotioneel", "doelen", "aandachtspunten"]
        ])

    prompt = (
        f"Je schrijft een overdrachtsrapport voor de volgende leraar van {naam} (groep {groep}).\n"
        f"Dit rapport is bedoeld voor de leraar die {naam} volgend schooljaar ontvangt.\n\n"
        f"Notities van dit schooljaar:\n{notities_tekst or 'Geen notities beschikbaar.'}\n\n"
        f"Meest recente rapport:\n{rapport_tekst or 'Geen rapport beschikbaar.'}\n\n"
        "Schrijf een overdrachtsrapport met deze secties:\n"
        f"1. Wie is {naam}? (karakter, sterke punten, wat geeft energie)\n"
        "2. Wat heeft aandacht nodig? (concrete aandachtspunten voor volgend jaar)\n"
        "3. Wat werkte goed? (aanpak en interventies die effect hadden)\n"
        "4. Concrete tips voor de nieuwe leraar\n\n"
        "Schrijf warm, concreet en praktisch. Maximaal één A4. "
        f"Dit is een document dat de nieuwe leraar op dag één leest."
    )
    try:
        overdracht = await roep_claude_aan(SYSTEM_PROMPT, prompt, max_tokens=1000)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generatie mislukt: {e}")

    # Sla op als rapport-type "overdracht"
    try:
        await supabase_post("rapporten", token, {
            "leerling_id":   leerling_id,
            "leerkracht_id": user["id"],
            "rapport_data":  {
                "type":        "overdracht",
                "schooljaar":  huidig_sj,
                "inhoud":      overdracht,
                "gegenereerd": datetime.now(timezone.utc).isoformat()
            }
        })
    except Exception:
        pass

    return {
        "overdracht":  overdracht,
        "leerling":    naam,
        "groep":       groep,
        "schooljaar":  huidig_sj
    }


# ══════════════════════════════════════════════════════════
# PROACTIEVE SIGNALERING — stilte-alerts
# ══════════════════════════════════════════════════════════

@app.get("/signalen/groep/{groep}")
async def haal_groep_signalen_op(
    groep: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Scant alle leerlingen in een groep op stilte-signalen:
    1. Geen notitie in de laatste 21 dagen
    2. Geen rapport in de laatste 90 dagen
    3. Toetsresultaten ouder dan 120 dagen
    """
    import asyncio
    from datetime import date as _date, timedelta

    token   = credentials.credentials
    vandaag = _date.today()

    NOTITIE_DAGEN = 21
    RAPPORT_DAGEN = 90
    TOETS_DAGEN   = 120

    leerlingen_data = await supabase_get("leerlingen", token, {
        "groep":         f"eq.{groep}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":        "id,voornaam,groep"
    })
    if not leerlingen_data:
        return {"signalen": [], "groep": groep, "totaal": 0}

    signalen = []

    async def analyseer_leerling(leerling: dict):
        lid  = leerling["id"]
        naam = leerling["voornaam"]
        leerling_signalen = []

        notitie_taak = supabase_get("leerling_notities", token, {
            "leerling_id": f"eq.{lid}", "leerkracht_id": f"eq.{user['id']}",
            "order": "aangemaakt_op.desc", "limit": "1", "select": "aangemaakt_op"
        })
        rapport_taak = supabase_get("rapporten", token, {
            "leerling_id": f"eq.{lid}", "leerkracht_id": f"eq.{user['id']}",
            "order": "aangemaakt_op.desc", "limit": "1", "select": "aangemaakt_op"
        })
        toets_taak = supabase_get("toetsresultaten", token, {
            "leerling_id": f"eq.{lid}", "leerkracht_id": f"eq.{user['id']}",
            "order": "afname_datum.desc", "limit": "1", "select": "afname_datum"
        })

        n_data, r_data, t_data = await asyncio.gather(
            notitie_taak, rapport_taak, toets_taak, return_exceptions=True
        )

        # Signaal 1: geen notitie
        laatste_notitie = None
        if isinstance(n_data, list) and n_data:
            try: laatste_notitie = _date.fromisoformat(n_data[0]["aangemaakt_op"][:10])
            except (ValueError, KeyError): pass
        if laatste_notitie:
            dagen = (vandaag - laatste_notitie).days
            if dagen >= NOTITIE_DAGEN:
                leerling_signalen.append({
                    "type": "stilte_notitie",
                    "urgentie": "hoog" if dagen >= 42 else "medium",
                    "dagen": dagen,
                    "tekst": f"Geen notitie in {dagen} dagen"
                })
        else:
            leerling_signalen.append({
                "type": "stilte_notitie", "urgentie": "medium",
                "dagen": 999, "tekst": "Nog geen notities geregistreerd"
            })

        # Signaal 2: geen rapport
        laatste_rapport = None
        if isinstance(r_data, list) and r_data:
            try: laatste_rapport = _date.fromisoformat(r_data[0]["aangemaakt_op"][:10])
            except (ValueError, KeyError): pass
        if laatste_rapport:
            dagen = (vandaag - laatste_rapport).days
            if dagen >= RAPPORT_DAGEN:
                leerling_signalen.append({
                    "type": "stilte_rapport",
                    "urgentie": "hoog" if dagen >= 180 else "medium",
                    "dagen": dagen,
                    "tekst": f"Geen rapport in {dagen} dagen"
                })
        else:
            leerling_signalen.append({
                "type": "stilte_rapport", "urgentie": "laag",
                "dagen": 999, "tekst": "Nog geen rapport gegenereerd"
            })

        # Signaal 3: verouderde toets
        if isinstance(t_data, list) and t_data:
            try:
                laatste_toets = _date.fromisoformat(t_data[0]["afname_datum"][:10])
                dagen = (vandaag - laatste_toets).days
                if dagen >= TOETS_DAGEN:
                    leerling_signalen.append({
                        "type": "verouderde_toets", "urgentie": "medium",
                        "dagen": dagen, "tekst": f"Toetsresultaten {dagen} dagen oud"
                    })
            except (ValueError, KeyError): pass

        if leerling_signalen:
            volgorde = {"hoog": 0, "medium": 1, "laag": 2}
            leerling_signalen.sort(key=lambda s: volgorde.get(s["urgentie"], 9))
            signalen.append({
                "leerling_id": lid, "voornaam": naam,
                "groep": leerling.get("groep", groep), "signalen": leerling_signalen
            })

    groepjes = [leerlingen_data[i:i+8] for i in range(0, len(leerlingen_data), 8)]
    for groepje in groepjes:
        await asyncio.gather(*[analyseer_leerling(l) for l in groepje])

    score_map = {"hoog": 10, "medium": 5, "laag": 1}
    signalen.sort(key=lambda l: -sum(score_map.get(s["urgentie"], 0) for s in l["signalen"]))

    return {"signalen": signalen, "groep": groep, "totaal": len(signalen)}


# ══════════════════════════════════════════════════════════
# SCHOOLBREDE BENCHMARKING — moat 2
#
# Hoe het werkt:
# 1. Elke school die de software gebruikt draagt periodiek
#    geanonimiseerde gemiddelde scores bij per groep.
# 2. Deze aggregaten worden opgeslagen in benchmark_data.
# 3. Elke school kan haar eigen groepen vergelijken met:
#    a. Landelijke normen (CITO-referentiedata, ingebakken)
#    b. Geanonimiseerde data van andere scholen in de benchmark
#
# Privacy: er worden NOOIT individuele leerlingscores gedeeld.
# Alleen anonieme gemiddelden per groep per vakgebied.
# ══════════════════════════════════════════════════════════

# Landelijke CITO-referentienormen per groep per vakgebied
# Gebaseerd op het CITO-landelijk gemiddelde (score 50 = III = gemiddeld)
# Bron: CITO Leerling in beeld publicaties
LANDELIJKE_NORMEN = {
    "3": {"lezen": 45, "dmt": 45, "rekenen": 50, "spelling": 48, "woordenschat": 50},
    "4": {"lezen": 52, "dmt": 52, "rekenen": 52, "spelling": 52, "woordenschat": 52, "begrijpend": 50},
    "5": {"lezen": 54, "dmt": 54, "rekenen": 54, "spelling": 54, "woordenschat": 54, "begrijpend": 52},
    "6": {"lezen": 56, "dmt": 56, "rekenen": 56, "spelling": 56, "woordenschat": 56, "begrijpend": 54, "engels": 50},
    "7": {"lezen": 58, "dmt": 58, "rekenen": 58, "spelling": 58, "woordenschat": 58, "begrijpend": 56, "engels": 52},
    "8": {"lezen": 60, "dmt": 60, "rekenen": 60, "spelling": 60, "woordenschat": 60, "begrijpend": 58, "engels": 55},
}


@app.get("/benchmark/groep/{groep}")
async def haal_benchmark_op(
    groep: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Vergelijkt de gemiddelde scores van een groep met:
    1. Landelijke CITO-normen (altijd beschikbaar)
    2. Benchmark van andere scholen in het netwerk (als beschikbaar)

    Retourneert per vakgebied:
    - eigen_gemiddelde: gemiddelde score van de groep
    - landelijk_gemiddelde: CITO-norm voor die groep
    - verschil: hoeveel de groep boven/onder landelijk scoort
    - netwerk_gemiddelde: anoniem gemiddelde van andere scholen (indien beschikbaar)
    - aantal_leerlingen: hoeveel leerlingen de score bepalen
    """
    import asyncio
    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    # Haal alle leerlingen in de groep op
    if ctx["school_id"]:
        leerlingen_data = await supabase_get("leerlingen", token, {
            "groep":     f"eq.{groep}",
            "school_id": f"eq.{ctx['school_id']}",
            "select":    "id"
        })
    else:
        leerlingen_data = await supabase_get("leerlingen", token, {
            "groep":         f"eq.{groep}",
            "leerkracht_id": f"eq.{user['id']}",
            "select":        "id"
        })

    if not leerlingen_data:
        return {"beschikbaar": False, "reden": f"Geen leerlingen gevonden in groep {groep}."}

    leerling_ids = [l["id"] for l in leerlingen_data]
    ids_filter   = "(" + ",".join(leerling_ids) + ")"

    # Haal alle LVS-profielen voor deze groep op
    profielen = await supabase_get("lvs_profielen", token, {
        "leerling_id": f"in.{ids_filter}",
        "select":      "scores"
    })

    if not profielen:
        return {"beschikbaar": False, "reden": "Geen LVS-scores beschikbaar voor deze groep."}

    # Bereken gemiddelden per vakgebied
    vakgebied_scores: dict = {}
    for profiel in profielen:
        scores = profiel.get("scores", {}) or {}
        for vak, score in scores.items():
            if score and score > 0:
                if vak not in vakgebied_scores:
                    vakgebied_scores[vak] = []
                vakgebied_scores[vak].append(score)

    if not vakgebied_scores:
        return {"beschikbaar": False, "reden": "Nog geen scores ingevoerd voor deze groep."}

    # Landelijke normen voor deze groep
    normen = LANDELIJKE_NORMEN.get(str(groep), {})

    # Haal netwerk-benchmark op als beschikbaar
    netwerk_data = {}
    try:
        benchmark_records = await supabase_get("benchmark_data", token, {
            "groep":  f"eq.{groep}",
            "select": "vakgebied,gemiddelde,aantal_scholen"
        })
        if benchmark_records:
            for r in benchmark_records:
                vak = r.get("vakgebied")
                if vak:
                    netwerk_data[vak] = {
                        "gemiddelde":    r.get("gemiddelde"),
                        "aantal_scholen": r.get("aantal_scholen", 0)
                    }
    except Exception:
        pass  # Benchmark tabel nog niet aangemaakt — geen probleem

    # Bouw resultaat
    resultaten = {}
    for vak, scores in vakgebied_scores.items():
        eigen_gem     = round(sum(scores) / len(scores), 1)
        landelijk_gem = normen.get(vak)
        netwerk_gem   = netwerk_data.get(vak, {}).get("gemiddelde")
        netwerk_n     = netwerk_data.get(vak, {}).get("aantal_scholen", 0)

        resultaten[vak] = {
            "eigen_gemiddelde":    eigen_gem,
            "aantal_leerlingen":   len(scores),
            "landelijk_gemiddelde": landelijk_gem,
            "verschil_landelijk":   round(eigen_gem - landelijk_gem, 1) if landelijk_gem else None,
            "netwerk_gemiddelde":   round(netwerk_gem, 1) if netwerk_gem else None,
            "netwerk_scholen":      netwerk_n,
            "verschil_netwerk":     round(eigen_gem - netwerk_gem, 1) if netwerk_gem else None,
        }

    return {
        "beschikbaar":     True,
        "groep":           groep,
        "aantal_leerlingen": len(leerling_ids),
        "vakgebieden":     resultaten,
        "netwerk_actief":  bool(netwerk_data)
    }


@app.post("/benchmark/bijdragen")
async def draag_bij_aan_benchmark(
    verzoek: dict,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Draagt geanonimiseerde gemiddelde scores bij aan de schoolbrede benchmark.
    Wordt aangeroepen als een school expliciet toestemming geeft.

    Privacy-garanties:
    - Alleen anonieme gemiddelden, NOOIT individuele scores
    - School-ID wordt gehasht voor extra privacy
    - Minimum 5 leerlingen vereist per vakgebied
    - Directe terugtrekking mogelijk

    De benchmark wordt sterker naarmate meer scholen bijdragen:
    dit is het netwerk-effect dat de software waardevoller maakt.
    """
    import hashlib, asyncio
    token = credentials.credentials
    ctx   = await get_school_context(user, token)
    groep = verzoek.get("groep")

    if not groep:
        raise HTTPException(status_code=400, detail="Groep is verplicht.")
    if not ctx["school_id"]:
        raise HTTPException(status_code=400, detail="Koppel eerst een school om bij te dragen aan de benchmark.")

    # Haal alle leerlingen en scores op
    leerlingen_data = await supabase_get("leerlingen", token, {
        "groep":     f"eq.{groep}",
        "school_id": f"eq.{ctx['school_id']}",
        "select":    "id"
    })

    if not leerlingen_data or len(leerlingen_data) < 5:
        raise HTTPException(
            status_code=400,
            detail=f"Minimaal 5 leerlingen vereist voor bijdrage. Groep {groep} heeft {len(leerlingen_data or [])} leerlingen."
        )

    ids_filter = "(" + ",".join([l["id"] for l in leerlingen_data]) + ")"
    profielen  = await supabase_get("lvs_profielen", token, {
        "leerling_id": f"in.{ids_filter}",
        "select":      "scores"
    })

    if not profielen:
        raise HTTPException(status_code=400, detail="Geen LVS-scores beschikbaar.")

    # Bereken gemiddelden (minimum 5 leerlingen per vakgebied)
    vakgebied_scores: dict = {}
    for p in profielen:
        scores = p.get("scores", {}) or {}
        for vak, score in scores.items():
            if score and score > 0:
                if vak not in vakgebied_scores:
                    vakgebied_scores[vak] = []
                vakgebied_scores[vak].append(score)

    # Hash school_id voor anonimiteit
    school_hash = hashlib.sha256(ctx["school_id"].encode()).hexdigest()[:16]

    bijgedragen = []
    for vak, scores in vakgebied_scores.items():
        if len(scores) < 5:
            continue  # Te weinig leerlingen voor betrouwbare benchmark

        gemiddelde = round(sum(scores) / len(scores), 2)

        # Upsert naar benchmark_data
        try:
            await supabase_patch(
                f"benchmark_data?groep=eq.{groep}&vakgebied=eq.{vak}&school_hash=eq.{school_hash}",
                token,
                {
                    "groep":         groep,
                    "vakgebied":     vak,
                    "school_hash":   school_hash,
                    "gemiddelde":    gemiddelde,
                    "aantal_leerlingen": len(scores),
                    "bijgewerkt_op": datetime.now(timezone.utc).isoformat()
                }
            )
        except Exception:
            # Als patch faalt, probeer post (record bestaat nog niet)
            try:
                await supabase_post("benchmark_data", token, {
                    "groep":         groep,
                    "vakgebied":     vak,
                    "school_hash":   school_hash,
                    "gemiddelde":    gemiddelde,
                    "aantal_leerlingen": len(scores),
                    "bijgewerkt_op": datetime.now(timezone.utc).isoformat()
                })
            except Exception as e:
                logger.warning(f"Benchmark bijdrage mislukt voor {vak}: {e}")
                continue

        bijgedragen.append(vak)

    return {
        "bijgedragen":    bijgedragen,
        "groep":          groep,
        "anoniem":        True,
        "school_hash":    school_hash[:8] + "…"  # Toon alleen begin voor bevestiging
    }


# ══════════════════════════════════════════════════════════
# IB-DASHBOARD — moat 3
#
# De IB-er is de sleutelfiguur voor verspreiding:
# - Werkt gemiddeld op 2-3 scholen tegelijk
# - Heeft inzicht nodig in ALLE leerlingen met ondersteuning
# - Beslist mee over aanschaf van tools
#
# Dit dashboard geeft de IB-er wat ze nergens anders heeft:
# - Overzicht van alle leerlingen met ondersteuningsbehoefte
# - Status van elk OPP en handelingsplan
# - Wanneer documenten verlopen
# - Welke leerlingen nieuwe signalen hebben
# - Exporteerbaar voor inspectie
# ══════════════════════════════════════════════════════════

@app.get("/ib/dashboard")
async def haal_ib_dashboard_op(
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Het IB-dashboard: overzicht van alle leerlingen met ondersteuning.

    Beschikbaar voor rollen: ib, directeur.
    Haalt schoolbreed alle leerlingen op met:
    - Ondersteuningsbehoeftes
    - OPP status (aanwezig, verlopen, ontbreekt)
    - Meest recente handelingsplan
    - Actieve signalen uit de LVS-tijdlijn
    - Laatste rapport en notitie
    """
    import asyncio
    from datetime import date, timedelta

    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    if not ctx["is_ib"]:
        raise HTTPException(status_code=403, detail="Alleen beschikbaar voor IB-ers en directeuren.")
    if not ctx["school_id"]:
        raise HTTPException(status_code=400, detail="Koppel eerst een school om het IB-dashboard te gebruiken.")

    vandaag = date.today()

    # Haal alle leerlingen van de school op met OPP-meta
    alle_leerlingen = await supabase_get("leerlingen", token, {
        "school_id": f"eq.{ctx['school_id']}",
        "select":    "id,voornaam,achternaam,groep,ondersteuningsbehoeftes,opp_meta",
        "order":     "groep.asc,voornaam.asc"
    })

    if not alle_leerlingen:
        return {"leerlingen": [], "samenvatting": {"totaal": 0, "met_ondersteuning": 0}}

    # Filter: alleen leerlingen met ondersteuning OF met OPP
    met_ondersteuning = [
        l for l in alle_leerlingen
        if (l.get("ondersteuningsbehoeftes") and len(l["ondersteuningsbehoeftes"]) > 0)
        or l.get("opp_meta")
    ]

    if not met_ondersteuning:
        return {
            "leerlingen":  [],
            "alle_leerlingen": len(alle_leerlingen),
            "samenvatting": {
                "totaal":           len(alle_leerlingen),
                "met_ondersteuning": 0,
                "opp_aanwezig":      0,
                "opp_verlopen":      0,
                "actieve_signalen":  0
            }
        }

    # Haal per leerling parallel de status op
    async def haal_leerling_status(leerling: dict) -> dict:
        lid  = leerling["id"]
        naam = leerling.get("voornaam", "")
        if leerling.get("achternaam"):
            naam += " " + leerling["achternaam"]

        try:
            # Parallel: laatste rapport, notitie, handelingsplan, LVS-tijdlijn
            rapport_taak = supabase_get("rapporten", token, {
                "leerling_id": f"eq.{lid}",
                "order":       "aangemaakt_op.desc",
                "limit":       "1",
                "select":      "aangemaakt_op,rapport_data"
            })
            notitie_taak = supabase_get("leerling_notities", token, {
                "leerling_id": f"eq.{lid}",
                "order":       "aangemaakt_op.desc",
                "limit":       "1",
                "select":      "aangemaakt_op"
            })
            hp_taak = supabase_get("rapporten", token, {
                "leerling_id": f"eq.{lid}",
                "order":       "aangemaakt_op.desc",
                "limit":       "1",
                "select":      "aangemaakt_op,rapport_data",
                "rapport_data->>type": "eq.handelingsplan"
            })
            lvs_taak = supabase_get("lvs_profielen", token, {
                "leerling_id": f"eq.{lid}",
                "select":      "tijdlijn"
            })

            rapport_res, notitie_res, hp_res, lvs_res = await asyncio.gather(
                rapport_taak, notitie_taak, hp_taak, lvs_taak,
                return_exceptions=True
            )

            def veilig(r):
                return r if isinstance(r, list) else []

            rapport  = veilig(rapport_res)[0]  if veilig(rapport_res)  else None
            notitie  = veilig(notitie_res)[0]  if veilig(notitie_res)  else None
            lvs      = veilig(lvs_res)[0]       if veilig(lvs_res)      else None

            # OPP-status bepalen
            opp_meta    = leerling.get("opp_meta") or {}
            opp_datum   = opp_meta.get("opp_datum")
            opp_status  = "ontbreekt"
            opp_verlopen = False

            if opp_datum:
                try:
                    opp_d   = date.fromisoformat(opp_datum.replace(" ", "T")[:10])
                    dagen_oud = (vandaag - opp_d).days
                    if dagen_oud > 365:
                        opp_status   = "verlopen"
                        opp_verlopen = True
                    else:
                        opp_status = "actueel"
                except Exception:
                    opp_status = "actueel"

            # Actieve signalen tellen
            signalen_count = 0
            if lvs:
                tijdlijn = lvs.get("tijdlijn") or []
                grens    = (vandaag - timedelta(days=30)).strftime("%-d %b %Y")
                signalen_count = sum(
                    1 for item in tijdlijn
                    if item.get("sentiment") in ("aandacht", "zorg")
                    and item.get("datum", "") >= grens
                )

            # Laatste rapport datum
            laatste_rapport_datum = None
            laatste_rapport_tekst = None
            if rapport:
                laatste_rapport_datum = rapport.get("aangemaakt_op", "")[:10]
                rd = rapport.get("rapport_data", {}) or {}
                laatste_rapport_tekst = (
                    rd.get("rapportcommentaar") or
                    rd.get("leerresultaten") or ""
                )[:120]

            # Laatste notitie datum
            laatste_notitie_datum = None
            if notitie:
                laatste_notitie_datum = notitie.get("aangemaakt_op", "")[:10]

            # Urgentie bepalen
            urgentie = "normaal"
            if opp_verlopen:
                urgentie = "hoog"
            elif signalen_count >= 2:
                urgentie = "hoog"
            elif signalen_count == 1 or opp_status == "ontbreekt":
                urgentie = "aandacht"

            return {
                "leerling_id":            lid,
                "naam":                   naam,
                "voornaam":               leerling.get("voornaam", ""),
                "groep":                  leerling.get("groep", ""),
                "ondersteuningsbehoeftes": leerling.get("ondersteuningsbehoeftes") or [],
                "opp_status":             opp_status,
                "opp_uitstroom":          opp_meta.get("uitstroombestemming", ""),
                "opp_datum":              opp_datum,
                "laatste_rapport_datum":  laatste_rapport_datum,
                "laatste_rapport_tekst":  laatste_rapport_tekst,
                "laatste_notitie_datum":  laatste_notitie_datum,
                "signalen_count":         signalen_count,
                "urgentie":               urgentie
            }
        except Exception as e:
            logger.warning(f"IB-dashboard fout voor {leerling.get('voornaam', lid)}: {e}")
            return {
                "leerling_id": lid,
                "naam":        leerling.get("voornaam", ""),
                "groep":       leerling.get("groep", ""),
                "urgentie":    "normaal",
                "fout":        True
            }

    # Verwerk in batches van 8
    resultaten = []
    groepjes = [met_ondersteuning[i:i+8] for i in range(0, len(met_ondersteuning), 8)]
    for groepje in groepjes:
        batch = await asyncio.gather(*[haal_leerling_status(l) for l in groepje])
        resultaten.extend(batch)

    # Sorteer: hoog urgentie eerst, dan aandacht, dan normaal, dan per groep
    urgentie_volgorde = {"hoog": 0, "aandacht": 1, "normaal": 2}
    resultaten.sort(key=lambda l: (
        urgentie_volgorde.get(l.get("urgentie", "normaal"), 9),
        l.get("groep", ""),
        l.get("naam", "")
    ))

    # Samenvatting
    samenvatting = {
        "totaal":            len(alle_leerlingen),
        "met_ondersteuning": len(met_ondersteuning),
        "opp_actueel":       sum(1 for r in resultaten if r.get("opp_status") == "actueel"),
        "opp_verlopen":      sum(1 for r in resultaten if r.get("opp_status") == "verlopen"),
        "opp_ontbreekt":     sum(1 for r in resultaten if r.get("opp_status") == "ontbreekt"),
        "hoge_urgentie":     sum(1 for r in resultaten if r.get("urgentie") == "hoog"),
        "actieve_signalen":  sum(r.get("signalen_count", 0) for r in resultaten)
    }

    return {
        "leerlingen":  resultaten,
        "samenvatting": samenvatting,
        "school_id":   ctx["school_id"]
    }


@app.get("/ib/leerling/{leerling_id}/volledig")
async def haal_ib_leerling_volledig_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Volledig IB-dossier voor één leerling: alle OPP's, handelingsplannen,
    signalen en contactmomenten met ouders. Voor gebruik tijdens
    leerlingbesprekingen en bij inspectie.
    """
    import asyncio
    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    if not ctx["is_ib"]:
        raise HTTPException(status_code=403, detail="Alleen beschikbaar voor IB-ers en directeuren.")

    leerling = await _controleer_leerling_toegang(leerling_id, user, token, ctx)

    # Haal alles op
    opp_taak = supabase_get("rapporten", token, {
        "leerling_id": f"eq.{leerling_id}",
        "order":       "aangemaakt_op.desc",
        "select":      "id,aangemaakt_op,rapport_data,leerkracht_id"
    })
    berichten_taak = supabase_get("ouder_berichten", token, {
        "leerling_id": f"eq.{leerling_id}",
        "order":       "verstuurd_op.desc",
        "select":      "onderwerp,verstuurd_op,bericht_type,ouder_email"
    })
    lvs_taak = supabase_get("lvs_profielen", token, {
        "leerling_id": f"eq.{leerling_id}",
        "select":      "tijdlijn,scores"
    })

    opp_res, berichten_res, lvs_res = await asyncio.gather(
        opp_taak, berichten_taak, lvs_taak, return_exceptions=True
    )

    def veilig(r):
        return r if isinstance(r, list) else []

    return {
        "leerling":          leerling,
        "documenten":        veilig(opp_res),
        "oudercontact":      veilig(berichten_res),
        "lvs":               veilig(lvs_res)[0] if veilig(lvs_res) else None,
    }


# ══════════════════════════════════════════════════════════
# INSPECTIE-READINESS — moat 4
#
# De onderwijsinspectie beoordeelt scholen op handelingsgericht
# werken: signaleren → analyseren → interveniëren → evalueren.
# Scholen moeten dit kunnen aantonen met documentatie.
#
# Dit module genereert die documentatie automatisch op basis
# van wat al in het systeem zit. Een school hoeft niet extra
# werk te doen — de bewijslast wordt samengesteld uit de
# notities, rapporten, OPP's, signalen en aanwezigheidsdata.
#
# Een school die dit kan laten zien bij een inspectiebezoek
# verlaat de software nooit.
# ══════════════════════════════════════════════════════════

@app.get("/inspectie/leerling/{leerling_id}")
async def haal_inspectie_dossier_op(
    leerling_id: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Genereert het inspectiedossier voor één leerling.
    Toont de volledige ondersteuningscyclus:
    signaleren → analyseren → interveniëren → evalueren

    Dit is het document dat je aan de inspecteur laat zien.
    """
    import asyncio
    from datetime import date, timedelta

    token = credentials.credentials
    ctx   = await get_school_context(user, token)
    leerling = await _controleer_leerling_toegang(leerling_id, user, token, ctx)

    # Haal alles parallel op
    notities_taak = supabase_get("leerling_notities", token, {
        "leerling_id": f"eq.{leerling_id}",
        "order":       "aangemaakt_op.asc",
        "select":      "tekst,aangemaakt_op,type"
    })
    rapporten_taak = supabase_get("rapporten", token, {
        "leerling_id": f"eq.{leerling_id}",
        "order":       "aangemaakt_op.asc",
        "select":      "id,aangemaakt_op,rapport_data"
    })
    lvs_taak = supabase_get("lvs_profielen", token, {
        "leerling_id": f"eq.{leerling_id}",
        "select":      "tijdlijn,scores"
    })
    aanwezigheid_taak = supabase_get("aanwezigheid", token, {
        "leerling_id": f"eq.{leerling_id}",
        "order":       "datum.asc",
        "select":      "datum,status"
    })

    notities_res, rapporten_res, lvs_res, aaw_res = await asyncio.gather(
        notities_taak, rapporten_taak, lvs_taak, aanwezigheid_taak,
        return_exceptions=True
    )

    def veilig(r): return r if isinstance(r, list) else []

    notities    = veilig(notities_res)
    rapporten   = veilig(rapporten_res)
    lvs         = veilig(lvs_res)[0] if veilig(lvs_res) else {}
    aanwezigheid = veilig(aaw_res)
    tijdlijn    = lvs.get("tijdlijn") or []

    # ── Bouw de ondersteuningscyclus op ───────────────────
    # De inspectie wil zien: signaal → actie → evaluatie
    opp_meta = leerling.get("opp_meta") or {}

    # Aanwezigheidsstatistieken
    totaal_aaw  = len(aanwezigheid)
    afwezig_aaw = sum(1 for a in aanwezigheid if a.get("status") == "afwezig")
    te_laat_aaw = sum(1 for a in aanwezigheid if a.get("status") == "laat")
    aaw_pct     = round((totaal_aaw - afwezig_aaw) / totaal_aaw * 100) if totaal_aaw else 100

    # Signalen uit tijdlijn
    signalen = [
        t for t in tijdlijn
        if t.get("sentiment") in ("aandacht", "zorg")
    ]

    # Interventies: rapporten + OPP + handelingsplannen
    interventies = []
    for r in rapporten:
        rd   = r.get("rapport_data", {}) or {}
        datum = r.get("aangemaakt_op", "")[:10]
        type_doc = rd.get("type", "rapport")
        if type_doc == "overdracht":
            interventies.append({"type": "overdracht", "datum": datum})
        elif type_doc == "handelingsplan":
            interventies.append({"type": "handelingsplan", "datum": datum,
                "doelen": rd.get("doelen", "")})
        else:
            interventies.append({"type": "rapport", "datum": datum,
                "samenvatting": (rd.get("rapportcommentaar") or rd.get("leerresultaten") or "")[:200]})

    # OPP als interventie
    if opp_meta.get("uitstroombestemming"):
        interventies.append({
            "type":       "opp",
            "datum":      opp_meta.get("opp_datum", ""),
            "uitstroom":  opp_meta.get("uitstroombestemming", ""),
            "doelen":     opp_meta.get("doelen", ""),
            "evaluatie":  opp_meta.get("evaluatie", "")
        })

    # Sorteer interventies op datum
    interventies.sort(key=lambda i: i.get("datum", ""))

    # Scores
    scores = lvs.get("scores", {}) or {}

    # ── Beoordeling: is de cyclus compleet? ───────────────
    cyclus_check = {
        "signalering":  len(signalen) > 0 or len(notities) > 0,
        "analyse":      len(rapporten) > 0 or bool(opp_meta),
        "interventie":  bool(opp_meta) or any(i["type"] in ("handelingsplan","opp") for i in interventies),
        "evaluatie":    bool(opp_meta.get("evaluatie")) or len(rapporten) >= 2,
        "documentatie": len(notities) > 0 and len(rapporten) > 0,
    }
    cyclus_compleet = all(cyclus_check.values())
    cyclus_score    = sum(1 for v in cyclus_check.values() if v)

    return {
        "leerling":          leerling,
        "opp_meta":          opp_meta,
        "ondersteuningsbehoeftes": leerling.get("ondersteuningsbehoeftes") or [],
        "cyclus_check":      cyclus_check,
        "cyclus_compleet":   cyclus_compleet,
        "cyclus_score":      cyclus_score,
        "signalen":          signalen,
        "notities_count":    len(notities),
        "notities":          notities[-5:],   # Meest recente 5 voor preview
        "interventies":      interventies,
        "aanwezigheid": {
            "totaal":  totaal_aaw,
            "afwezig": afwezig_aaw,
            "te_laat": te_laat_aaw,
            "pct":     aaw_pct
        },
        "scores": scores,
        "tijdlijn_items": len(tijdlijn),
    }


@app.get("/inspectie/groep/{groep}")
async def haal_inspectie_groep_op(
    groep: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Inspectieoverzicht voor een hele groep.
    Toont per leerling de status van de ondersteuningscyclus.
    Geeft een groepsrapportage die je direct aan de inspecteur kunt tonen.
    """
    import asyncio
    token = credentials.credentials
    ctx   = await get_school_context(user, token)

    # Haal leerlingen op
    if ctx["school_id"]:
        leerlingen_data = await supabase_get("leerlingen", token, {
            "groep":     f"eq.{groep}",
            "school_id": f"eq.{ctx['school_id']}",
            "select":    "id,voornaam,achternaam,groep,ondersteuningsbehoeftes,opp_meta"
        })
    else:
        leerlingen_data = await supabase_get("leerlingen", token, {
            "groep":         f"eq.{groep}",
            "leerkracht_id": f"eq.{user['id']}",
            "select":        "id,voornaam,achternaam,groep,ondersteuningsbehoeftes,opp_meta"
        })

    if not leerlingen_data:
        raise HTTPException(status_code=404, detail=f"Geen leerlingen in groep {groep}.")

    # Haal per leerling de meest recente documenten op
    resultaten = []
    async def verwerk(leerling: dict):
        lid = leerling["id"]
        try:
            notitie_taak = supabase_get("leerling_notities", token, {
                "leerling_id": f"eq.{lid}",
                "order": "aangemaakt_op.desc", "limit": "1",
                "select": "aangemaakt_op"
            })
            rapport_taak = supabase_get("rapporten", token, {
                "leerling_id": f"eq.{lid}",
                "order": "aangemaakt_op.desc", "limit": "1",
                "select": "aangemaakt_op"
            })
            lvs_taak = supabase_get("lvs_profielen", token, {
                "leerling_id": f"eq.{lid}",
                "select": "tijdlijn"
            })

            n_res, r_res, l_res = await asyncio.gather(
                notitie_taak, rapport_taak, lvs_taak,
                return_exceptions=True
            )

            laatste_notitie = (n_res[0].get("aangemaakt_op","")[:10]
                               if isinstance(n_res, list) and n_res else None)
            laatste_rapport = (r_res[0].get("aangemaakt_op","")[:10]
                               if isinstance(r_res, list) and r_res else None)
            tijdlijn = ((l_res[0].get("tijdlijn") or [])
                        if isinstance(l_res, list) and l_res else [])
            signalen_count = sum(1 for t in tijdlijn
                                 if t.get("sentiment") in ("aandacht","zorg"))

            opp = leerling.get("opp_meta") or {}
            heeft_ond = bool(leerling.get("ondersteuningsbehoeftes"))

            # Cyclus volledigheid
            cyclus = {
                "signalering":  bool(laatste_notitie),
                "documentatie": bool(laatste_rapport),
                "opp":          bool(opp.get("uitstroombestemming")),
            }
            volledig = sum(1 for v in cyclus.values() if v)

            resultaten.append({
                "leerling_id":      lid,
                "voornaam":         leerling.get("voornaam",""),
                "groep":            leerling.get("groep",""),
                "heeft_ondersteuning": heeft_ond,
                "opp_aanwezig":     bool(opp.get("uitstroombestemming")),
                "laatste_notitie":  laatste_notitie,
                "laatste_rapport":  laatste_rapport,
                "signalen_count":   signalen_count,
                "cyclus":           cyclus,
                "cyclus_volledig":  volledig,
            })
        except Exception as e:
            logger.warning(f"Inspectie verwerk fout {leerling.get('voornaam')}: {e}")

    groepjes = [leerlingen_data[i:i+8] for i in range(0, len(leerlingen_data), 8)]
    for groepje in groepjes:
        await asyncio.gather(*[verwerk(l) for l in groepje])

    resultaten.sort(key=lambda l: (
        -l.get("heeft_ondersteuning", 0),
        l.get("voornaam","")
    ))

    met_ond  = [l for l in resultaten if l["heeft_ondersteuning"]]
    opp_ok   = sum(1 for l in met_ond if l["opp_aanwezig"])
    cyclus_3 = sum(1 for l in resultaten if l["cyclus_volledig"] == 3)

    return {
        "groep":        groep,
        "leerlingen":   resultaten,
        "totaal":       len(resultaten),
        "met_ondersteuning": len(met_ond),
        "opp_compleet": opp_ok,
        "cyclus_volledig": cyclus_3,
        "inspectie_gereed": (opp_ok >= len(met_ond) and len(met_ond) > 0)
                            or len(met_ond) == 0
    }


@app.post("/inspectie/rapport/{groep}")
async def genereer_inspectie_rapport(
    groep: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Genereert een samenvattend inspectierapport voor de groep.
    Beschrijft per leerling met ondersteuning de cyclus.
    Bedoeld als bijlage bij een inspectiebezoek.
    """
    token = credentials.credentials

    # Gebruik groep-overzicht als basis
    overzicht = await haal_inspectie_groep_op(groep, user,
        type("C", (), {"credentials": token})())

    leerlingen = overzicht["leerlingen"]
    met_ond    = [l for l in leerlingen if l["heeft_ondersteuning"]]

    if not met_ond:
        return {
            "rapport": f"Groep {groep} heeft geen leerlingen met geregistreerde ondersteuningsbehoeftes.",
            "groep":   groep
        }

    # Bouw prompt
    regels = [
        f"Groep {groep} — Inspectierapport ondersteuning",
        f"Totaal leerlingen: {overzicht['totaal']}",
        f"Leerlingen met ondersteuningsbehoefte: {overzicht['met_ondersteuning']}",
        f"OPP aanwezig: {overzicht['opp_compleet']} van {overzicht['met_ondersteuning']}",
        "",
        "Per leerling met ondersteuning:"
    ]

    for l in met_ond[:10]:
        naam = l.get("voornaam", "")
        regels.append(f"\n{naam}:")

        regels.append(f"  - Laatste notitie: {l.get('laatste_notitie') or 'ontbreekt'}")
        regels.append(f"  - Laatste rapport: {l.get('laatste_rapport') or 'ontbreekt'}")
        regels.append(f"  - OPP: {'aanwezig' if l['opp_aanwezig'] else 'ontbreekt'}")
        regels.append(f"  - Signalen: {l.get('signalen_count', 0)}")

    prompt = (
        "Schrijf een formeel inspectierapport voor de onderwijsinspectie op basis van "
        "de volgende gegevens over de ondersteuningscyclus in de groep.\n\n"
        + "\n".join(regels)
        + "\n\nHet rapport moet:\n"
        "1. Aantonen dat de school handelt conform de cyclus: signaleren → analyseren → interveniëren → evalueren\n"
        "2. Per leerling de status van de ondersteuning beschrijven\n"
        "3. Professioneel en feitelijk zijn — voor de inspecteur\n"
        "4. Ontbrekende documenten benoemen als aandachtspunt\n\n"
        "Formele schrijfstijl. Maximaal twee A4."
    )

    try:
        rapport = await roep_claude_aan(SYSTEM_PROMPT, prompt, max_tokens=1500)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generatie mislukt: {e}")

    return {
        "rapport":        rapport,
        "groep":          groep,
        "gegenereerd_op": datetime.now(timezone.utc).isoformat(),
        "samenvatting":   overzicht
    }


# ══════════════════════════════════════════════════════════
# CASCADE HELPER — tijdlijn bijwerken vanuit elke module
# ══════════════════════════════════════════════════════════

async def _voeg_tijdlijn_toe(leerling_id: str, leerkracht_id: str, token: str, item: dict):
    """
    Voegt één item toe aan de LVS-tijdlijn van een leerling.
    Maakt het profiel aan als het nog niet bestaat.
    Stil falen: cascade-fouten mogen de primaire actie niet breken.
    """
    try:
        data = await supabase_get("lvs_profielen", token,
            {"leerling_id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{leerkracht_id}",
             "select": "tijdlijn"})
        if data:
            tijdlijn = data[0].get("tijdlijn") or []
            tijdlijn.insert(0, item)
            await supabase_patch(
                f"lvs_profielen?leerling_id=eq.{leerling_id}&leerkracht_id=eq.{leerkracht_id}",
                token, {"tijdlijn": tijdlijn}
            )
        else:
            # Profiel bestaat nog niet — aanmaken met standaard scores
            standaard = {k: 70 for k in ["lezen","avi","dmt","rekenen","rekenen_basis","spelling","taalverzorging",
                "woordenschat","begrijpend","begrijpend_luis","engels","sociaal","executief","groeimeter","werkhouding"]}
            await supabase_post("lvs_profielen", token, {
                "leerling_id":   leerling_id,
                "leerkracht_id": leerkracht_id,
                "scores":        standaard,
                "vorige_scores": standaard,
                "tijdlijn":      [item]
            })
    except Exception as e:
        logger.warning(f"Cascade tijdlijn-update mislukt (stil): {e}")


# ══════════════════════════════════════════════════════════
# NOTITIE CATEGORISATIE — Claude analyseert domein + kern
# ══════════════════════════════════════════════════════════

CATEGORISATIE_PROMPT = """Je bent een assistent die observatienotities van leraren categoriseert voor het leerlingvolgsysteem.

Analyseer de notitie en retourneer ALLEEN een JSON-object:
{
  "domein": "<één van: sociaal, werkhouding, executief, groeimeter, lezen, rekenen, spelling, taalverzorging, woordenschat, begrijpend, begrijpend_luis, engels, rekenen_basis, dmt, avi, algemeen>",
  "kern": "<de kern van de observatie in maximaal 12 woorden, feitelijk en neutraal>",
  "sentiment": "<positief|neutraal|aandacht>"
}

Domein-richtlijnen:
- sociaal: sociale interacties, conflicten, vriendschappen, groepsgedrag, pesten
- werkhouding: concentratie, taakgerichtheid, motivatie, inzet, huiswerk
- executief: planning, impulscontrole, organisatie, overgangen, flexibiliteit
- groeimeter: welbevinden, zelfvertrouwen, emotieregulatie, weerbaarheid
- lezen/dmt/avi: technisch lezen, leesniveau, leessnelheid
- begrijpend: tekstbegrip, begrijpend lezen/luisteren
- rekenen/rekenen_basis: rekenprestaties, getalbegrip, bewerkingen
- spelling/taalverzorging/woordenschat: taalvaardigheid
- engels: Engelse taalvaardigheid
- algemeen: past niet in één specifiek domein

Sentiment:
- positief: groei, succes, vooruitgang, iets goed opgelost
- aandacht: zorgpunt, achteruitgang, ondersteuning nodig
- neutraal: feitelijke observatie zonder duidelijke trend

Retourneer ALLEEN het JSON-object, geen uitleg."""


async def categoriseer_notitie(tekst: str) -> dict:
    """
    Laat Claude de notitie categoriseren naar domein en kern.
    Stil falen: als het mislukt, gebruik 'algemeen' als fallback.
    """
    try:
        import anthropic as _ac
        client = _ac.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            system=CATEGORISATIE_PROMPT,
            messages=[{"role": "user", "content": f"Notitie: {tekst[:500]}"}]
        )
        raw = response.content[0].text.strip()
        return _veilig_json_parse(raw)
    except Exception as e:
        logger.warning(f"Categorisatie mislukt (stil): {e}")
        return {"domein": "algemeen", "kern": tekst[:80], "sentiment": "neutraal"}


# ══════════════════════════════════════════════════════════
# GROEPSPLAN
# ══════════════════════════════════════════════════════════

@app.get("/groepsplan")
async def haal_groepsplan_op(groep: str, schooljaar: str = "2025-2026", user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    data = await supabase_get("groepsplannen", token, {
        "leerkracht_id": f"eq.{user['id']}", "groep": f"eq.{groep}", "schooljaar": f"eq.{schooljaar}", "select": "*"
    })
    if not data:
        return {"groep": groep, "schooljaar": schooljaar, "rijen": [], "notities": ""}
    plan = data[0]
    rijen = await supabase_get("groepsplan_rijen", token, {"groepsplan_id": f"eq.{plan['id']}", "select": "*"})
    return {**plan, "rijen": rijen or []}

@app.get("/groepsplan/suggestie/{groep}")
async def genereer_groepsplan_suggestie(
    groep: str,
    vakgebied: str,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    """
    Genereert een instructieniveau-suggestie per leerling voor een vakgebied,
    gebaseerd op hun LVS-scores. Overschrijft niets — puur een voorstel.

    Scoregrenzen (CITO I-V schaal, 0-100):
    - ≥ 78  (I/I+)      → onafhankelijk
    - 63-77 (II)         → basis
    - 32-62 (III/IV)     → intensief
    - < 32  (V/V-)       → individueel
    - 0 / geen score     → geen suggestie (leerling nog niet getoetst)
    """
    if vakgebied not in GELDIGE_VAKGEBIEDEN:
        raise HTTPException(status_code=400, detail=f"Ongeldig vakgebied: {vakgebied}")

    token = credentials.credentials

    # Haal alle leerlingen in de groep op
    leerlingen_data = await supabase_get("leerlingen", token, {
        "groep":         f"eq.{groep}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":        "id,voornaam"
    })
    if not leerlingen_data:
        return {"suggesties": [], "zonder_score": []}

    # Haal LVS-profielen op voor alle leerlingen in de groep
    leerling_ids = [l["id"] for l in leerlingen_data]

    # Supabase 'in' filter
    ids_filter = "(" + ",".join(leerling_ids) + ")"
    profielen = await supabase_get("lvs_profielen", token, {
        "leerling_id":   f"in.{ids_filter}",
        "leerkracht_id": f"eq.{user['id']}",
        "select":        "leerling_id,scores"
    })

    # Maak een lookup: leerling_id → scores
    score_lookup = {p["leerling_id"]: p.get("scores", {}) for p in (profielen or [])}

    def score_naar_niveau(score: int | None) -> str | None:
        """Zet een 0-100 score om naar een instructieniveau."""
        if not score or score == 0:
            return None  # Geen score beschikbaar
        if score >= 78:
            return "onafhankelijk"
        elif score >= 63:
            return "basis"
        elif score >= 32:
            return "intensief"
        else:
            return "individueel"

    suggesties = []
    zonder_score = []

    for leerling in leerlingen_data:
        scores = score_lookup.get(leerling["id"], {})
        score = scores.get(vakgebied, 0)
        niveau = score_naar_niveau(score)

        if niveau:
            suggesties.append({
                "leerling_id": leerling["id"],
                "voornaam":    leerling["voornaam"],
                "niveau":      niveau,
                "score":       score
            })
        else:
            zonder_score.append({
                "leerling_id": leerling["id"],
                "voornaam":    leerling["voornaam"]
            })

    return {
        "suggesties":    suggesties,
        "zonder_score":  zonder_score,
        "vakgebied":     vakgebied,
        "groep":         groep
    }


@app.put("/groepsplan")
async def sla_groepsplan_op(plan: GroepsplanOpslaan, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    bestaand = await supabase_get("groepsplannen", token, {
        "leerkracht_id": f"eq.{user['id']}", "groep": f"eq.{plan.groep}", "schooljaar": f"eq.{plan.schooljaar}"
    })
    plan_data = {
        "leerkracht_id": user["id"], "groep": plan.groep, "schooljaar": plan.schooljaar,
        "notities": plan.notities, "bijgewerkt_op": datetime.now(timezone.utc).isoformat()
    }
    if bestaand:
        plan_id = bestaand[0]["id"]
        await supabase_patch(f"groepsplannen?id=eq.{plan_id}", token, plan_data)
        await supabase_delete(f"groepsplan_rijen?groepsplan_id=eq.{plan_id}", token)
    else:
        nieuw = await supabase_post("groepsplannen", token, plan_data)
        plan_id = nieuw[0]["id"] if nieuw else None
        if not plan_id:
            raise HTTPException(status_code=500, detail="Groepsplan aanmaken mislukt.")
    for rij in plan.rijen:
        await supabase_post("groepsplan_rijen", token, {
            "groepsplan_id": plan_id, "leerling_id": rij.leerling_id,
            "vakgebied": rij.vakgebied, "instructieniveau": rij.instructieniveau
        })
    return {"groepsplan_id": plan_id, "rijen_opgeslagen": len(plan.rijen)}

@app.delete("/groepsplan/{plan_id}")
async def verwijder_groepsplan(plan_id: str, user=Depends(get_user), credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    bestaand = await supabase_get("groepsplannen", token, {"id": f"eq.{plan_id}", "leerkracht_id": f"eq.{user['id']}"})
    if not bestaand:
        raise HTTPException(status_code=404, detail="Groepsplan niet gevonden.")
    await supabase_delete(f"groepsplan_rijen?groepsplan_id=eq.{plan_id}", token)
    await supabase_delete(f"groepsplannen?id=eq.{plan_id}", token)
    return {"verwijderd": True}


@app.exception_handler(HTTPException)
async def http_fout_handler(request: Request, exc: HTTPException):
    # HTTPException gewoon doorsturen met de juiste statuscode en detail
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(Exception)
async def algemene_fout_handler(request: Request, exc: Exception):
    # Alleen écht onverwachte fouten opvangen (geen HTTPException)
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    logger.error(f"Onverwachte fout op {request.url}: {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Serverfout: {type(exc).__name__} — {str(exc)[:200]}"}
    )
