// ══════════════════════════════════════════════════════════
// auth.js — Authenticatie en sessie management
// Supabase auth, token refresh, login/logout flow
// ══════════════════════════════════════════════════════════
import { SUPABASE_URL, SUPABASE_ANON, TOKEN_REFRESH_MARGE_SECONDEN } from './config.js';
import { resetState, setHuidigToken, setHuidigGebruiker, setSchoolContext,
         setGeselecteerdeRol } from './state.js';

// Supabase client — singleton
export const sb = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);

let _tokenRefreshTimer = null;
let _tokenExpiresAt    = null;

export function _scheduleTokenRefresh(expiresAt) {
  /**
   * Plan een proactieve token refresh 5 minuten voor het verlopen.
   * Supabase tokens verlopen standaard na 3600 seconden (1 uur).
   *
   * @param {number} expiresAt - Unix timestamp waarop het token verloopt
   */
  if (_tokenRefreshTimer) {
    clearTimeout(_tokenRefreshTimer);
    _tokenRefreshTimer = null;
  }

  _tokenExpiresAt = expiresAt;
  const nu   = Math.floor(Date.now() / 1000);
  const over = expiresAt - nu;  // seconden tot verlopen

  if (over <= 0) {
    // Al verlopen — direct uitloggen
    console.warn('[SvM] Token al verlopen, uitloggen');
    uitloggen();
    return;
  }

  // Vernieuw 5 minuten (300s) voor het verlopen, minimaal over 30s
  const refreshOver = Math.max(30, over - 300) * 1000;
  console.info(`[SvM] Token refresh gepland over ${Math.round(refreshOver/1000)}s`);

  _tokenRefreshTimer = setTimeout(async () => {
    try {
      const { data, error } = await sb.auth.refreshSession();
      if (error || !data.session) {
        console.warn('[SvM] Token refresh mislukt, uitloggen:', error);
        uitloggen();
        return;
      }
      huidigToken = data.session.access_token;
      console.info('[SvM] Token vernieuwd');
      // Plan volgende refresh
      _scheduleTokenRefresh(data.session.expires_at);
    } catch(e) {
      console.error('[SvM] Token refresh error:', e);
      // Niet uitloggen bij netwerk-error — probeer opnieuw
      _scheduleTokenRefresh(_tokenExpiresAt);
    }
  }, refreshOver);
}

export async function inloggen() {
  const email     = document.getElementById('login-email').value.trim();
  const wachtwoord = document.getElementById('login-ww').value;
  const fout      = document.getElementById('login-fout');
  fout.textContent = '';

  const { data, error } = await sb.auth.signInWithPassword({ email, password: wachtwoord });
  if (error) { fout.textContent = 'Inloggen mislukt: ' + error.message; return; }

  // Sla de geselecteerde rol op in user_metadata
  if (_geselecteerdeRol !== 'leerkracht' || !data.user?.user_metadata?.rol) {
    try {
      await sb.auth.updateUser({
        data: { rol: _geselecteerdeRol }
      });
    } catch(e) {
      console.warn('Rol opslaan mislukt (stil):', e);
    }
  }
}

export async function registreren() {
  const email = document.getElementById('login-email').value.trim();
  const wachtwoord = document.getElementById('login-ww').value;
  const fout = document.getElementById('login-fout');
  fout.textContent = '';
  fout.style.color = '#000';
  if (!email) { fout.textContent = 'Vul een e-mailadres in.'; return; }
  if (wachtwoord.length < 8) { fout.textContent = 'Wachtwoord moet minimaal 8 tekens zijn.'; return; }
  const { error } = await sb.auth.signUp({ email, password: wachtwoord, options: { data: { rol: _geselecteerdeRol || 'leerkracht' } } });
  if (error) {
    // Account bestaat al — probeer dan direct in te loggen
    if (error.message.toLowerCase().includes('already') || error.message.toLowerCase().includes('registered')) {
      fout.style.color = 'var(--tz)';
      fout.textContent = 'Account bestaat al, je wordt ingelogd…';
      const { error: loginError } = await sb.auth.signInWithPassword({ email, password: wachtwoord });
      if (loginError) {
        fout.style.color = '#000';
        fout.textContent = 'Inloggen mislukt: ' + loginError.message;
      }
    } else {
      fout.textContent = 'Registreren mislukt: ' + error.message;
    }
  } else {
    fout.style.color = '#333';
    fout.textContent = 'Account aangemaakt! Check je e-mail om te bevestigen.';
  }
}

