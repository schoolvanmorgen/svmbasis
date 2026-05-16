// ══════════════════════════════════════════════════════════
// api.js — Centrale HTTP laag
// Alle communicatie met de backend gaat via apiCall().
// XSS-sanitization via esc().
// ══════════════════════════════════════════════════════════
import { API_BASE } from './config.js';
import { uitloggen } from './auth.js';

export function esc(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;')
    .replace(/[/]/g, '&#x2F;');
}

export async function apiCall(url, options = {}

export function _parseCsvRegel(regel) {
  // Verwerkt CSV-regels correct, ook met quotes en komma's in velden
  const velden = [];
  let huidig = '';
  let inQuote = false;
  for (let i = 0; i < regel.length; i++) {
    const c = regel[i];
    if (c === '"') {
      inQuote = !inQuote;
    } else if (c === ',' && !inQuote) {
      velden.push(huidig.trim());
      huidig = '';
    } else {
      huidig += c;
    }
  }
  velden.push(huidig.trim());
  return velden;
}

export function _leesCSVBestand(bestand, callback) {
  const reader = new FileReader();
  reader.onload = function(e) {
    const buffer = e.target.result;
    const bytes  = new Uint8Array(buffer);
    let start = 0;
    if (bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) start = 3;
    let tekst;
    try {
      tekst = new TextDecoder("utf-8", {fatal: true}).decode(buffer.slice(start));
    } catch(_enc) {
      tekst = new TextDecoder("windows-1252").decode(buffer.slice(start));
    }
    // Normaliseer regeleindes naar LF
    tekst = tekst.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    callback(tekst);
  };
  reader.readAsArrayBuffer(bestand);
}

