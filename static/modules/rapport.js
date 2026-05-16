// ══════════════════════════════════════════════════════════
// rapport.js — Rapport generatie
// Individuele rapporten, batch, streaming AI-output
// ══════════════════════════════════════════════════════════
import { apiCall, esc } from './api.js';
import { huidigToken, leerlingen, huidigeLeerling } from './state.js';
import { MAX_RAPPORT_TOKENS, MAX_NOTITIES_IN_CONTEXT, API_BASE } from './config.js';

export function rapportLaadKlas(groep) {
  // Sync batch-groep mee
  const batchSel = document.getElementById('batch-groep');
  if (batchSel && groep) batchSel.value = groep;
  const lijst = document.getElementById('rapport-leerlingen-lijst');
  if (!lijst) return;
  if (!groep) {
    lijst.innerHTML = '<div style="font-size:11px;color:var(--tz);padding:8px 6px">Kies een groep.</div>';
    return;
  }
  const klasLeerlingen = leerlingen
    .filter(function(l) { return String(l.groep) === String(groep); })
    .sort(function(a,b) { return (a.voornaam||'').localeCompare(b.voornaam||''); });

  if (!klasLeerlingen.length) {
    lijst.innerHTML = '<div style="font-size:11px;color:var(--tz);padding:8px 6px">Geen leerlingen in groep ' + groep + '.</div>';
    return;
  }

  let html = '';
  klasLeerlingen.forEach(function(l) {
    const ach = [l.tussenvoegsel, l.achternaam].filter(Boolean).join(' ');
    html += '<div class="rapport-leerling-item" data-lid="' + l.id + '"'
      + ' style="padding:7px 8px;font-size:12px;cursor:pointer;border-radius:6px;margin-bottom:2px;color:var(--t)">'
      + '<div style="font-weight:500">' + esc(l.voornaam) + '</div>'
      + (ach ? '<div style="font-size:10px;color:var(--tz)">' + ach + '</div>' : '')
      + '</div>';
  });
  lijst.innerHTML = html;

  lijst.querySelectorAll('.rapport-leerling-item').forEach(function(el) {
    el.addEventListener('click', function() { rapportSelecteerLeerling(el.dataset.lid); });
    el.addEventListener('mouseover', function() { if (!el.classList.contains('actief')) el.style.background = '#f0f0f0'; });
    el.addEventListener('mouseout',  function() { if (!el.classList.contains('actief')) el.style.background = ''; });
  });
}

export function rapportSelecteerLeerling(id) {
  const l = leerlingen.find(function(x) { return x.id === id; });
  if (!l) return;

  // Vul naam en groep in
  const naamEl  = document.getElementById('naam');
  const groepEl = document.getElementById('groep');
  if (naamEl) {
    const volledig = [l.voornaam, l.tussenvoegsel, l.achternaam].filter(Boolean).join(' ');
    naamEl.value = volledig;
    if (typeof telTekens === 'function') telTekens();
  }
  if (groepEl) groepEl.value = l.groep || '';

  // Zet ondersteuningsbehoeftes aan
  const behoeftes = l.ondersteuningsbehoeftes || [];
  document.querySelectorAll('#od-checklijst input[type=checkbox]').forEach(function(cb) {
    cb.checked = behoeftes.includes(cb.value);
  });
  if (typeof updateTags === 'function') updateTags();

  // Markeer geselecteerde leerling in de lijst
  document.querySelectorAll('.rapport-leerling-item').forEach(function(el) {
    const actief = el.dataset.id === id;
    el.style.background = actief ? 'var(--gl)' : '';
    el.style.fontWeight  = actief ? '600' : '';
  });

  // Sync globale context
  stelLeerlingIn(l);
}

