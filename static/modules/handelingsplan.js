// ══════════════════════════════════════════════════════════
// handelingsplan.js — Handelingsplan generatie
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { huidigToken } from './state.js';
import { MAX_HP_TOKENS, MAX_NOTITIES_IN_CONTEXT } from './config.js';

export async function genereerHandelingsplan() {
  const naam = document.getElementById('hp-naam')?.value.trim() || '';
  const groep = document.getElementById('hp-groep')?.value || '';
  const behoefte = document.getElementById('hp-behoefte')?.value || '';
  const notities = document.getElementById('hp-notities')?.value.trim() || '';
  if (!behoefte) { alert('Selecteer een ondersteuningsbehoefte.'); return; }
  const knop = document.getElementById('hp-knop');
  const spin = document.getElementById('hp-spin');
  const resultaat = document.getElementById('hp-resultaat');
  knop.disabled=true; spin.style.display='block';
  document.getElementById('hp-ktekst').textContent='Verwerken...';
  // LVS-scores als leescontext
  let _hpLvsContext = '';
  let _hpNotitiesContext = '';
  if (window._huidigeLeerlingId) {
    try {
      const [_r2, _nr2] = await Promise.all([
        fetch(API_BASE + '/lvs/' + window._huidigeLeerlingId, { headers: { 'Authorization': 'Bearer ' + huidigToken } }),
        fetch(API_BASE + '/leerlingen/' + window._huidigeLeerlingId + '/notities', { headers: { 'Authorization': 'Bearer ' + huidigToken } })
      ]);
      if (_r2.ok) {
        const _p2 = await _r2.json();
        const _s2 = (_p2.scores || {});
        const _gevuld2 = LVS_DOMEINEN.filter(d => _s2[d.k] && _s2[d.k] > 0).map(d => d.l + ': ' + _s2[d.k]).join(', ');
        if (_gevuld2) _hpLvsContext = '\nBekende LVS-scores: ' + _gevuld2 + '.';
      }
      if (_nr2.ok) {
        const _nd2 = await _nr2.json();
        const _items2 = (_nd2.notities || _nd2 || []).slice(0, 10);
        if (_items2.length) _hpNotitiesContext = '\nEerdere observaties:\n' + _items2.map(function(n) {
          return '- [' + (n.aangemaakt_op||'').slice(0,10) + '] ' + (n.tekst||'').slice(0,150);
        }).join('\n');
      }
    } catch(e) { /* niet-kritiek — fout wordt genegeerd */ }
  }
  const prompt = `Produceer een concreet handelingsplan voor${naam?' '+naam:' een leerling'}${groep?' in groep '+groep:''} met ${behoefte}.
Notities: "${notities}"${_hpLvsContext}${_hpNotitiesContext}
Retourneer ALLEEN een JSON-object met:
{"doel":"Concreet doel voor de komende 6 weken","aanpak":"Bewezen interventies en hoe ze in te zetten in 3-4 zinnen","materialen":"Benodigde materialen of aanpassingen","evaluatie":"Hoe en wanneer evalueren","ouders":"Aanbeveling voor thuissituatie in 2 zinnen"}`;
  // Stuur leerling_id mee voor cascade naar LVS
  const _hpLeerlingId = window._huidigeLeerlingId || null;
  try {
    const _hpRes = await apiCall('/handelingsplan', {method: 'POST',
      body: JSON.stringify({ notities, naam, groep, ondersteuningsbehoefte: behoefte, leerling_id: _hpLeerlingId})
    });
    let tekst;
    if (_hpRes.ok) {
      const _hpData = await _hpRes.json();
      tekst = _hpData.tekst || JSON.stringify(_hpData.data || {});
    } else {
      tekst = await roepAPIaan(prompt);
    }
    const d = JSON.parse(tekst.replace(/```json|```/g,'').trim());
    resultaat.innerHTML = `<div class="naambalk">${naam?naam+' — ':''}Handelingsplan ${behoefte}</div>`
      + Object.entries({doel:'Doel',aanpak:'Aanpak & interventies',materialen:'Materialen',evaluatie:'Evaluatie',ouders:'Tips voor thuis'})
        .map(([k,l]) => d[k] ? `<div class="kaart tb"><div class="ktop"><div class="dot"></div><div class="knaam">${l.toUpperCase()}</div></div><div class="kbody">${d[k]}</div></div>` : '').join('');
    // Handelingsplan toevoegen aan LVS tijdlijn
    if (window._huidigeLeerlingId) {
      const leerlingId = window._huidigeLeerlingId;
      const datum = new Date().toLocaleDateString('nl-NL', {day:'numeric',month:'short',year:'numeric'});
      const profiel = await lvsHaalProfielOp(leerlingId);
      if (profiel) {
        const tijdlijn = [{ type:'handelingsplan', datum, tekst:'Handelingsplan ' + behoefte + ' gegenereerd.' + (d.doel ? ' Doel: ' + d.doel.slice(0,60) + '…' : '') }, ...(profiel.tijdlijn||[])];
        await lvsSlaProfielOp(leerlingId, { scores: profiel.scores||{}, vorige_scores: profiel.vorige_scores||{}, tijdlijn });
        if (lvsHuidig && lvsHuidig?.id === leerlingId) { lvsHuidig.tijdlijn = tijdlijn; lvsRenderTijdlijn(); }
      }
    }
  } catch(e) { resultaat.innerHTML = `<div class="fout">${e.message}</div>`; }
  finally { knop.disabled=false; spin.style.display='none'; document.getElementById('hp-ktekst').textContent='Produceer handelingsplan'; hpLaadLijst(); }
}

