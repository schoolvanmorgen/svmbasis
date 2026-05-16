// ══════════════════════════════════════════════════════════
// config.js — Centrale configuratie voor School van Morgen
// Pas hier URL's en limieten aan — nergens anders.
// ══════════════════════════════════════════════════════════

// Supabase — anon key is by design publiek (Row Level Security beschermt data)
// Zie: https://supabase.com/docs/guides/api/api-keys
export const SUPABASE_URL  = 'https://ruaorbvprxcnnaltyvpi.supabase.co';
export const SUPABASE_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ1YW9yYnZwcnhjbm5hbHR5dnBpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzgxMzgxMjEsImV4cCI6MjA5MzcxNDEyMX0.LPHqXtYQj3-qo8x5BUFLcOvWAPuBc1NkpWe1AyNKElk';

// Backend API — lege string = zelfde server (relatieve URLs)
// Productie: 'https://app.jouwdomein.nl'
export const API_BASE = window.__API_BASE__ || '';

// Limieten
export const MAX_LEERLINGEN_PER_BATCH     = 50;
export const MAX_NOTITIES_IN_CONTEXT      = 10;
export const MAX_RAPPORT_TOKENS           = 600;
export const MAX_OPP_TOKENS               = 1500;
export const MAX_HP_TOKENS                = 1200;
export const RATE_LIMIT_AI_PER_MINUUT     = 30;
export const TOKEN_REFRESH_MARGE_SECONDEN = 300;
export const LVS_TIJDLIJN_LIMIT           = 50;
export const BATCH_RAPPORT_MAX            = 35;
