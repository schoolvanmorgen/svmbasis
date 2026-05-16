// ══════════════════════════════════════════════════════════
// opp.js — OPP & Handelingsplan generatie
// Wettelijk verplichte documenten op basis van leerlingdossier
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { huidigToken } from './state.js';
import { MAX_OPP_TOKENS, MAX_NOTITIES_IN_CONTEXT } from './config.js';

export async function genereerOpp() {
  const naam = document.getElementById('opp-naam')?.value.trim() || '';
  const groep = document.getElementById('opp-groep')?.value || '';
  const behoefte = document.getElementById('opp-behoefte')?.value || '';
  const notities = document.getElementById('opp-notities')?.value.trim() || '';
  const knop = document.getElementById('opp-knop');
  const spin = document.getElementById('opp-spin');
  const resultaat = document.getElementById('opp-resultaat');
  knop.disabled = true; spin.style.display = 'block';
  document.getElementById('opp-ktekst').textContent = 'Verwerken...';
  // LVS-scores als leescontext
  let _oppLvsContext = '';
  let _oppNotitiesContext = '';
  if (window._huidigeLeerlingId) {
    try {
      const [_r, _nr] = await Promise.all([
        fetch(API_BASE + '/lvs/' + window._huidigeLeerlingId, { headers: { 'Authorization': 'Bearer ' + huidigToken } }),
        fetch(API_BASE + '/leerlingen/' + window._huidigeLeerlingId + '/notities', { headers: { 'Authorization': 'Bearer ' + huidigToken } })
      ]);
      if (_r.ok) {
        const _p = await _r.json();
        const _s = (_p.scores || {});
        const _gevuld = LVS_DOMEINEN.filter(d => _s[d.k] && _s[d.k] > 0).map(d => d.l + ': ' + _s[d.k]).join(', ');
        if (_gevuld) _oppLvsContext = '\nBekende LVS-scores: ' + _gevuld + '.';
      }
      if (_nr.ok) {
        const _nd = await _nr.json();
        const _items = (_nd.notities || _nd || []).slice(0, 10);
        if (_items.length) _oppNotitiesContext = '\nEerdere observaties:\n' + _items.map(function(n) {
          return '- [' + (n.aangemaakt_op||'').slice(0,10) + '] ' + (n.tekst||'').slice(0,150);
        }).join('\n');
      }
    } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }
  }
  const prompt = `Produceer een wettelijk compleet Ontwikkelingsperspectief (OPP) conform passend onderwijs voor${naam ? ' ' + naam : ' een leerling'}${groep ? ' in groep ' + groep : ''}${behoefte ? ' met ' + behoefte : ''}.
Notities: "${notities}"${_oppLvsContext}${_oppNotitiesContext}
Retourneer ALLEEN een JSON-object met:
{"uitstroom":"Uitstroombestemming en motivatie in 3-4 zinnen","bevorderend":"Bevorderende factoren in 2-3 zinnen","belemmerend":"Belemmerende factoren in 2-3 zinnen","doelen":"SMART-geformuleerde doelen voor de komende periode","hoorrecht":"Samenvatting hoorrecht leerling en ouders in 2 zinnen","evaluatie":"Evaluatiemoment en -methode"}`;
  // Stuur leerling_id mee zodat de backend de cascade naar LVS kan doen
  const _oppLeerlingId = window._huidigeLeerlingId || null;
  try {
    // Gebruik het dedicated /opp endpoint voor betere privacyfiltering en cascade
    const _oppRes = await apiCall('/opp', {method: 'POST',
      body: JSON.stringify({ notities, naam, groep, ondersteuningsbehoefte: behoefte, leerling_id: _oppLeerlingId})
    });
    let tekst;
    if (_oppRes.ok) {
      const _oppData = await _oppRes.json();
      tekst = _oppData.tekst || JSON.stringify(_oppData.data || {});
    } else {
      // Fallback naar generieke stream als het OPP-endpoint niet beschikbaar is
      tekst = await roepAPIaan(prompt);
    }
    const d = JSON.parse(tekst.replace(/```json|```/g,'').trim());
    resultaat.innerHTML = `<div class="naambalk">${naam ? naam+(groep?' — groep '+groep:'') : 'OPP'}</div>`
      + Object.entries({uitstroom:'Uitstroombestemming',bevorderend:'Bevorderende factoren',belemmerend:'Belemmerende factoren',doelen:'SMART-doelen',hoorrecht:'Hoorrecht',evaluatie:'Evaluatie'})
        .map(([k,l]) => d[k] ? `<div class="kaart tg"><div class="ktop"><div class="dot"></div><div class="knaam">${l.toUpperCase()}</div></div><div class="kbody">${d[k]}</div></div>` : '').join('');
    // OPP toevoegen aan LVS tijdlijn
    if (window._huidigeLeerlingId) {
      const leerlingId = window._huidigeLeerlingId;
      const datum = new Date().toLocaleDateString('nl-NL', {day:'numeric',month:'short',year:'numeric'});
      const profiel = await lvsHaalProfielOp(leerlingId);
      if (profiel) {
        const tijdlijn = [{ type:'opp', datum, tekst:'OPP gegenereerd.' + (d.uitstroom ? ' Uitstroom: ' + d.uitstroom.slice(0,60) + '…' : '') + (d.doelen ? ' Doel: ' + d.doelen.slice(0,60) + '…' : '') }, ...(profiel.tijdlijn||[])];
        await lvsSlaProfielOp(leerlingId, { scores: profiel.scores||{}, vorige_scores: profiel.vorige_scores||{}, tijdlijn });
        if (lvsHuidig && lvsHuidig?.id === leerlingId) { lvsHuidig.tijdlijn = tijdlijn; lvsRenderTijdlijn(); }
      }
    }
  } catch(e) { resultaat.innerHTML = `<div class="fout">${e.message}</div>`; }
  finally { knop.disabled=false; spin.style.display='none'; document.getElementById('opp-ktekst').textContent='Produceer OPP'; oppLaadLijst(); }
}

