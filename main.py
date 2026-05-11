from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime, timezone
import httpx
import os
import json
import logging

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("school-van-morgen")

# ── App setup ─────────────────────────────────────────────
app = FastAPI(title="School van morgen", version="1.0.0")

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "DELETE", "PUT"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── Config ────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-sonnet-4-20250514"

SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

security = HTTPBearer(auto_error=False)

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
    logger.info(f"Opstartcontrole geslaagd. CORS toegestaan voor: {ALLOWED_ORIGINS}")

# ══════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Je bent een rapportgenerator voor Nederlandse basisscholen (groep 1 t/m 8).
Je schrijft rapportteksten die direct voor ouders en kinderen begrijpelijk zijn, geen vakjargon, geen standaardzinnen.

KINDGERICHT SCHRIJVEN - DE KERNREGEL
Schrijf alsof je de ouder aankijkt en over hun kind vertelt. Gebruik altijd de voornaam van het kind.
Nooit: "De leerling laat leeftijdsadequate ontwikkeling zien."
Altijd: "Sem groeit dit kwartaal mooi - we zien dat hij steeds meer..."

TOON PER GROEP
Groep 1-3 (onderbouw): warm en verhalend, geen cijfers of toetsniveaus, focus op spelen en ontdekken.
Groep 4-6 (middenbouw): toegankelijk en concreet, CITO-niveaus uitleggen in gewone taal.
Groep 7-8 (bovenbouw): respecteer dat het kind zelf ook meeleest, eerlijk maar positief over uitdagingen.

RAPPORTCOMMENTAAR - VERPLICHTE STRUCTUUR
1. Persoonlijke opening over dit kind
2. Wat goed gaat, concreet en herkenbaar
3. Waar we aan werken, positief geframed
4. Als van toepassing: hoe de ondersteuning eruitziet in gewone taal
5. Persoonlijke afsluiting, NOOIT "Wij kijken vol vertrouwen naar de toekomst"

ONDERSTEUNING - VOOR OUDERS BEGRIJPELIJK
Leg uit wat de interventie inhoudt in gewone taal. Beschrijf concreet wat het kind ervaart. Geef een concrete tip voor thuis.

ABSOLUTE VERBODEN
Nooit: "leeftijdsadequaat", "cognitief", "intrinsiek", "auditief", "fonemisch", "metacognitief"
Nooit standaard afsluitingen. Nooit informatie verzinnen die niet in de notities staat.
Nooit een sectie invullen als de notities er niets over zeggen, zet op null.

