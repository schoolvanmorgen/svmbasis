// ══════════════════════════════════════════════════════════
// aanwezigheid.js — Aanwezigheidsregistratie
// Per dag en per klas aanwezig/afwezig registreren
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { huidigToken, aawLeerlingen, aawStatus, aawRegistraties,
         aawDatum, aawGeselecteerdeKlas, setAawDatum,
         setAawGeselecteerdeKlas, setAawLeerlingen } from './state.js';
import { notitieFilterOpKlas } from './notities.js';

export async function aawInit() {
  if (!huidigToken) return;
  document.getElementById('aaw-datum-tekst').textContent = aawDatumTekst(aawDatum);
  // Gebruik de centrale leerlingenlijst — één bron van waarheid
  if (leerlingen.length > 0) {
    aawLeerlingen = leerlingen;
  } else {
    try {
      await laadLeerlingen();
      aawLeerlingen = leerlingen;
    } catch(e) { aawLeerlingen = []; }
  }
  aawRenderKlassen();
  if (aawGeselecteerdeKlas) aawToonKlas(aawGeselecteerdeKlas);
}

export async function aawVandaag() {
  aawDatum = new Date();
  document.getElementById('aaw-datum-tekst').textContent = aawDatumTekst(aawDatum);
  await aawLaadDag(aawDatum);
  aawRender();
}

export async function aawVorigeDag() {
  aawDatum.setDate(aawDatum.getDate() - 1);
  document.getElementById('aaw-datum-tekst').textContent = aawDatumTekst(aawDatum);
  await aawLaadDag(aawDatum);
  aawRender();
}

export async function aawVolgendeDag() {
  aawDatum.setDate(aawDatum.getDate() + 1);
  document.getElementById('aaw-datum-tekst').textContent = aawDatumTekst(aawDatum);
  await aawLaadDag(aawDatum);
  aawRender();
}

export function aawDatumSleutel(datum) {
  return datum.toISOString().slice(0, MAX_NOTITIES_IN_CONTEXT);
}

export function aawSelecteerKlas(groep) {
  aawGeselecteerdeKlas = groep;
  // Sync naar snelle notitie: filter leerlingen op dezelfde klas
  if (groep && typeof notitieFilterOpKlas === 'function') {
    notitieFilterOpKlas(groep);
  }
  const leeg = document.getElementById('aaw-leeg');
  const lijst = document.getElementById('aaw-lijst');
  if (!groep) {
    if (leeg) leeg.style.display = 'block';
    if (lijst) lijst.innerHTML = '';
    aawTelBijwerken();
    return;
  }
  if (leeg) leeg.style.display = 'none';
  aawToonKlas(groep);

  // Sync notitie-tab: filter leerlingenlijst op dezelfde klas
  if (typeof notitieVulZijbalk === 'function') {
    notitieVulZijbalk(groep);
  }
}

export function aawRenderKlassen() {
  const el = document.getElementById('aaw-klassen-lijst');
  if (!el) return;
  const groepen = [...new Set(aawLeerlingen.map(function(l) { return l.groep; }).filter(Boolean))].sort(function(a,b) {
    return parseInt(a) - parseInt(b);
  });
  if (!groepen.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--tz);padding:10px 0">Nog geen leerlingen — voeg ze hieronder toe.</div>';
    aawToonLeerlingToevoegen();
    return;
  }
  el.style.display = 'flex'; el.style.gap = '6px'; el.style.flexWrap = 'wrap';
  el.innerHTML = groepen.map(function(g) {
    const actief = aawGeselecteerdeKlas === g;
    const aantal = aawLeerlingen.filter(function(l) { return l.groep === g; }).length;
    return '<button onclick="aawToonKlas(this.getAttribute(\'data-g\'))" data-g="' + g + '" style="padding:6px 16px;border:1.5px solid ' + (actief ? '#2a3a2a' : 'var(--rand)') + ';background:' + (actief ? '#2a3a2a' : '#fff') + ';color:' + (actief ? '#fff' : 'var(--tm)') + ';font-size:12px;font-weight:' + (actief ? '600' : '400') + ';cursor:pointer;font-family:inherit">Groep ' + g + ' <span style="opacity:.6;font-size:11px">(' + aantal + ')</span></button>';
  }).join('');
}