export async function oppLaadLijst() {
  const el = document.getElementById('opp-lijst');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--n-400);font-size:12px;padding:12px">Laden…</div>';
  try {
    const res = await apiCall('/rapporten?type=opp');
    if (!res.ok) throw new Error();
    const data = await res.json();
    const items = data.rapporten || data || [];
    if (!items.length) {
      el.innerHTML = '<div style="color:var(--n-400);font-size:12px;padding:12px">Nog geen OPP\u0027s aangemaakt.</div>';
      return;
    }
    el.innerHTML = items.map(function(r) {
      const datum = new Date(r.aangemaakt_op).toLocaleDateString('nl-NL');
      const naam  = (r.rapport_data && r.rapport_data.naam) || r.leerling_naam || '—';
      return '<div class="doc-item" data-id="' + r.id + '" data-type="opp" style="padding:10px 12px;border-radius:var(--r-md);cursor:pointer;border:1px solid var(--n-200);background:var(--n-0);margin-bottom:6px">';
      return '<div class="doc-item" data-id="' + r.id + '" data-type="opp" style="padding:10px 12px;border-radius:var(--r-md);cursor:pointer;border:1px solid var(--n-200);background:var(--n-0);margin-bottom:6px">';
        + '<div style="font-size:11px;color:var(--n-400);margin-top:2px">' + datum + '</div>'
        + '</div>';
    }).join('');
  } catch(e) {
    el.innerHTML = '<div style="color:var(--n-400);font-size:12px;padding:12px">Laden mislukt.</div>';
  }
}

export async function oppToonDocument(id) {
  const res = document.getElementById('opp-resultaat');
  if (!res) return;
  res.innerHTML = '<div style="padding:24px;color:var(--n-400);font-size:13px">Laden…</div>';
  try {
    const r = await apiCall('/rapporten/' + id);
    if (!r.ok) throw new Error();
    const data = await r.json();
    const tekst = (data.rapport_data && data.rapport_data.rapportcommentaar) || JSON.stringify(data.rapport_data, null, 2) || '—';
    const naam  = (data.rapport_data && data.rapport_data.naam) || '—';
    res.innerHTML = '<div class="rres" style="margin:20px">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">'
      + '<div style="font-family:var(--font-display);font-size:18px">OPP — ' + esc(naam) + '</div>'
      + '<button id="hp-download-knop" class="kknop">⬇ Download PDF</button>'
      + '</div>'
      + '<div style="font-size:13px;line-height:1.9;white-space:pre-wrap;opacity:.92">' + tekst + '</div>'
      + '</div>';
    const dlKnop = res.querySelector('#opp-download-knop');
    if (dlKnop) dlKnop.addEventListener('click', function() { documentDownloadPdf(id, 'opp'); });
  } catch(e) {
    res.innerHTML = '<div style="padding:24px;color:var(--danger);font-size:13px">Laden mislukt.</div>';
  }
}

export function documentDownloadPdf(id, type) {
  // Genereer PDF via print-venster met de inhoud
  const res = document.getElementById(type === 'opp' ? 'opp-resultaat' : 'hp-resultaat');
  if (!res) return;
  const inhoud = res.querySelector('.rres');
  if (!inhoud) return;

  const win = window.open('', '_blank');
  win.document.write('<html><head><title>' + (type === 'opp' ? 'OPP' : 'Handelingsplan') + '</title>'
    + '<style>body{font-family:Georgia,serif;font-size:13px;line-height:1.9;padding:40px;color:#222;max-width:720px;margin:0 auto}'
    + 'h2{font-size:18px;margin-bottom:16px;border-bottom:2px solid #3d6b52;padding-bottom:8px;color:#3d6b52}'
    + 'pre{white-space:pre-wrap;font-family:Georgia,serif;font-size:13px;line-height:1.9}'
    + '@media print{body{padding:20px}}'
    + '</style></head><body>');
  win.document.write('<h2>' + inhoud.querySelector('div').textContent + '</h2>');
  win.document.write('<pre>' + (inhoud.querySelector('div:last-child') ? inhoud.querySelector('div:last-child').textContent : '') + '</pre>');
  win.document.write('</body></html>');
  win.document.close();
  win.focus();
  setTimeout(function() { win.print(); }, 500);
}

