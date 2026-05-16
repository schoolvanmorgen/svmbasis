// ══════════════════════════════════════════════════════════
// ib.js — IB-overzicht
// Schoolbrede groepsoverzicht voor IB-ers en directeuren
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { huidigToken, leerlingen } from './state.js';

export async function ibLaad() { ibToonGroepen(); }

export async function ibToonGroepen() {
  const inhoud = document.getElementById('ib-inhoud');
  const titel  = document.getElementById('ib-header-titel');
  const sub    = document.getElementById('ib-header-sub');
  const terug  = document.getElementById('ib-terug-knop');
  if (!inhoud) return;

  if (titel) titel.textContent = 'Groepsoverzicht';
  if (sub)   sub.textContent   = 'Klik op een groep voor de leerlingdetails.';
  if (terug) terug.style.display = 'none';
  inhoud.innerHTML = '<div style="color:var(--n-400);font-size:13px">Groepen ophalen…</div>';

  // Groepeer leerlingen
  const groepen = {};
  (leerlingen || []).forEach(function(l) {
    const g = String(l.groep || '?');
    if (!groepen[g]) groepen[g] = [];
    groepen[g].push(l);
  });

  const groepKeys = Object.keys(groepen).sort(function(a,b){ return parseInt(a)-parseInt(b); });

  if (!groepKeys.length) {
    inhoud.innerHTML = '<div style="color:var(--n-400);font-size:13px">Geen leerlingendata. Importeer eerst een klas via "+ Klas toevoegen".</div>';
    return;
  }

  // Haal LVS-scores op per groep (parallel, batches van 6)
  const vakken = ['lezen','rekenen','spelling','begrijpend','sociaal'];
  const vakLabels = { lezen:'Lezen', rekenen:'Rekenen', spelling:'Spelling', begrijpend:'Begrijpend lezen', sociaal:'Sociaal-em.' };

  const groepScores = {};

  await Promise.all(groepKeys.map(async function(g) {
    const ids = groepen[g].map(function(l){ return l.id; }).slice(0, 30);
    const acc = {};
    vakken.forEach(function(v){ acc[v] = []; });

    const batches = [];
    for (var i = 0; i < ids.length; i += 6) batches.push(ids.slice(i, i+6));

    for (var b = 0; b < batches.length; b++) {
      await Promise.all(batches[b].map(async function(id) {
        try {
          const res = await apiCall('/lvs/' + id + '/profiel');
          if (!res.ok) return;
          const data = await res.json();
          const s = data.scores || {};
          vakken.forEach(function(v) {
            const val = parseFloat(s[v]);
            if (!isNaN(val) && val > 0) acc[v].push(val);
          });
        } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }
      }));
    }

    const gem = {};
    vakken.forEach(function(v) {
      gem[v] = acc[v].length ? Math.round(acc[v].reduce(function(a,b){return a+b;},0)/acc[v].length) : null;
    });
    groepScores[g] = gem;
  }));

  // Render
  inhoud.innerHTML = ibRenderGroepKaarten(groepKeys, groepen, groepScores, vakken, vakLabels);

  // Events
  inhoud.querySelectorAll('.ib-groep-kaart').forEach(function(kaart) {
    kaart.addEventListener('click', function() {
      ibToonGroepDetail(kaart.dataset.groep, groepen[kaart.dataset.groep], groepScores[kaart.dataset.groep], vakken, vakLabels);
    });
  });
}

export function ibToonGroepDetail(groep, leerlingenInGroep, scores, vakken, vakLabels) {
  const inhoud = document.getElementById('ib-inhoud');
  const titel  = document.getElementById('ib-header-titel');
  const sub    = document.getElementById('ib-header-sub');
  const terug  = document.getElementById('ib-terug-knop');
  if (!inhoud) return;

  if (titel) titel.textContent = 'Groep ' + groep;
  if (sub)   sub.textContent   = leerlingenInGroep.length + ' leerlingen';
  if (terug) terug.style.display = 'block';

  // Sorteer op naam
  const gesorteerd = leerlingenInGroep.slice().sort(function(a,b){
    return (a.voornaam||'').localeCompare(b.voornaam||'');
  });

  let html = '<div style="display:flex;flex-direction:column;gap:8px">';

  // Kolomheader
  html += '<div style="display:grid;grid-template-columns:180px repeat(' + vakken.length + ',1fr);gap:8px;padding:8px 14px;font-size:10px;font-weight:700;color:var(--n-400);letter-spacing:.06em">';
  html += '<div>LEERLING</div>';
  vakken.forEach(function(v){ html += '<div style="text-align:center">' + (vakLabels[v]||v).toUpperCase() + '</div>'; });
  html += '</div>';

  gesorteerd.forEach(function(l) {
    // Per leerling: haal scores op uit globale cache of toon —
    const lScores = window._ibLeerlingScores && window._ibLeerlingScores[l.id] ? window._ibLeerlingScores[l.id] : {};
    const naam = [l.voornaam, l.tussenvoegsel, l.achternaam].filter(Boolean).join(' ');
    const heeftOnd = (l.ondersteuningsbehoeftes||[]).length > 0;

    html += '<div style="display:grid;grid-template-columns:180px repeat(' + vakken.length + ',1fr);gap:8px;padding:10px 14px;background:var(--n-0);border:1px solid var(--n-200);border-radius:var(--r-md);align-items:center">';
    html += '<div><div style="font-size:13px;font-weight:500;color:var(--n-900)">' + esc(naam) + '</div>';
    if (heeftOnd) html += '<div style="font-size:10px;color:var(--accent);margin-top:1px">ondersteuning</div>';
    html += '</div>';

    vakken.forEach(function(v) {
      const pct = parseFloat(lScores[v]) || null;
      const lbl = ibNiveauLabel(pct);
      const kleur = pct === null ? 'var(--n-300)' : pct >= 65 ? 'var(--success)' : pct >= 38 ? 'var(--warning)' : 'var(--danger)';
      html += '<div style="text-align:center;font-size:12px;font-weight:600;color:' + kleur + '">' + lbl + '</div>';
    });
    html += '</div>';
  });

  html += '</div>';
  html += '<div style="margin-top:12px;font-size:11px;color:var(--n-400)">Scores worden geladen vanuit het LVS. Lege velden betekenen dat er nog geen score is ingevoerd.</div>';
  inhoud.innerHTML = html;

  // Haal individuele scores op op de achtergrond
  window._ibLeerlingScores = window._ibLeerlingScores || {};
  Promise.all(gesorteerd.slice(0,30).map(async function(l) {
    if (window._ibLeerlingScores[l.id]) return;
    try {
      const res = await apiCall('/lvs/' + l.id + '/profiel');
      if (!res.ok) return;
      const data = await res.json();
      window._ibLeerlingScores[l.id] = data.scores || {};
    } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }
  })).then(function() {
    // Herrender na ophalen
    ibToonGroepDetail(groep, leerlingenInGroep, scores, vakken, vakLabels);
  });
}

