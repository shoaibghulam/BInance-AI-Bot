/* ============================================================
   The Trader — pro tabbed terminal client
   Vanilla JS, no deps. Wired to docs/api-contract.md (global)
   and docs/architecture/v2-design.md (per-bot scanner/indicators/ml).
   Global WS frames:  {type, data, ts}, type ∈ status|account|positions|bots|log|equity
   Bot WS frames:     scanner|indicators|ml  → data carries { bot, ... }
   Client announces active bot via { type:"subscribe", bot:"<id>" }
   ============================================================ */
'use strict';

/* ---------- constants ---------- */
const POLL_MS = 4000;            // REST fallback / active-tab detail cadence
const WS_MAX_BACKOFF = 15000;
const EQUITY_MAX_POINTS = 120;
const LOG_MAX_LINES = 200;
const LIQ_PROXIMITY = 0.10;
const MIN_TRADES_TO_TRAIN = 50;
const MARGIN_RISK_CUTOFFS = { warn: 50, danger: 80 };
/* stable per-segment palette for the rebalancing donut (BOT_META colors + accents) */
const REBAL_PALETTE = ['#3b82f6', '#a855f7', '#14b8a6', '#f5a623', '#ec4899', '#36d39a', '#f0a020', '#5e6b7d'];

/* bot ids used by v2 are the strategy names */
const BOT_IDS = ['day-trading', 'scalping', 'grid', 'dca', 'rebalancing'];

const BOT_TYPES = {
  day_trading: { label: 'Day Trading', color: '#3b82f6' },
  scalping:    { label: 'Scalping',    color: '#a855f7' },
  grid:        { label: 'Grid',        color: '#14b8a6' },
  dca:         { label: 'DCA',         color: '#f5a623' },
  rebalancing: { label: 'Rebalancing', color: '#ec4899' },
};
/* per bot-id meta (v2 uses id == strategy name) */
const BOT_META = {
  'day-trading': { label: 'Day Trading', color: '#3b82f6',
    desc: 'Session-gated directional swings inside London/US windows; flat by session end to dodge the overnight gap. Pullback-to-trend entries confirmed by RSI, MACD and a VWAP reclaim.',
    indicators: ['EMA50/200', 'RSI14', 'MACD', 'VWAP', 'ATR14'] },
  'scalping': { label: 'Scalping', color: '#a855f7',
    desc: 'Very-high-frequency 1–5m trades with tight stops and small targets. Bollinger mean-reversion / VWAP momentum on liquid symbols. Fees and slippage are existential at this turnover.',
    indicators: ['BB %B', 'RSI14', 'VWAP', 'MACD', 'ATR14'] },
  'grid': { label: 'Grid', color: '#14b8a6',
    desc: 'Ladder of buy-low / sell-high limit orders across a range; profits from oscillation. A sustained trend out of range drives the accumulated position toward liquidation — isolated margin caps the blast radius.',
    indicators: ['Bollinger', 'ATR14', 'VWAP', 'Range bounds'] },
  'dca': { label: 'DCA', color: '#f5a623',
    desc: 'Base order plus a ladder of averaging safety orders that pull break-even toward price. Effectively martingale — high win-rate on small targets at the cost of rare, large losers. Hard SL essential.',
    indicators: ['RSI14', 'Price deviation', 'ATR14'] },
  'rebalancing': { label: 'Rebalancing', color: '#ec4899',
    desc: 'Keeps a basket of futures positions at target weights; trims winners and adds to laggards when drift breaches the band. Harvests the rebalancing premium rather than predicting direction.',
    indicators: ['Weight drift', 'ATR14', 'Realized vol'] },
};

/* ---------- state ---------- */
const state = {
  status: { env: null, connected: false, kill_switch_active: false },
  account: null,
  equity: [],
  prevValues: {},
  botBusy: {},
  wsOpen: false,
  activeTab: 'overview',   // 'overview' | one of BOT_IDS
  activeBot: null,         // bot id when a bot tab is active
  botStatuses: {},         // id -> 'running'|'stopped'|'error' (for sidebar dots)
  botDetail: null,         // last GET /api/bots/{id} payload for active bot
};

