// ══════════════════════════════════════════════════════════
// leerlingen.js — Leerling beheer
// Laden, weergeven, verwijderen van leerlingen
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { leerlingen, setLeerlingen, huidigToken, schoolContext } from './state.js';
import { MAX_LEERLINGEN_PER_BATCH } from './config.js';

export async function laadLeerlingen() {
  if (!huidigToken) return;
  try {
    const res = await apiCall('/leerlingen?limit=' + MAX_LEERLINGEN_PER_BATCH + '&offset=0');
    if (res.status === 401) { uitloggen(); return; }
    if (!res.ok) {
      console.error('Leerlingen laden mislukt:', res.status);
      return;
    }
    const data = await res.json();
    leerlingen = Array.isArray(data) ? data : (data.leerlingen || []);
    toonLeerlingenLijst();
    // Vul notities-zijbalk bij als die al open staat
    if (typeof notitieVulZijbalk === 'function') notitieVulZijbalk();
    // Sync aanwezigheids-leerlingenlijst
    aawLeerlingen = leerlingen;
    // Update GLS badge
    if (typeof glsUpdateBadge === 'function') glsUpdateBadge();

    // Als er precies 50 terugkwamen kunnen er meer zijn — laad de rest stil
    if (leerlingen.length === 50) {
      laadLeerlingenRest(50);
    }
  } catch(e) { console.error(e); }
}

export function toonLeerlingenLijst() {
  const lijst = document.getElementById('leerlingen-lijst');
  if (!lijst) return;
  if (leerlingen.length === 0) {
    lijst.innerHTML = '<div style="color:var(--tz);font-size:12px;padding:10px 0">Nog geen leerlingen. Voeg er een toe.</div>';
    return;
  }

  lijst.innerHTML = '';

  function maakLeerlingEl(l) {
    const div = document.createElement('div');
    div.className = 'leerling-item';
    div.dataset.id = l.id;
    div.innerHTML = '<div class="leerling-naam">' + esc(l.voornaam) + '</div>'
      + '<div class="leerling-meta">Groep ' + (l.groep || '?') + '</div>';
    div.addEventListener('click', function() { selecteerLeerling(l.id); });
    return div;
  }

  if (schoolContext.is_ib && leerlingen.some(l => l.leerkracht_naam)) {
    const perLeerkracht = {};
    leerlingen.forEach(l => {
      const naam = l.leerkracht_naam || 'Onbekend';
      if (!perLeerkracht[naam]) perLeerkracht[naam] = [];
      perLeerkracht[naam].push(l);
    });
    Object.entries(perLeerkracht).forEach(function([leerkracht, lls]) {
      const groepDiv = document.createElement('div');
      groepDiv.style.marginBottom = '8px';
      const kop = document.createElement('div');
      kop.style.cssText = 'font-size:10px;font-weight:700;color:#888;letter-spacing:.08em;text-transform:uppercase;padding:4px 14px;background:var(--warm)';
      kop.textContent = leerkracht;
      groepDiv.appendChild(kop);
      lls.forEach(l => groepDiv.appendChild(maakLeerlingEl(l)));
      lijst.appendChild(groepDiv);
    });
  } else {
    leerlingen.forEach(l => lijst.appendChild(maakLeerlingEl(l)));
  }
}

export async function verwijderLeerling(id) {
  if (!confirm('Weet je zeker dat je deze leerling en alle rapporten wilt verwijderen?')) return;
  await apiCall('/leerlingen/' + id, { method: 'DELETE' });
  await laadLeerlingen();
  document.getElementById('naam').value = '';
}

export function vulRapportKlasSelect() {
  const sel      = document.getElementById('rapport-klas');
  const batchSel = document.getElementById('batch-groep');
  if (!sel) return;
  const huidig = sel.value;
  const groepen = [...new Set((leerlingen||[]).map(function(l){ return l.groep; }).filter(Boolean))]
    .sort(function(a,b){ return parseInt(a)-parseInt(b); });

  const maakOpties = function(el, kort) {
    const leeg = kort ? '—' : 'Kies groep…';
    el.innerHTML = '<option value="">' + leeg + '</option>';
    groepen.forEach(function(g) {
      const opt = document.createElement('option');
      opt.value = g;
      opt.textContent = kort ? g : ('Groep ' + g + ' — ' + leerlingen.filter(function(l){ return l.groep === g; }).length + ' leerlingen');
      el.appendChild(opt);
    });
  };

  maakOpties(sel, false);
  if (batchSel) maakOpties(batchSel, false);
  const groepEl = document.getElementById('groep');
  if (groepEl) maakOpties(groepEl, true);
  if (huidig && groepen.includes(huidig)) sel.value = huidig;
}

