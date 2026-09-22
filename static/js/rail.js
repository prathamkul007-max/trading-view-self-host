// Left index rail: quick-access index list, grouped by region.
import { $, esc, state } from './state.js';
import { toast } from './ui.js';
import { switchSymbol } from './symbol.js';

const symbolInput = $('symbol-input');

const INDEX_META = {
  NIFTY: ['Nifty 50', 'India'], BANKNIFTY: ['Bank Nifty', 'India'], NIFTYIT: ['Nifty IT', 'India'], SENSEX: ['BSE Sensex', 'India'],
  SPX: ['S&P 500', 'US'], NASDAQ: ['Nasdaq Composite', 'US'], DOWJONES: ['Dow Jones', 'US'],
  KOSPI: ['KOSPI', 'Asia'], TAIEX: ['Taiwan Weighted', 'Asia'], CHINA: ['CSI 300', 'Asia'], SSE: ['Shanghai Composite', 'Asia'],
};
const RAIL_GROUPS = ['India', 'US', 'Asia', 'Other'];

export function highlightRail() {
  // While on ES=F the rail should still show SPX as the active index.
  const active = (state.CONFIG.futuresMeta[state.currentSymbol] || {}).alias || state.currentSymbol;
  document.querySelectorAll('.rail-item').forEach(b => {
    b.setAttribute('aria-current', String(b.dataset.sym === active));
  });
}

export async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) throw new Error();
    state.CONFIG = await res.json();
  } catch (e) {
    toast('Couldn\'t load the index list — search still works', 'error');
    return;
  }
  const byGroup = Object.fromEntries(RAIL_GROUPS.map(g => [g, []]));
  for (const sym of state.CONFIG.quickIndices) byGroup[(INDEX_META[sym] || [null, 'Other'])[1]].push(sym);
  $('rail-groups').innerHTML = RAIL_GROUPS.filter(g => byGroup[g].length).map(g =>
    `<div class="rail-group"><div class="rail-title">${g}</div>${byGroup[g].map(sym =>
      `<button type="button" class="rail-item" data-sym="${esc(sym)}"><span class="rail-sym">${esc(sym)}</span><span class="rail-name">${esc((INDEX_META[sym] || [sym])[0])}</span></button>`
    ).join('')}</div>`).join('');
  $('rail-groups').addEventListener('click', (e) => {
    const btn = e.target.closest('.rail-item');
    if (!btn) return;
    symbolInput.value = btn.dataset.sym;
    switchSymbol(btn.dataset.sym);
  });
  highlightRail();
}