/* ---------- DOM helpers ---------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/* ---------- formatting ---------- */
const isNum = (v) => typeof v === 'number' && isFinite(v);
function fmtUSD(v, signed) {
  if (!isNum(v)) return '$0.00';
  const sign = signed && v > 0 ? '+' : v < 0 ? '-' : '';
  const abs = Math.abs(v);
  const s = abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${sign}$${s}`;
}
function fmtPct(v, signed) {
  if (!isNum(v)) return '0.00%';
  const sign = signed && v > 0 ? '+' : '';
  return `${sign}${v.toFixed(2)}%`;
}
function fmtNum(v, dp) {
  if (!isNum(v)) return '—';
  return v.toLocaleString('en-US', { minimumFractionDigits: dp || 0, maximumFractionDigits: dp != null ? dp : 8 });
}
/* price formatter: decimals scale with magnitude so micro-coins (0.0000017)
   are not truncated to 0.00. Never emits scientific notation. */
function fmtPrice(v) {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  const dp = a >= 1000 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 6 : 8;
  return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: dp });
}
/* compact for large 24h volumes */
function fmtCompact(v) {
  if (!isNum(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (a >= 1e3) return (v / 1e3).toFixed(2) + 'K';
  return v.toFixed(2);
}
function fmtProb(v) { return isNum(v) ? (v * 100).toFixed(0) + '%' : '—'; }
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return d.toLocaleTimeString('en-US', { hour12: false });
}
function pnlClass(v) { return isNum(v) && v < 0 ? 'down' : isNum(v) && v > 0 ? 'up' : ''; }

function setWithFlash(node, key, newText, numeric) {
  if (!node) return;
  const prev = state.prevValues[key];
  node.textContent = newText;
  if (numeric != null && prev != null && numeric !== prev) {
    const cls = numeric > prev ? 'flash-up' : 'flash-down';
    node.classList.remove('flash-up', 'flash-down');
    void node.offsetWidth;
    node.classList.add(cls);
  }
  if (numeric != null) state.prevValues[key] = numeric;
}

/* ============================================================
   STATUS / ENV / CONNECTION  (global, all tabs)
   ============================================================ */
function renderStatus(s) {
  state.status = Object.assign({}, state.status, s);
  const env = (s.env || '').toLowerCase();
  const badge = $('#envBadge');
  if (env === 'mainnet') { badge.textContent = 'MAINNET'; badge.className = 'env-badge env-mainnet'; }
  else if (env === 'testnet') { badge.textContent = 'TESTNET'; badge.className = 'env-badge env-testnet'; }
  else { badge.textContent = '—'; badge.className = 'env-badge env-unknown'; }

  $('#serverTime').textContent = s.server_time ? `srv ${fmtTime(s.server_time)}` : '—';
  renderBanners();
  updateKillButton();
}

function updateKillButton() {
  const btn = $('#killBtn');
  if (state.status.kill_switch_active) {
    btn.disabled = true;
    btn.querySelector('.kill-text').textContent = 'HALTED';
  } else {
    btn.disabled = false;
    btn.querySelector('.kill-text').textContent = 'KILL SWITCH';
  }
}

function renderBanners() {
  const wrap = $('#banners');
  wrap.innerHTML = '';

  if (state.status.kill_switch_active) {
    const b = el('div', 'banner banner-danger');
    b.append(
      el('span', 'banner-icon', '⛔'),
      Object.assign(el('span'), { innerHTML: '<strong>KILL SWITCH ACTIVE</strong> — trading halted. All positions flattened and bots stopped.' })
    );
    const reset = el('button', 'banner-action', 'Reset');
    reset.setAttribute('aria-label', 'Reset kill switch and re-enable trading');
    reset.addEventListener('click', resetKill);
    b.append(reset);
    wrap.append(b);
  }

  if (!state.status.connected) {
    const b = el('div', 'banner banner-warn');
    b.append(
      el('span', 'banner-icon', '🔑'),
      Object.assign(el('span'), { innerHTML: '<strong>Not connected</strong> — add Binance testnet keys to <code>.env</code> to enable live data.' })
    );
    wrap.append(b);
  }
}

function setWsState(kind) {
  const dot = $('#wsDot'), label = $('#wsLabel');
  if (kind === 'on')   { dot.className = 'dot dot-on';   label.textContent = 'Live (WS)'; }
  if (kind === 'wait') { dot.className = 'dot dot-wait'; label.textContent = 'Reconnecting…'; }
  if (kind === 'off')  { dot.className = 'dot dot-off';  label.textContent = 'Offline (poll)'; }
}

/* ============================================================
   ACCOUNT CARDS (Overview)
   ============================================================ */
function renderAccount(a) {
  state.account = a || {};
  const get = (k) => (a && isNum(a[k]) ? a[k] : 0);

  const equity = get('equity');
  setWithFlash($('#topEquity'), 'equity', fmtUSD(equity), equity);

  setWithFlash($('#card-balance [data-field]'), 'balance', fmtUSD(get('balance')), get('balance'));
  setWithFlash($('#card-available [data-field]'), 'available', fmtUSD(get('available')), get('available'));

  const up = get('unrealized_pnl');
  const upNode = $('#card-upnl [data-field]');
  upNode.className = `card-value mono ${pnlClass(up)}`;
  setWithFlash(upNode, 'upnl', fmtUSD(up, true), up);
  $('#card-upnl [data-field-foot]').textContent =
    isNum(get('margin_used')) ? `Margin used ${fmtUSD(get('margin_used'))}` : '—';

  const mr = get('margin_ratio');
  const mrPct = mr * 100;
  setWithFlash($('#card-margin [data-field]'), 'mr', fmtPct(mrPct), mrPct);
  const fill = $('#marginFill');
  fill.style.width = `${Math.max(0, Math.min(100, mrPct))}%`;
  if (mrPct >= 80) fill.style.background = 'linear-gradient(90deg, var(--down), #ff6670)';
  else if (mrPct >= 50) fill.style.background = 'linear-gradient(90deg, var(--amber), #ffc24d)';
  else fill.style.background = 'linear-gradient(90deg, var(--up), #36d39a)';

  const dp = get('daily_pnl');
  const dpNode = $('#card-daily [data-field]');
  dpNode.className = `card-value mono ${pnlClass(dp)}`;
  setWithFlash(dpNode, 'daily', fmtUSD(dp, true), dp);
  const dpFoot = $('#card-daily [data-field-foot]');
  dpFoot.className = `card-foot ${pnlClass(get('daily_pnl_pct'))}`;
  dpFoot.textContent = fmtPct(get('daily_pnl_pct'), true);
}

/* ============================================================
   EQUITY SPARKLINE (overview, hand-drawn)
   ============================================================ */
function pushEquity(v) {
  if (!isNum(v)) return;
  state.equity.push(v);
  if (state.equity.length > EQUITY_MAX_POINTS) state.equity.shift();
  drawSparkline();
}

function drawSparkline() {
  const canvas = $('#equityCanvas');
  if (!canvas) return;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 200, 64);

  const pts = state.equity;
  const delta = $('#sparkDelta');
  if (pts.length < 2) { if (delta) delta.textContent = '—'; return; }

  const pad = 4;
  const { yOf } = _curveGeom(pts, cssW, cssH, pad);
  const w = cssW, h = cssH - pad * 2;
  const xStep = w / (pts.length - 1);
  const rising = pts[pts.length - 1] >= pts[0];
  const color = rising ? '#16c784' : '#ea3943';

  ctx.beginPath();
  ctx.moveTo(0, yOf(pts[0]));
  pts.forEach((v, i) => ctx.lineTo(i * xStep, yOf(v)));
  ctx.lineTo(w, cssH); ctx.lineTo(0, cssH); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, cssH);
  grad.addColorStop(0, rising ? 'rgba(22,199,132,0.28)' : 'rgba(234,57,67,0.26)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad; ctx.fill();

  // faint dashed session-open line
  ctx.save();
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = 'rgba(255,255,255,0.18)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, yOf(pts[0])); ctx.lineTo(w, yOf(pts[0])); ctx.stroke();
  ctx.restore();

  ctx.beginPath();
  ctx.moveTo(0, yOf(pts[0]));
  pts.forEach((v, i) => ctx.lineTo(i * xStep, yOf(v)));
  ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.lineJoin = 'round'; ctx.stroke();

  const lx = (pts.length - 1) * xStep, ly = yOf(pts[pts.length - 1]);
  ctx.beginPath(); ctx.arc(lx, ly, 2.5, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.fill();

  _markSparkExtremes(ctx, pts, xStep, yOf);

  if (delta) {
    const d = pts[pts.length - 1] - pts[0];
    const pct = pts[0] ? (d / pts[0]) * 100 : 0;
    delta.textContent = `${d >= 0 ? '▲' : '▼'} ${fmtPct(Math.abs(pct))}`;
    delta.className = `spark-delta mono ${rising ? 'up' : 'down'}`;
  }
}

/* muted dots at the min & max of the equity spark + a max-value label */
function _markSparkExtremes(ctx, pts, xStep, yOf) {
  let iMin = 0, iMax = 0;
  for (let i = 1; i < pts.length; i++) {
    if (pts[i] < pts[iMin]) iMin = i;
    if (pts[i] > pts[iMax]) iMax = i;
  }
  ctx.fillStyle = 'rgba(154,167,184,0.7)';
  for (const i of [iMin, iMax]) {
    ctx.beginPath(); ctx.arc(i * xStep, yOf(pts[i]), 2, 0, Math.PI * 2); ctx.fill();
  }
  ctx.save();
  ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'top';
  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(154,167,184,0.85)';
  ctx.fillText(fmtUSD(pts[iMax]), (pts.length - 1) * xStep, yOf(pts[iMax]) - 12);
  ctx.restore();
}

/* ============================================================
   POSITIONS (Overview)
   ============================================================ */
function renderPositions(list) {
  const body = $('#positionsBody');
  const empty = $('#positionsEmpty');
  // Cache the latest positions so per-bot strips, sort re-renders and liq
  // bars can reuse the same source of truth.
  if (Array.isArray(list)) state.allPositions = list;
  const arr = (state.allPositions || []).slice();
  $('#posCount').textContent = arr.length;

  body.innerHTML = '';
  if (arr.length === 0) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';

  // optional click-to-sort (uPnL / notional, desc)
  if (state.posSort) {
    const key = state.posSort;
    arr.sort((a, b) => (Number(b[key]) || 0) - (Number(a[key]) || 0));
  }

  for (const p of arr) {
    const tr = el('tr');
    const side = (p.side || '').toUpperCase();
    const isLong = side === 'LONG';
    const liqNear = isNum(p.mark_price) && isNum(p.liquidation_price) && p.liquidation_price > 0 &&
      Math.abs(p.mark_price - p.liquidation_price) / p.mark_price <= LIQ_PROXIMITY;
    const upnl = isNum(p.unrealized_pnl) ? p.unrealized_pnl : 0;
    const upnlPct = isNum(p.unrealized_pnl_pct) ? p.unrealized_pnl_pct : 0;

    tr.innerHTML = `
      <td class="sym">${esc(p.symbol || '—')}</td>
      <td><span class="pill ${isLong ? 'pill-long' : 'pill-short'}">${side || '—'}</span></td>
      <td class="num cell-mono">${fmtNum(p.size, undefined)}</td>
      <td class="num cell-mono">${fmtPrice(p.entry_price)}</td>
      <td class="num cell-mono">${fmtPrice(p.mark_price)}</td>
      <td class="num cell-mono liq-cell ${liqNear ? 'liq-warn' : ''}">${fmtPrice(p.liquidation_price)}<canvas class="liq-bar" width="46" height="8"></canvas>${liqNear ? '<span class="liq-flag" title="Mark within 10% of liquidation">⚠</span>' : ''}</td>
      <td class="num cell-mono">${isNum(p.leverage) ? p.leverage + '×' : '—'}</td>
      <td class="num cell-mono upnl-cell"></td>
    `;
    // uPnL cell through setWithFlash (keyed per symbol) so it pulses on change.
    const upnlCell = tr.querySelector('.upnl-cell');
    upnlCell.className = `num cell-mono upnl-cell ${pnlClass(upnl)}`;
    setWithFlash(upnlCell, `pos:${p.symbol}`, '', upnl);
    upnlCell.innerHTML = `${fmtUSD(upnl, true)}<br><span style="font-size:11px;opacity:.8">${fmtPct(upnlPct, true)}</span>`;
    body.append(tr);
    drawLiqBar(tr.querySelector('.liq-bar'), p.mark_price, p.liquidation_price, side);
  }
}

/* ============================================================
   BOTS SUMMARY GRID (Overview) + sidebar dots
   ============================================================ */
let botCache = [];
function setBotCache(list) {
  botCache = Array.isArray(list) ? list : [];
  // track statuses for sidebar dots (key by id)
  for (const b of botCache) {
    if (b && b.id) state.botStatuses[b.id] = (b.status || 'stopped').toLowerCase();
  }
  renderBots(botCache);
  renderFleet();
  updateSidebarDots();
}
function replaceBot(bot) {
  const i = botCache.findIndex((b) => b.id === bot.id);
  if (i >= 0) botCache[i] = bot; else botCache.push(bot);
  if (bot && bot.id) state.botStatuses[bot.id] = (bot.status || 'stopped').toLowerCase();
  renderBots(botCache);
  renderFleet();
  updateSidebarDots();
}

/* ---- Overview fleet strip: one tile per bot (all 5 first-class) ---- */
const FLEET_STATE_LABEL = {
  trading: 'Trading', searching: 'Searching', warming: 'Warming up',
  cooling: 'Cooling down', stopped: 'Stopped', error: 'Halted',
};
function renderFleet() {
  const strip = $('#fleetStrip');
  if (!strip) return;
  const order = BOT_IDS;
  const rows = botCache.slice().sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
  strip.innerHTML = '';
  for (const b of rows) {
    if (!BOT_IDS.includes(b.id)) continue;
    const meta = BOT_META[b.id] || { color: '#5e6b7d', label: b.id };
    const status = (b.status || 'stopped').toLowerCase();
    const activity = (b.activity || (status === 'running' ? 'searching' : status)).toLowerCase();
    const dotCls = status === 'running' ? 'running' : status === 'error' ? 'error' : 'stopped';
    // match live positions for this bot's open symbols (aggregate uPnL).
    const coins = (state.allPositions || []).filter((p) =>
      (b.open_symbols || []).includes(p.symbol));
    const pnl = coins.reduce((s, c) => s + (isNum(c.unrealized_pnl) ? c.unrealized_pnl : 0), 0);

    const tile = el('button', 'fleet-tile');
    tile.dataset.bot = b.id;
    tile.style.borderLeft = `3px solid ${meta.color}`;
    tile.setAttribute('aria-label', `Open ${meta.label} bot`);

    const chipsHtml = coins.slice(0, 2).map((c) =>
      `<span class="coin-chip mini">${esc(c.symbol)}</span>`).join('');
    tile.innerHTML = `
      <div class="ft-head"><span class="nav-dot dot-${dotCls}"></span><span class="ft-name">${esc(meta.label)}</span></div>
      <div class="ft-state">${esc(FLEET_STATE_LABEL[activity] || activity)}</div>
      <div class="ft-coins">${chipsHtml}</div>
      <div class="ft-pnl mono ${coins.length ? pnlClass(pnl) : ''}"></div>
    `;
    const pnlNode = tile.querySelector('.ft-pnl');
    setWithFlash(pnlNode, `fleet:${b.id}`, coins.length ? fmtUSD(pnl, true) : '—', coins.length ? pnl : null);
    tile.addEventListener('click', () => switchTab(b.id));
    strip.append(tile);
  }
}

function updateSidebarDots() {
  for (const id of BOT_IDS) {
    const dot = document.querySelector(`[data-bot-dot="${id}"]`);
    if (!dot) continue;
    const st = state.botStatuses[id] || 'stopped';
    dot.className = `nav-dot dot-${st === 'running' ? 'running' : st === 'error' ? 'error' : 'stopped'}`;
  }
}

function renderBots(list) {
  const body = $('#botsBody');
  const arr = Array.isArray(list) ? list : [];
  $('#botCount').textContent = arr.length;
  body.innerHTML = '';
  body.className = 'bots-grid';
  if (arr.length === 0) {
    body.append(Object.assign(el('div', 'empty-state'), { textContent: 'No bots configured.' }));
    return;
  }
  const order = Object.keys(BOT_TYPES);
  const sorted = arr.slice().sort((a, b) => order.indexOf(a.type) - order.indexOf(b.type));
  for (const b of sorted) body.append(botCard(b));
}

function botCard(b) {
  const meta = BOT_TYPES[b.type] || { label: b.type || 'Unknown', color: '#5e6b7d' };
  const status = (b.status || 'stopped').toLowerCase();
  const running = status === 'running';
  const busy = !!state.botBusy[b.id];

  const card = el('div', `bot-card is-${status}`);
  const sig = (b.last_signal || 'HOLD').toUpperCase();
  const pnl = isNum(b.pnl) ? b.pnl : 0;
  // clickable -> open the matching bot tab if id is a known v2 id
  const canOpen = BOT_IDS.includes(b.id);
  if (canOpen) card.classList.add('clickable');

  card.innerHTML = `
    <div class="bot-top">
      <div>
        <div class="bot-id">${esc(b.id || '—')}</div>
        <div class="bot-type">
          <span class="tdot" style="background:${meta.color}"></span>${esc(meta.label)} · ${esc(b.symbol || 'top30')}
        </div>
      </div>
      <div class="bot-status-col">
        <span class="status-pill status-${status}">${status}</span>
        ${running && b.activity ? `<span class="bot-activity">${esc(FLEET_STATE_LABEL[b.activity] || b.activity)}</span>` : ''}
      </div>
    </div>
    <div class="bot-meta">
      <div><div class="m-label">PnL today</div><div class="m-val ${pnlClass(pnl)}">${fmtUSD(pnl, true)}</div></div>
      <div><div class="m-label">Trades</div><div class="m-val">${isNum(b.trades_today) ? b.trades_today : 0}</div></div>
      <div><div class="m-label">Leverage</div><div class="m-val">${isNum(b.leverage) ? b.leverage + '×' : '—'}</div></div>
      <div><div class="m-label">Signal</div><div class="m-val bot-signal sig-${sig}">${esc(sig)}</div></div>
    </div>
    <div class="bot-foot">
      <span class="m-label" style="font-size:10px;color:var(--text-3)">upd ${fmtTime(b.updated_at)}</span>
    </div>
  `;

  const toggle = el('button', `bot-toggle ${running ? 'to-stop' : 'to-start'}`, busy ? '…' : (running ? 'Stop' : 'Start'));
  toggle.disabled = busy;
  toggle.setAttribute('aria-label', `${running ? 'Stop' : 'Start'} bot ${b.id}`);
  toggle.addEventListener('click', (e) => { e.stopPropagation(); toggleBot(b, running); });
  card.querySelector('.bot-foot').append(toggle);

  if (canOpen) {
    card.addEventListener('click', () => switchTab(b.id));
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchTab(b.id); } });
  }
  return card;
}

/* ============================================================
   RISK RAILS CONFIG (Overview)
   ============================================================ */
const CONFIG_ROWS = [
  { key: 'max_leverage',                 label: 'Max leverage',          fmt: (v) => v + '×',   rail: true },
  { key: 'max_daily_loss_pct',           label: 'Max daily loss',        fmt: (v) => fmtPct(v), rail: true },
  { key: 'max_account_drawdown_pct',     label: 'Max drawdown',          fmt: (v) => fmtPct(v), rail: true },
  { key: 'risk_pct_per_trade',           label: 'Risk / trade',          fmt: (v) => fmtPct(v) },
  { key: 'max_concurrent_positions',     label: 'Max positions',         fmt: (v) => String(v) },
  { key: 'max_notional_per_symbol_pct',  label: 'Max notional / symbol', fmt: (v) => fmtPct(v) },
  { key: 'default_margin_type',          label: 'Margin type',           fmt: (v) => String(v) },
  { key: 'min_signal_confidence',        label: 'Min signal conf.',      fmt: (v) => isNum(v) ? v.toFixed(2) : v },
];
function renderConfig(cfg) {
  const body = $('#configBody');
  body.innerHTML = '';
  if (!cfg || typeof cfg !== 'object') {
    body.append(Object.assign(el('div', 'config-skeleton'), { textContent: 'Config unavailable.' }));
    return;
  }
  for (const row of CONFIG_ROWS) {
    const v = cfg[row.key];
    const wrap = el('div', `cfg-row ${row.rail ? 'cfg-rail' : ''}`);
    wrap.append(el('span', 'cfg-label', row.label));
    wrap.append(el('span', 'cfg-val', v == null ? '—' : row.fmt(v)));
    body.append(wrap);
  }
}

/* ============================================================
   LOG CONSOLE (Overview)
   ============================================================ */
function appendLog(entry) {
  const body = $('#logBody');
  const ph = body.querySelector('.log-empty');
  if (ph) ph.remove();
  const level = (entry.level || 'info').toLowerCase();
  const line = el('div', `log-line log-${level}`);
  line.append(
    el('span', 'log-time', new Date().toLocaleTimeString('en-US', { hour12: false })),
    el('span', 'log-tag', level.toUpperCase()),
    el('span', 'log-msg', entry.msg || '')
  );
  body.append(line);
  while (body.children.length > LOG_MAX_LINES) body.removeChild(body.firstChild);
  if ($('#autoScroll').checked) body.scrollTop = body.scrollHeight;
}
function logLocal(level, msg) { appendLog({ level, msg }); }

/* ============================================================
   TAB NAVIGATION
   ============================================================ */
function switchTab(tab) {
  state.activeTab = tab;
  const isBot = BOT_IDS.includes(tab);
  state.activeBot = isBot ? tab : null;

  // nav active state
  $$('.nav-item').forEach((n) => {
    const on = n.dataset.tab === tab;
    n.classList.toggle('is-active', on);
    n.setAttribute('aria-selected', on ? 'true' : 'false');
  });

  // panel visibility (single reusable bot panel)
  const isAi = tab === 'ai-model';
  $('#tab-overview').classList.toggle('is-active', tab === 'overview');
  $('#tab-overview').hidden = tab !== 'overview';
  $('#tab-bot').classList.toggle('is-active', isBot);
  $('#tab-bot').hidden = !isBot;
  const aiPanel = $('#tab-ai-model');
  if (aiPanel) { aiPanel.classList.toggle('is-active', isAi); aiPanel.hidden = !isAi; }

  if (tab === 'overview') {
    drawSparkline();
    sendSubscribe(null);
  } else if (isAi) {
    sendSubscribe(null);
    loadAiModel();
  } else if (isBot) {
    sendSubscribe(tab);
    loadBotDetail(tab);   // immediate fetch of all 5 panels' data
  }
}

function wireNav() {
  $$('.nav-item').forEach((n) => {
    n.addEventListener('click', () => switchTab(n.dataset.tab));
  });
  const trainAll = $('#aiTrainAll');
  if (trainAll) trainAll.addEventListener('click', trainAllModels);
  const backfill = $('#aiBackfill');
  if (backfill) backfill.addEventListener('click', startBackfill);
}

/* ============================================================
   AI MODEL TAB — aggregate self-learning model view
   ============================================================ */
async function loadAiModel() {
  const res = await apiGet('/api/model').catch(() => null);
  if (!res) return;
  renderAiModel(res);
  // resume the progress bar if a backfill is already running.
  const bf = await apiGet('/api/model/backfill').catch(() => null);
  if (bf && bf.status !== 'idle') { renderBackfill(bf); if (bf.status === 'running') pollBackfill(); }
}

function renderAiModel(m) {
  $('#aiApproach').textContent = m.approach || '';
  $('#aiModelType').textContent = m.model_type || m.backend || '—';
  $('#aiBackend').textContent = 'backend: ' + (m.backend || '—');
  $('#aiParams').textContent = isNum(m.total_parameters) ? m.total_parameters.toLocaleString('en-US') : '—';
  $('#aiSamples').textContent = isNum(m.total_training_samples) ? m.total_training_samples.toLocaleString('en-US') : '—';
  $('#aiAccuracy').textContent = m.avg_accuracy == null ? '—' : (m.avg_accuracy * 100).toFixed(1) + '%';
  $('#aiTrainedCount').textContent = `${m.bots_trained || 0}/${m.bots_total || 0} models trained`;
  $('#aiUpdated').textContent = `min ${m.min_samples_to_train} trades to train`;

  const body = $('#aiModelBody');
  body.innerHTML = '';
  let bestHist = { bot: null, hist: [] };
  for (const r of (m.per_bot || [])) {
    const tr = el('tr', '');
    const acc = r.accuracy == null ? '—' : (r.accuracy * 100).toFixed(1) + '%';
    const auc = r.auc == null ? '—' : r.auc.toFixed(3);
    const brier = r.brier == null ? '—' : r.brier.toFixed(3);
    const dur = isNum(r.train_duration_s) && r.train_duration_s > 0 ? r.train_duration_s.toFixed(1) + 's' : '—';
    const tdata = isNum(r.training_samples) ? r.training_samples : (r.n_samples || 0);
    const statusCls = r.status === 'trained' ? 'up' : (r.status === 'training' ? '' : 'muted');
    const warm = r.status !== 'trained' && isNum(r.samples_needed) && r.samples_needed > 0
      ? ` <span class="muted">(${r.samples_needed} more)</span>` : '';
    tr.innerHTML = `
      <td class="sym">${esc(r.bot)}</td>
      <td class="${statusCls}">${esc(r.status || '—')}${warm}</td>
      <td class="num cell-mono">${tdata.toLocaleString('en-US')}</td>
      <td class="num cell-mono">${isNum(r.n_parameters) ? r.n_parameters.toLocaleString('en-US') : 0}</td>
      <td class="num cell-mono">${acc}</td>
      <td class="num cell-mono">${auc}</td>
      <td class="num cell-mono">${brier}</td>
      <td class="num cell-mono">${dur}</td>
      <td>${r.calibrated ? '✓' : '—'}</td>`;
    body.append(tr);
    if (Array.isArray(r.accuracy_history) && r.accuracy_history.length > bestHist.hist.length) {
      bestHist = { bot: r.bot, hist: r.accuracy_history };
    }
  }

  // Accuracy-over-time: overlay ALL bots that have any history.
  const series = (m.per_bot || [])
    .map((b) => ({ color: (BOT_META[b.id] || BOT_META[b.bot] || {}).color || '#8b5cf6',
      label: b.bot, pts: normalizeCurve(b.accuracy_history) }))
    .filter((s) => s.pts.length >= 2);
  $('#aiAccBot').textContent = series.length ? `${series.length} models` : '—';
  // y-domain padded around observed accuracy so movement is visible.
  let amin = 0.5, amax = 0.5;
  for (const s of series) for (const v of s.pts) { amin = Math.min(amin, v); amax = Math.max(amax, v); }
  drawMultiCurve('#aiAccCanvas', '#aiAccEmpty', series,
    { baseline: 0.5, yDomain: [Math.min(0.4, amin), Math.max(0.7, amax)] });
  const legend = $('#aiAccLegend');
  if (legend) {
    legend.innerHTML = '';
    for (const s of series) {
      const item = el('span', 'ai-legend-item');
      item.innerHTML = `<span class="lg-dot" style="background:${s.color}"></span>${esc(s.label)}`;
      legend.append(item);
    }
  }

  const ex = $('#aiExplain');
  if (ex) ex.innerHTML = `
    <p>Each bot trains its <strong>own</strong> model on <strong>its own closed trades</strong>. Every trade that closes becomes one labeled example (win = 1 / loss = 0) built from ~${(m.feature_names || []).length} indicator features, and the model retrains on that history — so it <strong>learns from its own mistakes</strong> as it trades.</p>
    <ul class="ai-points">
      <li><strong>Model:</strong> gradient-boosted decision trees (LightGBM) — the best-in-class choice for small tabular data; it beats neural nets until there are 10k+ trades.</li>
      <li><strong>Calibrated:</strong> raw scores are Platt-scaled into real probabilities, so the “win-prob ≥ threshold” gate is meaningful.</li>
      <li><strong>Recency-weighted:</strong> recent trades count more, so the model adapts as the market regime changes.</li>
      <li><strong>Abstains while warming up:</strong> below ${m.min_samples_to_train} trades it trades on pure indicator rules — a model trained on too few (or single-class) samples is noise.</li>
      <li><strong>Neural-net upgrade</strong> is a staged, data-gated option for later (10k+ trades); at this scale trees are genuinely stronger.</li>
    </ul>
    <p class="muted">Click “Train more” to retrain every bot now on its latest trades.</p>`;
}

async function trainAllModels() {
  const btn = $('#aiTrainAll');
  if (btn) { btn.disabled = true; btn.textContent = 'Training…'; }
  try {
    await apiPost('/api/model/train', {});
    setTimeout(loadAiModel, 4000);
  } catch (_) {}
  setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = 'Train on live trades'; } }, 4000);
}

/* ---- Historical backfill training (train on old data) ---- */
let _bfPollTimer = null;
async function startBackfill() {
  const btn = $('#aiBackfill');
  const sel = $('#aiLookback');
  const days = parseInt((sel && sel.value) || '90', 10);
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
  const panel = $('#aiBackfillPanel');
  if (panel) panel.hidden = false;
  try {
    const res = await apiPost('/api/model/backfill', { lookback_days: days });
    if (res && res.ok === false) $('#aiBfStatus').textContent = res.detail || 'already running';
    pollBackfill();
  } catch (_) {
    $('#aiBfStatus').textContent = 'failed to start';
    if (btn) { btn.disabled = false; btn.textContent = 'Train on historical data'; }
  }
}

async function pollBackfill() {
  if (_bfPollTimer) clearTimeout(_bfPollTimer);
  let p = null;
  try { p = await apiGet('/api/model/backfill'); } catch (_) {}
  if (p) renderBackfill(p);
  const btn = $('#aiBackfill');
  if (p && p.status === 'running') {
    _bfPollTimer = setTimeout(pollBackfill, 2000);
  } else {
    if (btn) { btn.disabled = false; btn.textContent = 'Train on historical data'; }
    if (p && p.status === 'done') loadAiModel();
  }
}

function renderBackfill(p) {
  const panel = $('#aiBackfillPanel');
  if (panel) panel.hidden = false;
  const pct = isNum(p.progress_pct) ? p.progress_pct : 0;
  $('#aiBfFill').style.width = pct + '%';
  const statusTxt = { running: 'training…', done: 'done', error: 'error', idle: 'idle' }[p.status] || p.status;
  $('#aiBfStatus').textContent = `${statusTxt} · ${pct}%`;
  const parts = [];
  if (p.current_bot) parts.push(`bot: ${p.current_bot}`);
  if (p.current_symbol && p.status === 'running') parts.push(`scanning ${p.current_symbol}`);
  parts.push(`${p.symbols_done}/${p.symbols_total} symbols`);
  parts.push(`${(p.examples || 0).toLocaleString('en-US')} examples`);
  if (isNum(p.duration_s) && p.duration_s > 0) parts.push(`${p.duration_s.toFixed(1)}s`);
  if (p.status === 'error' && p.error) parts.push(`⚠ ${p.error}`);
  if (p.status === 'done' && p.results) {
    const done = Object.entries(p.results).map(([b, r]) =>
      r.accuracy != null ? `${b}: ${(r.accuracy * 100).toFixed(1)}% on ${r.samples}`
                         : `${b}: ${r.skipped ? 'skipped' : r.samples + ' ex'}`);
    if (done.length) parts.push('— ' + done.join(' · '));
  }
  $('#aiBfMeta').textContent = parts.join(' · ');
}

/* announce active bot to server so it pushes scanner/indicators/ml frames */
function sendSubscribe(botId) {
  if (!state.wsOpen || !ws) return;
  try { ws.send(JSON.stringify({ type: 'subscribe', bot: botId })); } catch (_) {}
}

/* ============================================================
   BOT DETAIL — load + render all five panels
   ============================================================ */
async function loadBotDetail(id) {
  // render header shell immediately from static meta (no blank state)
  renderBotHeaderShell(id);
  // fetch the 4 data endpoints in parallel; each renders independently
  const [detail, trades, scan, ind, ml, positions] = await Promise.allSettled([
    apiGet(`/api/bots/${encodeURIComponent(id)}`),
    apiGet(`/api/bots/${encodeURIComponent(id)}/trades`),
    apiGet(`/api/bots/${encodeURIComponent(id)}/scanner`),
    apiGet(`/api/bots/${encodeURIComponent(id)}/indicators`),
    apiGet(`/api/bots/${encodeURIComponent(id)}/ml`),
    apiGet('/api/positions'),
  ]);
  if (state.activeBot !== id) return; // user switched away
  state.allPositions = positions.status === 'fulfilled'
    ? (Array.isArray(positions.value) ? positions.value : (positions.value.positions || []))
    : [];
  if (detail.status === 'fulfilled') renderBotDetail(detail.value, id);
  if (trades.status === 'fulfilled') renderTrades(trades.value);
  if (scan.status === 'fulfilled') { state.lastScan = scan.value; renderScanner(scan.value); }
  if (ind.status === 'fulfilled') renderIndicators(ind.value);
  if (ml.status === 'fulfilled') renderML(ml.value);
}

/* ---- Bot status strip: Now trading [coin] · P/L | Searching… | Stopped ---- */
function renderBotStrip(detail, id) {
  const strip = $('#bdStrip');
  if (!strip) return;
  const status = (detail && detail.status || 'stopped').toLowerCase();
  const openSyms = (detail && detail.open_symbols) || [];
  const act = (detail && detail.activity) || null;  // backend-authoritative state
  // Match this bot's symbols to live positions for P/L.
  const coins = (state.allPositions || []).filter((p) => openSyms.includes(p.symbol));
  let stateName, label, pnl = null;
  if (status === 'error' && !openSyms.length) { stateName = 'error'; label = 'Halted — see log'; }
  else if (status !== 'running') { stateName = 'stopped'; label = 'Stopped'; }
  else if (openSyms.length) {
    stateName = 'trading';
    label = openSyms.length > 1 ? `Trading ${openSyms.length} coins` : 'Now trading';
    pnl = coins.reduce((s, c) => s + (isNum(c.unrealized_pnl) ? c.unrealized_pnl : 0), 0);
  } else {
    if (act === 'error') { stateName = 'error'; label = 'Halted — see log'; }
    else if (act === 'warming') { stateName = 'warming'; label = 'Warming up scanner…'; }
    else if (act === 'cooling') { stateName = 'cooling'; label = 'Cooling down (rate-limit)…'; }
    else { stateName = 'searching'; label = 'Searching for a coin…'; }
  }

  strip.dataset.state = stateName;
  $('#bdStripLabel').textContent = label;
  const chips = $('#bdStripCoins'); chips.innerHTML = '';
  if (stateName === 'trading') {
    for (const sym of openSyms) {
      const pos = coins.find((c) => c.symbol === sym);
      const chip = el('span', 'coin-chip');
      let html = `<span class="cc-sym">${esc(sym)}</span>`;
      if (pos) {
        html += ` <span class="pill pill-${(pos.side || 'LONG').toLowerCase()}">${esc(pos.side || '')}</span>`;
        html += ` <span class="cc-pnl ${pnlClass(pos.unrealized_pnl)}">${fmtUSD(pos.unrealized_pnl, true)}</span>`;
      }
      chip.innerHTML = html;
      chips.append(chip);
    }
  } else if (stateName === 'searching') {
    const cands = ((state.lastScan && state.lastScan.results) || []).filter((r) => r.passed).slice(0, 3);
    if (cands.length) for (const r of cands) chips.append(el('span', 'coin-chip searching', r.symbol));
    else chips.append(el('span', 'coin-chip searching', 'scanning market…'));
  }
  const pnlNode = $('#bdStripPnl');
  if (pnl == null) {
    pnlNode.className = 'strip-pnl-val mono';
    setWithFlash(pnlNode, 'stripPnl', '—', null);
  } else {
    pnlNode.className = `strip-pnl-val mono ${pnlClass(pnl)}`;
    setWithFlash(pnlNode, 'stripPnl', fmtUSD(pnl, true), pnl);
  }

  // Trading → live equity sparkline; otherwise clear it.
  const spark = $('#bdStripSpark');
  if (spark) {
    if (stateName === 'trading') {
      spark.style.display = 'inline-block';
      drawCurve('#bdStripSpark', null, (state.equity || []).slice(-40),
        (BOT_META[id] || {}).color || 'auto');
    } else {
      spark.style.display = 'none';
      const c = spark.getContext && spark.getContext('2d');
      if (c) c.clearRect(0, 0, spark.width, spark.height);
    }
  }
}

function renderBotHeaderShell(id) {
  const meta = BOT_META[id] || { label: id, desc: '', indicators: [] };
  $('#bdName').textContent = meta.label;
  $('#bdDesc').textContent = meta.desc;
  const ind = $('#bdIndicators');
  ind.innerHTML = '';
  for (const i of meta.indicators) ind.append(el('span', 'ind-chip', i));
  // status from cache while detail loads
  applyBotStatus(state.botStatuses[id] || 'stopped', id);
}

function applyBotStatus(status, id) {
  const st = (status || 'stopped').toLowerCase();
  const running = st === 'running';
  $('#bdStatusDot').className = `bd-dot dot-${running ? 'running' : st === 'error' ? 'error' : 'stopped'}`;
  const pill = $('#bdStatusPill');
  pill.className = `status-pill status-${st}`;
  pill.textContent = st;
  const toggle = $('#bdToggle');
  const busy = !!state.botBusy[id];
  toggle.className = `bot-toggle ${running ? 'to-stop' : 'to-start'}`;
  toggle.textContent = busy ? '…' : (running ? 'Stop' : 'Start');
  toggle.disabled = busy;
  toggle.onclick = () => toggleBot({ id }, running);
}

function renderBotDetail(d, id) {
  state.botDetail = d || {};
  applyBotStatus(d.status, id);

  const cfg = d.config || {};
  // populate config form (don't clobber while user is editing focus)
  const form = $('#bdConfigForm');
  if (!form.contains(document.activeElement)) {
    if (cfg.timeframe) $('#cfgTimeframe').value = cfg.timeframe;
    $('#cfgStopLoss').value = isNum(cfg.stop_loss_atr) ? cfg.stop_loss_atr : '';
    $('#cfgTakeProfit').value = isNum(cfg.take_profit_r) ? cfg.take_profit_r : '';
    $('#cfgLeverage').value = isNum(d.leverage) ? d.leverage : '';
    $('#cfgMinWinProb').value = isNum(cfg.min_win_prob) ? cfg.min_win_prob : '';
    $('#cfgInvest').value = isNum(cfg.investment_usdt) && cfg.investment_usdt > 0 ? cfg.investment_usdt : '';
    $('#cfgSlPct').value = isNum(cfg.stop_loss_pct) && cfg.stop_loss_pct > 0 ? cfg.stop_loss_pct : '';
    $('#cfgTpPct').value = isNum(cfg.take_profit_pct) && cfg.take_profit_pct > 0 ? cfg.take_profit_pct : '';
  }
  // indicators from config override static meta if present
  if (Array.isArray(cfg.indicators) && cfg.indicators.length) {
    const ind = $('#bdIndicators');
    ind.innerHTML = '';
    for (const i of cfg.indicators) ind.append(el('span', 'ind-chip', i));
  }

  renderPerformance(d.performance || {}, d.updated_at);
  renderStrategyState(d.strategy_state, id);
  renderBotStrip(d, id);
}

/* ---- Panel 2b: Strategy state (grid / DCA / rebalancing) ---- */
function renderStrategyState(ss, id) {
  const panel = $('#bdStrategyPanel');
  const body = $('#bdStrategyBody');
  const title = $('#bdStrategyTitle');
  const sub = $('#bdStrategySub');
  if (!ss || !['grid', 'dca', 'rebalancing', 'day-trading', 'scalping'].includes(id)) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  body.innerHTML = '';
  const color = (BOT_META[id] || {}).color || '#16c784';
  // per-symbol mark price from live positions.
  const markOf = (sym) => {
    const p = (state.allPositions || []).find((q) => q.symbol === sym);
    return p && isNum(p.mark_price) ? p.mark_price : null;
  };

  if (id === 'grid') {
    title.textContent = 'Grid ladder';
    if (!ss.active) {
      sub.textContent = 'idle';
      body.append(el('div', 'empty-state', 'No active grid — scanning for a coin that supports a full ladder.'));
      return;
    }
    sub.textContent = ss.symbol || '—';
    const markPrice = markOf(ss.symbol);
    const c = el('canvas', 'strat-canvas'); c.id = 'gridLadderCanvas'; c.height = 180;
    const em = el('div', 'empty-state'); em.id = 'gridLadderEmpty'; em.hidden = true;
    body.append(c, em);
    drawGridLadder('#gridLadderCanvas', '#gridLadderEmpty', ss, markPrice, color);
    const stats = el('div', 'perf-stats');
    stats.append(
      kv('Coin', ss.symbol || '—'),
      kv('Active grids', String(ss.active_levels ?? 0)),
      kv('Filled', String(ss.filled_levels ?? 0)),
      kv('Net qty', isNum(ss.net_qty) ? fmtNum(ss.net_qty, 4) : '—'),
      kv('Band low', fmtPrice(ss.band_low)),
      kv('Band high', fmtPrice(ss.band_high)),
    );
    body.append(stats);
  } else if (id === 'dca') {
    title.textContent = 'DCA deals';
    const deals = (ss.open_deals || []);
    sub.textContent = deals.length ? `${deals.length} open` : 'idle';
    if (!deals.length) { body.append(el('div', 'empty-state', 'No open deal.')); return; }
    const soCount = (state.botDetail && state.botDetail.config &&
      state.botDetail.config.safety_order_count) || null;
    for (const dl of deals) {
      const markPrice = markOf(dl.symbol);
      const c = el('canvas', 'dca-be'); c.height = 46;
      body.append(c);
      drawDcaBreakeven(c, dl, markPrice, soCount || dl.safety_orders_filled, color);
      const stats = el('div', 'perf-stats');
      stats.append(
        kv('Coin', dl.symbol || '—'),
        kv('Side', dl.side || '—'),
        kv('Avg entry', fmtPrice(dl.avg_entry)),
        kv('Target', fmtPrice(dl.target_price)),
        kv('Safety filled', String(dl.safety_orders_filled ?? 0)),
        kv('Total qty', isNum(dl.total_qty) ? fmtNum(dl.total_qty, 4) : '—'),
      );
      body.append(stats);
    }
  } else if (id === 'rebalancing') {
    title.textContent = 'Basket';
    const legs = (ss.basket || []);
    sub.textContent = legs.length ? `${legs.length} legs · ${fmtNum(ss.total_notional, 2)} notional` : 'idle';
    if (!legs.length) { body.append(el('div', 'empty-state', 'Basket empty.')); return; }
    const c = el('canvas', 'strat-canvas'); c.id = 'rebalDonutCanvas'; c.height = 200;
    const em = el('div', 'empty-state'); em.id = 'rebalDonutEmpty'; em.hidden = true;
    body.append(c, em);
    const tgt = (state.botDetail && state.botDetail.config &&
      state.botDetail.config.target_weights) || null;
    drawRebalDonut('#rebalDonutCanvas', '#rebalDonutEmpty', legs, ss.total_notional, tgt);
    const grid = el('div', 'perf-stats');
    for (const lg of legs) {
      grid.append(
        kv('Coin', lg.symbol || '—'),
        kv('Qty', isNum(lg.qty) ? fmtNum(lg.qty, 4) : '—'),
        kv('Avg entry', fmtPrice(lg.avg_entry)),
        kv('Notional', isNum(lg.notional) ? fmtNum(lg.notional, 2) : '—'),
      );
    }
    body.append(grid);
  } else {
    // day-trading / scalping — single directional position.
    title.textContent = id === 'scalping' ? 'Scalp position' : 'Day-trade position';
    const openSyms = (state.botDetail && state.botDetail.open_symbols) || [];
    const pos = (state.allPositions || []).find((p) => openSyms.includes(p.symbol));
    if (!pos) {
      sub.textContent = 'flat';
      body.append(el('div', 'empty-state', id === 'scalping'
        ? 'Flat — waiting for a momentum setup.'
        : 'Flat — waiting for a session-gated setup.'));
      return;
    }
    sub.textContent = pos.symbol || '—';
    const c = el('canvas', 'strat-canvas'); c.id = 'posLadderCanvas'; c.height = 120;
    body.append(c);
    drawPosLadder(c, pos, color);
    const card = el('div', 'pos-card');
    const rMult = _rMultiple(pos);
    card.append(
      kv('Entry', fmtPrice(pos.entry_price)),
      kv('Mark', fmtPrice(pos.mark_price)),
      kv('Stop', fmtPrice(pos.stop_loss ?? pos.sl_price)),
      kv('Target', fmtPrice(pos.take_profit ?? pos.tp_price)),
      kv('R-multiple', rMult == null ? '—' : rMult.toFixed(2) + 'R'),
    );
    body.append(card);
    if (id === 'day-trading') {
      const chip = el('div', 'session-chip',
        (state.lastScan && state.lastScan.results || []).some((r) => r._session_active)
          ? 'Session active' : 'Session gated');
      body.append(chip);
    }
  }
}

/* R-multiple = (mark - entry) / (entry - stop), sign-aware for SHORT */
function _rMultiple(pos) {
  const entry = pos.entry_price, mark = pos.mark_price;
  const stop = pos.stop_loss != null ? pos.stop_loss : pos.sl_price;
  if (!isNum(entry) || !isNum(mark) || !isNum(stop)) return null;
  const risk = Math.abs(entry - stop);
  if (risk <= 0) return null;
  const dir = (pos.side || 'LONG').toUpperCase() === 'SHORT' ? -1 : 1;
  return (dir * (mark - entry)) / risk;
}
function kv(label, value) {
  const c = el('div', 'pstat');
  c.append(el('div', 'p-label', label));
  c.append(Object.assign(el('div', 'p-val'), { textContent: value }));
  return c;
}

/* ---- Panel 2: Performance ---- */
function renderPerformance(p, updatedAt) {
  $('#perfUpdated').textContent = updatedAt ? `upd ${fmtTime(updatedAt)}` : '—';

  const total = isNum(p.trades_total) ? p.trades_total : 0;
  const wins = isNum(p.wins) ? p.wins : 0;
  const losses = isNum(p.losses) ? p.losses : 0;
  const wr = isNum(p.win_rate) ? p.win_rate : 0;
  const realized = isNum(p.realized_pnl) ? p.realized_pnl : 0;
  const unreal = isNum(p.unrealized_pnl) ? p.unrealized_pnl : 0;
  const dd = isNum(p.max_drawdown_pct) ? p.max_drawdown_pct : 0;
  const pf = isNum(p.profit_factor) ? p.profit_factor : null;

  const stats = [
    { label: 'Trades', val: String(total), cls: '' },
    { label: 'Realized', val: fmtUSD(realized, true), cls: pnlClass(realized) },
    { label: 'Unrealized', val: fmtUSD(unreal, true), cls: pnlClass(unreal) },
    { label: 'Profit factor', val: pf == null ? '—' : pf.toFixed(2), cls: pf != null && pf >= 1 ? 'up' : pf != null ? 'down' : '' },
    { label: 'Max drawdown', val: fmtPct(dd), cls: 'down' },
    { label: 'Open pos.', val: String(isNum(p.open_positions) ? p.open_positions : 0), cls: '' },
  ];
  const grid = $('#perfStats');
  grid.innerHTML = '';
  for (const s of stats) {
    const c = el('div', 'pstat');
    c.append(el('div', 'p-label', s.label));
    c.append(Object.assign(el('div', `p-val ${s.cls}`), { textContent: s.val }));
    grid.append(c);
  }

  const wrPct = wr <= 1 ? wr * 100 : wr; // accept 0..1 or 0..100
  $('#perfWinRateVal').textContent = wrPct.toFixed(1) + '%';
  $('#perfWinBar').style.width = Math.max(0, Math.min(100, wrPct)) + '%';
  $('#perfWins').textContent = `${wins} W`;
  $('#perfLosses').textContent = `${losses} L`;

  drawCurve('#botEquityCanvas', '#botEquityEmpty', normalizeCurve(p.equity_curve), '#16c784');
}

/* equity_curve is [[ts, equity], ...] → take equity values */
function normalizeCurve(curve) {
  if (!Array.isArray(curve)) return [];
  return curve.map((pt) => Array.isArray(pt) ? pt[1] : pt).filter(isNum);
}

/* ---- Shared canvas helpers (DPR-correct sizing + curve geometry) ---- */
function _dprCanvas(canvas, fbW, fbH) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || fbW, cssH = canvas.clientHeight || fbH;
  if (canvas.width !== cssW * dpr) { canvas.width = cssW * dpr; canvas.height = cssH * dpr; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  return { ctx, cssW, cssH };
}
function _curveGeom(pts, cssW, cssH, pad, yDomain) {
  const min = yDomain ? yDomain[0] : Math.min(...pts);
  const max = yDomain ? yDomain[1] : Math.max(...pts);
  const range = (max - min) || 1;
  const w = cssW - pad * 2, h = cssH - pad * 2;
  const xStep = w / (pts.length - 1);
  return { min, max, range, w, h, xStep, yOf: (v) => pad + h - ((v - min) / range) * h };
}

/* generic line chart (equity curve, accuracy-over-time)
   opts (all optional): { baseline, hoverX, yDomain } */
function drawCurve(canvasSel, emptySel, pts, baseColor, opts) {
  const canvas = $(canvasSel);
  const empty = emptySel ? $(emptySel) : null;
  if (!canvas) return;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 300, 120);

  if (!pts || pts.length < 2) {
    if (empty) empty.hidden = false;
    canvas.style.display = 'block';
    return;
  }
  if (empty) empty.hidden = true;

  const pad = 8;
  const o = opts || {};
  const { w, h, xStep, yOf } = _curveGeom(pts, cssW, cssH, pad, o.yDomain);
  const rising = pts[pts.length - 1] >= pts[0];
  const color = baseColor === 'auto' ? (rising ? '#16c784' : '#ea3943') : baseColor;

  // baseline grid
  ctx.strokeStyle = 'rgba(255,255,255,0.05)';
  ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const y = pad + (h / 3) * g;
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(pad + w, y); ctx.stroke();
  }

  // drawdown shading — fill under-water segments (below the running max) in red
  let runMax = pts[0];
  for (let i = 1; i < pts.length; i++) {
    const prevMax = runMax;
    runMax = Math.max(runMax, pts[i]);
    if (pts[i] < prevMax || pts[i - 1] < prevMax) {
      const x0 = pad + (i - 1) * xStep, x1 = pad + i * xStep;
      ctx.beginPath();
      ctx.moveTo(x0, yOf(pts[i - 1]));
      ctx.lineTo(x1, yOf(pts[i]));
      ctx.lineTo(x1, pad + h);
      ctx.lineTo(x0, pad + h);
      ctx.closePath();
      ctx.fillStyle = hexA('#ea3943', 0.10);
      ctx.fill();
    }
  }

  // area
  ctx.beginPath();
  ctx.moveTo(pad, yOf(pts[0]));
  pts.forEach((v, i) => ctx.lineTo(pad + i * xStep, yOf(v)));
  ctx.lineTo(pad + w, pad + h); ctx.lineTo(pad, pad + h); ctx.closePath();
  const grad = ctx.createLinearGradient(0, pad, 0, pad + h);
  grad.addColorStop(0, hexA(color, 0.26));
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = grad; ctx.fill();

  // dashed starting baseline
  const baseVal = o.baseline != null ? o.baseline : pts[0];
  ctx.save();
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(255,255,255,0.22)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, yOf(baseVal)); ctx.lineTo(pad + w, yOf(baseVal)); ctx.stroke();
  ctx.restore();

  // line
  ctx.beginPath();
  ctx.moveTo(pad, yOf(pts[0]));
  pts.forEach((v, i) => ctx.lineTo(pad + i * xStep, yOf(v)));
  ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.lineJoin = 'round'; ctx.stroke();

  const lx = pad + (pts.length - 1) * xStep, ly = yOf(pts[pts.length - 1]);
  ctx.beginPath(); ctx.arc(lx, ly, 2.6, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();

  // hover crosshair + label (wired once per canvas)
  if (!canvas.dataset.wired) {
    canvas.dataset.wired = '1';
    canvas.addEventListener('mousemove', (e) => {
      const rect = canvas.getBoundingClientRect();
      canvas.dataset.hoverIdx = String(Math.round((e.clientX - rect.left - pad) / xStep));
      if (canvas._redraw) canvas._redraw();
    });
    canvas.addEventListener('mouseleave', () => {
      delete canvas.dataset.hoverIdx;
      if (canvas._redraw) canvas._redraw();
    });
  }
  canvas._redraw = () => drawCurve(canvasSel, emptySel, pts, baseColor, opts);
  const hi = canvas.dataset.hoverIdx != null ? parseInt(canvas.dataset.hoverIdx, 10) : null;
  if (hi != null && hi >= 0 && hi < pts.length) {
    const hx = pad + hi * xStep, hy = yOf(pts[hi]);
    ctx.save();
    ctx.strokeStyle = 'rgba(255,255,255,0.3)';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(hx, pad); ctx.lineTo(hx, pad + h); ctx.stroke();
    ctx.beginPath(); ctx.arc(hx, hy, 3, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
    const lbl = o.hoverPct ? fmtPct(pts[hi] * 100) : fmtUSD(pts[hi]);
    ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
    ctx.textBaseline = 'top';
    const tw = ctx.measureText(lbl).width + 8;
    const bx = Math.min(Math.max(hx - tw / 2, pad), pad + w - tw);
    ctx.fillStyle = 'rgba(8,12,18,0.9)';
    roundRect(ctx, bx, pad + 2, tw, 16, 3); ctx.fill();
    ctx.fillStyle = '#e6edf5';
    ctx.textAlign = 'left';
    ctx.fillText(lbl, bx + 4, pad + 4);
    ctx.restore();
  }
}
function hexA(hex, a) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) return `rgba(22,199,132,${a})`;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* multi-series line chart (accuracy over time across all bots).
   series = [{ color, label, pts }]; opts = { baseline, yDomain }. */
function drawMultiCurve(canvasSel, emptySel, series, opts) {
  const canvas = $(canvasSel);
  const empty = emptySel ? $(emptySel) : null;
  if (!canvas) return;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 300, 120);
  const drawable = (series || []).filter((s) => Array.isArray(s.pts) && s.pts.length >= 2);
  if (!drawable.length) { if (empty) empty.hidden = false; return; }
  if (empty) empty.hidden = true;

  const o = opts || {};
  const pad = 8, w = cssW - pad * 2, h = cssH - pad * 2;
  // shared y-domain across all series
  let min = Infinity, max = -Infinity;
  for (const s of drawable) for (const v of s.pts) { if (v < min) min = v; if (v > max) max = v; }
  if (o.yDomain) { min = o.yDomain[0]; max = o.yDomain[1]; }
  const range = (max - min) || 1;
  const yOf = (v) => pad + h - ((v - min) / range) * h;

  // grid
  ctx.strokeStyle = 'rgba(255,255,255,0.05)'; ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const y = pad + (h / 3) * g;
    ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(pad + w, y); ctx.stroke();
  }
  // dashed baseline (e.g. 0.5 coin-flip accuracy)
  if (o.baseline != null && o.baseline >= min && o.baseline <= max) {
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(255,255,255,0.28)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(pad, yOf(o.baseline)); ctx.lineTo(pad + w, yOf(o.baseline)); ctx.stroke();
    ctx.restore();
  }
  for (const s of drawable) {
    const xStep = w / (s.pts.length - 1);
    ctx.beginPath();
    ctx.moveTo(pad, yOf(s.pts[0]));
    s.pts.forEach((v, i) => ctx.lineTo(pad + i * xStep, yOf(v)));
    ctx.strokeStyle = s.color || '#8b5cf6'; ctx.lineWidth = 1.8; ctx.lineJoin = 'round'; ctx.stroke();
    const lx = pad + (s.pts.length - 1) * xStep, ly = yOf(s.pts[s.pts.length - 1]);
    ctx.beginPath(); ctx.arc(lx, ly, 2.4, 0, Math.PI * 2); ctx.fillStyle = s.color || '#8b5cf6'; ctx.fill();
  }
}

/* recent trades table */
function renderTrades(data) {
  const trades = data && Array.isArray(data.trades) ? data.trades : [];
  const summary = (data && data.summary) || {};
  const body = $('#tradesBody');
  const empty = $('#tradesEmpty');
  body.innerHTML = '';
  if (trades.length === 0) { empty.style.display = 'block'; }
  else {
    empty.style.display = 'none';
    for (const t of trades.slice(0, 25)) {
      const side = (t.side || '').toUpperCase();
      const isLong = side === 'LONG';
      const pnl = isNum(t.pnl) ? t.pnl : 0;
      const outcome = (t.outcome || (pnl >= 0 ? 'win' : 'loss')).toLowerCase();
      const tr = el('tr');
      tr.innerHTML = `
        <td class="sym">${esc(t.symbol || '—')}</td>
        <td><span class="pill ${isLong ? 'pill-long' : 'pill-short'}">${side || '—'}</span></td>
        <td class="num cell-mono">${fmtPrice(t.entry_price)}</td>
        <td class="num cell-mono">${fmtPrice(t.exit_price)}</td>
        <td class="num cell-mono ${pnlClass(pnl)}">${fmtUSD(pnl, true)}</td>
        <td><span class="outcome-${outcome === 'win' ? 'win' : 'loss'}">${outcome === 'win' ? 'WIN' : 'LOSS'}</span></td>
        <td class="num cell-mono">${fmtProb(t.win_prob)}</td>
      `;
      body.append(tr);
    }
  }
  // summary stats can enrich profit factor if detail lacked it
  if (state.botDetail && isNum(summary.profit_factor)) {
    const pfCell = $$('#perfStats .pstat').find((c) => c.querySelector('.p-label').textContent === 'Profit factor');
    if (pfCell) {
      const v = summary.profit_factor;
      const valNode = pfCell.querySelector('.p-val');
      valNode.textContent = v.toFixed(2);
      valNode.className = `p-val ${v >= 1 ? 'up' : 'down'}`;
    }
  }
}

/* ---- Panel 3: Scanner ---- */
function renderScanner(data) {
  const results = data && Array.isArray(data.results) ? data.results : [];
  $('#scanCount').textContent = results.length;
  $('#scanUpdated').textContent = data && data.scanned_at ? `scan ${fmtTime(data.scanned_at)}` : '—';
  const body = $('#scanBody');
  const empty = $('#scanEmpty');
  body.innerHTML = '';
  if (results.length === 0) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';

  // Contract: render the ranked TOP 30 only (universe is top-30 by volume).
  const sorted = results.slice().sort((a, b) => (a.rank || 999) - (b.rank || 999)).slice(0, 30);
  for (const r of sorted) {
    const ready = r.entry_ready === true || r.passed === true;
    const chg = isNum(r.change_pct) ? r.change_pct : 0;
    const tr = el('tr', ready ? 'row-ready' : '');
    const filters = r.filters && typeof r.filters === 'object'
      ? Object.entries(r.filters).map(([k, v]) =>
          `<span class="fchip ${v ? 'fchip-pass' : 'fchip-fail'}" title="${esc(k)}">${v ? '✓' : '✕'} ${esc(shortFilter(k))}</span>`).join('')
      : '<span class="fchip fchip-fail">—</span>';
    tr.innerHTML = `
      <td class="num cell-mono">${isNum(r.rank) ? r.rank : '—'}${ready ? ' <span class="ready-flag" title="Entry ready">●</span>' : ''}</td>
      <td class="sym">${esc(r.symbol || '—')}</td>
      <td class="num cell-mono">${isNum(r.score) ? r.score.toFixed(2) : '—'}</td>
      <td><div class="filters">${filters}</div></td>
      <td class="num cell-mono">${fmtProb(r.win_prob)}</td>
      <td class="num cell-mono">${fmtPrice(r.last_price)}</td>
      <td class="num cell-mono">${fmtCompact(r.vol_24h)}</td>
      <td class="num cell-mono ${pnlClass(chg)}">${fmtPct(chg, true)}</td>
    `;
    body.append(tr);
  }
}
function shortFilter(k) {
  return String(k).replace(/_/g, ' ').replace(/\b(ema|rsi|macd|vwap|atr|bb)\b/gi, (m) => m.toUpperCase());
}

/* ---- Panel 4: Live indicators ---- */
// [key, label, decimals, isPrice] — price fields use the magnitude-aware
// formatter so micro-coin values aren't truncated to 0.00.
const IND_FIELDS = [
  ['price', 'Price', 2, true], ['ema50', 'EMA50', 2, true], ['ema200', 'EMA200', 2, true],
  ['rsi14', 'RSI14', 1, false], ['macd_hist', 'MACD h', 3, false], ['bb_pctb', 'BB %B', 2, false],
  ['atr14', 'ATR14', 2, true], ['vwap', 'VWAP', 2, true],
];
function renderIndicators(data) {
  const symbols = data && Array.isArray(data.symbols) ? data.symbols : [];
  $('#indUpdated').textContent = data && data.ts ? `upd ${fmtTime(data.ts)}` : '—';
  const list = $('#indList');
  const empty = $('#indEmpty');
  list.innerHTML = '';
  if (symbols.length === 0) { empty.style.display = 'block'; return; }
  empty.style.display = 'none';

  for (const s of symbols) {
    const ind = s.indicators || {};
    const conds = s.conditions || {};
    const ready = s.entry_ready === true;
    const card = el('div', `ind-card ${ready ? 'ready' : ''}`);
    const vals = IND_FIELDS.map(([k, lbl, dp, isPrice]) =>
      `<div class="ind-kv"><span class="k">${lbl}</span><span class="v">${isPrice ? fmtPrice(ind[k]) : fmtNum(ind[k], dp)}</span></div>`).join('');
    const condChips = Object.entries(conds).map(([k, v]) =>
      `<span class="cond ${v ? 'on' : 'off'}">${v ? '✓' : '✕'} ${esc(shortFilter(k))}</span>`).join('');
    card.innerHTML = `
      <div class="ind-card-head">
        <span class="ind-sym">${esc(s.symbol || '—')}</span>
        <span class="${ready ? 'ready-flag' : 'm-label'}">${ready ? '● ENTRY READY' : 'no entry'}</span>
      </div>
      <div class="ind-vals">${vals}</div>
      <div class="ind-conds">${condChips || '<span class="cond off">no conditions</span>'}</div>
    `;
    list.append(card);
  }
}

/* ---- Panel 5: AI insight ---- */
function renderML(data) {
  const d = data || {};
  const status = (d.status || 'warming_up').toLowerCase();
  const badge = $('#mlBadge');
  const labelMap = { trained: 'Trained', training: 'Training', warming_up: 'Warming up', error: 'Error' };
  badge.textContent = labelMap[status] || status;
  badge.className = `ml-badge ml-${status === 'warming_up' ? 'warming' : status}`;
  $('#mlBackend').textContent = d.model ? d.model : '—';

  const note = $('#mlNote');
  if (status === 'warming_up') {
    const n = isNum(d.n_samples) ? d.n_samples : 0;
    note.hidden = false;
    note.textContent = `Model is learning — needs ~${MIN_TRADES_TO_TRAIN} closed trades before it predicts. Currently ${n}/${MIN_TRADES_TO_TRAIN}. Until then the bot trades on pure indicator rules.`;
  } else if (status === 'error') {
    note.hidden = false;
    note.textContent = 'Model error — falling back to pure indicator rules. Check the backend log.';
  } else {
    note.hidden = true;
  }

  const m = d.metrics || {};
  const stats = [
    { label: 'Samples', val: isNum(d.n_samples) ? String(d.n_samples) : '—', cls: '' },
    { label: 'Accuracy', val: isNum(m.accuracy) ? (m.accuracy * 100).toFixed(1) + '%' : '—', cls: '' },
    { label: 'AUC', val: isNum(m.auc) ? m.auc.toFixed(3) : '—', cls: '' },
    { label: 'Min win prob', val: isNum(d.min_win_prob) ? d.min_win_prob.toFixed(2) : '—', cls: '' },
    { label: 'Trained', val: d.trained_at ? fmtTime(d.trained_at) : '—', cls: '' },
  ];
  const grid = $('#mlStats');
  grid.innerHTML = '';
  for (const s of stats) {
    const c = el('div', 'pstat');
    c.append(el('div', 'p-label', s.label));
    c.append(Object.assign(el('div', `p-val ${s.cls}`), { textContent: s.val }));
    grid.append(c);
  }

  // accuracy-over-time: metrics.history = [[ts, accuracy], ...]
  const accPts = normalizeCurve(m.history);
  let lo = 0.4, hi = 0.7;
  for (const v of accPts) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  drawCurve('#mlAccCanvas', '#mlAccEmpty', accPts, '#3b82f6',
    { baseline: 0.5, yDomain: [lo, hi], hoverPct: true });

  // feature importance bar chart
  drawFeatureBars(d.feature_importance);

  // live predictions
  renderPredictions(d.live_predictions);
}

function drawFeatureBars(feats) {
  const canvas = $('#mlFeatCanvas');
  const empty = $('#mlFeatEmpty');
  if (!canvas) return;
  const arr = Array.isArray(feats)
    ? feats.map((f) => Array.isArray(f) ? { name: f[0], val: f[1] } : f).filter((f) => f && isNum(f.val))
    : [];
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 300;
  const cssH = canvas.clientHeight || 140;
  if (canvas.width !== cssW * dpr) { canvas.width = cssW * dpr; canvas.height = cssH * dpr; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  if (arr.length === 0) { if (empty) empty.hidden = false; return; }
  if (empty) empty.hidden = true;

  const top = arr.slice().sort((a, b) => b.val - a.val).slice(0, 8);
  const max = Math.max(...top.map((f) => f.val)) || 1;
  const rowH = cssH / top.length;
  const labelW = Math.min(96, cssW * 0.38);
  const barMax = cssW - labelW - 44;

  ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';
  top.forEach((f, i) => {
    const y = i * rowH + rowH / 2;
    // label
    ctx.fillStyle = '#9aa7b8';
    ctx.textAlign = 'right';
    ctx.fillText(truncate(String(f.name), 13), labelW - 6, y);
    // bar
    const bw = Math.max(2, (f.val / max) * barMax);
    const bg = ctx.createLinearGradient(labelW, 0, labelW + bw, 0);
    bg.addColorStop(0, '#2257c0'); bg.addColorStop(1, '#6aa6ff');
    ctx.fillStyle = bg;
    const bh = Math.min(14, rowH * 0.55);
    roundRect(ctx, labelW, y - bh / 2, bw, bh, 3); ctx.fill();
    // value
    ctx.fillStyle = '#e6edf5';
    ctx.textAlign = 'left';
    ctx.fillText(f.val.toFixed(2), labelW + bw + 6, y);
  });
}
function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function roundRect(ctx, x, y, w, h, r) {
  r = Math.min(r, h / 2, w / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* ============================================================
   STRATEGY-TAILORED CANVAS CHARTS
   ============================================================ */

/* Grid ladder: vertical band_low→band_high axis, evenly-spaced rungs,
   first `filled_levels` solid teal / rest dashed hollow, bright mark line. */
function drawGridLadder(sel, emptySel, ss, markPrice, color) {
  const canvas = $(sel);
  if (!canvas) return;
  const empty = emptySel ? $(emptySel) : null;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 300, 180);
  const lo = ss.band_low, hi = ss.band_high;
  if (!ss.active || !isNum(lo) || !isNum(hi) || hi <= lo) {
    if (empty) { empty.hidden = false; empty.textContent = 'No active grid.'; }
    return;
  }
  if (empty) empty.hidden = true;
  const pad = 12;
  const h = cssH - pad * 2;
  const labelW = 62;
  const xLeft = pad + labelW, xRight = cssW - pad;
  const yOf = (v) => pad + h - ((v - lo) / (hi - lo)) * h;
  const levels = Math.max(1, ss.active_levels || 0);
  const filled = Math.max(0, Math.min(levels, ss.filled_levels || 0));
  const mid = isNum(markPrice) ? markPrice : (lo + hi) / 2;

  // band edges (bold) + tinted zones (buys below mid teal, sells above red)
  ctx.fillStyle = hexA('#14b8a6', 0.08);
  ctx.fillRect(xLeft, yOf(mid), xRight - xLeft, yOf(lo) - yOf(mid));
  ctx.fillStyle = hexA('#ea3943', 0.07);
  ctx.fillRect(xLeft, yOf(hi), xRight - xLeft, yOf(mid) - yOf(hi));

  ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'right';
  for (const edge of [lo, hi]) {
    ctx.strokeStyle = 'rgba(255,255,255,0.35)';
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(xLeft, yOf(edge)); ctx.lineTo(xRight, yOf(edge)); ctx.stroke();
    ctx.fillStyle = '#9aa7b8';
    ctx.fillText(fmtPrice(edge), xLeft - 4, yOf(edge));
  }

  // rungs
  for (let i = 0; i < levels; i++) {
    const v = lo + ((hi - lo) * i) / (levels - 1 || 1);
    const y = yOf(v);
    ctx.beginPath();
    if (i < filled) {
      ctx.setLineDash([]);
      ctx.strokeStyle = '#14b8a6'; ctx.lineWidth = 2;
    } else {
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = 'rgba(154,167,184,0.5)'; ctx.lineWidth = 1;
    }
    ctx.moveTo(xLeft, y); ctx.lineTo(xRight, y); ctx.stroke();
  }
  ctx.setLineDash([]);

  // mark line (bright)
  if (isNum(markPrice) && markPrice >= lo && markPrice <= hi) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(xLeft, yOf(markPrice)); ctx.lineTo(xRight, yOf(markPrice)); ctx.stroke();
    ctx.fillStyle = color; ctx.textAlign = 'left';
    ctx.fillText(fmtPrice(markPrice), xRight - 48, yOf(markPrice) - 8);
  }
}

/* DCA break-even: horizontal track with BE (avg entry), TP (target), mark;
   progress fill toward target + safety-order pips below. */
function drawDcaBreakeven(canvas, deal, markPrice, safetyCount, color) {
  if (!canvas) return;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 300, 46);
  const be = deal.avg_entry, tp = deal.target_price;
  const mark = isNum(markPrice) ? markPrice : be;
  const pad = 10;
  const trackY = 16, x0 = pad, x1 = cssW - pad, tw = x1 - x0;
  const isShort = (deal.side || 'LONG').toUpperCase() === 'SHORT';
  let frac = 0;
  if (isNum(be) && isNum(tp) && tp !== be) {
    frac = (mark - be) / (tp - be);
    if (isShort) frac = (be - mark) / (be - tp);
  }
  frac = Math.max(0, Math.min(1, frac));

  // base track
  ctx.fillStyle = 'rgba(255,255,255,0.08)';
  roundRect(ctx, x0, trackY - 3, tw, 6, 3); ctx.fill();
  // progress fill (green toward target, red if under-water)
  const fillCls = frac >= 0.5 ? '#16c784' : color;
  const uw = isNum(mark) && isNum(be) && ((!isShort && mark < be) || (isShort && mark > be));
  ctx.fillStyle = uw ? '#ea3943' : fillCls;
  roundRect(ctx, x0, trackY - 3, tw * frac, 6, 3); ctx.fill();

  // ticks
  ctx.font = '9px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'bottom';
  const tick = (xf, col, lbl) => {
    const x = x0 + tw * Math.max(0, Math.min(1, xf));
    ctx.strokeStyle = col; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(x, trackY - 7); ctx.lineTo(x, trackY + 7); ctx.stroke();
    ctx.fillStyle = col; ctx.textAlign = 'center';
    ctx.fillText(lbl, x, trackY - 8);
  };
  tick(0, '#9aa7b8', 'BE');
  tick(1, '#f5a623', 'TP');
  tick(frac, color, fmtPct(frac * 100));

  // safety-order pips below
  const n = Math.max(0, safetyCount || 0);
  const filled = Math.max(0, deal.safety_orders_filled || 0);
  if (n > 0) {
    const gap = tw / (n + 1);
    for (let i = 1; i <= n; i++) {
      ctx.beginPath();
      ctx.arc(x0 + gap * i, trackY + 16, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = i <= filled ? color : 'rgba(154,167,184,0.4)';
      ctx.fill();
    }
  }
}

/* Rebalancing donut: arcs sized by notional share, target-weight outer ring,
   drift-tinted. Falls back to a stacked bar for >8 legs. */
function drawRebalDonut(sel, emptySel, basket, totalNotional, targetWeights) {
  const canvas = $(sel);
  if (!canvas) return;
  const empty = emptySel ? $(emptySel) : null;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 300, 200);
  const legs = (basket || []).filter((l) => isNum(l.notional) && l.notional > 0);
  const total = isNum(totalNotional) && totalNotional > 0
    ? totalNotional : legs.reduce((s, l) => s + l.notional, 0);
  if (!legs.length || total <= 0) {
    if (empty) { empty.hidden = false; empty.textContent = 'Basket empty.'; }
    return;
  }
  if (empty) empty.hidden = true;
  const n = legs.length;
  const tgtOf = (i) => (targetWeights && isNum(targetWeights[legs[i].symbol]))
    ? targetWeights[legs[i].symbol] : 1 / n;

  if (n > 8) {
    // stacked horizontal allocation bar
    const pad = 12, y = cssH / 2 - 10, barH = 20, tw = cssW - pad * 2;
    let x = pad;
    legs.forEach((l, i) => {
      const w = tw * (l.notional / total);
      ctx.fillStyle = REBAL_PALETTE[i % REBAL_PALETTE.length];
      ctx.fillRect(x, y, w, barH);
      x += w;
    });
    return;
  }

  const cx = cssW / 2, cy = cssH / 2, R = Math.min(cx, cy) - 14, hole = R * 0.55;
  let a0 = -Math.PI / 2;
  legs.forEach((l, i) => {
    const share = l.notional / total;
    const a1 = a0 + share * Math.PI * 2;
    const col = REBAL_PALETTE[i % REBAL_PALETTE.length];
    // main arc
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, R, a0, a1);
    ctx.closePath();
    ctx.fillStyle = col; ctx.fill();
    // target-weight thin outer arc + drift tint
    const tgt = tgtOf(i);
    const drift = share - tgt;
    const driftPct = Math.abs(drift) * 100;
    ctx.beginPath();
    ctx.arc(cx, cy, R + 5, a0, a0 + tgt * Math.PI * 2);
    ctx.strokeStyle = driftPct >= MARGIN_RISK_CUTOFFS.warn / 5 ? '#ea3943'
      : driftPct >= 5 ? '#f5a623' : 'rgba(255,255,255,0.35)';
    ctx.lineWidth = 3;
    ctx.stroke();
    a0 = a1;
  });
  // punch the hole
  ctx.globalCompositeOperation = 'destination-out';
  ctx.beginPath(); ctx.arc(cx, cy, hole, 0, Math.PI * 2); ctx.fill();
  ctx.globalCompositeOperation = 'source-over';

  // center label
  ctx.fillStyle = '#e6edf5';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = '600 15px ui-monospace, Menlo, Consolas, monospace';
  ctx.fillText(fmtCompact(total), cx, cy - 6);
  ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
  ctx.fillStyle = '#9aa7b8';
  ctx.fillText(`${n} legs`, cx, cy + 10);
}

/* Position ladder: vertical price ladder SL / entry / mark / TP + R readout. */
function drawPosLadder(canvas, pos, color) {
  if (!canvas) return;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 300, 120);
  const entry = pos.entry_price, mark = pos.mark_price;
  const sl = pos.stop_loss != null ? pos.stop_loss : pos.sl_price;
  const tp = pos.take_profit != null ? pos.take_profit : pos.tp_price;
  const vals = [entry, mark, sl, tp].filter(isNum);
  if (vals.length < 2) {
    ctx.fillStyle = '#9aa7b8';
    ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('Awaiting position marks…', cssW / 2, cssH / 2);
    return;
  }
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = 12, h = cssH - pad * 2, labelW = 58;
  const xLeft = pad + labelW, xRight = cssW - pad;
  const range = (hi - lo) || 1;
  const yOf = (v) => pad + h - ((v - lo) / range) * h;
  const lines = [
    { v: sl, c: '#ea3943', lbl: 'SL' },
    { v: entry, c: '#9aa7b8', lbl: 'Entry' },
    { v: mark, c: color, lbl: 'Mark' },
    { v: tp, c: '#16c784', lbl: 'TP' },
  ];
  ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
  ctx.textBaseline = 'middle';
  for (const ln of lines) {
    if (!isNum(ln.v)) continue;
    ctx.strokeStyle = ln.c; ctx.lineWidth = ln.lbl === 'Mark' ? 1.8 : 1.2;
    ctx.beginPath(); ctx.moveTo(xLeft, yOf(ln.v)); ctx.lineTo(xRight, yOf(ln.v)); ctx.stroke();
    ctx.fillStyle = ln.c; ctx.textAlign = 'right';
    ctx.fillText(`${ln.lbl} ${fmtPrice(ln.v)}`, xLeft - 4 + labelW, yOf(ln.v));
    ctx.textAlign = 'left';
    ctx.fillStyle = '#9aa7b8';
    ctx.fillText(ln.lbl, pad, yOf(ln.v));
  }
  const r = _rMultiple(pos);
  if (r != null) {
    ctx.fillStyle = r >= 0 ? '#16c784' : '#ea3943';
    ctx.textAlign = 'right'; ctx.font = '600 11px ui-monospace, Menlo, Consolas, monospace';
    ctx.fillText(`${r.toFixed(2)}R`, xRight, pad + 2);
  }
}

/* Liquidation proximity bar: fuller + redder as mark nears liq. */
function drawLiqBar(canvas, mark, liq, side) {
  if (!canvas) return;
  const { ctx, cssW, cssH } = _dprCanvas(canvas, 46, 8);
  ctx.fillStyle = 'rgba(255,255,255,0.10)';
  roundRect(ctx, 0, 0, cssW, cssH, 3); ctx.fill();
  if (!isNum(mark) || !isNum(liq) || liq <= 0 || mark <= 0) return;
  const dist = Math.abs(mark - liq) / mark;         // 0 = at liq
  const prox = 1 - Math.max(0, Math.min(1, dist / LIQ_PROXIMITY)); // 1 = touching
  const col = prox >= MARGIN_RISK_CUTOFFS.danger / 100 ? '#ea3943'
    : prox >= MARGIN_RISK_CUTOFFS.warn / 100 ? '#f5a623' : '#16c784';
  ctx.fillStyle = col;
  roundRect(ctx, 0, 0, cssW * prox, cssH, 3); ctx.fill();
}

function renderPredictions(preds) {
  const arr = Array.isArray(preds) ? preds : [];
  const wrap = $('#mlPreds');
  wrap.innerHTML = '';
  if (arr.length === 0) {
    wrap.append(Object.assign(el('div', 'empty-state'), { textContent: 'No live setups.' }));
    return;
  }
  const minProb = isNum(state.botDetail && state.botDetail.config && state.botDetail.config.min_win_prob)
    ? state.botDetail.config.min_win_prob : null;
  for (const p of arr) {
    const prob = isNum(p.win_prob) ? p.win_prob : 0;
    const row = el('div', 'ml-pred');
    const passes = minProb != null ? prob >= minProb : prob >= 0.5;
    row.innerHTML = `
      <span class="mp-sym">${esc(p.symbol || '—')}</span>
      <span class="mp-bar"><span class="mp-bar-fill" style="width:${Math.max(0, Math.min(100, prob * 100))}%"></span></span>
      <span class="mp-prob ${passes ? 'up' : 'down'}">${fmtProb(prob)}</span>
    `;
    wrap.append(row);
  }
}

/* ---- bot config save ---- */
async function saveBotConfig(e) {
  e.preventDefault();
  const id = state.activeBot;
  if (!id) return;
  const status = $('#bdConfigStatus');
  const btn = $('#bdConfigSave');
  const body = {
    timeframe: $('#cfgTimeframe').value,
    stop_loss_atr: parseFloat($('#cfgStopLoss').value),
    take_profit_r: parseFloat($('#cfgTakeProfit').value),
    leverage: parseInt($('#cfgLeverage').value, 10),
    min_win_prob: parseFloat($('#cfgMinWinProb').value),
    investment_usdt: parseFloat($('#cfgInvest').value),
    stop_loss_pct: parseFloat($('#cfgSlPct').value),
    take_profit_pct: parseFloat($('#cfgTpPct').value),
  };
  // drop NaN fields so we never POST garbage
  Object.keys(body).forEach((k) => { if (typeof body[k] === 'number' && !isFinite(body[k])) delete body[k]; });

  btn.disabled = true;
  status.textContent = 'Saving…'; status.className = 'cfg-status';
  try {
    await apiPost(`/api/bots/${encodeURIComponent(id)}/config`, body);
    status.textContent = 'Saved ✓'; status.className = 'cfg-status ok';
    logLocal('info', `Config updated for ${id}.`);
    loadBotDetail(id);
  } catch (err) {
    status.textContent = `Failed: ${err.message}`; status.className = 'cfg-status err';
    logLocal('error', `Config save failed for ${id}: ${err.message}`);
  } finally {
    btn.disabled = false;
    setTimeout(() => { if (status.textContent.startsWith('Saved')) status.textContent = ''; }, 3000);
  }
}

/* ---- ML retrain ---- */
async function retrainML() {
  const id = state.activeBot;
  if (!id) return;
  const btn = $('#mlRetrain');
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = 'Training…';
  try {
    await apiPost(`/api/bots/${encodeURIComponent(id)}/ml/train`);
    logLocal('info', `Retrain triggered for ${id}.`);
    $('#mlBadge').textContent = 'Training'; $('#mlBadge').className = 'ml-badge ml-training';
  } catch (err) {
    logLocal('error', `Retrain failed for ${id}: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

/* ============================================================
   ACTIONS (REST)
   ============================================================ */
async function apiGet(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
async function apiPost(path, body) {
  const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); detail = j.error || j.detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

async function triggerKill() {
  try {
    logLocal('warn', 'Kill switch requested…');
    const r = await apiPost('/api/kill');
    state.status.kill_switch_active = true;
    (r.actions || []).forEach((a) => logLocal('warn', `kill: ${a}`));
    renderBanners(); updateKillButton();
  } catch (e) {
    logLocal('error', `Kill switch failed: ${e.message}`);
  }
}
async function resetKill() {
  try {
    logLocal('info', 'Resetting kill switch…');
    const r = await apiPost('/api/kill/reset');
    state.status.kill_switch_active = r && r.kill_switch_active === true;
    renderBanners(); updateKillButton();
    logLocal('info', 'Trading re-enabled.');
  } catch (e) {
    logLocal('error', `Reset failed: ${e.message}`);
  }
}

async function toggleBot(bot, running) {
  const id = bot.id;
  state.botBusy[id] = true;
  renderBots(botCache);
  if (state.activeBot === id) applyBotStatus(running ? 'running' : 'stopped', id); // reflect busy
  try {
    const path = running ? `/api/bots/${encodeURIComponent(id)}/stop` : `/api/bots/${encodeURIComponent(id)}/start`;
    const r = await apiPost(path);
    state.botBusy[id] = false;
    if (r && r.bot) {
      replaceBot(r.bot);
      if (state.activeBot === id) applyBotStatus(r.bot.status, id);
      logLocal('info', `Bot ${id} ${running ? 'stopped' : 'started'}.`);
    } else {
      refreshBots();
    }
  } catch (e) {
    state.botBusy[id] = false;
    logLocal('error', `Bot ${id} toggle failed: ${e.message}`);
    refreshBots();
  }
}

/* ============================================================
   DATA FETCH (REST) — initial + polling fallback
   ============================================================ */
async function refreshStatus()    { try { renderStatus(await apiGet('/api/status')); } catch (_) {} }
async function refreshAccount()   { try { const a = await apiGet('/api/account'); renderAccount(a); pushEquity(a && a.equity); } catch (_) {} }
async function refreshPositions() { try { renderPositions(await apiGet('/api/positions')); } catch (_) {} }
async function refreshBots()      { try { setBotCache(await apiGet('/api/bots')); } catch (_) {} }
async function refreshConfig()    { try { renderConfig(await apiGet('/api/config')); } catch (_) {} }

async function fullRefresh() {
  await Promise.allSettled([refreshStatus(), refreshAccount(), refreshPositions(), refreshBots(), refreshConfig()]);
}

let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    // global feed only when WS is down (WS is primary)
    if (!state.wsOpen) { refreshStatus(); refreshAccount(); refreshPositions(); refreshBots(); }
    // active bot detail: poll lightly regardless (covers WS gaps; only active tab)
    if (state.activeBot) loadBotDetail(state.activeBot);
  }, POLL_MS);
}

