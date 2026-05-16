// ══════════════════════════════════════════════════════════
// ui.js — UI navigatie en gedeelde UI-helpers
// Tab routing, modal management, microfoon, zoekbalk
// ══════════════════════════════════════════════════════════
import { huidigeLeerling, schoolContext } from './state.js';
import { aawInit } from './aanwezigheid.js';
import { notitieRender } from './notities.js';
import { lvsInit } from './lvs.js';
import { ibToonGroepen } from './ib.js';

export function toonTab(tab, knop) {
  // Sla actieve tab op voor herstel bij terugkomen
  try { localStorage.setItem('svm_actieve_tab', tab); } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }

  // Reset knoppen
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('actief'));
  if (knop) knop.classList.add('actief');

  // Verberg alle containers
  ['tab-inhoud','tab-lvs','tab-aanwezigheid','tab-ib','tab-inspectie','tab-klas'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.querySelectorAll('.sectie,.sectie-vol').forEach(function(s) { s.classList.remove('actief'); });
  var notEl = document.getElementById('tab-notities');
  if (notEl) { notEl.style.display = 'none'; notEl.classList.remove('actief'); }

  // Toon gevraagde tab
  if (tab === 'aanwezigheid') {
    var el = document.getElementById('tab-aanwezigheid');
    if (el) el.style.display = 'flex';
    if (typeof aawInit === 'function') aawInit();

  } else if (tab === 'notities') {
    var tiNot = document.getElementById('tab-inhoud');
    if (tiNot) tiNot.style.display = 'none';
    var el2 = document.getElementById('tab-notities');
    if (el2) { el2.style.display = 'flex'; el2.classList.add('actief'); }
    if (typeof notitieRender === 'function') notitieRender();

  } else if (tab === 'lvs') {
    var tiLvs = document.getElementById('tab-inhoud');
    if (tiLvs) tiLvs.style.display = 'none';
    var el3 = document.getElementById('tab-lvs');
    if (el3) el3.style.display = 'flex';
    if (typeof lvsInit === 'function') lvsInit();

  } else if (tab === 'klas') {
    var tiKlas = document.getElementById('tab-inhoud');
    if (tiKlas) tiKlas.style.display = 'none';
    var elKlas = document.getElementById('tab-klas');
    if (elKlas) elKlas.style.display = 'flex';

  } else if (tab === 'ib') {
    var el4 = document.getElementById('tab-ib');
    if (el4) { el4.style.display = 'flex'; el4.style.flexDirection = 'column'; }
    ibToonGroepen();

  } else if (tab === 'inspectie') {
    var el5 = document.getElementById('tab-inspectie');
    if (el5) el5.style.display = 'block';

  } else {
    // rapport, opp, handelingsplan — in tab-inhoud
    var inhoud = document.getElementById('tab-inhoud');
    if (inhoud) inhoud.style.display = 'block';
    var sectieEl = document.getElementById('tab-' + tab);
    if (sectieEl) sectieEl.classList.add('actief');
  }
}

export function stelLeerlingIn(leerling) {
  if (typeof obsUpdateProfiel === 'function') obsUpdateProfiel();
  huidigeLeerling = leerling;
  window._huidigeLeerlingId = leerling ? leerling.id : null;

  // Vul rapport-tab velden
  if (leerling) {
    document.getElementById('naam').value = leerling.voornaam || '';
    document.getElementById('groep').value = leerling.groep || '';
    if (leerling.notities) document.getElementById('notities').value = leerling.notities;
    if (leerling.ondersteuningsbehoeftes) {
      document.querySelectorAll('#od-checklijst input').forEach(cb => {
        cb.checked = leerling.ondersteuningsbehoeftes.includes(cb.value);
      });
      if (typeof updateTags === 'function') updateTags();
    }
  }

  // Vul OPP-velden
  const oppNaam  = document.getElementById('opp-naam');
  const oppGroep = document.getElementById('opp-groep');
  if (oppNaam)  oppNaam.value  = leerling ? (leerling.voornaam || '') : '';
  if (oppGroep) oppGroep.value = leerling ? (leerling.groep || '')    : '';

  // Vul handelingsplan-velden
  const hpNaam  = document.getElementById('hp-naam');
  const hpGroep = document.getElementById('hp-groep');
  if (hpNaam)  hpNaam.value  = leerling ? (leerling.voornaam || '') : '';
  if (hpGroep) hpGroep.value = leerling ? (leerling.groep || '')    : '';

  // Toon/verberg actieve-leerling banner in alle tabs
  const naam   = leerling ? leerling.voornaam : '';
  const groep  = leerling ? (leerling.groep ? ' · groep ' + leerling.groep : '') : '';
  const tekst  = leerling ? naam + groep : '';
  document.querySelectorAll('.actieve-leerling-banner').forEach(el => {
    el.style.display = leerling ? 'flex' : 'none';
    const span = el.querySelector('.alb-naam');
    if (span) span.textContent = tekst;
  });

  // Sync LVS als leerling ook in LVS-lijst staat
  if (leerling && typeof lvsSelecteer === 'function') {
    const lvsMatch = lvsLeerlingen.find(l => l.id === leerling.id);
    if (lvsMatch && (!lvsHuidig || lvsHuidig?.id !== leerling.id)) {
      lvsSelecteer(leerling.id);
    }
  }

  // Notities tab: herlaad + update zijbalk + header
  if (leerling && _notitiesHuidigId !== leerling.id) {
    if (typeof notitiesLaad === 'function') notitiesLaad(leerling.id);
  }
  // Update globale leerling-selector badge
  if (typeof glsUpdateBadge === 'function') glsUpdateBadge();
  if (leerling) {
    // Update notitie-zijbalk highlight
    if (typeof notitieRenderZijbalk === 'function') {
      const zoekEl = document.getElementById('notitie-zoek');
      const zoek = zoekEl ? zoekEl.value : '';
      const lijst = zoek
        ? leerlingen.filter(x => x.voornaam.toLowerCase().includes(zoek.toLowerCase()))
        : leerlingen;
      notitieRenderZijbalk(lijst);
    }
    // Update notitie-tab header
    const header = document.getElementById('notitie-leerling-header');
    const naamEl = document.getElementById('notitie-actieve-naam');
    const metaEl = document.getElementById('notitie-actieve-meta');
    if (header) header.style.display = 'block';
    if (naamEl) naamEl.textContent = leerling.voornaam;
    if (metaEl) metaEl.textContent = 'Groep ' + (leerling.groep || '?');
  }
}