export async function uitloggen() {
  const _leerlingId = window._huidigeLeerlingId; // lokaal gecaptured — voorkomt race condition
  await sb.auth.signOut();
  // Reset alle state — voorkom data-lekkage tussen sessies
  huidigToken = null;
  huidigGebruiker = null;
  huidigeLeerling = null;
  leerlingen = [];
  lvsLeerlingen = [];
  lvsHuidig = null;
  schoolContext = { school_id: null, rol: 'leerkracht', is_ib: false, is_directeur: false, heeft_school: false, naam: null };
  aawLeerlingen = [];
  aawStatus = {};
  aawRegistraties = {};
  _leerlingId = null;
  window._lvsProfielCache = {};
  window._ibLeerlingScores = {};
  window._notitieGroepFilter = null;
  _geselecteerdeRol = 'leerkracht';
}

export function toonLogin() {
  document.getElementById('login-scherm').style.display = 'flex';
  document.getElementById('app-scherm').style.display = 'none';
}

export function toonApp() {
  document.getElementById('login-scherm').style.display = 'none';
  document.getElementById('app-scherm').style.display = 'flex';
  document.getElementById('globale-leerling-selector').style.display = 'flex';
  // Toon IB/Inspectie direct op basis van geselecteerde rol bij inloggen
  // (vóór schoolContext laden — zodat tabs meteen zichtbaar zijn)
  if (_geselecteerdeRol === 'ib' || _geselecteerdeRol === 'directeur') {
    const ibKnop = document.getElementById('tab-ib-knop');
    if (ibKnop) ibKnop.style.display = '';
    const inspKnop = document.getElementById('tab-inspectie-knop');
    if (inspKnop) inspKnop.style.display = '';
  }

  laadSchoolContext().then(function() {
    laadLeerlingen();
    // Sync met schoolContext (kan anders zijn als rol in metadata wijkt)
    if (schoolContext && (schoolContext.is_ib || schoolContext.is_directeur)) {
      const ibKnop = document.getElementById('tab-ib-knop');
      if (ibKnop) ibKnop.style.display = '';
      const inspKnop = document.getElementById('tab-inspectie-knop');
      if (inspKnop) inspKnop.style.display = '';
    }
  });
  setTimeout(() => {
    let herstelTab = 'aanwezigheid';
    try {
      const opgeslagen = localStorage.getItem('svm_actieve_tab');
      if (opgeslagen && ['klas','aanwezigheid','notities','lvs','rapport','ib','inspectie'].includes(opgeslagen)) {
        herstelTab = opgeslagen;
      }
    } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }
    const tabKnop = document.querySelector('.tab[onclick*="' + herstelTab + '"]');
    toonTab(herstelTab, tabKnop);
  }, 50);
  // Vertraagd IB/Inspectie tabs tonen (na schoolContext laden)
  setTimeout(function() {
    if (schoolContext && (schoolContext.is_ib || schoolContext.is_directeur)) {
      const ibKnop = document.getElementById('tab-ib-knop');
      if (ibKnop) ibKnop.style.display = '';
      const inspKnop = document.getElementById('tab-inspectie-knop');
      if (inspKnop) inspKnop.style.display = '';
    }
  }, 2000);
}

export async function laadSchoolContext() {
  if (!huidigToken) return;
  try {
    const res = await apiCall('/school/profiel');
    if (!res.ok) return;
    const data = await res.json();
    schoolContext = {
      school_id:    data.school_id,
      rol:          data.rol || 'leerkracht',
      is_ib:        data.rol === 'ib' || data.rol === 'directeur',
      is_directeur: data.rol === 'directeur',
      heeft_school: data.aangemeld,
      naam:         data.naam
    };
    toonRolBadge();
    // Toon IB-tab als de gebruiker IB-er of directeur is
    if (schoolContext.is_ib || schoolContext.is_directeur) {
      const ibKnop = document.getElementById('tab-ib-knop');
      if (ibKnop) ibKnop.style.display = '';
    }
  } catch(e) { console.error('School context laden mislukt:', e); }
}