/* ============================================================
   WEBSOCKET — primary feed, auto-reconnect w/ backoff
   ============================================================ */
let ws = null, wsBackoff = 1000, pingTimer = null;

function connectWS() {
  const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
  setWsState('wait');
  try { ws = new WebSocket(url); } catch (_) { scheduleReconnect(); return; }

  ws.onopen = () => {
    state.wsOpen = true; wsBackoff = 1000;
    setWsState('on');
    logLocal('info', 'WebSocket connected.');
    if (state.activeBot) sendSubscribe(state.activeBot);
    pingTimer = setInterval(() => { try { ws.send(JSON.stringify({ type: 'ping' })); } catch (_) {} }, 25000);
  };

  ws.onmessage = (ev) => {
    let frame;
    try { frame = JSON.parse(ev.data); } catch (_) { return; }
    if (!frame || !frame.type) return;
    dispatchFrame(frame.type, frame.data);
  };

  ws.onclose = () => { cleanupWS(); setWsState('off'); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

/* v2 bot frames carry { bot, ... } in data — ignore if not the active bot */
function isActiveBotFrame(data) {
  if (!data) return false;
  if (data.bot && data.bot !== state.activeBot) return false;
  return state.activeBot != null;
}

function dispatchFrame(type, data) {
  switch (type) {
    case 'status':    renderStatus(data); break;
    case 'account':   renderAccount(data); break;
    case 'positions': renderPositions(data); renderFleet(); if (state.activeBot) renderBotStrip(state.botDetail, state.activeBot); break;
    case 'bots':      setBotCache(data); break;
    case 'log':       if (data) appendLog(data); break;
    case 'equity':    if (data) pushEquity(data.equity); break;
    case 'scanner':   if (isActiveBotFrame(data)) renderScanner(data); break;
    case 'indicators':if (isActiveBotFrame(data)) renderIndicators(data); break;
    case 'ml':        if (isActiveBotFrame(data)) renderML(data); break;
    case 'pong':      break;
    default: break;
  }
}

function cleanupWS() {
  state.wsOpen = false;
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
}
function scheduleReconnect() {
  cleanupWS();
  setWsState('wait');
  setTimeout(connectWS, wsBackoff);
  wsBackoff = Math.min(WS_MAX_BACKOFF, Math.round(wsBackoff * 1.7));
}

/* ============================================================
   KILL SWITCH MODAL
   ============================================================ */
function wireKillModal() {
  const modal = $('#killModal');
  const open = () => { modal.hidden = false; $('#killConfirm').focus(); };
  const close = () => { modal.hidden = true; $('#killBtn').focus(); };
  $('#killBtn').addEventListener('click', () => { if (!$('#killBtn').disabled) open(); });
  $('#killCancel').addEventListener('click', close);
  $('#killConfirm').addEventListener('click', async () => { close(); await triggerKill(); });
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !modal.hidden) close(); });
}

