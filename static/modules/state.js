// ══════════════════════════════════════════════════════════
// state.js — Centrale applicatiestatus
// Alle modules importeren state van hier.
// Nooit direct window.* gebruiken voor app-state.
// ══════════════════════════════════════════════════════════

export let huidigGebruiker  = null;
export let huidigToken      = null;
export let leerlingen       = [];
export let schoolContext     = {
  school_id: null, rol: 'leerkracht',
  is_ib: false, is_directeur: false,
  heeft_school: false, naam: null
};
export let lvsLeerlingen    = [];
export let lvsHuidig        = null;
export let huidigeLeerling  = null;
export let _geselecteerdeRol = 'leerkracht';

// Aanwezigheid state
export let aawLeerlingen   = [];
export let aawStatus       = {};
export let aawRegistraties = {};
export let aawDatum        = null;
export let aawGeselecteerdeKlas = null;

// Setters — gebruik deze zodat modules state kunnen updaten
export function setHuidigToken(token)         { huidigToken       = token; }
export function setHuidigGebruiker(user)      { huidigGebruiker   = user;  }
export function setLeerlingen(lijst)          { leerlingen        = lijst; }
export function setSchoolContext(ctx)         { schoolContext      = ctx;   }
export function setLvsHuidig(leerling)        { lvsHuidig         = leerling; }
export function setHuidigeLeerling(leerling)  { huidigeLeerling   = leerling; }
export function setGeselecteerdeRol(rol)      { _geselecteerdeRol = rol;   }
export function setAawLeerlingen(lijst)       { aawLeerlingen     = lijst; }
export function setAawDatum(datum)            { aawDatum          = datum; }
export function setAawGeselecteerdeKlas(klas) { aawGeselecteerdeKlas = klas; }

export function resetState() {
  huidigToken        = null;
  huidigGebruiker    = null;
  huidigeLeerling    = null;
  leerlingen         = [];
  lvsLeerlingen      = [];
  lvsHuidig          = null;
  schoolContext      = { school_id: null, rol: 'leerkracht', is_ib: false, is_directeur: false, heeft_school: false, naam: null };
  aawLeerlingen      = [];
  aawStatus          = {};
  aawRegistraties    = {};
  _geselecteerdeRol  = 'leerkracht';
}