export async function aawToonKlas(groep) {
  const lijst = document.getElementById('aaw-lijst');
  const leeg  = document.getElementById('aaw-leeg');
  if (!lijst) return;

  const klasLeerlingen = aawLeerlingen
    .filter(function(l) { return String(l.groep) === String(groep); })
    .sort(function(a,b) { return (a.voornaam||'').localeCompare(b.voornaam||''); });

  if (!klasLeerlingen.length) {
    lijst.innerHTML = '';
    if (leeg) { leeg.style.display = 'block'; leeg.textContent = 'Geen leerlingen in groep ' + groep + '.'; }
    aawTelBijwerken();
    return;
  }
  if (leeg) leeg.style.display = 'none';

  let html = '';
  klasLeerlingen.forEach(function(l) {
    const status = (aawStatus[aawDatum] || {})[l.id] || 'aanwezig';
    const isAanwezig = status === 'aanwezig';
    const isAfwezig  = status === 'afwezig';
    const ach = [l.tussenvoegsel, l.achternaam].filter(Boolean).join(' ');

    const stijlAanwezig = isAanwezig
      ? 'border:1.5px solid #4a7a4a;background:#4a7a4a;color:#fff'
      : 'border:1.5px solid var(--rand);background:#fff;color:var(--tz)';
    const stijlAfwezig = isAfwezig
      ? 'border:1.5px solid #991b1b;background:#991b1b;color:#fff'
      : 'border:1.5px solid var(--rand);background:#fff;color:var(--tz)';

    html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;background:#fff;border-bottom:1px solid var(--warmrand)">';
    html += '<div style="font-size:14px;font-weight:500;color:var(--t)">' + l.voornaam;
    if (ach) html += ' <span style="font-weight:400;color:var(--tz)">' + ach + '</span>';
    html += '</div>';
    html += '<div style="display:flex;gap:8px">';
    html += '<button class="aaw-status-knop" data-id="' + l.id + '" data-status="aanwezig"'
          + ' style="padding:8px 22px;' + stijlAanwezig + ';font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;border-radius:7px">Aanwezig</button>';
    html += '<button class="aaw-status-knop" data-id="' + l.id + '" data-status="afwezig"'
          + ' style="padding:8px 22px;' + stijlAfwezig + ';font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;border-radius:7px">Afwezig</button>';
    html += '</div></div>';
  });

  lijst.innerHTML = html;

  lijst.querySelectorAll('.aaw-status-knop').forEach(function(knop) {
    knop.addEventListener('click', function() {
      aawZetStatus(knop.dataset.id, knop.dataset.status);
    });
  });

  aawTelBijwerken();
}

export function aawZetStatus(leerlingId, status) {
  if (!aawStatus[aawDatum]) aawStatus[aawDatum] = {};
  aawStatus[aawDatum][leerlingId] = status;
  // Herrender de klas
  if (aawGeselecteerdeKlas) aawToonKlas(aawGeselecteerdeKlas);
}

export function aawTelBijwerken() {
  if (!aawDatum || !aawStatus[aawDatum] || !aawGeselecteerdeKlas) {
    document.getElementById('aaw-tel-aanwezig').textContent = '—';
    document.getElementById('aaw-tel-afwezig').textContent  = '—';
    return;
  }
  const klasLeerlingen = aawLeerlingen.filter(function(l) { return String(l.groep) === String(aawGeselecteerdeKlas); });
  const dagStatus = aawStatus[aawDatum] || {};
  const afwezig  = klasLeerlingen.filter(function(l) { return dagStatus[l.id] === 'afwezig'; }).length;
  const aanwezig = klasLeerlingen.length - afwezig;
  document.getElementById('aaw-tel-aanwezig').textContent = aanwezig;
  document.getElementById('aaw-tel-afwezig').textContent  = afwezig;
}