export function ibRenderGroepKaarten(groepKeys, groepen, groepScores, vakken, vakLabels) {
  let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px">';

  groepKeys.forEach(function(g) {
    const scores = groepScores[g] || {};
    const st = ibStatusVanGem(scores);
    const aantalLeerl = groepen[g].length;
    const metOnd = groepen[g].filter(function(l){ return (l.ondersteuningsbehoeftes||[]).length > 0; }).length;

    html += '<div class="ib-groep-kaart" data-groep="' + g + '" style="background:var(--n-0);border:1px solid var(--n-200);border-radius:var(--r-lg);overflow:hidden;box-shadow:var(--shadow-xs);cursor:pointer;transition:box-shadow 120ms,transform 120ms">';

    // Kaart header
    html += '<div style="padding:16px 18px;background:' + st.bg + ';border-bottom:1px solid var(--n-200);display:flex;align-items:center;justify-content:space-between">';
    html += '<div><div style="font-family:var(--font-display);font-size:19px;color:var(--n-900)">Groep ' + g + '</div>';
    html += '<div style="font-size:11px;color:var(--n-500);margin-top:1px">' + aantalLeerl + ' leerlingen' + (metOnd ? ' · ' + metOnd + ' met ondersteuning' : '') + '</div></div>';
    html += '<div style="text-align:right">';
    html += '<div style="font-size:22px;font-weight:700;color:' + st.kleur + ';font-family:var(--font-display)">' + (st.pct || '—') + '%</div>';
    html += '<div style="font-size:10px;font-weight:700;color:' + st.kleur + ';letter-spacing:.05em">' + st.tekst.toUpperCase() + '</div>';
    html += '</div></div>';

    // Score balken
    html += '<div style="padding:14px 18px;display:flex;flex-direction:column;gap:9px">';
    vakken.forEach(function(v) {
      const pct = scores[v];
      const k = ibStatusVanGem({v: pct}).kleur;
      const lbl = ibNiveauLabel(pct);
      html += '<div style="display:flex;align-items:center;gap:10px">';
      html += '<div style="width:100px;font-size:11px;color:var(--n-600);flex-shrink:0">' + (vakLabels[v]||v) + '</div>';
      html += '<div style="flex:1;height:6px;background:var(--n-100);border-radius:var(--r-pill);overflow:hidden">';
      html += pct !== null ? '<div style="height:100%;width:' + pct + '%;background:' + k + ';border-radius:var(--r-pill)"></div>' : '';
      html += '</div>';
      html += '<div style="font-size:11px;font-weight:600;color:' + k + ';width:24px;text-align:right">' + lbl + '</div>';
      html += '</div>';
    });
    html += '</div>';

    html += '<div style="padding:10px 18px;border-top:1px solid var(--n-100);font-size:11px;color:var(--n-400)">Klik voor leerlingdetails →</div>';
    html += '</div>';
  });

  html += '</div>';
  return html;
}

export function ibStatusVanGem(gem) {
  const waarden = Object.values(gem).filter(function(v){ return v !== null; });
  if (!waarden.length) return { kleur:'var(--n-300)', bg:'var(--n-100)', tekst:'Geen data', pct: 0 };
  const avg = waarden.reduce(function(a,b){return a+b;},0) / waarden.length;
  if (avg >= 65) return { kleur:'var(--success)', bg:'var(--success-subtle)', tekst:'Goed', pct: Math.round(avg) };
  if (avg >= 38) return { kleur:'var(--warning)', bg:'var(--warning-subtle)', tekst:'Aandacht', pct: Math.round(avg) };
  return { kleur:'var(--danger)', bg:'var(--danger-subtle)', tekst:'Zorg', pct: Math.round(avg) };
}

export function ibNiveauLabel(pct) {
  if (pct === null) return '—';
  const niveaus = [[90,'I+'],[75,'I'],[60,'II'],[45,'III'],[30,'IV'],[15,'V'],[0,'V-']];
  for (var i = 0; i < niveaus.length; i++) {
    if (pct >= niveaus[i][0]) return niveaus[i][1];
  }
  return 'V-';
}


// ibLaadGroepen is een alias voor ibToonGroepen
export { ibToonGroepen as ibLaadGroepen };
