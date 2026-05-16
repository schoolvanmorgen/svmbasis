// ══════════════════════════════════════════════════════════
// notities.js — Snelle notities
// Observaties schrijven, filteren en opslaan per leerling
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { huidigToken, leerlingen, huidigeLeerling,
         setHuidigeLeerling } from './state.js';
import { MAX_NOTITIES_IN_CONTEXT } from './config.js';

export function notitieRender() {
  notitieVulZijbalk();
  const leerlingId = window._huidigeLeerlingId;
  if (leerlingId) {
    notitiesLaad(leerlingId);
  } else {
    const el = document.getElementById('notitie-tijdlijn');
    if (el) el.innerHTML = '<div style="color:var(--tz);font-size:13px;padding:20px 0">Selecteer een leerling in de zijbalk.</div>';
  }
}

export function notitieVulZijbalk(filterGroep) {
  const lijst = document.getElementById('notitie-leerlingen-lijst');
  if (!lijst) return;

  // Sync zoekbalk met de klas-filter als meegegeven vanuit aanwezigheid
  if (filterGroep) {
    window._notitieGroepFilter = filterGroep;
  }

  if (leerlingen && leerlingen.length > 0) {
    const gefilterd = window._notitieGroepFilter
      ? leerlingen.filter(function(l){ return String(l.groep) === String(window._notitieGroepFilter); })
      : leerlingen;
    // Toon/verberg klas-indicator
    const ind = document.getElementById('notitie-klas-indicator');
    if (ind) {
      if (window._notitieGroepFilter) {
        ind.textContent = 'Groep ' + window._notitieGroepFilter;
        ind.style.display = 'inline-block';
      } else {
        ind.style.display = 'none';
      }
    }
    notitieRenderZijbalk(gefilterd);
  } else {
    // Leerlingen nog niet geladen — haal ze op
    lijst.innerHTML = '<div style="color:var(--tz);font-size:12px;padding:10px">Laden…</div>';
    if (huidigToken) {
      laadLeerlingen().then(() => {
        if (leerlingen.length) {
          notitieRenderZijbalk(leerlingen);
        } else {
          lijst.innerHTML = '<div style="color:var(--tz);font-size:12px;padding:10px">Nog geen leerlingen toegevoegd.</div>';
        }
      }).catch(() => {
        lijst.innerHTML = '<div style="color:var(--tz);font-size:12px;padding:10px">Laden mislukt.</div>';
      });
    }
  }
}

export function notitieRenderZijbalk(lijst) {
  const el = document.getElementById('notitie-leerlingen-lijst');
  if (!el) return;
  if (!lijst || !lijst.length) {
    el.innerHTML = '<div style="color:var(--tz);font-size:12px;padding:10px">Geen leerlingen gevonden.</div>';
    return;
  }
  el.innerHTML = lijst.map(l => {
    const actief = window._huidigeLeerlingId === l.id;
    return '<div onclick="notitieSelecteerLeerling(' + JSON.stringify(l.id) + ')" style="'
      + 'padding:9px 14px;cursor:pointer;border-left:2px solid ' + (actief ? '#4a7a4a' : 'transparent') + ';'
      + 'background:' + (actief ? '#f8f8f8' : '#fff') + ';transition:all .1s">'
      + '<div style="font-size:12px;font-weight:500;color:#2a3a2a">' + esc(l.voornaam) + '</div>'
      + '<div style="font-size:10px;color:var(--tz);margin-top:1px">Groep ' + (l.groep || '?') + '</div>'
      + '</div>';
  }).join('');
}

export function notitieSelecteerLeerling(id) {
  const l = leerlingen.find(x => x.id === id);
  if (!l) return;
  // Zet als actieve leerling (sync met rest van de app)
  stelLeerlingIn(l);
  // Update zijbalk highlight
  notitieRenderZijbalk(
    document.getElementById('notitie-zoek')?.value
      ? leerlingen.filter(x => x.voornaam.toLowerCase().includes(document.getElementById('notitie-zoek').value.toLowerCase()))
      : leerlingen
  );
  // Toon header
  const header = document.getElementById('notitie-leerling-header');
  const naamEl = document.getElementById('notitie-actieve-naam');
  const metaEl = document.getElementById('notitie-actieve-meta');
  if (header) header.style.display = 'block';
  if (naamEl) naamEl.textContent = l.voornaam;
  if (metaEl) metaEl.textContent = 'Groep ' + (l.groep || '?');
  // Laad notities
  notitiesLaad(id);
}

export function notitieFilterLeerlingen(zoek) {
  const gefilterd = leerlingen.filter(l =>
    l.voornaam.toLowerCase().includes(zoek.toLowerCase())
  );
  notitieRenderZijbalk(gefilterd);
}

export function notitieFilterOpKlas(groep) {
  window._notitieGroepFilter = groep;
  const zoekEl = document.getElementById('notitie-zoek');
  if (zoekEl) zoekEl.value = '';
  notitieVulZijbalk(groep);
}

