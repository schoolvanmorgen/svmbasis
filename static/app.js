// ══════════════════════════════════════════════════════════
// app.js — Entry point voor School van Morgen
// Importeert alle modules en initialiseert de applicatie.
// ══════════════════════════════════════════════════════════

import { sb, inloggen, registreren, uitloggen, toonLogin, toonApp,
         laadSchoolContext } from './modules/auth.js';
import { apiCall, esc } from './modules/api.js';
import { resetState, huidigToken, setHuidigToken, leerlingen,
         schoolContext } from './modules/state.js';
import { toonTab, stelLeerlingIn, glsZoek, glsSluit, glsUpdateBadge,
         toggleMic, openImportModal, sluitLeerlingModal,
         leerlingTab } from './modules/ui.js';
import { laadLeerlingen, toonLeerlingenLijst,
         verwijderLeerling, vulRapportKlasSelect } from './modules/leerlingen.js';
import { klasLeesBestand, klasVerwerk, klasImporteer } from './modules/klas.js';
import { aawInit, aawVandaag, aawVorigeDag, aawVolgendeDag,
         aawSelecteerKlas, aawRenderKlassen, aawToonKlas,
         aawZetStatus, aawSlaOp, aawAlleAanwezig } from './modules/aanwezigheid.js';
import { notitieRender, notitieVulZijbalk, notitieSelecteerLeerling,
         notitieFilterLeerlingen, notitieFilterOpKlas,
         notitieOpslaan, notitieVerwijder } from './modules/notities.js';
import { lvsInit, lvsFilter, lvsToonTab, lvsRenderLijst,
         lvsInitVanuitLeerlingen, lvsSlaNotitieOp,
         scoreNaarLabel, obsUpdateProfiel, LVS_DOMEINEN } from './modules/lvs.js';
import { genereer, rapportLaadKlas, rapportSelecteerLeerling,
         startBatchRapporten, roepAPIaanStream,
         roepAPIaan } from './modules/rapport.js';
import { genereerOpp, oppLaadLijst, oppToonDocument,
         documentDownloadPdf } from './modules/opp.js';
import { genereerHandelingsplan, hpLaadLijst,
         hpToonDocument } from './modules/handelingsplan.js';
import { ibLaad, ibToonGroepen,
         ibToonGroepDetail } from './modules/ib.js';

// ── Maak alles globaal beschikbaar voor inline HTML event handlers ──
// HTML onclick="..." kan geen ES module imports gebruiken.
// Dit is de brug tussen het module systeem en de HTML.
Object.assign(window, {
  // Auth
  inloggen, registreren, uitloggen, toonLogin, toonApp, laadSchoolContext,
  // UI
  toonTab, stelLeerlingIn, glsZoek, glsSluit, glsUpdateBadge,
  toggleMic, openImportModal, sluitLeerlingModal, leerlingTab,
  // Leerlingen
  laadLeerlingen, toonLeerlingenLijst, verwijderLeerling, vulRapportKlasSelect,
  // Klas
  klasLeesBestand, klasVerwerk, klasImporteer,
  // Aanwezigheid
  aawInit, aawVandaag, aawVorigeDag, aawVolgendeDag, aawSelecteerKlas,
  aawRenderKlassen, aawToonKlas, aawZetStatus, aawSlaOp, aawAlleAanwezig,
  // Notities
  notitieRender, notitieVulZijbalk, notitieSelecteerLeerling,
  notitieFilterLeerlingen, notitieFilterOpKlas, notitieOpslaan, notitieVerwijder,
  // LVS
  lvsInit, lvsFilter, lvsToonTab, lvsRenderLijst, lvsInitVanuitLeerlingen,
  lvsSlaNotitieOp, scoreNaarLabel, obsUpdateProfiel, LVS_DOMEINEN,
  // Rapport
  genereer, rapportLaadKlas, rapportSelecteerLeerling, startBatchRapporten,
  roepAPIaanStream, roepAPIaan,
  // OPP
  genereerOpp, oppLaadLijst, oppToonDocument, documentDownloadPdf,
  // Handelingsplan
  genereerHandelingsplan, hpLaadLijst, hpToonDocument,
  // IB
  ibLaad, ibToonGroepen, ibToonGroepDetail,
  // Gedeeld
  sb, apiCall, esc,
});

// ── Supabase auth listener — start de applicatie ────────────────
sb.auth.onAuthStateChange(async (event, session) => {
  if (session) {
    setHuidigToken(session.access_token);
    if (session.expires_at) {
      const { _scheduleTokenRefresh } = await import('./modules/auth.js');
      _scheduleTokenRefresh(session.expires_at);
    }
    await toonApp();
  } else {
    resetState();
    toonLogin();
  }
});