export async function aawSlaOp() {
  if (!huidigToken || !aawGeselecteerdeKlas) return;
  const sleutel    = aawDatumSleutel(aawDatum);
  const dagData    = aawRegistraties[sleutel] || {};
  const klasLeerlingen = aawLeerlingen.filter(function(l) { return l.groep === aawGeselecteerdeKlas; });
  const balk = document.getElementById('aaw-export-balk');

  // ── Stap 1: sla alle registraties op ──────────────────────
  const resultaten = await Promise.allSettled(klasLeerlingen.map(function(l) {
    const d = dagData[l.id] || { status: 'onbekend', reden: '', opmerking: '' };
    return fetch(API_BASE + '/aanwezigheid', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + huidigToken, 'Content-Type': 'application/json' },
      body: JSON.stringify({ leerling_id: l.id, datum: sleutel, status: d.status, reden: d.reden || '', opmerking: d.opmerking || '' })
    }).then(function(res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res;
    });
  }));

  const mislukt = resultaten.filter(function(r) { return r.status === 'rejected'; });

  if (!balk) return;

  if (mislukt.length > 0) {
    const gelukt = resultaten.length - mislukt.length;
    balk.style.cssText = 'display:block;background:#fef2f2;border-color:#fecaca;color:#991b1b;padding:10px 24px;font-size:12px;flex-shrink:0';
    balk.innerHTML = '⚠ ' + mislukt.length + ' van de ' + resultaten.length + ' registraties niet opgeslagen. '
      + (gelukt > 0 ? gelukt + ' wel opgeslagen. ' : '')
      + 'Controleer je verbinding en probeer opnieuw.';
    setTimeout(function() { balk.style.display = 'none'; }, 6000);
    return; // Niet verder analyseren als opslaan mislukt
  }

  // ── Stap 2: patroondetectie op de achtergrond ─────────────
  // Toon direct bevestiging, analyseer daarna stil
  balk.style.cssText = 'display:block;background:var(--warm);border-color:var(--warmrand);color:var(--t);padding:10px 24px;font-size:12px;flex-shrink:0';
  balk.innerHTML = '✓ Opgeslagen voor ' + aawDatumTekst(aawDatum) + ' — aanwezigheidspatronen worden geanalyseerd…';

  try {
    const analyseRes = await apiCall('/aanwezigheid/analyseer_groep/' + aawGeselecteerdeKlas, { method: 'POST' });

    if (analyseRes.ok) {
      const analyseData = await analyseRes.json();
      const signalen = analyseData.signalen || [];

      if (signalen.length === 0) {
        // Geen patronen — gewone bevestiging
        balk.innerHTML = '✓ Opgeslagen voor ' + aawDatumTekst(aawDatum);
        setTimeout(function() { balk.style.display = 'none'; }, 3000);
      } else {
        // Patronen gevonden — toon melding met namen
        aawToonSignalen(signalen, balk);
      }
    } else {
      // Analyse mislukt — toon gewoon de opslabevestiging
      balk.innerHTML = '✓ Opgeslagen voor ' + aawDatumTekst(aawDatum);
      setTimeout(function() { balk.style.display = 'none'; }, 3000);
    }
  } catch(e) {
    // Stil falen — analyse is optioneel
    balk.innerHTML = '✓ Opgeslagen voor ' + aawDatumTekst(aawDatum);
    setTimeout(function() { balk.style.display = 'none'; }, 3000);
  }
}

export function aawAlleAanwezig() {
  const sleutel = aawDatumSleutel(aawDatum);
  if (!aawRegistraties[sleutel]) aawRegistraties[sleutel] = {};
  aawLeerlingen.filter(function(l) { return l.groep === aawGeselecteerdeKlas; }).forEach(function(l) {
    aawRegistraties[sleutel][l.id] = { status: 'aanwezig', reden: '', opmerking: '' };
  });
  aawRender();
}

export async function aawLaadDag(datum) {
  if (!huidigToken) return;
  const sleutel = aawDatumSleutel(datum);
  if (aawRegistraties[sleutel]) return;
  try {
    const res = await apiCall('/aanwezigheid?datum=' + sleutel);
    const data = await res.json();
    aawRegistraties[sleutel] = {};
    (Array.isArray(data) ? data : []).forEach(function(r) {
      aawRegistraties[sleutel][r.leerling_id] = { status: r.status, reden: r.reden || '', opmerking: r.opmerking || '' };
    });
  } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }
}