Output: alleen een geldig JSON-object zonder markdown, backticks of uitleg."""

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

class LeerlingAanmaken(BaseModel):
    voornaam: str
    groep: Optional[str] = ""
    ondersteuningsbehoeftes: Optional[List[str]] = []
    notities: Optional[str] = ""
    leerlingnummer: Optional[str] = None
    achternaam: Optional[str] = None
    tussenvoegsel: Optional[str] = None

class LeerlingBijwerken(BaseModel):
    voornaam: Optional[str] = None
    groep: Optional[str] = None
    ondersteuningsbehoeftes: Optional[List[str]] = None
    notities: Optional[str] = None
    leerlingnummer: Optional[str] = None
    achternaam: Optional[str] = None
    tussenvoegsel: Optional[str] = None

class RapportOpslaan(BaseModel):
    leerling_id: str
    rapport_data: dict

class OppVerzoek(BaseModel):
    notities: str
    naam: str = ""
    groep: str = ""
    ondersteuningsbehoefte: str = ""

class HandelingsplanVerzoek(BaseModel):
    notities: str
    naam: str = ""
    groep: str = ""
    ondersteuningsbehoefte: str = ""

class OudergesprekVerzoek(BaseModel):
    notities: str
    naam: str = ""
    groep: str = ""
    datum: str = ""

class LvsProfielOpslaan(BaseModel):
    leerling_id: str
    scores: dict
    vorige_scores: dict
    tijdlijn: list

class LvsTijdlijnItem(BaseModel):
    leerling_id: str
    item: dict  # {type, datum, tekst}

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
# SUPABASE HELPERS
# ══════════════════════════════════════════════════════════

def _supabase_headers(token: str) -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

async def supabase_get(path: str, token: str, params: dict = {}):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(
                f"{SUPABASE_URL}/rest/v1/{path}",
                headers=_supabase_headers(token),
                params=params
            )
            if res.status_code == 401:
                raise HTTPException(status_code=401, detail="Sessie verlopen.")
            if res.status_code != 200:
                raise HTTPException(status_code=res.status_code, detail=f"Databasefout: {res.text[:300]}")
            return res.json()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout: {str(e)}")

async def supabase_post(path: str, token: str, data: dict):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                f"{SUPABASE_URL}/rest/v1/{path}",
                headers={**_supabase_headers(token), "Prefer": "return=representation"},
                json=data
            )
            if res.status_code == 401:
                raise HTTPException(status_code=401, detail="Sessie verlopen.")
            if res.status_code not in (200, 201):
                raise HTTPException(status_code=res.status_code, detail=f"Opslaan mislukt: {res.text[:300]}")
            return res.json()
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout: {str(e)}")

async def supabase_patch(path: str, token: str, data: dict):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.patch(
                f"{SUPABASE_URL}/rest/v1/{path}",
                headers={**_supabase_headers(token), "Prefer": "return=representation"},
                json=data
            )
            if res.status_code == 401:
                raise HTTPException(status_code=401, detail="Sessie verlopen.")
            if res.status_code not in (200, 204):
                raise HTTPException(status_code=res.status_code, detail=f"Bijwerken mislukt: {res.text[:300]}")
            return res.json() if res.content else []
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout: {str(e)}")

async def supabase_delete(path: str, token: str):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.delete(
                f"{SUPABASE_URL}/rest/v1/{path}",
                headers=_supabase_headers(token)
            )
            if res.status_code == 401:
                raise HTTPException(status_code=401, detail="Sessie verlopen.")
            if res.status_code not in (200, 204):
                raise HTTPException(status_code=res.status_code, detail=f"Verwijderen mislukt: {res.text[:300]}")
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=503, detail="Database niet bereikbaar (timeout).")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Verbindingsfout: {str(e)}")

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

async def roep_claude_aan(system: str, user: str, max_tokens: int = 1500) -> str:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="Anthropic API-sleutel niet ingesteld op de server.")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            res = await client.post(
                ANTHROPIC_URL,
                headers=_anthropic_headers(),
                json=_claude_body(system, user, max_tokens),
            )
            if res.status_code == 401:
                raise HTTPException(status_code=500, detail="Ongeldige Anthropic API-sleutel.")
            if res.status_code == 429:
                raise HTTPException(status_code=429, detail="Te veel verzoeken. Probeer het over een moment opnieuw.")
            if res.status_code == 529:
                raise HTTPException(status_code=503, detail="Anthropic API tijdelijk overbelast. Probeer opnieuw.")
            if res.status_code != 200:
                raise HTTPException(status_code=502, detail=f"API-fout ({res.status_code}): {res.text[:200]}")
            data = res.json()
            return data["content"][0]["text"]
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Claude reageert niet (timeout na 90s). Probeer opnieuw.")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Kan Claude niet bereiken: {str(e)}")

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

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "api_key_ingesteld": bool(ANTHROPIC_API_KEY),
        "supabase_ingesteld": bool(SUPABASE_ANON_KEY),
    }

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
                            except (json.JSONDecodeError, KeyError):
                                pass
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
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    return await supabase_get(
        "leerlingen", token,
        {"leerkracht_id": f"eq.{user['id']}", "order": "bijgewerkt_op.desc", "select": "*"}
    )

@app.post("/leerlingen")
async def maak_leerling_aan(
    leerling: LeerlingAanmaken,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    if not leerling.voornaam or not leerling.voornaam.strip():
        raise HTTPException(status_code=400, detail="Voornaam is verplicht.")
    token = credentials.credentials
    record = {
        "leerkracht_id": user["id"],
        "voornaam": leerling.voornaam.strip(),
        "groep": leerling.groep,
        "ondersteuningsbehoeftes": leerling.ondersteuningsbehoeftes,
        "notities": leerling.notities,
    }
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
    return data[0] if isinstance(data, list) and data else data

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
async def opp(verzoek: OppVerzoek, user=Depends(get_user)):
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
        return {"data": parsed, "tekst": tekst}
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"OPP JSON parse mislukt: {tekst[:100]}")
        return {"data": None, "tekst": tekst}

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

@app.post("/handelingsplan")
async def handelingsplan(verzoek: HandelingsplanVerzoek, user=Depends(get_user)):
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
        return {"data": parsed, "tekst": tekst}
    except (json.JSONDecodeError, ValueError):
        logger.warning(f"Handelingsplan JSON parse mislukt: {tekst[:100]}")
        return {"data": None, "tekst": tekst}

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
        "scores": {"lezen":70,"rekenen":70,"spelling":70,"begrijpend":70,"sociaal":70,"werkhouding":70},
        "vorige_scores": {"lezen":70,"rekenen":70,"spelling":70,"begrijpend":70,"sociaal":70,"werkhouding":70},
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