/* ============================================================
   INIT
   ============================================================ */
function init() {
  $('#clearLog').addEventListener('click', () => {
    $('#logBody').innerHTML = '<div class="log-empty">Log cleared.</div>';
  });
  $('#logBody').innerHTML = '<div class="log-empty">Waiting for events…</div>';

  wireKillModal();
  wireNav();
  $('#bdConfigForm').addEventListener('submit', saveBotConfig);
  const startAll = $('#startAllBtn');
  if (startAll) startAll.addEventListener('click', async () => {
    startAll.disabled = true; startAll.textContent = 'Starting…';
    try { await apiPost('/api/bots/start-all', {}); } catch (_) {}
    setTimeout(() => { startAll.disabled = false; startAll.textContent = 'Start all'; }, 3000);
  });
  const stopAll = $('#stopAllBtn');
  if (stopAll) stopAll.addEventListener('click', async () => {
    stopAll.disabled = true;
    try { await apiPost('/api/bots/stop-all', {}); } catch (_) {}
    setTimeout(() => { stopAll.disabled = false; }, 2000);
  });
  $('#mlRetrain').addEventListener('click', retrainML);

  // safe-mode zeros immediately so nothing is blank/NaN
  renderStatus({ env: null, connected: false, kill_switch_active: false });
  renderAccount({});
  renderPositions([]);
  drawSparkline();
  updateSidebarDots();

  fullRefresh();
  connectWS();
  startPolling();

  // click-to-sort positions by uPnL / notional (desc; click again to clear)
  const posTable = $('#positionsTable');
  if (posTable) {
    const ths = posTable.querySelectorAll('thead th');
    const uPnlTh = ths[ths.length - 1];      // uPnL column
    const sizeTh = ths[2];                    // Size (proxy for notional)
    const bindSort = (th, key) => {
      if (!th) return;
      th.classList.add('sortable');
      th.addEventListener('click', () => {
        state.posSort = state.posSort === key ? null : key;
        renderPositions(state.allPositions);
      });
    };
    bindSort(uPnlTh, 'unrealized_pnl');
    bindSort(sizeTh, 'notional');
  }

  window.addEventListener('resize', () => {
    if (state.activeTab === 'overview') { drawSparkline(); renderPositions(state.allPositions); renderFleet(); }
    else if (state.activeBot) {
      const p = state.botDetail && state.botDetail.performance;
      drawCurve('#botEquityCanvas', '#botEquityEmpty', normalizeCurve(p && p.equity_curve), '#16c784');
      if (state.botDetail && state.botDetail.strategy_state) {
        renderStrategyState(state.botDetail.strategy_state, state.activeBot);
      }
      renderBotStrip(state.botDetail, state.activeBot);
    }
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
