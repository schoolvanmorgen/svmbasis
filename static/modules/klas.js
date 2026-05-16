// ══════════════════════════════════════════════════════════
// klas.js — Klas importeren vanuit ParnasSys/CSV/Excel
// Verwerkt bestanden en voegt leerlingen toe aan het systeem
// ══════════════════════════════════════════════════════════
import { apiCall } from './api.js';
import { huidigToken } from './state.js';
import { laadLeerlingen } from './leerlingen.js';
import { _parseCsvRegel, _leesCSVBestand } from './api.js';

export function klasLeesBestand(bestand) {
  if (!bestand) return;
  document.getElementById('klas-bestandsnaam').textContent = bestand.name;
  const ext = bestand.name.split('.').pop().toLowerCase();

  if (ext === 'csv') {
    _leesCSVBestand(bestand, function(tekst) { klasVerwerk(tekst); });
  } else {
    const reader = new FileReader();
    reader.onload = function(e) {
      const wb = XLSX.read(new Uint8Array(e.target.result), {type:'array'});
      const ws = wb.Sheets[wb.SheetNames[0]];
      klasVerwerk(XLSX.utils.sheet_to_csv(ws));
    };
    reader.readAsArrayBuffer(bestand);
  }
}

export function klasVerwerk(csv) {
  const regels = csv.split("\n").filter(function(r) { return r.trim(); });

  if (!regels.length) return;

  const headers = _parseCsvRegel(regels[0]).map(function(h) {
    return h.toLowerCase().replace(/"/g,'').replace(/\r/g,'').replace(/\uFEFF/g,'').trim();

  });

  const idx = {
    voornaam:       headers.findIndex(function(h) { return h.includes('roepnaam') || h.includes('voornaam') || h === 'naam'; }),
    achternaam:     headers.findIndex(function(h) { return h.includes('achternaam') || h.includes('familienaam'); }),
    tussenvoegsel:  headers.findIndex(function(h) { return h.includes('tussenvoegsel') || h.includes('voorvoegsel'); }),
    groep:          headers.findIndex(function(h) { return h.includes('groepsnaam') || h === 'klas' || h === 'groep' || h.includes('klas'); }),
    leerlingnummer: headers.findIndex(function(h) { return h.includes('leerlingnummer') || h.includes('leerlingnr') || h === 'nummer' || h === 'id'; }),
  };

  if (idx.voornaam < 0) {
    const info = document.getElementById('klas-info');
    if (info) { info.textContent = '✗ Voornaam/Roepnaam kolom niet gevonden. Controleer de kolomnamen.'; info.style.background='#fff5f5'; info.style.color='#991b1b'; info.style.borderColor='#fca5a5'; }
    document.getElementById('klas-preview').style.display = 'flex';
    document.getElementById('klas-lijst').innerHTML = '';
    document.getElementById('klas-knop').disabled = true;
    return;
  }

  const leerlingen = [];
  for (var i = 1; i < regels.length; i++) {
    if (!regels[i].trim()) continue;
    const cols = _parseCsvRegel(regels[i]);
    const voornaam = (cols[idx.voornaam] || '').replace(/"/g,'').trim();
    if (!voornaam) continue;
    const groepRuw = idx.groep >= 0 ? (cols[idx.groep] || '').replace(/"/g,'').trim() : '';
    const groepMatch = groepRuw.match(/\d+/);
    leerlingen.push({
      voornaam:       voornaam,
      achternaam:     idx.achternaam >= 0    ? (cols[idx.achternaam] || '').replace(/"/g,'').trim()    : '',
      tussenvoegsel:  idx.tussenvoegsel >= 0 ? (cols[idx.tussenvoegsel] || '').replace(/"/g,'').trim() : '',
      groep:          groepMatch ? groepMatch[0] : '',
      leerlingnummer: idx.leerlingnummer >= 0 ? (cols[idx.leerlingnummer] || '').replace(/"/g,'').trim() : '',
    });
  }

  window._klasLeerlingen = leerlingen;

  // Groepen tellen
  const groepen = {};
  leerlingen.forEach(function(l) { groepen[l.groep||'?'] = (groepen[l.groep||'?']||0)+1; });
  const groepTekst = Object.keys(groepen).sort(function(a,b){return parseInt(a)-parseInt(b);})
    .map(function(g) { return 'Groep ' + g + ' (' + groepen[g] + ')'; }).join(' · ');

  const info = document.getElementById('klas-info');
  if (info) {
    info.style.background='#f0faf0'; info.style.color='#2d6a2d'; info.style.borderColor='#c0e0c0';
    info.textContent = '✓ ' + leerlingen.length + ' leerlingen herkend — ' + groepTekst;
  }

  // Preview tabel
  const lijst = document.getElementById('klas-lijst');
  if (lijst) {
    lijst.innerHTML = leerlingen.slice(0,8).map(function(l) {
      return '<div style="padding:7px 14px;border-bottom:1px solid #f5f5f5;font-size:12px;display:grid;grid-template-columns:1fr 1fr 80px 120px;gap:8px;color:var(--t)">'
        + '<span>' + esc(l.voornaam) + '</span>'
        + '<span>' + [l.tussenvoegsel,l.achternaam].filter(Boolean).join(' ') + '</span>'
        + '<span>' + (l.groep ? l.groep : '—') + '</span>'
        + '<span style="color:var(--tz)">' + (l.leerlingnummer||'—') + '</span>'
        + '</div>';
    }).join('') + (leerlingen.length > 8 ? '<div style="padding:7px 14px;font-size:11px;color:var(--tz)">… en ' + (leerlingen.length-8) + ' meer</div>' : '');
  }

  document.getElementById('klas-preview').style.display = 'flex';
  const knop = document.getElementById('klas-knop');
  if (knop) { knop.disabled = false; knop.style.opacity = '1'; }
}

export async function klasImporteer() {
  const lijst = window._klasLeerlingen || [];
  if (!lijst.length) return;

  const knop      = document.getElementById('klas-knop');
  const voortgang = document.getElementById('klas-voortgang');
  if (knop) { knop.disabled = true; knop.textContent = 'Importeren…'; }
  if (voortgang) voortgang.style.display = 'block';

  let nieuw = 0, dubbel = 0, fout = 0;
  const BATCH = 6;

  for (var i = 0; i < lijst.length; i += BATCH) {
    const batch = lijst.slice(i, i + BATCH);
    await Promise.all(batch.map(async function(l) {
      try {
        const body = { voornaam: l.voornaam, groep: l.groep };
        if (l.achternaam)     body.achternaam     = l.achternaam;
        if (l.tussenvoegsel)  body.tussenvoegsel  = l.tussenvoegsel;
        if (l.leerlingnummer) body.leerlingnummer = l.leerlingnummer;

        const res = await apiCall('/leerlingen', {method: 'POST',
          body: JSON.stringify(body)});
        const data = await res.json();
        if (res.ok) {
          if (data._bestaand) dubbel++; else nieuw++;
        } else { fout++; }
      } catch(e) { fout++; }
    }));

    if (voortgang) {
      voortgang.innerHTML = '<span style="color:#2d6a2d">⏳ ' + Math.min(i+BATCH, lijst.length) + ' / ' + lijst.length + ' verwerkt…</span>';
    }
  }

  // Ververs alle lijsten
  await laadLeerlingen();
  aawLeerlingen = leerlingen;
  if (typeof lvsInitVanuitLeerlingen === 'function') lvsInitVanuitLeerlingen();
  if (typeof lvsRenderLijst === 'function') lvsRenderLijst(lvsLeerlingen);
  if (typeof notitieVulZijbalk === 'function') notitieVulZijbalk();
  if (typeof rapportLaadKlas === 'function') {
    const klasSelect = document.getElementById('rapport-klas');
    if (klasSelect && klasSelect.value) rapportLaadKlas(klasSelect.value);
  }

  if (voortgang) {
    let html = '';
    if (nieuw > 0)   html += '<div style="color:#2d6a2d;font-weight:600">✓ ' + nieuw + ' leerlingen geïmporteerd</div>';
    if (dubbel > 0)  html += '<div style="color:#92400e">⚠ ' + dubbel + ' al aanwezig (overgeslagen)</div>';
    if (fout > 0)    html += '<div style="color:#991b1b">✗ ' + fout + ' mislukt</div>';
    html += '<div style="font-size:11px;color:var(--tz);margin-top:6px">De klas is nu zichtbaar in alle secties.</div>';
    voortgang.innerHTML = html;
  }

  if (knop) { knop.textContent = 'Nog een klas importeren'; knop.disabled = false; knop.onclick = function() {
    window._klasLeerlingen = [];
    document.getElementById('klas-bestandsnaam').textContent = 'Sleep bestand hierheen of klik om te kiezen';
    document.getElementById('klas-preview').style.display = 'none';
    if (voortgang) voortgang.style.display = 'none';
  }; }
}

export function parnassysLeesBestand(bestand) {
  if (!bestand) return;
  document.getElementById('parnassys-bestandsnaam').textContent = bestand.name;
  const ext = bestand.name.split('.').pop().toLowerCase();
  if (ext === 'csv') {
    _leesCSVBestand(bestand, function(tekst) { parnassysVerwerk(tekst, 'csv'); });
  } else {
    const reader = new FileReader();
    reader.onload = function(e) {
      const data = new Uint8Array(e.target.result);
      const wb = XLSX.read(data, {type:'array'});
      const ws = wb.Sheets[wb.SheetNames[0]];
      const csv = XLSX.utils.sheet_to_csv(ws);
      parnassysVerwerk(csv, 'xlsx');
    };
    reader.readAsArrayBuffer(bestand);
  }
}

export function parnassysVerwerk(csv, type) {
  const regels = csv.split('\n').filter(r => r.trim());
  if (!regels.length) return;

  const headers = _parseCsvRegel(regels[0]).map(h => h.toLowerCase().replace(/"/g,'').replace(/\r/g,'').replace(/\uFEFF/g,'').trim());

  // Kolom-detectie: breed opgezet voor ParnasSys, Magister en handmatige exports
  const idx = {
    voornaam:       headers.findIndex(h => h.includes('roepnaam') || h.includes('voornaam') || h === 'naam'),
    achternaam:     headers.findIndex(h => h.includes('achternaam') || h.includes('familienaam')),
    tussenvoegsel:  headers.findIndex(h => h.includes('tussenvoegsel') || h.includes('voorvoegsel')),
    groep:          headers.findIndex(h => h.includes('groepsnaam') || h === 'klas' || h === 'groep' || h.includes('klas')),
    leerlingnummer: headers.findIndex(h => h.includes('leerlingnummer') || h.includes('leerlingnr') || h === 'nummer' || h === 'id'),
  };

  const leerlingen = [];
  for (let i = 1; i < regels.length; i++) {
    if (!regels[i].trim()) continue;
    const cols = _parseCsvRegel(regels[i]);
    const voornaam = idx.voornaam >= 0 ? (cols[idx.voornaam] || '').replace(/"/g,'').trim() : '';
    if (!voornaam) continue;

    const groepRuw = idx.groep >= 0 ? (cols[idx.groep] || '').replace(/"/g,'').trim() : '';
    // Extraheer groepnummer: "Groep 4", "4a", "4" → "4"
    const groepMatch = groepRuw.match(/\d+/);
    const groep = groepMatch ? groepMatch[0] : '';

    leerlingen.push({
      voornaam:       voornaam,
      achternaam:     idx.achternaam >= 0  ? (cols[idx.achternaam] || '').replace(/"/g,'').trim()  : '',
      tussenvoegsel:  idx.tussenvoegsel >= 0 ? (cols[idx.tussenvoegsel] || '').replace(/"/g,'').trim() : '',
      groep:          groep,
      leerlingnummer: idx.leerlingnummer >= 0 ? (cols[idx.leerlingnummer] || '').replace(/"/g,'').trim() : '',
    });
  }

  window._parnassysLeerlingen = leerlingen;

  const preview = document.getElementById('parnassys-preview');
  const info    = document.getElementById('parnassys-info');
  const lijst   = document.getElementById('parnassys-lijst');
  const knop    = document.getElementById('parnassys-knop');

  if (!leerlingen.length) {
    if (info) info.textContent = 'Geen leerlingen herkend. Controleer de kolomnamen.';
    if (info) info.style.color = '#991b1b';
    if (preview) preview.style.display = 'flex';
    return;
  }

  // Groepeer voor overzicht
  const groepen = {};
  leerlingen.forEach(l => {
    const g = l.groep || '?';
    groepen[g] = (groepen[g] || 0) + 1;
  });
  const groepTekst = Object.entries(groepen).sort((a,b) => parseInt(a[0])-parseInt(b[0]))
    .map(([g,n]) => 'Groep ' + g + ' (' + n + ')')
    .join(' · ');

  if (preview) preview.style.display = 'flex';
  if (info) {
    info.style.color = '';
    info.innerHTML = '<strong>' + leerlingen.length + ' leerlingen herkend</strong>'
      + (idx.leerlingnummer >= 0 ? ' · met leerlingnummer' : ' · zonder leerlingnummer')
      + '<br><span style="color:#888;font-size:11px">' + groepTekst + '</span>';
  }
  if (lijst) lijst.innerHTML = leerlingen.slice(0,6).map(function(l) {
    const vollenaam = [l.voornaam, l.tussenvoegsel, l.achternaam].filter(Boolean).join(' ');
    return '<div style="padding:4px 8px;border-bottom:1px solid #eee;font-size:12px;display:flex;justify-content:space-between">'
      + '<span>' + vollenaam + '</span>'
      + '<span style="color:#888">' + (l.groep ? 'Groep ' + l.groep : '—') + (l.leerlingnummer ? ' · #' + l.leerlingnummer : '') + '</span>'
      + '</div>';
  }).join('') + (leerlingen.length > 6
    ? '<div style="padding:4px 8px;color:#888;font-size:11px">en ' + (leerlingen.length-6) + ' meer…</div>'
    : '');
  if (knop) { knop.disabled = false; knop.style.opacity = '1'; }
}

export async function parnassysImporteer() {
  const lijst = window._parnassysLeerlingen || [];
  if (!lijst.length) return;

  const knop     = document.getElementById('parnassys-knop');
  const voortgang = document.getElementById('parnassys-voortgang');
  if (knop) { knop.disabled = true; knop.style.opacity = '.5'; }
  if (voortgang) { voortgang.style.display = 'block'; voortgang.style.color = '#3a5a3a'; }

  let gedaan = 0;
  let nieuw  = 0;
  let dubbel = 0;
  let fout   = 0;

  // Importeer in batches van 8 parallel voor snelheid
  const BATCH = 8;
  for (let start = 0; start < lijst.length; start += BATCH) {
    const batch = lijst.slice(start, start + BATCH);
    const resultaten = await Promise.allSettled(batch.map(function(l) {
      const body = {
        voornaam: l.voornaam,
        groep:    l.groep || '',
      };
      if (l.achternaam)     body.achternaam     = l.achternaam;
      if (l.tussenvoegsel)  body.tussenvoegsel  = l.tussenvoegsel;
      if (l.leerlingnummer) body.leerlingnummer = l.leerlingnummer;
      return fetch(API_BASE + '/leerlingen', {
        method:  'POST',
        headers: { 'Authorization': 'Bearer ' + huidigToken, 'Content-Type': 'application/json' },
        body:    JSON.stringify(body)
      }).then(function(res) { return res.json(); });
    }));

    resultaten.forEach(function(r) {
      gedaan++;
      if (r.status === 'fulfilled') {
        if (r.value._bestaand) dubbel++; else nieuw++;
      } else {
        fout++;
      }
    });

    if (voortgang) {
      voortgang.textContent = gedaan + ' / ' + lijst.length + ' verwerkt'
        + (dubbel ? ' · ' + dubbel + ' al bekend' : '')
        + (fout   ? ' · ' + fout   + ' mislukt'   : '') + '…';
    }
  }

  await laadLeerlingen();

  // Ververs alle lijsten direct — niet wachten op modal sluiten
  aawLeerlingen = leerlingen;
  if (typeof lvsInitVanuitLeerlingen === 'function') lvsInitVanuitLeerlingen();
  if (typeof lvsRenderLijst === 'function') lvsRenderLijst(lvsLeerlingen);
  if (typeof notitieVulZijbalk === 'function') notitieVulZijbalk();
  if (typeof toonLeerlingenLijst === 'function') toonLeerlingenLijst();

  if (voortgang) {
    voortgang.style.color = fout > 0 ? '#92400e' : '#2d6a2d';
    voortgang.innerHTML = '✓ <strong>' + nieuw + ' nieuwe leerlingen</strong> geïmporteerd'
      + (dubbel ? ' · ' + dubbel + ' al aanwezig (overgeslagen)' : '')
      + (fout   ? ' · ' + fout   + ' mislukt' : '') + '.';
  }

  if (fout === 0) setTimeout(sluitLeerlingModal, 1800);
  else if (knop) { knop.disabled = false; knop.style.opacity = '1'; knop.textContent = 'Opnieuw proberen'; }
}

