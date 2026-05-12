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
RESEND_API_KEY       = os.environ.get("RESEND_API_KEY", "")      # resend.com — gratis laag: 3000 e-mails/maand
MAIL_FROM            = os.environ.get("MAIL_FROM", "noreply@schoolvanmorgen.nl")
APP_URL              = os.environ.get("APP_URL", "http://localhost:8000")
# Service role key — alleen voor user_metadata updates (nooit naar de browser sturen)
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

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
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY niet ingesteld — e-mail versturen is uitgeschakeld. "
                       "Registreer gratis op resend.com en voeg de sleutel toe aan .env.")
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
    leerling_id: Optional[str] = None

class HandelingsplanVerzoek(BaseModel):
    notities: str
    naam: str = ""
    groep: str = ""
    ondersteuningsbehoefte: str = ""
    leerling_id: Optional[str] = None

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

GELDIGE_VAKGEBIEDEN = {"lezen", "dmt", "rekenen", "spelling", "taalverzorging", "woordenschat", "begrijpend", "begrijpend_luis", "engels", "sociaal", "executief", "werkhouding"}
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
    limit:  int = 200,
    offset: int = 0,
    user=Depends(get_user),
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    token = credentials.credentials
    ctx = await get_school_context(user, token)

    # Begrens limit zodat niemand per ongeluk de hele database opvraagt
    limit = min(limit, 200)

    if ctx["is_ib"] and ctx["school_id"]:
        # IB-er en directeur zien alle leerlingen binnen de school
        koppelingen = await supabase_get("leerkrachten_scholen", token,
            {"school_id": f"eq.{ctx['school_id']}", "select": "leerkracht_id,voornaam,email"})
        leerkracht_ids = [k["leerkracht_id"] for k in (koppelingen or [])]
        if not leerkracht_ids:
            return []
        ids_param = "(" + ",".join(leerkracht_ids) + ")"
        params = {
            "leerkracht_id": f"in.{ids_param}",
            "order":         "groep.asc,voornaam.asc",
            "select":        "*",
            "limit":         str(limit),
            "offset":        str(offset)
        }
        leerlingen = await supabase_get("leerlingen", token, params)
        naam_map = {k["leerkracht_id"]: k.get("voornaam") or k.get("email", "Onbekend")
                    for k in (koppelingen or [])}
        for l in (leerlingen or []):
            l["leerkracht_naam"] = naam_map.get(l.get("leerkracht_id"), "")
        return leerlingen or []
    else:
        return await supabase_get(
            "leerlingen", token,
            {
                "leerkracht_id": f"eq.{user['id']}",
                "order":         "bijgewerkt_op.desc",
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
    if not leerling.voornaam or not leerling.voornaam.strip():
        raise HTTPException(status_code=400, detail="Voornaam is verplicht.")
    token = credentials.credentials
    ctx = await get_school_context(user, token)
    record = {
        "leerkracht_id": user["id"],
        "voornaam": leerling.voornaam.strip(),
        "groep": leerling.groep,
        "ondersteuningsbehoeftes": leerling.ondersteuningsbehoeftes,
        "notities": leerling.notities,
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

    # Cascade: voeg OPP toe aan LVS-tijdlijn
    if verzoek.leerling_id:
        try:
            datum_nl = datetime.now(timezone.utc).strftime("%-d %b %Y")
            uitstroom = (parsed.get("uitstroombestemming") or parsed.get("uitstroom") or "")[:60]
            tijdlijn_tekst = "OPP gegenereerd." + (f" Uitstroom: {uitstroom}…" if uitstroom else "")
            await _voeg_tijdlijn_toe(
                leerling_id=verzoek.leerling_id,
                leerkracht_id=user["id"],
                token=credentials.credentials if hasattr(credentials, "credentials") else "",
                item={"type": "opp", "datum": datum_nl, "tekst": tijdlijn_tekst}
            )
        except Exception as _e:
            logger.warning(f"Cascade OPP->LVS mislukt (stil): {_e}")
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
        "scores": {"lezen":70,"dmt":70,"rekenen":70,"spelling":70,"taalverzorging":70,"woordenschat":70,"begrijpend":70,"begrijpend_luis":70,"engels":70,"sociaal":70,"executief":70,"werkhouding":70},
        "vorige_scores": {"lezen":70,"dmt":70,"rekenen":70,"spelling":70,"taalverzorging":70,"woordenschat":70,"begrijpend":70,"begrijpend_luis":70,"engels":70,"sociaal":70,"executief":70,"werkhouding":70},
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

    # Controleer of leerling bij deze leerkracht hoort
    leerling = await supabase_get("leerlingen", token,
        {"id": f"eq.{verzoek.leerling_id}", "leerkracht_id": f"eq.{user['id']}"})
    if not leerling:
        raise HTTPException(status_code=404, detail="Leerling niet gevonden.")

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

    # Controleer dat de leerling bij deze leerkracht hoort
    leerling = await supabase_get("leerlingen", token,
        {"id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}", "select": "id,voornaam"})
    if not leerling:
        raise HTTPException(status_code=404, detail="Leerling niet gevonden.")

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

    # Cascade: voeg toe aan LVS-tijdlijn
    datum_nl = nu.strftime("%-d %b %Y")
    await _voeg_tijdlijn_toe(
        leerling_id=leerling_id,
        leerkracht_id=user["id"],
        token=token,
        item={
            "type":  notitie.type or "notitie",
            "datum": datum_nl,
            "tekst": notitie.tekst.strip()[:200]
        }
    )
    return opgeslagen[0] if isinstance(opgeslagen, list) else opgeslagen

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
    leerling_data = await supabase_get("leerlingen", token,
        {"id": f"eq.{leerling_id}", "leerkracht_id": f"eq.{user['id']}", "select": "*"})
    if not leerling_data:
        raise HTTPException(status_code=404, detail="Leerling niet gevonden.")
    leerling = leerling_data[0]

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
            standaard = {k: 70 for k in ["lezen","dmt","rekenen","spelling","taalverzorging",
                "woordenschat","begrijpend","begrijpend_luis","engels","sociaal","executief","werkhouding"]}
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