export async function startBatchRapporten() {
  const groep = document.getElementById('batch-groep').value;
  if (!groep) { alert('Kies eerst een groep.'); return; }

  const knop      = document.getElementById('batch-knop');
  const voortgang = document.getElementById('batch-voortgang');

  knop.disabled  = true;
  knop.textContent = 'Bezig…';
  voortgang.style.display = 'block';
  voortgang.innerHTML = '<span style="color:#4a7a4a">⏳ Rapporten worden gegenereerd voor groep ' + groep + '…</span>';

  try {
    const res = await apiCall('/rapporten/batch', {method:  'POST',
      body:    JSON.stringify({ groep, schooljaar: '2025-2026'})
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Status ' + res.status);
    }

    const data = await res.json();
    const { totaal, gegenereerd, overgeslagen, fouten: aantalFouten, fouten_detail } = data;

    // Toon resultaat
    let html = '';

    if (gegenereerd > 0) {
      html += '<div style="color:#2d6a2d;font-weight:600;margin-bottom:6px">✓ ' + gegenereerd + ' van de ' + totaal + ' rapporten gegenereerd en opgeslagen.</div>';
    }

    if (overgeslagen > 0) {
      const namen = fouten_detail
        .filter(f => f.status === 'overgeslagen')
        .map(f => f.voornaam)
        .join(', ');
      html += '<div style="color:#92400e;margin-bottom:4px">⚠ ' + overgeslagen + ' overgeslagen (geen notities): ' + namen + '</div>';
    }

    if (aantalFouten > 0) {
      const namen = fouten_detail
        .filter(f => f.status === 'fout')
        .map(f => f.voornaam)
        .join(', ');
      html += '<div style="color:#991b1b;margin-bottom:4px">✗ ' + aantalFouten + ' mislukt: ' + namen + '</div>';
    }

    if (gegenereerd > 0) {
      html += '<div style="margin-top:6px;font-size:10px;color:var(--tz)">Selecteer een leerling om het rapport te bekijken en eventueel aan te passen.</div>';
      // Herlaad leerlingenlijst zodat rapport-geschiedenis up to date is
      await laadLeerlingen();
    }

    voortgang.innerHTML = html;

  } catch(e) {
    voortgang.innerHTML = '<span style="color:#991b1b">Fout: ' + e.message + '</span>';
  } finally {
    knop.disabled  = false;
    knop.textContent = 'Produceer alle rapporten';
  }
}

export function toonResultaten(data, naam, groep) {
  const r = document.getElementById('resultaat');
  r.innerHTML = '';
  const leegEl = document.getElementById('leeg');
  if (leegEl) leegEl.style.display = 'none';

  // ── Rapport header ─────────────────────────────────────────
  const datum = new Date().toLocaleDateString('nl-NL', {day:'numeric',month:'long',year:'numeric'});
  const header = document.createElement('div');
  header.style.cssText = 'background:#fff;border:1px solid var(--warmrand);border-radius:7px;padding:16px 18px;margin-bottom:8px';
  header.innerHTML = `
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <div>
        <div style="font-family:Georgia,serif;font-size:20px;font-weight:600;color:var(--t);margin-bottom:3px">
          ${naam || 'Rapport'}${groep ? ' <span style="font-size:14px;font-weight:400;color:var(--tz)">— groep ' + groep + '</span>' : ''}
        </div>
        <div style="font-size:11px;color:var(--tz);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span>Gegenereerd op ${datum}</span>
          <span style="display:inline-flex;align-items:center;gap:3px;color:#222;background:#f5f5f5;border:0.5px solid #ddd;border-radius:6px;padding:2px 7px;font-weight:500">
            🛡 AVG-gecheckt — naam geanonimiseerd verzonden
          </span>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button onclick="allesKopierenRapport()" style="display:flex;align-items:center;gap:5px;background:var(--grijs);border:1px solid var(--rand);border-radius:6px;padding:6px 12px;font-size:11px;font-weight:500;cursor:pointer;font-family:inherit;color:var(--tm);white-space:nowrap">
          📋 Alles kopiëren
        </button>
        <button onclick="downloadPDF()" style="display:flex;align-items:center;gap:5px;background:var(--grijs);border:1px solid var(--rand);border-radius:6px;padding:6px 12px;font-size:11px;font-weight:500;cursor:pointer;font-family:inherit;color:var(--tm);white-space:nowrap">
          ⬇ PDF
        </button>
        ${window._huidigeLeerlingId ? `<button onclick="slaRapportOpVanuitHeader()" style="display:flex;align-items:center;gap:5px;background:var(--gm);border:none;border-radius:6px;padding:6px 12px;font-size:11px;font-weight:600;cursor:pointer;font-family:inherit;color:#fff;white-space:nowrap">
          💾 Opslaan
        </button>` : ''}
      </div>
    </div>`;
  r.appendChild(header);

  SECTIES.forEach((s, i) => {
    const w = data[s.k];
    if (!w) return;
    const kaart = document.createElement('div');

    if (s.k === 'ondersteuning') {
      if (!w || w === 'null') return;
      const kaart = document.createElement('div');
      kaart.className = 'kaart tp';
      let tekst = '';
      if (Array.isArray(w)) {
        tekst = w.map(item => `<strong>${item.behoefte || ''}</strong>: ${item.praktijk || item.interventie || ''}`).join('<br><br>');
      } else if (typeof w === 'string') {
        tekst = w.replace(/\n/g,'<br>');
      } else {
        tekst = JSON.stringify(w);
      }
      const oid = 'o' + Date.now();
      kaart.innerHTML = '<div class="ktop"><div class="dot"></div><div class="knaam">ONDERSTEUNING IN DE PRAKTIJK</div><span class="mtag">Ondersteuning</span><button class="kknop" onclick="kopieer(\'' + oid + '\',this)">Kopieer</button></div><div class="kbody" id="' + oid + '" style="font-size:12px">' + tekst + '</div>';
      r.appendChild(kaart);
      return;
    }
    if (s.k === 'rapportcommentaar') {
      const kid = 'rt' + Date.now();
      kaart.className = 'rblok';
      kaart.innerHTML = `
        <div class="rtop">
          <span class="rtitel">Rapportcommentaar — klaar voor ParnasSys</span>
          <button class="kknop" onclick="kopieer('${kid}',this)">Kopieer</button>
        </div>
        <div class="rtext" id="${kid}">${w}</div>`;
      r.appendChild(kaart); return;
    }

    const kid2 = 'k' + Date.now() + Math.random().toString(36).slice(2);
    kaart.className = `kaart ${s.c}`;
    kaart.innerHTML = `
      <div class="ktop"><div class="dot"></div>
      <div class="knaam">${s.l.toUpperCase()}</div>
      <span class="mtag">${s.p}</span>
      <button class="kknop" onclick="kopieer('${kid2}',this)">Kopieer</button></div>
      <div class="kbody" id="${kid2}">${w}</div>`;
    r.appendChild(kaart);
  });

  // PDF downloadknop
  const pdfKnop = document.createElement('button');
  pdfKnop.className = 'genknop';
  pdfKnop.style.cssText = 'background:#222;box-shadow:none;margin-top:4px';
  pdfKnop.innerHTML = '&#000; Download als PDF';
  pdfKnop.onclick = downloadPDF;
  r.appendChild(pdfKnop);

  // Pedagogisch advies
  const ped = document.createElement('div');
  ped.className = 'pedblok';
  ped.innerHTML = `
    <div class="pedtop">
      <div><div class="pedtitel">Pedagogisch advies</div><div class="pedsub">Alleen zichtbaar voor de leerkracht</div></div>
      <button class="pedknop" id="pedknop" onclick="laadPedagogisch()">Analyseer</button>
    </div>
    <div class="pedbody" id="pedbody"><div class="pedwacht">Klik op "Analyseer" voor advies op basis van 5 wetenschappelijke theorieën.</div></div>`;
  r.appendChild(ped);

  // Progressie staafdiagrammen
  const progEl = document.createElement('div');
  progEl.innerHTML = bouwProgressieSectie(naam);
  r.appendChild(progEl.firstElementChild);

  // Delen met ouders
  const ouder = document.createElement('div');
  ouder.className = 'ouderblok';
  ouder.innerHTML = `
    <div class="oudertop">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="#222" stroke-width="1.5"><rect x="1" y="3" width="14" height="10" rx="2"/><path d="M1 6l7 4 7-4"/></svg>
      <span class="oudertitel">DELEN MET OUDERS</span>
    </div>
    <div class="ouderbody">
      <div style="font-size:11px;color:var(--tz);line-height:1.5">Het rapport wordt direct per e-mail verstuurd en opgeslagen in de communicatiegeschiedenis.</div>
      <div class="emailrij">
        <input type="text" id="oudermail" placeholder="e-mailadres ouder(s)" class="oudermail-invoer">
        <button class="deelknop" onclick="deelMetOuders(this)">Verstuur</button>
      </div>
      <div class="deelok" id="deelok"></div>
      <div style="margin-top:10px">
        <button onclick="ouderGeschiedenisToggle(this)" style="background:none;border:none;font-size:11px;color:var(--tz);cursor:pointer;padding:0;text-decoration:underline">Communicatiegeschiedenis tonen</button>
        <div id="ouder-geschiedenis-container" style="display:none;margin-top:8px"></div>
      </div>
    </div>`;
  r.appendChild(ouder);

  // Feedback
  const fb = document.createElement('div');
  fb.className = 'feedblok';
  fb.style.display = 'block';
  const feedTop = document.createElement('div');
  feedTop.className = 'feedtop';
  feedTop.innerHTML = '<span class="feedtitel">WAS DIT RAPPORT BRUIKBAAR?</span>';
  const feedBody = document.createElement('div');
  feedBody.className = 'feedbody';
  const duimen = document.createElement('div');
  duimen.className = 'feedduimen';
  const btnGoed = document.createElement('button');
  btnGoed.className = 'dknop';
  btnGoed.textContent = '👍 Ja';
  btnGoed.addEventListener('click', function() { selecteerDuim('goed', this); });
  const btnMatig = document.createElement('button');
  btnMatig.className = 'dknop';
  btnMatig.textContent = '👎 Nog niet';
  btnMatig.addEventListener('click', function() { selecteerDuim('matig', this); });
  duimen.appendChild(btnGoed);
  duimen.appendChild(btnMatig);
  const feedTxt = document.createElement('textarea');
  feedTxt.style.cssText = 'height:65px;font-size:11px;border:1px solid var(--rand);border-radius:7px;padding:8px 10px;width:100%;outline:none';
  feedTxt.id = 'feedtekst';
  feedTxt.placeholder = 'Wat kan er beter? (optioneel)';
  const feedVerstuur = document.createElement('button');
  feedVerstuur.className = 'feedverstuur';
  feedVerstuur.textContent = 'Feedback versturen';
  feedVerstuur.addEventListener('click', verstuurFeedback);
  feedBody.appendChild(duimen);
  feedBody.appendChild(feedTxt);
  feedBody.appendChild(feedVerstuur);
  fb.appendChild(feedTop);
  fb.appendChild(feedBody);
  r.appendChild(fb);
}

export async function deelMetOuders(knop) {
  const emailEl = document.getElementById('oudermail');
  const email = emailEl.value.trim();
  if (!email || !email.includes('@')) {
    emailEl.style.borderColor = '#c00';
    emailEl.focus();
    return;
  }
  emailEl.style.borderColor = '';

  const naam  = document.getElementById('naam').value.trim();
  const groep = document.getElementById('groep').value;
  const leerlingId = window._huidigeLeerlingId;
  if (!leerlingId) { alert('Selecteer eerst een leerling.'); return; }

  // Bouw HTML en platte tekst uit het rapportresultaat
  const resultaatEl = document.getElementById('resultaat');
  const rapportHtml = resultaatEl ? resultaatEl.innerHTML : '';
  const rapportTekst = resultaatEl ? resultaatEl.innerText : '';

  const origTekst = knop.textContent;
  knop.textContent = 'Versturen...';
  knop.disabled = true;

  try {
    const res = await apiCall('/ouder/verstuur', {method: 'POST',
      body: JSON.stringify({
        leerling_id: leerlingId,
        ouder_email: email,
        onderwerp: `Rapport ${naam || 'uw kind'}${groep ? ' (groep ' + groep + ')' : ''}`,
        inhoud_html: rapportHtml,
        inhoud_tekst: rapportTekst,
        bericht_type: 'rapport'})
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Versturen mislukt');

    const ok = document.getElementById('deelok');
    ok.textContent = '✓ Rapport verstuurd naar ' + email;
    ok.style.display = 'block';
    ok.style.color = 'var(--gm)';
    knop.textContent = 'Verstuurd ✓';

    // Ververs de communicatiegeschiedenis als die open is
    if (typeof ouderBerichtenVervers === 'function') ouderBerichtenVervers(leerlingId);

  } catch(e) {
    const ok = document.getElementById('deelok');
    ok.textContent = '✗ ' + e.message;
    ok.style.display = 'block';
    ok.style.color = '#c00';
    knop.textContent = origTekst;
    knop.disabled = false;
  }
}

export async function ouderBerichtenLaad(leerlingId) {
  if (!huidigToken || !leerlingId) return [];
  try {
    const res = await apiCall('/ouder/berichten/' + leerlingId);
    if (!res.ok) return [];
    const data = await res.json();
    ouderBerichtenCache[leerlingId] = data;
    return data;
  } catch(e) { return []; }
}

export async function ouderBerichtenVervers(leerlingId) {
  const data = await ouderBerichtenLaad(leerlingId);
  const container = document.getElementById('ouder-geschiedenis-' + leerlingId);
  if (!container) return;
  ouderBerichtenRender(container, data);
}

export function ouderBerichtenRender(container, berichten) {
  if (!berichten.length) {
    container.innerHTML = '<div style="font-size:12px;color:var(--tz);padding:8px 0">Nog geen berichten verstuurd.</div>';
    return;
  }
  container.innerHTML = berichten.map(b => {
    const datum = new Date(b.verstuurd_op).toLocaleDateString('nl-NL', {day:'numeric',month:'short',year:'numeric'});
    const typeIcoon = { rapport:'📄', handelingsplan:'📋', opp:'📝', algemeen:'✉️' }[b.bericht_type] || '✉️';
    return '<div style="padding:8px 0;border-bottom:0.5px solid var(--rand);font-size:12px">'
      + '<div style="display:flex;align-items:center;gap:6px">'
      + '<span>' + typeIcoon + '</span>'
      + '<span style="font-weight:500;flex:1">' + (b.onderwerp || 'Bericht') + '</span>'
      + '<span style="color:var(--tz);font-size:11px">' + datum + '</span>'
      + '</div>'
      + '<div style="color:var(--tz);margin-top:2px">Naar: ' + b.ouder_email + '</div>'
      + '<div style="color:var(--tz);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (b.inhoud_tekst || '').slice(0, 80) + '…</div>'
      + '</div>';
  }).join('');
}


export async function genereer() {
  const naam     = document.getElementById('naam').value.trim();
  const groep    = document.getElementById('groep').value;
  const notities = document.getElementById('notities').value.trim();
  // Notities zijn optioneel — data uit DB wordt altijd meegenomen

  document.getElementById('spin').style.display = 'block';
  document.getElementById('ktekst').textContent = 'Verwerken...';

  document.getElementById('gknop').disabled = true;

  const behoeftes = getGeselecteerdeBehoeftes();
  const groepType = parseInt(groep) <= 3 ? 'onderbouw (groep 1-3)' :
                   parseInt(groep) <= 6 ? 'middenbouw (groep 4-6)' :
                   groep ? 'bovenbouw (groep 7-8)' : '';

  const ondersteuningContext = behoeftes.length > 0
    ? '\nOndersteuningsbehoeftes: ' + behoeftes.join(', ') + '.\nVul de sleutel "ondersteuning" in als een ARRAY van objecten, één per behoefte: [{"behoefte":"naam","praktijk":"hoe dit er voor dit kind concreet uitziet in gewone taal voor ouders","thuistip":"één concrete tip voor thuis"}]'
    : '';

  // Haal bestaande LVS-scores op als context voor Claude (alleen lezen, niet terugschrijven)
  let lvsContextTekst = '';
  if (window._huidigeLeerlingId) {
    try {
      const lvsRes = await apiCall('/lvs/' + window._huidigeLeerlingId);
      if (lvsRes.ok) {
        const lvsProfiel = await lvsRes.json();
        const scores = lvsProfiel.scores || {};
        const gevuldeScores = LVS_DOMEINEN
          .filter(d => scores[d.k] && scores[d.k] > 0)
          .map(d => d.l + ': ' + scores[d.k])
          .join(', ');
        if (gevuldeScores) {
          lvsContextTekst = '\nBekende LVS-scores (ter informatie, niet aanpassen): ' + gevuldeScores + '.';
        }
      }
    } catch(e) { /* stil falen — context is optioneel */ }
  }

  // Haal opgeslagen notities op uit de database
  let dbNotitiesTekst = '';
  if (window._huidigeLeerlingId) {
    try {
      const notRes = await apiCall('/leerlingen/' + window._huidigeLeerlingId + '/notities');
      if (notRes.ok) {
        const notData = await notRes.json();
        const items = (notData.notities || notData || []).slice(0, 8);
        if (items.length) {
          dbNotitiesTekst = '\nEerdere observaties (chronologisch):\n'
            + items.map(function(n) {
                const datum = (n.aangemaakt_op || '').slice(0,10);
                return '- [' + datum + '] ' + (n.tekst || '').slice(0,200);
              }).join('\n');
        }
      }
    } catch(e) { /* stil falen */ }
  }

  const prompt = (naam ? 'Leerling: ' + naam + (groep ? ', groep ' + groep : '') + '.' : '')
    + (groepType ? ' Dit kind zit in de ' + groepType + '.' : '')
    + ondersteuningContext
    + lvsContextTekst
    + dbNotitiesTekst
    + (notities ? '\nAanvullende notities leerkracht:\n"' + notities + '"' : '')
    + '\n\nRetourneer ALLEEN een geldig JSON-object met deze sleutels:'
    + '\n{'
    + '"leerresultaten":null,'
    + '"werkhouding":null,'
    + '"sociaal_emotioneel":null,'
    + '"aandachtspunten":null,'
    + '"doelen":null,'
    + '"positieve_punten":null,'
    + '"ondersteuning":null,'
    + '"rapportcommentaar":null,'

    + '}'

  // Toon streaming indicator
  verbergResultaten();
  const r = document.getElementById('resultaat');
  const leeg = document.getElementById('leeg');
  if (leeg) leeg.style.display = 'none';
  const streamDiv = document.createElement('div');
  streamDiv.style.cssText = 'padding:20px;font-size:13px;color:var(--tz);line-height:1.8;white-space:pre-wrap;font-family:inherit';
  streamDiv.textContent = 'Rapport wordt gegenereerd…';
  r.appendChild(streamDiv);

  try {
    const tekst = await roepAPIaanStream(prompt, (chunk) => {
      // Toon geen tussentijdse foutobjecten als rapporttekst
      if (!chunk.trimStart().startsWith('{"error"')) {
        streamDiv.textContent = chunk;
      }
    });
    const schoon = tekst.replace(/```json|```/g, '').trim();
    const parsed = JSON.parse(schoon);
    huidigeSectieData = parsed;
    toonResultaten(parsed, naam, groep);
    registreerRapport();
    if (window._huidigeLeerlingId) slaRapportOp(parsed);
    // Werk LVS bij met scores en tijdlijn uit het rapport
    await werkLvsVanuitRapportBij(parsed, naam, groep);
  } catch (err) {
    if (r) r.innerHTML = '<div class="fout">' + err.message + '</div>';
  } finally {
    const spin = document.getElementById('spin');
    const ktekst = document.getElementById('ktekst');
    const gknop = document.getElementById('gknop');
    if (spin) spin.style.display = 'none';
    if (ktekst) ktekst.textContent = 'Produceer rapport';
    if (gknop) gknop.disabled = false;
  }
}

// ── Score helpers ────────────────────────────────────────────
const LABEL_MAP = {
  95: { tekst: 'Uitstekend', kleur: '#222', bg: '#f5f5f5' },
  80: { tekst: 'Goed',       kleur: '#222', bg: '#f5f5f5' },
  60: { tekst: 'Voldoende',  kleur: '#333', bg: '#f5f5f5' },
  40: { tekst: 'Matig',      kleur: '#222', bg: '#f5f5f5' },
  20: { tekst: 'Onvoldoende',kleur: '#222', bg: '#f5f5f5' },
};

// Zet numerieke score om naar CITO-niveau letter
function scoreNaarCITO(score) {
  if (score === null || score === undefined) return null;
  if (score >= 90) return { niveau: 'A', kleur: '#222', bg: '#f5f5f5', hoogte: 90 };
  if (score >= 75) return { niveau: 'B', kleur: '#222', bg: '#f5f5f5', hoogte: 75 };
  if (score >= 55) return { niveau: 'C', kleur: '#333', bg: '#f5f5f5', hoogte: 55 };
  if (score >= 35) return { niveau: 'D', kleur: '#444', bg: '#f5f5f5', hoogte: 35 };
  return                 { niveau: 'E', kleur: '#222', bg: '#f5f5f5', hoogte: 18 };
}

// Label (sociaal/werkhouding) op basis van score-waarde
function scoreNaarLabel(score) {
  if (!score) return null;
  const s = parseInt(score);
  // Zoek dichtstbijzijnde waarde
  const nrs = Object.keys(LABEL_MAP).map(Number);
  const dichtstbij = nrs.reduce((a, b) => Math.abs(b - s) < Math.abs(a - s) ? b : a);
  return LABEL_MAP[dichtstbij];
}

// Bouw progressie-sectie op basis van LVS-profielen (alle rapporten)
function bouwProgressieSectie(naam) {
  // Haal alle opgeslagen rapporten op uit lvsProfielen cache
  const profielData = lvsHuidig ? lvsProfielen[lvsHuidig.id] : null;
  const tijdlijn = profielData?.tijdlijn || [];

  // Zoek rapport-items in tijdlijn
  const rapportItems = tijdlijn.filter(i => i.type === 'rapport').slice(0, 3);

  // Huidige scores
  const huidigScores = profielData?.scores || {};

  return `
    <div style="background:var(--warm);border:0.5px solid var(--warmrand);border-radius:7px;overflow:hidden">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:0.5px solid var(--warmrand)">
        <div style="font-size:10px;font-weight:500;letter-spacing:.06em;color:var(--tz);text-transform:uppercase">Progressie per rapportageperiode</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          ${LVS_DOMEINEN.map(d => `<span style="display:flex;align-items:center;gap:3px;font-size:10px;color:var(--tm)"><span style="width:8px;height:8px;border-radius:7px;background:${d.kleur};display:inline-block"></span>${d.l}</span>`).join('')}
        </div>
      </div>
      <div style="padding:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
        ${bouwPeriodeBlok('Rapport 1', rapportItems[2] || null, huidigScores, false)}
        ${bouwPeriodeBlok('Rapport 2', rapportItems[1] || null, huidigScores, false)}
        ${bouwPeriodeBlok('Rapport 3 — huidig', null, huidigScores, true)}
      </div>
    </div>`;
}

function bouwPeriodeBlok(label, tijdlijnItem, huidigScores, isHuidig) {
  const scores = isHuidig ? huidigScores : (tijdlijnItem?.scores || null);
  const datum = isHuidig
    ? new Date().toLocaleDateString('nl-NL', {month:'long', year:'numeric'})
    : (tijdlijnItem?.datum || null);

  const rand = isHuidig ? 'border:1.5px solid #4a7a4a' : 'border:0.5px solid var(--warmrand)';
  const subKleur = isHuidig ? 'color:#3a5a3a;font-weight:500' : 'color:var(--tz)';

  if (!scores && !isHuidig) {
    return `<div style="background:#fff;${rand};border-radius:7px;padding:10px 12px;opacity:.5">
      <div style="font-size:11px;font-weight:500;color:var(--t);margin-bottom:2px">${label}</div>
      <div style="font-size:10px;color:var(--tz);margin-bottom:10px">Nog niet beschikbaar</div>
      <div style="height:80px;display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--tz);font-style:italic">—</div>
    </div>`;
  }

  const citoVakken = LVS_DOMEINEN.filter(d => d.type === 'cito');
  const labelVakken = LVS_DOMEINEN.filter(d => d.type === 'label');

  const staven = citoVakken.map(d => {
    const s = scores ? scores[d.k] : null;
    const cito = s ? scoreNaarCITO(s) : null;
    const hoogte = cito ? cito.hoogte * 0.8 : 4;
    const kleur = cito ? cito.kleur : 'var(--rand)';
    const niveau = cito ? cito.niveau : '—';
    return `<div style="display:flex;flex-direction:column;align-items:center;gap:3px;flex:1">
      <div style="font-size:9px;font-weight:500;color:${cito ? cito.kleur : 'var(--tz)'}">${niveau}</div>
      <div style="width:100%;height:${hoogte}px;background:${d.kleur};border-radius:7px;min-height:3px"></div>
      <div style="font-size:9px;color:var(--tz);text-align:center">${d.l.slice(0,3)}</div>
    </div>`;
  }).join('');

  const badges = labelVakken.map(d => {
    const s = scores ? scores[d.k] : null;
    const lbl = s ? scoreNaarLabel(s) : null;
    return `<div style="display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:10px;color:var(--tz)">${d.l}</span>
      ${lbl
        ? `<span style="font-size:10px;font-weight:500;color:${lbl.kleur};background:${lbl.bg};border-radius:7px;padding:1px 7px">${lbl.tekst}</span>`
        : `<span style="font-size:10px;color:var(--tz)">—</span>`}
    </div>`;
  }).join('');

  return `<div style="background:#fff;${rand};border-radius:7px;padding:10px 12px">
    <div style="font-size:11px;font-weight:500;color:var(--t);margin-bottom:2px">${label}</div>
    <div style="font-size:10px;${subKleur};margin-bottom:10px">${datum || '—'}</div>
    <div style="display:flex;align-items:flex-end;gap:3px;height:80px;margin-bottom:8px">${staven}</div>
    <div style="border-top:0.5px solid var(--warmrand);padding-top:7px;display:flex;flex-direction:column;gap:4px">${badges}</div>
  </div>`;
}

function toonResultaten(data, naam, groep) {
  const r = document.getElementById('resultaat');
  r.innerHTML = '';
  const leegEl = document.getElementById('leeg');
  if (leegEl) leegEl.style.display = 'none';

  // ── Rapport header ─────────────────────────────────────────
  const datum = new Date().toLocaleDateString('nl-NL', {day:'numeric',month:'long',year:'numeric'});
  const header = document.createElement('div');
  header.style.cssText = 'background:#fff;border:1px solid var(--warmrand);border-radius:7px;padding:16px 18px;margin-bottom:8px';
  header.innerHTML = `
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <div>
        <div style="font-family:Georgia,serif;font-size:20px;font-weight:600;color:var(--t);margin-bottom:3px">
          ${naam || 'Rapport'}${groep ? ' <span style="font-size:14px;font-weight:400;color:var(--tz)">— groep ' + groep + '</span>' : ''}
        </div>
        <div style="font-size:11px;color:var(--tz);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span>Gegenereerd op ${datum}</span>
          <span style="display:inline-flex;align-items:center;gap:3px;color:#222;background:#f5f5f5;border:0.5px solid #ddd;border-radius:6px;padding:2px 7px;font-weight:500">
            🛡 AVG-gecheckt — naam geanonimiseerd verzonden
          </span>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button onclick="allesKopierenRapport()" style="display:flex;align-items:center;gap:5px;background:var(--grijs);border:1px solid var(--rand);border-radius:6px;padding:6px 12px;font-size:11px;font-weight:500;cursor:pointer;font-family:inherit;color:var(--tm);white-space:nowrap">
          📋 Alles kopiëren
        </button>
        <button onclick="downloadPDF()" style="display:flex;align-items:center;gap:5px;background:var(--grijs);border:1px solid var(--rand);border-radius:6px;padding:6px 12px;font-size:11px;font-weight:500;cursor:pointer;font-family:inherit;color:var(--tm);white-space:nowrap">
          ⬇ PDF
        </button>
        ${window._huidigeLeerlingId ? `<button onclick="slaRapportOpVanuitHeader()" style="display:flex;align-items:center;gap:5px;background:var(--gm);border:none;border-radius:6px;padding:6px 12px;font-size:11px;font-weight:600;cursor:pointer;font-family:inherit;color:#fff;white-space:nowrap">
          💾 Opslaan
        </button>` : ''}
      </div>
    </div>`;
  r.appendChild(header);

  SECTIES.forEach((s, i) => {
    const w = data[s.k];
    if (!w) return;
    const kaart = document.createElement('div');

    if (s.k === 'ondersteuning') {
      if (!w || w === 'null') return;
      const kaart = document.createElement('div');
      kaart.className = 'kaart tp';
      let tekst = '';
      if (Array.isArray(w)) {
        tekst = w.map(item => `<strong>${item.behoefte || ''}</strong>: ${item.praktijk || item.interventie || ''}`).join('<br><br>');
      } else if (typeof w === 'string') {
        tekst = w.replace(/\n/g,'<br>');
      } else {
        tekst = JSON.stringify(w);
      }
      const oid = 'o' + Date.now();
      kaart.innerHTML = '<div class="ktop"><div class="dot"></div><div class="knaam">ONDERSTEUNING IN DE PRAKTIJK</div><span class="mtag">Ondersteuning</span><button class="kknop" onclick="kopieer(\'' + oid + '\',this)">Kopieer</button></div><div class="kbody" id="' + oid + '" style="font-size:12px">' + tekst + '</div>';
      r.appendChild(kaart);
      return;
    }
    if (s.k === 'rapportcommentaar') {
      const kid = 'rt' + Date.now();
      kaart.className = 'rblok';
      kaart.innerHTML = `
        <div class="rtop">
          <span class="rtitel">Rapportcommentaar — klaar voor ParnasSys</span>
          <button class="kknop" onclick="kopieer('${kid}',this)">Kopieer</button>
        </div>
        <div class="rtext" id="${kid}">${w}</div>`;
      r.appendChild(kaart); return;
    }

    const kid2 = 'k' + Date.now() + Math.random().toString(36).slice(2);
    kaart.className = `kaart ${s.c}`;
    kaart.innerHTML = `
      <div class="ktop"><div class="dot"></div>
      <div class="knaam">${s.l.toUpperCase()}</div>
      <span class="mtag">${s.p}</span>
      <button class="kknop" onclick="kopieer('${kid2}',this)">Kopieer</button></div>
      <div class="kbody" id="${kid2}">${w}</div>`;
    r.appendChild(kaart);
  });

  // PDF downloadknop
  const pdfKnop = document.createElement('button');
  pdfKnop.className = 'genknop';
  pdfKnop.style.cssText = 'background:#222;box-shadow:none;margin-top:4px';
  pdfKnop.innerHTML = '&#000; Download als PDF';
  pdfKnop.onclick = downloadPDF;
  r.appendChild(pdfKnop);

  // Pedagogisch advies
  const ped = document.createElement('div');
  ped.className = 'pedblok';
  ped.innerHTML = `
    <div class="pedtop">
      <div><div class="pedtitel">Pedagogisch advies</div><div class="pedsub">Alleen zichtbaar voor de leerkracht</div></div>
      <button class="pedknop" id="pedknop" onclick="laadPedagogisch()">Analyseer</button>
    </div>
    <div class="pedbody" id="pedbody"><div class="pedwacht">Klik op "Analyseer" voor advies op basis van 5 wetenschappelijke theorieën.</div></div>`;
  r.appendChild(ped);

  // Progressie staafdiagrammen
  const progEl = document.createElement('div');
  progEl.innerHTML = bouwProgressieSectie(naam);
  r.appendChild(progEl.firstElementChild);

  // Delen met ouders
  const ouder = document.createElement('div');
  ouder.className = 'ouderblok';
  ouder.innerHTML = `
    <div class="oudertop">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="#222" stroke-width="1.5"><rect x="1" y="3" width="14" height="10" rx="2"/><path d="M1 6l7 4 7-4"/></svg>
      <span class="oudertitel">DELEN MET OUDERS</span>
    </div>
    <div class="ouderbody">
      <div style="font-size:11px;color:var(--tz);line-height:1.5">Het rapport wordt direct per e-mail verstuurd en opgeslagen in de communicatiegeschiedenis.</div>
      <div class="emailrij">
        <input type="text" id="oudermail" placeholder="e-mailadres ouder(s)" class="oudermail-invoer">
        <button class="deelknop" onclick="deelMetOuders(this)">Verstuur</button>
      </div>
      <div class="deelok" id="deelok"></div>
      <div style="margin-top:10px">
        <button onclick="ouderGeschiedenisToggle(this)" style="background:none;border:none;font-size:11px;color:var(--tz);cursor:pointer;padding:0;text-decoration:underline">Communicatiegeschiedenis tonen</button>
        <div id="ouder-geschiedenis-container" style="display:none;margin-top:8px"></div>
      </div>
    </div>`;
  r.appendChild(ouder);

  // Feedback
  const fb = document.createElement('div');
  fb.className = 'feedblok';
  fb.style.display = 'block';
  const feedTop = document.createElement('div');
  feedTop.className = 'feedtop';
  feedTop.innerHTML = '<span class="feedtitel">WAS DIT RAPPORT BRUIKBAAR?</span>';
  const feedBody = document.createElement('div');
  feedBody.className = 'feedbody';
  const duimen = document.createElement('div');
  duimen.className = 'feedduimen';
  const btnGoed = document.createElement('button');
  btnGoed.className = 'dknop';
  btnGoed.textContent = '👍 Ja';
  btnGoed.addEventListener('click', function() { selecteerDuim('goed', this); });
  const btnMatig = document.createElement('button');
  btnMatig.className = 'dknop';
  btnMatig.textContent = '👎 Nog niet';
  btnMatig.addEventListener('click', function() { selecteerDuim('matig', this); });
  duimen.appendChild(btnGoed);
  duimen.appendChild(btnMatig);
  const feedTxt = document.createElement('textarea');
  feedTxt.style.cssText = 'height:65px;font-size:11px;border:1px solid var(--rand);border-radius:7px;padding:8px 10px;width:100%;outline:none';
  feedTxt.id = 'feedtekst';
  feedTxt.placeholder = 'Wat kan er beter? (optioneel)';
  const feedVerstuur = document.createElement('button');
  feedVerstuur.className = 'feedverstuur';
  feedVerstuur.textContent = 'Feedback versturen';
  feedVerstuur.addEventListener('click', verstuurFeedback);
  feedBody.appendChild(duimen);
  feedBody.appendChild(feedTxt);
  feedBody.appendChild(feedVerstuur);
  fb.appendChild(feedTop);
  fb.appendChild(feedBody);
  r.appendChild(fb);
}
}

export async function roepAPIaanStream(prompt, onChunk) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  let res;
  try {
    res = await apiCall('/analyseer', {method: 'POST',
      headers: { 'Content-Type': 'application/json', },
      body: JSON.stringify({ prompt}),
      signal: controller.signal
    });
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') throw new Error('Geen antwoord van de server (timeout na 60s).');
    throw new Error('Kan de server niet bereiken: ' + e.message);
  }
  clearTimeout(timer);
  if (!res.ok) {
    let foutTekst = 'Er ging iets mis op de server (status ' + res.status + ').';
    try { const fout = await res.json(); foutTekst = fout.detail || foutTekst; } catch {}
    throw new Error(foutTekst);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let volledig = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    volledig += chunk;
    if (onChunk) onChunk(volledig);
  }
  // Controleer of de server een foutobject heeft gestreamd als platte tekst
  const trimmed = volledig.trim();
  if (trimmed.startsWith('{') && trimmed.includes('"error"')) {
    try {
      const foutObj = JSON.parse(trimmed);
      if (foutObj.error) throw new Error(foutObj.error);
    } catch (e) {
      if (e.message !== 'Unexpected token') throw e;
    }
  }
  return volledig;
}

export async function roepAPIaan(prompt) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  let res;
  try {
    res = await apiCall('/analyseer', {method: 'POST',
      headers: { 'Content-Type': 'application/json', },
      body: JSON.stringify({ prompt}),
      signal: controller.signal
    });
  } catch (e) {
    clearTimeout(timer);
    throw new Error('Kan de server niet bereiken: ' + e.message);
  }
  clearTimeout(timer);
  if (!res.ok) {
    let foutTekst = 'Er ging iets mis (status ' + res.status + ').';
    try { const fout = await res.json(); foutTekst = fout.detail || foutTekst; } catch {}
    throw new Error(foutTekst);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let volledig = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    volledig += decoder.decode(value, { stream: true });
  }
  // Controleer of de server een foutobject heeft gestreamd als platte tekst
  const trimmed = volledig.trim();
  if (trimmed.startsWith('{') && trimmed.includes('"error"')) {
    try {
      const foutObj = JSON.parse(trimmed);
      if (foutObj.error) throw new Error(foutObj.error);
    } catch (e) {
      if (e.message !== 'Unexpected token') throw e;
    }
  }
  return volledig;
}

// ── Delen met ouders via server (Resend) ─────────────────────