export function aawToonLeerlingToevoegen() {
  const aawLijst = document.getElementById('aaw-lijst');
  if (!aawLijst) return;
  const wrap = document.getElementById('aaw-toevoeg-wrap') || aawLijst;
  wrap.style.display = 'block';
  // Scroll naar het formulier
  wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
  wrap.innerHTML = `
    <div style="max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:16px">

      <!-- Tabs: leerling of groep -->
      <div style="display:flex;border-bottom:1px solid var(--warmrand);gap:0">
        <button id="aaw-tab-leerling" onclick="aawWisselFormulier('leerling')"
          style="padding:10px 20px;border:none;border-bottom:2px solid #4a7a4a;background:#fff;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;color:#4a7a4a">
          Leerling toevoegen
        </button>
        <button id="aaw-tab-groep" onclick="aawWisselFormulier('groep')"
          style="padding:10px 20px;border:none;border-bottom:2px solid transparent;background:#fff;font-size:12px;font-weight:400;cursor:pointer;font-family:inherit;color:var(--tz)">
          Groep toevoegen
        </button>
      </div>

      <!-- Formulier: leerling -->
      <div id="aaw-formulier-leerling" style="background:#fff;border:1px solid var(--warmrand);padding:24px;display:flex;flex-direction:column;gap:12px">
        <div style="font-family:Georgia,serif;font-size:17px;color:var(--t);margin-bottom:2px">Leerling toevoegen</div>
        <div style="font-size:12px;color:var(--tz);margin-bottom:6px">Voeg één leerling toe aan een bestaande of nieuwe groep.</div>
        <div>
          <div class="vlabel" style="margin-bottom:4px">VOORNAAM</div>
          <input id="aaw-nieuw-voornaam" type="text" placeholder="Voornaam leerling"
            style="width:100%;border:1px solid var(--rand);border-bottom:1.5px solid #4a7a4a;padding:8px 10px;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box">
        </div>
        <div>
          <div class="vlabel" style="margin-bottom:4px">GROEP</div>
          <select id="aaw-nieuw-groep" style="width:100%;border:1px solid var(--rand);border-bottom:1.5px solid #4a7a4a;padding:8px 10px;font-size:13px;font-family:inherit;outline:none;background:#fff">
            <option value="">— kies groep —</option>
            ${[1,2,3,4,5,6,7,8].map(g => '<option value="' + g + '">Groep ' + g + '</option>').join('')}
          </select>
        </div>
        <div>
          <div class="vlabel" style="margin-bottom:4px">LEERLINGNUMMER <span style="font-weight:400;color:var(--tz)">(optioneel)</span></div>
          <input id="aaw-nieuw-nummer" type="text" placeholder="bijv. 12345"
            style="width:100%;border:1px solid var(--rand);padding:8px 10px;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box">
        </div>
        <div id="aaw-toevoegen-fout" style="display:none;font-size:12px;color:#a03030;padding:8px;background:#fff5f5;border:1px solid #f0c0c0"></div>
        <div style="display:flex;gap:8px;margin-top:4px">
          <button onclick="aawVoegLeerlingToe()" style="flex:1;background:#4a7a4a;color:#fff;border:none;padding:10px;font-size:12px;font-weight:600;letter-spacing:.04em;cursor:pointer;font-family:inherit">Toevoegen</button>
          <button onclick="aawVoegMeerToe()" style="flex:1;border:1px solid var(--rand);background:#fff;padding:10px;font-size:12px;cursor:pointer;font-family:inherit;color:var(--tm)">+ Nog een leerling</button>
        </div>
      </div>

      <!-- Formulier: groep -->
      <div id="aaw-formulier-groep" style="display:none;background:#fff;border:1px solid var(--warmrand);padding:24px;display:none;flex-direction:column;gap:12px">
        <div style="font-family:Georgia,serif;font-size:17px;color:var(--t);margin-bottom:2px">Groep toevoegen</div>
        <div style="font-size:12px;color:var(--tz);margin-bottom:6px">Voeg meerdere leerlingen tegelijk toe aan een groep. Zet één naam per regel.</div>
        <div>
          <div class="vlabel" style="margin-bottom:4px">GROEP</div>
          <select id="aaw-groep-nummer" style="width:100%;border:1px solid var(--rand);border-bottom:1.5px solid #4a7a4a;padding:8px 10px;font-size:13px;font-family:inherit;outline:none;background:#fff">
            <option value="">— kies groep —</option>
            ${[1,2,3,4,5,6,7,8].map(g => '<option value="' + g + '">Groep ' + g + '</option>').join('')}
          </select>
        </div>
        <div>
          <div class="vlabel" style="margin-bottom:4px">LEERLINGEN <span style="font-weight:400;color:var(--tz)">— één naam per regel</span></div>
          <textarea id="aaw-groep-namen" rows="8" placeholder="Emma&#10;Liam&#10;Fatima&#10;Noah&#10;..."
            style="width:100%;border:1px solid var(--rand);border-bottom:1.5px solid #4a7a4a;padding:8px 10px;font-size:13px;font-family:inherit;outline:none;resize:vertical;line-height:1.7;box-sizing:border-box"></textarea>
        </div>
        <div id="aaw-groep-voortgang" style="display:none;font-size:12px;color:#4a7a4a;padding:8px;background:#f0faf0;border:1px solid #c0e0c0"></div>
        <div id="aaw-groep-fout" style="display:none;font-size:12px;color:#a03030;padding:8px;background:#fff5f5;border:1px solid #f0c0c0"></div>
        <div style="display:flex;gap:8px;margin-top:4px">
          <button onclick="aawVoegGroepToe()" style="flex:1;background:#4a7a4a;color:#fff;border:none;padding:10px;font-size:12px;font-weight:600;letter-spacing:.04em;cursor:pointer;font-family:inherit">Groep aanmaken</button>
        </div>
      </div>

    </div>`;
}