export async function hpLaadLijst() {
  const el = document.getElementById('hp-lijst');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--n-400);font-size:12px;padding:12px">Laden…</div>';
  try {
    const res = await apiCall('/rapporten?type=handelingsplan');
    if (!res.ok) throw new Error();
    const data = await res.json();
    const items = data.rapporten || data || [];
    if (!items.length) {
      el.innerHTML = '<div style="color:var(--n-400);font-size:12px;padding:12px">Nog geen handelingsplannen aangemaakt.</div>';
      return;
    }
    el.innerHTML = items.map(function(r) {
      const datum = new Date(r.aangemaakt_op).toLocaleDateString('nl-NL');
      const naam  = (r.rapport_data && r.rapport_data.naam) || r.leerling_naam || '—';
      return '<div class="doc-item" data-id="' + r.id + '" data-type="hp" style="padding:10px 12px;border-radius:var(--r-md);cursor:pointer;border:1px solid var(--n-200);background:var(--n-0);margin-bottom:6px">';
        + '<div style="font-size:13px;font-weight:500;color:var(--n-900)">' + esc(naam) + '</div>'
        + '<div style="font-size:11px;color:var(--n-400);margin-top:2px">' + datum + '</div>'
        + '</div>';
    }).join('');
  } catch(e) {
    el.innerHTML = '<div style="color:var(--n-400);font-size:12px;padding:12px">Laden mislukt.</div>';
  }
}

export async function hpToonDocument(id) {
  const res = document.getElementById('hp-resultaat');
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
      + '<div style="font-family:var(--font-display);font-size:18px">Handelingsplan — ' + esc(naam) + '</div>'
      + '<button id="hp-download-knop" class="kknop">⬇ Download PDF</button>'
      + '</div>'
      + '<div style="font-size:13px;line-height:1.9;white-space:pre-wrap;opacity:.92">' + tekst + '</div>'
      + '</div>';
    const dlKnop2 = res.querySelector('#hp-download-knop');
    if (dlKnop2) dlKnop2.addEventListener('click', function() { documentDownloadPdf(id, 'handelingsplan'); });
  } catch(e) {
    res.innerHTML = '<div style="padding:24px;color:var(--danger);font-size:13px">Laden mislukt.</div>';
  }
}