export function glsZoek(waarde) {
  if (!waarde.trim()) {
    glsToonDropdown(leerlingen.slice(0, 30));
    return;
  }
  const q = waarde.toLowerCase();
  const gefilterd = leerlingen.filter(l =>
    l.voornaam.toLowerCase().includes(q) ||
    (l.achternaam || '').toLowerCase().includes(q) ||
    (l.groep || '').includes(q)
  ).slice(0, 20);
  glsToonDropdown(gefilterd);
}

export function glsSluit() {
  document.getElementById('gls-dropdown').style.display = 'none';
  glsUpdateBadge();
}

export function glsUpdateBadge() {
  const zoek     = document.getElementById('gls-zoek');
  const actNaam  = document.getElementById('gls-actief-naam');
  const wis      = document.getElementById('gls-wis');
  const badge    = document.getElementById('gls-actief-badge');
  if (huidigeLeerling) {
    const volNaam = [huidigeLeerling.voornaam, huidigeLeerling.tussenvoegsel, huidigeLeerling.achternaam].filter(Boolean).join(' ');
    const label   = volNaam + (huidigeLeerling.groep ? ' · gr.' + huidigeLeerling.groep : '');
    zoek.value = '';
    zoek.placeholder = '';
    actNaam.textContent = label;
    actNaam.style.display = 'block';
    wis.style.display = 'block';
    badge.textContent = label;
    badge.style.display = 'none'; // badge zit al in het input-veld zichtbaar
  } else {
    actNaam.style.display = 'none';
    wis.style.display = 'none';
    badge.style.display = 'none';
    zoek.placeholder = 'Zoek leerling…';
  }
}

export function toggleMic(veldId, knop) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    alert('Spraakherkenning wordt niet ondersteund in deze browser. Gebruik Chrome of Safari.');
    return;
  }
  if (activeMic) {
    activeMic.stop();
    return;
  }
  const rec = new SpeechRecognition();
  rec.lang = 'nl-NL';
  rec.continuous = true;
  rec.interimResults = false;
  activeMic = rec;
  actieveKnop = knop;
  knop.classList.add('luistert');
  knop.title = 'Klik om te stoppen';
  knop.textContent = '⏹';
  rec.onresult = function(e) {
    const veld = document.getElementById(veldId);
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) {
        const tekst = e.results[i][0].transcript;
        veld.value += (veld.value && !veld.value.endsWith(' ') ? ' ' : '') + tekst;
        veld.dispatchEvent(new Event('input'));
      }
    }
  };
  rec.onerror = function(e) { stopMic(); };
  rec.onend = function() { stopMic(); };
  rec.start();
}

export function openImportModal() {
  // Zet callback-flags zodat na sluiten de juiste tab ververst
  const actieveTab = document.querySelector('.tab.actief');
  const tabNaam = actieveTab ? actieveTab.getAttribute('onclick') : '';
  if (tabNaam.includes('lvs'))         window._lvsModalCallback = true;
  if (tabNaam.includes('aanwezigheid')) window._aawModalCallback = true;
  // Altijd beide zetten — import is schoolbreed
  window._lvsModalCallback = true;
  window._aawModalCallback = true;
  document.getElementById('leerling-modal').style.display = 'flex';
  leerlingTab(3);
}

export function sluitLeerlingModal() {
  document.getElementById('leerling-modal').style.display = 'none';
  // Altijd de actieve tab refreshen na modal sluiten
  const actief = document.querySelector('.tab.actief');
  const tab = actief ? actief.getAttribute('onclick') : '';
  if (window._lvsModalCallback || tab.includes('lvs')) {
    window._lvsModalCallback = false;
    lvsInit();
  }
  if (window._aawModalCallback || tab.includes('aanwezigheid')) {
    window._aawModalCallback = false;
    aawInit();
  }
}

export function leerlingTab(nr) {
  [1,2,3].forEach(function(i) {
    document.getElementById('ltab-inhoud-' + i).style.display = i === nr ? 'flex' : 'none';
    const btn = document.getElementById('ltab-' + i);
    if (btn) {
      btn.style.background = i === nr ? '#000' : '#f8f8f8';
      btn.style.color = i === nr ? '#fff' : '#888';
      btn.style.fontWeight = i === nr ? '600' : '500';
    }
  });
}

