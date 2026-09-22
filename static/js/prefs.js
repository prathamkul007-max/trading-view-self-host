import { $, state } from './state.js';

const PREFS_KEY = 'orazio.prefs.v1';

export function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      symbol: state.currentSymbol, style: state.currentStyle,
      sma20: $('sma20').checked, sma50: $('sma50').checked, vol: $('vol').checked,
    }));
  } catch (e) { /* storage unavailable (private mode) — preferences just won't persist */ }
}

export function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || 'null') || {}; } catch (e) { return {}; }
}