export async function notitieOpslaan() {
  const tekst = document.getElementById('notitie-tekst')?.value.trim();
  if (!tekst) return;
  const leerlingId = window._huidigeLeerlingId;
  if (!leerlingId) {
    alert('Selecteer eerst een leerling voordat je een notitie opslaat.');
    return;
  }
  const knop = document.querySelector('#tab-notities button[onclick="notitieOpslaan()"]');
  if (knop) { knop.textContent = 'Opslaan…'; knop.disabled = true; }
  try {
    const res = await apiCall('/leerlingen/' + leerlingId + '/notities', { method: 'POST', body: JSON.stringify({ tekst, type: 'notitie' }) });
    if (!res.ok) {
      const fout = await res.json().catch(() => ({}));
      throw new Error(fout.detail || 'Opslaan mislukt');
    }
    const opgeslagenData = await res.json().catch(() => ({}));
    document.getElementById('notitie-tekst').value = '';
    await notitiesLaad(leerlingId);
    stroomNotitieNaarModules(leerlingId);

    // ── Toon categorisatie-feedback ──
    const cat = opgeslagenData._categorie;
    if (cat) {
      const domeinLabels = {
        sociaal:'Sociaal-emotioneel', werkhouding:'Werkhouding', executief:'Executieve functies',
        groeimeter:'Groeimeter', lezen:'Technisch lezen', dmt:'DMT', avi:'AVI',
        rekenen:'Rekenen', rekenen_basis:'Rekenen basisbew.', spelling:'Spelling',
        taalverzorging:'Taalverzorging', woordenschat:'Woordenschat',
        begrijpend:'Begrijpend lezen', begrijpend_luis:'Begrijpend luisteren',
        engels:'Engels', algemeen:'Algemeen'
      };
      const sentimentKleur = { positief:'#2d6a2d', neutraal:'#555', aandacht:'#c07000' };
      const sentimentIcoon = { positief:'↑', neutraal:'→', aandacht:'!' };
      const domeinLabel = domeinLabels[cat.domein] || cat.domein;
      const kleur = sentimentKleur[cat.sentiment] || '#555';
      const icoon = sentimentIcoon[cat.sentiment] || '→';
      const bevestiging = document.getElementById('notitie-doorstroom-bevestiging');
      if (bevestiging) {
        bevestiging.style.display = 'block';
        bevestiging.style.color = kleur;
        bevestiging.innerHTML = icoon + ' Toegevoegd aan LVS <strong>' + domeinLabel + '</strong> · "' + cat.kern + '"';
        setTimeout(() => { bevestiging.style.display = 'none'; }, 5000);
      }
    }
  } catch(e) {
    alert('Notitie opslaan mislukt: ' + e.message);
  } finally {
    if (knop) { knop.textContent = 'Opslaan'; knop.disabled = false; }
  }
}

export function notitieRenderData(notities) {
  const el = document.getElementById('notitie-tijdlijn');
  if (!el) return;
  if (!notities || !notities.length) {
    el.innerHTML = '<div style="color:var(--tz);font-size:13px;padding:20px 0">Nog geen notities voor deze leerling.</div>';
    return;
  }
  el.innerHTML = notities.map((n, idx) => {
    const datum = new Date(n.aangemaakt_op).toLocaleDateString('nl-NL', {weekday:'long', day:'numeric', month:'long', year:'numeric'});
    const tijdstip = new Date(n.aangemaakt_op).toLocaleTimeString('nl-NL', {hour:'2-digit', minute:'2-digit'});
    const kort = n.tekst.length > 120 ? n.tekst.slice(0,120) + '...' : n.tekst;
    const lijn = idx < notities.length - 1
      ? '<div style="position:absolute;left:11px;top:26px;bottom:0;width:1px;background:var(--warmrand)"></div>'
      : '';
    return '<div style="display:flex;gap:14px;padding-bottom:20px;position:relative">'
      + lijn
      + '<div style="width:24px;height:24px;background:#4a7a4a;flex-shrink:0;margin-top:2px;display:flex;align-items:center;justify-content:center"><div style="width:6px;height:6px;background:#fff"></div></div>'
      + '<div style="flex:1;min-width:0">'
      + '<div style="font-size:10px;color:var(--tz);letter-spacing:.04em;margin-bottom:6px;text-transform:uppercase">' + datum + ' &middot; ' + tijdstip + '</div>'
      + '<div id="notitie-preview-' + n.id + '" style="font-size:13px;color:var(--tm);line-height:1.7;cursor:pointer;border:1px solid var(--warmrand);padding:12px 14px;background:#fff;white-space:pre-wrap" onclick="notitieToggle(this.dataset.id)" data-id="' + n.id + '">' + kort + '</div>'
      + '<div id="notitie-vol-' + n.id + '" style="display:none;font-size:13px;color:var(--tm);line-height:1.7;border:1px solid #4a7a4a;padding:12px 14px;background:#fff;white-space:pre-wrap">' + escHtml(n.tekst)
      + '<div style="display:flex;gap:8px;margin-top:10px;border-top:1px solid var(--warmrand);padding-top:10px">'
      + '<button onclick="notitieKopieer(this.dataset.id)" data-id="' + n.id + '" data-tekst="' + encodeURIComponent(n.tekst) + '" style="font-size:11px;padding:5px 10px;border:1px solid var(--rand);background:#fff;cursor:pointer;font-family:inherit">Kopieer</button>'
      + '<button onclick="notitieVerwijder(this.dataset.id,this.dataset.leerling)" data-id="' + n.id + '" data-leerling="' + _notitiesHuidigId + '" style="font-size:11px;padding:5px 10px;border:1px solid var(--rand);background:#fff;cursor:pointer;font-family:inherit;color:var(--tz)">Verwijder</button>'
      + '<button onclick="notitieToggle(this.dataset.id)" data-id="' + n.id + '" style="font-size:11px;padding:5px 10px;border:1px solid var(--rand);background:#fff;cursor:pointer;font-family:inherit;margin-left:auto">Sluiten</button>'
      + '</div></div>'
      + '</div></div>';
  }).join('');
}

export async function notitieVerwijder(id, leerlingId) {
  if (!confirm('Notitie verwijderen?')) return;
  try {
    const res = await apiCall('/leerlingen/' + leerlingId + '/notities/' + id, { method: 'DELETE' });
    if (!res.ok) throw new Error('Verwijderen mislukt');
    await notitiesLaad(leerlingId);
  } catch(e) {
    alert('Verwijderen mislukt: ' + e.message);
  }
}

