/* Treasury rebalancer page. Loads data/index.json + data/<client>.json (+ .live.json) written by
   scripts/publish.py / refresh_holdings.py, and re-runs the move sizing client-side. The Execute
   button talks to the local SafeAgent executor (scripts/executor.py) at EXECUTOR; nothing here signs. */
(() => {
  // Same-origin when the page is served by the executor itself (tunnelled http://127.0.0.1:8743/); otherwise loopback.
  const EXECUTOR = localStorage.getItem('kpk_executor') || (location.port === '8743' ? '' : location.hostname.endsWith('github.io') || location.hostname.endsWith('sslip.io') ? 'https://82-70-94-93.sslip.io' : 'http://127.0.0.1:8743');
  const token = () => localStorage.getItem('kpk_executor_token') || '';
  const authHeaders = (h = {}) => token() ? { ...h, Authorization: 'Bearer ' + token() } : h;
  function askToken(msg) { const t = prompt((msg || 'Executor token') + '\n(stored in this browser only; find it on the box: ~/kpk-treasury-rebalancer/.env.local -> EXECUTOR_TOKEN)', token()); if (t != null) { localStorage.setItem('kpk_executor_token', t.trim()); } return !!token(); }
  const $ = (s, el = document) => el.querySelector(s);
  const usd = (v, d = 0) => '$' + Number(v || 0).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });
  const pct = (v, d = 2) => v == null ? 'n/a' : (Number(v) * 100).toFixed(d) + '%';
  const compact = v => { v = Number(v || 0); return v >= 1e9 ? '$' + (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? '$' + (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? '$' + (v / 1e3).toFixed(0) + 'k' : usd(v); };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const GROUP_COLORS = { USD: '#2D8561', ETH: '#1D1D1D', EURO: '#8E6710', OTHER: '#706E66' };
  const srcTag = b => b.in_roles === false || b.apy_source === 'NOT IN ROLES' ? `<span class="src bad" title="listed by the Strategy API but not a target in the on-chain Roles (pending PUR); never proposed">not in Roles</span>`
    : b.apy == null ? '' : b.apy_source && b.apy_source !== 'vaults.fyi' ? `<span class="src" title="${esc(b.apy_source)}">${b.apy_source.startsWith('defillama') ? 'llama' : b.apy_source.includes('stale') ? 'stale' : b.apy_source === 'vaults.fyi live' ? 'live' : b.apy_source.startsWith('vaults.fyi') ? '' : 'realised'}</span>` : '';

  let index = null, snap = null, live = null, moves = [], executorOn = false;
  let sim = { pickupBps: 50, moveUsd: 250000, tvlCapPct: 10, basis: 'apy', exclude: new Set() };

  async function loadJSON(p) { const r = await fetch(p, { cache: 'no-store' }); if (!r.ok) throw new Error(p + ' ' + r.status); return r.json(); }

  async function init() {
    try { index = await loadJSON('data/index.json'); }
    catch (e) { $('#stamp').textContent = 'No snapshot yet. Run `python scripts/publish.py`.'; return; }
    const tabs = $('#clientTabs');
    tabs.innerHTML = index.clients.map((c, i) => `<button role="tab" data-c="${c.client}" aria-pressed="${i === 0}">${esc(c.display_name)}</button>`).join('');
    tabs.addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; [...tabs.children].forEach(x => x.setAttribute('aria-pressed', x === b)); select(b.dataset.c); });
    $('#viewTabs').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; showView(b.dataset.v); });
    const q = new URLSearchParams(location.search);
    const first = q.get('client') || index.clients[0]?.client;
    if (first) { [...tabs.children].forEach(x => x.setAttribute('aria-pressed', x.dataset.c === first)); await select(first); }
    showView(q.get('view') || 'holdings');
    await pingExecutor();
    bindModal();
    $('#refreshBtn').onclick = refreshClient;
  }

  function showView(v) {
    document.querySelectorAll('.view').forEach(el => el.hidden = el.dataset.view !== v);
    [...$('#viewTabs').children].forEach(x => x.setAttribute('aria-pressed', x.dataset.v === v));
    const q = new URLSearchParams(location.search); q.set('view', v); history.replaceState(null, '', '?' + q);
  }

  async function fromExecutorOrSite(name) {
    if (executorOn) { try { return await loadJSON(EXECUTOR + '/data/' + name); } catch (e) { /* fall through */ } }
    return loadJSON('data/' + name);
  }
  async function select(slug) {
    snap = await fromExecutorOrSite(slug + '.json');
    try { live = await fromExecutorOrSite(slug + '.live.json'); } catch (e) { live = null; }
    const q = new URLSearchParams(location.search); q.set('client', slug); history.replaceState(null, '', '?' + q);
    sim.pickupBps = snap.thresholds.min_pickup_bps; sim.moveUsd = snap.thresholds.min_move_usd; sim.tvlCapPct = snap.thresholds.venue_tvl_cap_pct;
    sim.exclude = new Set(snap.thresholds.exclude || []);
    render(); renderLive();
  }

  /* ------------------------------------------------------------------ holdings */
  function renderLive() {
    const sec = $('#liveSection');
    if (!live) { sec.hidden = true; return; }
    sec.hidden = false;
    if (!live.ok) {
      $('#liveCards').innerHTML = card('Live refresh failed', '—', esc(live.error || ''));
      $('#liveBox').innerHTML = `Last attempt ${new Date(live.as_of).toUTCString()}. The stored snapshot below is unaffected.`;
      return;
    }
    const drift = (live.nav_usd - snap.reconciliation.syncrone_nav_usd) / snap.reconciliation.syncrone_nav_usd;
    const ageH = (Date.now() - new Date(live.as_of)) / 36e5;
    const tc = live.token_check;
    $('#liveCards').innerHTML = [
      card('Live NAV (Syncrone)', compact(live.nav_usd), `${ageH < 1.5 ? 'under an hour' : Math.round(ageH) + ' h'} old · ${new Date(live.as_of).toISOString().slice(0, 16).replace('T', ' ')} UTC`),
      card('vs stored snapshot', (drift >= 0 ? '+' : '') + pct(drift), `<span class="pill ${Math.abs(drift) > 0.02 ? 'p-warn' : 'p-good'}">${Math.abs(drift) > 0.02 ? 'drifted, re-run' : 'in line'}</span>`),
      card('Idle at 0%', compact(live.idle_usd), live.idle.map(i => `${Number(i.balance).toLocaleString('en-US', { maximumFractionDigits: 2 })} ${esc(i.symbol)}`).join(', ') || 'none'),
      card('In-flight', compact(live.in_flight_usd), live.in_flight_items.map(i => `${Number(i.balance).toLocaleString('en-US', { maximumFractionDigits: 2 })} ${esc(i.asset)} in ${esc(i.protocol)}`).join(', ') || 'none'),
      card('Safe = Etherscan', `${tc.checked - tc.disagree}<span class="u">/ ${tc.checked}</span>`, `<span class="pill ${tc.disagree ? 'p-bad' : 'p-good'}">${tc.disagree ? 'disagree' : 'agree'}</span> · via ${esc(tc.etherscan_source)}`),
    ].join('');
    const stored = {}; snap.book.forEach(b => { if (b.kind !== 'idle') { const k = b.protocol + '/' + b.asset_group; stored[k] = (stored[k] || 0) + b.usd; } });
    const rows = live.sleeves.map(s => { const k = s.protocol + '/' + s.asset_group; const st = stored[k] || 0; const d = st ? (s.usd - st) / st : null;
      return `<tr><td class="k">${esc(s.protocol)}</td><td>${s.asset_group}</td><td class="dim">${esc(s.positions.join('; '))}</td><td class="n num">${usd(s.usd)}</td><td class="n num dim">${st ? usd(st) : '—'}</td><td class="n num ${d != null && Math.abs(d) > 0.02 ? 'k' : 'dim'}">${d == null ? 'new' : (d >= 0 ? '+' : '') + pct(d, 1)}</td></tr>`; }).join('');
    $('#liveBox').innerHTML = `<b>By protocol, live vs stored.</b> Live figures are Syncrone's book for this Safe on chain ${live.chain_id}; the stored column is the last local run. No APY here: the Strategy API is not reachable from GitHub.
      <div class="scroll" style="margin-top:8px"><table><thead><tr><th>Protocol</th><th>Group</th><th>Positions</th><th class="n">Live</th><th class="n">Stored</th><th class="n">Δ</th></tr></thead><tbody>${rows}</tbody></table></div>` +
      (live.flags.length ? `<div class="fine" style="margin-top:8px">${live.flags.map(esc).join('<br>')}</div>` : '');
  }

  function render() {
    $('#app').hidden = false;
    const r = snap.reconciliation;
    $('#stamp').innerHTML = `<b>${esc(snap.display_name)}</b> · chain ${snap.chain_id} · run ${esc(snap.run_folder)}`;
    if (snap.stale_note) { $('#stamp2').title = snap.stale_note; }
    const src = snap.apy_sources || {}; const gate = snap.roles_gate || {};
    const nLlama = Object.keys(src).filter(k => k.startsWith('defillama')).reduce((a, k) => a + src[k], 0);
    const nGated = (gate.excluded || []).length;
    const when = new Date(snap.as_of).toISOString().slice(0, 16).replace('T', ' ');
    // keep the masthead line short; the provenance detail lives in the tooltip
    $('#stamp2').innerHTML = `${when} UTC · ${snap.period} · ${src['vaults.fyi live'] || 0} live APYs`
      + (nGated ? ` · <b class="bad">${nGated} venue${nGated > 1 ? 's' : ''} not in Roles</b>` : '')
      + (snap.stale_note ? ' · <span class="bad">off-network</span>' : '');
    $('#stamp2').title = [`Data as of ${when} UTC · APY period ${snap.period}`,
      `${src['vaults.fyi live'] || 0} venues priced live by vaults.fyi${nLlama ? `, ${nLlama} via DeFiLlama` : ''}`,
      gate.checked ? `${gate.n_targets} on-chain Roles targets checked${nGated ? `; excluded: ${(gate.excluded || []).map(e => e.protocol + '/' + e.asset).join(', ')}` : '; all venues verified'}`
                   : 'Venues NOT verified against the on-chain Roles file',
      snap.stale_note || ''].filter(Boolean).join('
');

    const groups = {}; snap.book.forEach(b => groups[b.asset_group] = (groups[b.asset_group] || 0) + b.usd);
    const nav = snap.nav_usd; const stables = (groups.USD || 0) + (groups.EURO || 0);
    const priced = snap.book.filter(b => b.kind === 'position' && b.apy != null);
    const blended = weightedApy(priced);
    const idle = snap.book.filter(b => b.kind === 'idle').reduce((s, b) => s + b.usd, 0);
    const diffOk = r.diff == null || r.diff <= 0.01;
    $('#navCards').innerHTML = [
      card('NAV (book)', compact(nav), `Syncrone ${compact(r.syncrone_nav_usd)}`),
      card('Stables / volatile', `${(100 * stables / nav).toFixed(1)}<span class="u">/ ${(100 * (groups.ETH || 0) / nav).toFixed(1)}%</span>`, 'by market value'),
      card('Blended APY (priced)', pct(blended), `${priced.length} positions priced`),
      card('Idle at 0%', compact(idle), idle > 0 ? 'deployable' : 'none'),
      card('In-flight', compact(r.in_flight_usd), 'withdrawal queues'),
      card('Untracked by optimizer', compact(r.untracked_usd), 'no vaults.fyi feed'),
      card('Reconciliation', r.diff == null ? 'n/a' : pct(r.diff), `<span class="pill ${diffOk ? 'p-good' : 'p-bad'}">${diffOk ? 'ties' : 'STOP'}</span> · Safe=Etherscan ${r.tokens_checked - r.tokens_disagree}/${r.tokens_checked}`),
    ].join('');
    const others = Object.entries(r.other_wallets || {});
    $('#reconBox').innerHTML = `<b>Bridge.</b> Syncrone NAV ${usd(r.syncrone_nav_usd)} against Strategy API positions ${usd(r.strategy_api_usd)} + untracked ${usd(r.untracked_usd)} + in-flight ${usd(r.in_flight_usd)} + idle ${usd(r.idle_usd)} = ${usd(r.bridge_usd)}. Tolerance 1%.` +
      (others.length ? `<div class="fine" style="margin-top:6px">Other wallets/chains in the same Syncrone org, not in this view: ${others.map(([k, v]) => `${esc(k)} ${compact(v)}`).join(', ')}.</div>` : '') +
      (snap.debank ? `<div style="margin-top:8px"><b>Independent read (DeBank, live prices).</b> ${usd(snap.debank.nav_usd)} across ${Object.keys(snap.debank.protocols || {}).length} protocols${snap.debank.diff_vs_syncrone != null ? `, ${(snap.debank.diff_vs_syncrone * 100).toFixed(2)}% from Syncrone's mark (price timing, not a position gap)` : ''}. ` +
        (snap.debank.notes && snap.debank.notes.length ? `<span class="bad">${snap.debank.notes.map(esc).join(' · ')}</span>` : '<span class="pill p-good">every protocol Syncrone books, DeBank sees, within 8%</span>') + `</div>` : '');

    // composition cards: per group, protocol shares
    $('#composition').innerHTML = `<div class="comp">` + Object.keys(groups).sort().map(g => {
      const byP = {}; snap.book.filter(b => b.asset_group === g).forEach(b => { const k = b.kind === 'idle' ? 'idle' : b.protocol; byP[k] = (byP[k] || 0) + b.usd; });
      const rows = Object.entries(byP).sort((a, b) => b[1] - a[1]);
      return `<div class="card"><div class="t">${g}</div><div class="v sm">${compact(groups[g])} <span class="u">${(100 * groups[g] / nav).toFixed(1)}% of NAV</span></div>
        <div class="bar">${rows.map(([k, v], i) => `<span title="${esc(k)} ${compact(v)}" style="width:${100 * v / groups[g]}%;background:${k === 'idle' ? 'var(--warn)' : GROUP_COLORS[g] || '#999'};opacity:${k === 'idle' ? 1 : 0.35 + 0.65 * (1 - i / Math.max(1, rows.length))}"></span>`).join('')}</div>
        <div class="rows">${rows.map(([k, v]) => `<div><span class="${k === 'idle' ? 'dim' : ''}">${esc(k)}</span><span class="num">${usd(v)} · ${(100 * v / nav).toFixed(1)}%</span></div>`).join('')}</div></div>`;
    }).join('') + `</div>`;

    // policy
    const ps = $('#policySection');
    if (snap.policy) {
      ps.hidden = false; $('#policyTitle').textContent = snap.policy.name;
      $('#policyTable tbody').innerHTML = snap.policy_checks.map(k => `<tr><td class="k">${esc(k.check)}</td><td><span class="pill ${{ breached: 'p-bad', near: 'p-warn', 'in-bounds': 'p-good' }[k.status]}">${k.status}</span></td><td>${esc(k.value)}${k.effective_stable_target_pct ? ` · effective stable target <b>${k.effective_stable_target_pct}%</b>` : ''}</td></tr>`).join('');
    } else { ps.hidden = false; $('#policyTitle').textContent = 'No investment policy on file'; $('#policyTable tbody').innerHTML = '<tr><td colspan="3" class="dim">Pure yield optimisation within the Roles permissions. Add a policy block in clients.json to enable checks.</td></tr>'; }

    $('#flags').innerHTML = snap.flags.length ? snap.flags.map(f => `<li class="${/^NAV:|STOP/.test(f) ? 'stop' : ''}">${esc(f)}</li>`).join('') : '<li>No flags.</li>';

    /* ---------------------------------------------------------------- strategies */
    $('#groups').innerHTML = Object.keys(groups).sort().map(g => {
      const rows = snap.book.filter(b => b.asset_group === g).sort((a, b) => b.usd - a.usd);
      const tot = groups[g]; const ap = weightedApy(rows.filter(b => b.kind === 'position' && b.apy != null));
      return `<div class="grp"><div class="grp-h"><h3>${g}</h3><div class="tot"><b>${compact(tot)}</b> · ${(100 * tot / nav).toFixed(1)}% of NAV${ap != null ? ` · blended <b>${pct(ap)}</b>` : ''}</div></div>
        <div class="scroll"><table><thead><tr><th>Protocol</th><th>Venue</th><th class="n">Value</th><th class="n">Share</th><th class="n">APY</th><th class="n">30d</th></tr></thead><tbody>
        ${rows.map(b => `<tr class="${b.kind === 'idle' ? 'idle' : b.kind === 'in_flight' ? 'inflight' : ''}"><td class="k">${esc(b.protocol)}</td><td>${esc(b.venue)}${b.untracked && b.apy == null ? ' <span class="pill p-warn">untracked</span>' : ''}</td><td class="n num">${usd(b.usd)}</td><td class="n num">${(100 * b.usd / nav).toFixed(1)}%</td><td class="n num">${b.apy == null ? '<span class="dim">n/a</span>' : pct(b.apy) + srcTag(b)}</td><td class="n num dim">${apy30(b)}</td></tr>`).join('')}
        </tbody></table></div></div>`;
    }).join('');

    const protos = [...new Set(snap.permitted.map(p => p.protocol))].sort();
    $('#exclude').innerHTML = protos.map(p => `<button data-p="${esc(p)}" aria-pressed="${sim.exclude.has(p)}">${esc(p)}</button>`).join('');
    $('#exclude').onclick = e => { const b = e.target.closest('button'); if (!b) return; sim.exclude.has(b.dataset.p) ? sim.exclude.delete(b.dataset.p) : sim.exclude.add(b.dataset.p); b.setAttribute('aria-pressed', sim.exclude.has(b.dataset.p)); simulate(); };
    $('#basis').onclick = e => { const b = e.target.closest('button'); if (!b) return; [...$('#basis').children].forEach(x => x.setAttribute('aria-pressed', x === b)); sim.basis = b.dataset.b; simulate(); };
    bindRange('#pickup', 'pickupBps', v => v + ' bps'); bindRange('#move', 'moveUsd', v => compact(v)); bindRange('#tvl', 'tvlCapPct', v => v + '% of venue TVL (incl. holdings)');
    simulate();

    $('#opsCaveat').textContent = snap.ops_tools_caveat || '';
    $('#ops').innerHTML = snap.ops_tools.length ? snap.ops_tools.map(o => `<div class="opsrow"><b>${o.asset_group}</b>: ${pct(o.current_apy)} → ${pct(o.recommended_apy)} by moving ${compact(o.changed_usd)}. ${o.allocations.map(a => `${esc(a.protocol)}/${esc(a.venue)} → ${compact(a.usd)} @ ${pct(a.apy)}`).join('; ')}</div>`).join('') : '<p class="empty">No optimizer output for this client/chain.</p>';
  }

  function apy30(b) { const p = snap.permitted.find(p => p.protocol === b.protocol && p.priced && p.asset_group === b.asset_group && (p.asset === b.symbol || (p.vault && (b.venue || '').toLowerCase().includes((p.asset || '').toLowerCase())))); return p && p.apy_30d != null ? pct(p.apy_30d) : ''; }
  function card(t, v, foot) { return `<div class="card"><div class="t">${t}</div><div class="v num">${v}</div><div class="foot">${foot || ''}</div></div>`; }
  function weightedApy(rows) { const d = rows.reduce((s, b) => s + b.usd, 0); return d ? rows.reduce((s, b) => s + b.usd * b.apy, 0) / d : null; }
  function bindRange(sel, key, fmt) { const el = $(sel); el.value = sim[key]; const lab = $(sel + 'Val'); lab.textContent = fmt(sim[key]); el.oninput = () => { sim[key] = Number(el.value); lab.textContent = fmt(sim[key]); simulate(); }; }

  /* Matches same_venue() in scripts/assess.py: the vault address decides when both sides carry one,
     otherwise protocol + name. Used both to skip the venue a source already sits in and to work out
     how much of a venue we already own, which the TVL threshold has to account for. */
  const normv = s => String(s ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  function sameVenue(b, v) {
    if (String(b.protocol || '').toLowerCase() !== String(v.protocol || '').toLowerCase()) return false;
    const bv = String(b.vault || '').toLowerCase(), vv = String(v.vault || '').toLowerCase();
    if (bv && vv) return bv === vv;
    const va = normv(v.asset), bs = normv(b.symbol), bn = normv(b.venue);
    if (!va) return false;
    return va === bs || bn.includes(va) || (!!bs && va.includes(bs));
  }
  const heldIn = v => snap.book.filter(b => (b.kind === 'position' || b.kind === 'in_flight') && sameVenue(b, v))
    .reduce((s, b) => s + b.usd, 0);
  /* Supplying into a lending market spreads the same borrower interest over a bigger base, so the rate
     we actually receive is below the headline. Assuming borrow demand is unchanged in the short run,
     the pool rate falls to r x TVL / (TVL + what we add). Our existing stake in that venue dilutes too. */
  const diluted = (r, tvl, added) => (tvl && added > 0) ? r * tvl / (tvl + added) : r;

  /* Same method as scripts/assess.py: per asset group, fill the best permitted venues from idle
     balances and laggards, bounded by venue TVL cap and policy protocol-cap headroom. */
  function simulate() {
    const nav = snap.nav_usd; const cap = snap.policy?.protocol_cap_pct_nav;
    const byProto = {}; snap.book.forEach(b => { if (b.kind !== 'idle') byProto[b.protocol] = (byProto[b.protocol] || 0) + b.usd; });
    const minPick = sim.pickupBps / 1e4; moves = []; let before = 0, den = 0, idleDeployed = 0;
    const heldMap = new Map(), dep = new Map();   // venue -> what we already hold / what this plan adds
    // one protocol-cap budget shared by every venue of that protocol, spent as moves are allocated
    const protoRoom = new Map();
    Object.keys(byProto).concat(snap.permitted.map(p => p.protocol)).forEach(p => {
      if (!protoRoom.has(p)) protoRoom.set(p, cap ? Math.max(0, cap / 100 * nav - (byProto[p] || 0)) : Infinity);
    });
    const apyOf = p => (sim.basis === 'apy_30d' && p.apy_30d != null) ? p.apy_30d : (sim.basis === 'apy_1d' && p.apy_1d != null) ? p.apy_1d : p.apy;
    const STABLE_SYMS = ['USDC', 'USDT', 'USDS', 'DAI', 'GHO', 'EURC', 'PYUSD', 'RLUSD'];
    const isStable = s => STABLE_SYMS.some(t => (s || '').toUpperCase().includes(t));
    const IDLE_FLOOR = 5000;
    for (const g of [...new Set(snap.book.map(b => b.asset_group))]) {
      if (g === 'OTHER') continue;   // governance / non-yield tokens are never rotated
      const pos = snap.book.filter(b => b.asset_group === g && b.kind === 'position' && b.apy != null && !b.untracked);
      const idle = snap.book.filter(b => b.asset_group === g && b.kind === 'idle' && b.usd >= IDLE_FLOOR);
      const venues = snap.permitted.filter(p => p.asset_group === g && p.priced && apyOf(p) != null && !sim.exclude.has(p.protocol)).sort((a, b) => apyOf(b) - apyOf(a));
      pos.forEach(b => { before += b.usd * b.apy; den += b.usd; });
      if (!venues.length) continue;
      const best = apyOf(venues[0]);
      venues.forEach(v => { if (!heldMap.has(v)) heldMap.set(v, heldIn(v)); });
      // the threshold governs the resulting position, so subtract what we already own in the venue
      const headroom = new Map(venues.map(v => [v, v.tvl_usd ? Math.max(0, sim.tvlCapPct / 100 * v.tvl_usd - heldMap.get(v)) : Infinity]));
      const sources = [...idle.map(b => ({ ...b, apy: 0 })), ...pos.filter(b => sim.exclude.has(b.protocol) || (best - b.apy >= minPick && b.usd >= sim.moveUsd)).sort((a, b) => a.apy - b.apy)];
      for (const src of sources) {
        let rem = src.usd;
        for (const v of venues) {
          if (rem < sim.moveUsd && src.kind !== 'idle') break;
          if (sameVenue(src, v)) continue;
          const st = (src.symbol || '').toUpperCase(), vt = (v.asset || '').toUpperCase();
          const compatible = st === vt || (isStable(st) && isStable(vt)) || g === 'ETH';
          if (!compatible) continue;
          const amt = Math.min(rem, headroom.get(v), protoRoom.get(v.protocol) ?? Infinity);
          // the pickup is judged on the rate we would actually receive once this money lands
          const pick = diluted(apyOf(v), v.tvl_usd, (dep.get(v) || 0) + Math.max(amt, 0)) - src.apy;
          if (pick < minPick && src.kind === 'position' && !sim.exclude.has(src.protocol)) break;
          if (amt < Math.min(sim.moveUsd, rem) || amt <= 0) continue;
          moves.push({ id: moves.length, g, from: src, to: v, amt, pick, toApy: apyOf(v), forced: sim.exclude.has(src.protocol) });
          dep.set(v, (dep.get(v) || 0) + amt);
          headroom.set(v, headroom.get(v) - amt);
          protoRoom.set(v.protocol, (protoRoom.get(v.protocol) ?? Infinity) - amt);
          rem -= amt; if (src.kind === 'idle') idleDeployed += amt;
          if (rem <= 0) break;
        }
      }
    }
    // several moves can land in the same venue, so settle every rate against that venue's plan total
    let dilutionCost = 0;
    for (const [v, added] of dep) dilutionCost += (heldMap.get(v) || 0) * (apyOf(v) - diluted(apyOf(v), v.tvl_usd, added));
    moves.forEach(m => {
      const added = dep.get(m.to) || 0;
      m.toApyDiluted = diluted(apyOf(m.to), m.to.tvl_usd, added);
      m.pick = m.toApyDiluted - m.from.apy;
      m.shareAfter = m.to.tvl_usd ? ((heldMap.get(m.to) || 0) + added) / (m.to.tvl_usd + added) : null;
    });
    const gross = moves.reduce((s, m) => s + m.amt * m.pick, 0);
    const pickup = gross - dilutionCost; const after = before + pickup; den += idleDeployed;
    const headlineGross = moves.reduce((s, m) => s + m.amt * (m.toApy - m.from.apy), 0);
    const gx = (snap.roles_gate && snap.roles_gate.excluded) || [];
    let gateNote = $('#gateNote'); if (!gateNote) { gateNote = document.createElement('div'); gateNote.id = 'gateNote'; gateNote.className = 'sw-note'; $('#simOut').parentNode.insertBefore(gateNote, $('#simOut')); }
    gateNote.hidden = !gx.length;
    gateNote.innerHTML = gx.length ? `<div><b>Excluded, not in on-chain Roles:</b> ${gx.map(e => { const p = snap.permitted.find(q => q.vault === e.vault); return `${esc(e.protocol)} ${esc(e.asset)}${p && p.apy_total != null ? ` (${pct(p.apy_total)})` : ''}`; }).join(', ')}. The Strategy API lists them as permitted, but the Safe cannot call them until the PUR lands, so they never appear as candidates here.</div>` : '';
    $('#simOut').innerHTML = [
      ['Candidate moves', moves.length], ['Capital moved', compact(moves.reduce((s, m) => s + m.amt, 0))],
      ['Blended APY before', den ? pct(before / (den - idleDeployed)) : 'n/a'], ['Blended APY after', den ? pct(after / den) : 'n/a'],
      ['Yield dilution', headlineGross ? '-' + compact(headlineGross - gross + dilutionCost) : '$0'],
      ['Pickup per year', compact(pickup)],
    ].map(([t, v]) => `<div class="o"><div class="t">${t}</div><div class="v num">${v}</div></div>`).join('');
    $('#simMoves').innerHTML = moves.length ? moves.map(m => `<div class="mv ${m.from.kind === 'idle' ? 'idle' : ''}"><div class="path"><b>${m.g}</b> · ${esc(m.from.protocol)} ${esc(m.from.venue)} <span class="arr">→</span> ${esc(m.to.protocol)} ${esc(m.to.asset)} <small>(${m.to.action})</small><br><small>${pct(m.from.apy)} → <b>${pct(m.toApyDiluted)}</b>${m.toApyDiluted < m.toApy - 1e-6 ? ` after dilution (${pct(m.toApy)} headline)` : ''}${m.to.tvl_usd ? ` · venue TVL ${compact(m.to.tvl_usd)}` : ''}${m.shareAfter != null ? ` · our share after ${(m.shareAfter * 100).toFixed(1)}%` : ''}${m.forced ? ' · excluded venue: exit is mandatory' : ''}</small></div><div class="amt num">${usd(m.amt)}<small>+${usd(m.amt * m.pick)}/yr</small></div><button class="btn ${executorOn ? '' : 'ghost'}" data-exec="${m.id}" type="button" title="${executorOn ? 'Build, simulate and propose via the local SafeAgent executor' : 'Start scripts/executor.py to enable'}">Execute</button></div>`).join('')
      : '<p class="empty">No move clears these thresholds. Laggards are noted, not traded.</p>';
    $('#simMoves').onclick = e => { const b = e.target.closest('button[data-exec]'); if (b) openModal(moves[Number(b.dataset.exec)]); };
    renderRewards(); renderStage2(); renderSwap();
  }

  /* ------------------------------------------------------------------ rewards sweep */
  const rewardSel = {};   // client -> { rowKey: checked }
  const sw_groups = () => (typeof sw !== 'undefined' && sw.groups) || [];
  const pairOk_ = (s, b) => sw_groups().some(g => g.sell.some(x => x.toUpperCase() === String(s).toUpperCase()) && g.buy.some(x => x.toUpperCase() === String(b).toUpperCase()));
  function renderRewards() {
    const box = $('#rewardsBox');
    const sw = snap.rewards_sweep; const co = (sw && sw.claim_only) || [];
    if (!sw || ((!sw.items || !sw.items.length) && !co.length)) { box.innerHTML = '<p class="empty">Nothing claimable for this client right now. Reward tokens already in the Safe are listed under Holdings → REWARDS and can be sold from the Swap panel.</p>'; return; }
    const doSwap = $('#sweepToggle').checked;
    $('#sweepToggle').onchange = renderRewards;
    const best = sw.best_usd_venue;
    const n4 = v => Number(v || 0).toLocaleString('en-US', { maximumFractionDigits: 4 });
    // one row per claim / held token, Uniswap fee positions included (claim-only, never swapped)
    const rows = (sw.items || []).map((i, n) => ({ key: 'i' + n + i.symbol, kind: 'sweep', item: i, source: i.source, token: i.symbol, amount: n4(i.amount), usd: i.usd,
      claimable: i.claimable, claim_cmd: i.claim_cmd, state: (i.claimable ? 'claimable' : 'held in Safe') + (i.usd < sw.min_usd ? ' · below sweep floor' : '') + (doSwap && i.symbol.toUpperCase() !== 'USDC' && sw_groups().length && !pairOk_(i.symbol, 'USDC') ? ' · no CoW route to USDC' : ''), def: i.usd >= sw.min_usd }));
    const byPos = {}; co.forEach(c => { (byPos[c.token_id] = byPos[c.token_id] || []).push(c); });
    Object.entries(byPos).forEach(([tid, cs]) => rows.push({ key: 'u' + tid, kind: 'uni', source: 'uniswap v3 fees · #' + tid, token: cs.map(c => c.symbol).join(' + '), amount: cs.map(c => n4(c.amount)).join(' + '),
      usd: cs.reduce((s, c) => s + c.usd, 0), claimable: true, claim_cmd: cs[0].claim_cmd, state: 'claim only · fees stay as pool tokens', def: true }));
    const sel = rewardSel[snap.client] || (rewardSel[snap.client] = {});
    rows.forEach(r => { if (!(r.key in sel)) sel[r.key] = r.def; });
    const chosen = rows.filter(r => sel[r.key]);
    const claimCmds = [...new Set(chosen.filter(r => r.claimable && r.claim_cmd).map(r => r.claim_cmd))];
    const sweepItems = chosen.filter(r => r.kind === 'sweep').map(r => r.item);
    const routable = t => !sw_groups().length || pairOk_(t, 'USDC');
    const swapTokens = [...new Set(sweepItems.map(i => i.symbol).filter(t => t.toUpperCase() !== 'USDC' && routable(t)))];
    const heldBack = [...new Set(sweepItems.map(i => i.symbol).filter(t => t.toUpperCase() !== 'USDC' && !routable(t)))];
    const extra = [...new Set(chosen.filter(r => r.kind === 'uni').map(r => r.claim_cmd))];
    const total = chosen.reduce((s, r) => s + r.usd, 0);
    const swapMode = doSwap && sweepItems.length > 0;
    const canRun = executorOn && chosen.length > 0 && (swapMode ? !!best : claimCmds.length > 0);
    let title, summary;
    if (!chosen.length) { title = 'Nothing selected'; summary = ' · tick the claims to bundle into one transaction'; }
    else if (swapMode) {
      title = 'Claim, swap and deposit';
      summary = `${claimCmds.length ? 'claim (' + esc(claimCmds.join(', ')) + ') → ' : ''}CoW swap <b>${esc(swapTokens.join(', ') || 'nothing')}</b> to USDC${heldBack.length ? ` <small>(${esc(heldBack.join(', '))}: no permitted route to USDC, claimed and held)</small>` : ''} → deposit into <b>${best ? esc(best.protocol + ' ' + best.asset) : 'no permitted USDC venue'}</b>${best ? ` <small>(${pct(best.apy)})</small>` : ''}<br><small>one Safe transaction bundles the claims and one pre-signed CoW order per token; the USDC deposit follows once the orders fill</small>`;
    } else {
      title = 'Claim only';
      summary = `${esc(claimCmds.join(', ')) || 'nothing to claim (only held tokens selected)'}<br><small>one transaction; tokens stay in the Safe</small>`;
    }
    const allOn = rows.every(r => sel[r.key]);
    box.innerHTML = `<div class="grp"><div class="grp-h"><h3>Rewards</h3><div class="tot"><b>${compact(rows.reduce((s, r) => s + r.usd, 0))}</b> claimable · <b>${chosen.length}</b> of ${rows.length} selected · sweep floor ${usd(sw.min_usd)}</div></div>
      <div class="scroll"><table><thead><tr><th class="ck"><input type="checkbox" id="rwAll" ${allOn ? 'checked' : ''} title="select all"></th><th>Source</th><th>Token</th><th class="n">Amount</th><th class="n">Value</th><th>State</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${sel[r.key] ? '' : 'inflight'}"><td class="ck"><input type="checkbox" data-k="${esc(r.key)}" ${sel[r.key] ? 'checked' : ''}></td><td class="k">${esc(r.source)}</td><td>${esc(r.token)}</td><td class="n num">${esc(r.amount)}</td><td class="n num">${usd(r.usd)}</td><td>${esc(r.state)}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="mv" style="margin-top:10px"><div class="path"><b>${title}</b>${chosen.length ? ' · ' : ''}${summary}</div>
      <div class="amt num">${compact(total)}</div><button class="btn ${canRun ? '' : 'ghost'}" id="sweepBtn" type="button" ${canRun ? '' : 'disabled'}>Execute</button></div></div>`;
    box.querySelectorAll('input[data-k]').forEach(cb => cb.onchange = () => { sel[cb.dataset.k] = cb.checked; renderRewards(); });
    $('#rwAll').onchange = e => { rows.forEach(r => sel[r.key] = e.target.checked); renderRewards(); };
    $('#sweepBtn').onclick = () => swapMode ? openSweep(sweepItems, best, extra) : openClaimOnly(claimCmds.join(' ; '));
  }
  function openClaimOnly(cmd) {
    const cmds = cmd.split(' ; ').map(s => s.trim()).filter(Boolean);
    cur = { move: null, plan: null, stage2: { commands: cmds, label: cmd, wait_for: '' }, claimOnly: true };
    $('#mTitle').textContent = `Execute · ${snap.display_name} · claim`;
    $('#mParams').innerHTML = [['Client / chain', `${snap.client} · ${snap.chain_id}`], ['Avatar Safe', (snap.safes && snap.safes.avatar) || snap.avatar_safe], ['Action', 'CLAIM only'], ['Command', cmd]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
    $('#mPlan').hidden = true; $('#mSim').hidden = true; $('#mMsg').className = 'modal-msg';
    $('#mMsg').textContent = 'Builds the collect() call through the bot; the fee tokens land in the Safe. No swap, no deposit.';
    $('#mBuild').disabled = !executorOn; $('#mPropose').disabled = true; steps({}); $('#modal').hidden = false;
  }
  function openSweep(items, best, extra = []) {
    cur = { move: null, plan: null, sweep: { items, to: best, extra } };
    $('#mTitle').textContent = `Execute · ${snap.display_name} · rewards sweep`;
    $('#mParams').innerHTML = [['Client / chain', `${snap.client} · ${snap.chain_id}`], ['Avatar Safe', (snap.safes && snap.safes.avatar) || snap.avatar_safe],
      ['Action', 'CLAIM rewards → CoW swap to USDC → DEPOSIT (2 stages)'], ['Tokens', items.map(i => `${Number(i.amount).toLocaleString('en-US', { maximumFractionDigits: 4 })} ${i.symbol} (${i.source})`).join(', ')], ...(extra.length ? [['Also claim', extra.join(', ')]] : []),
      ['Deposit into', `${best.protocol} · ${best.asset}${best.vault ? ' · ' + best.vault : ''}`], ['Value', usd(items.reduce((s, i) => s + i.usd, 0))]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
    $('#mPlan').hidden = true; $('#mSim').hidden = true; $('#mMsg').className = 'modal-msg';
    $('#mMsg').textContent = 'Stage 1 builds the claim steps and one pre-signed CoW order per reward token; stage 2 (deposit) is offered once the orders fill.';
    $('#mBuild').disabled = !executorOn; $('#mPropose').disabled = true; steps({}); $('#modal').hidden = false;
  }

  /* ------------------------------------------------------------------ swap (CoW, within permissions) */
  // Layout follows the common DEX pattern (Uniswap, swap.cow.fi, 1inch): sell card over buy card, flip button,
  // token picker with balances, rate line, collapsible order details, one CTA whose label is the state.
  const SETTLEMENT = ['USDC', 'USDT', 'DAI', 'USDS', 'GHO', 'ETH', 'WETH', 'WBTC', 'WSTETH', 'STETH', 'RETH'];
  const sw = { groups: [], known: null, sell: null, buy: null, quote: null, timer: null, tick: null, inverted: false, picking: null, seq: 0, live: null, liveAt: null, liveBusy: false };
  const fmtTok = (v, d) => { v = Number(v || 0); if (d == null) d = v >= 1000 ? 2 : v >= 1 ? 4 : 6; return v.toLocaleString('en-US', { maximumFractionDigits: d }); };
  const upper = s => String(s || '').toUpperCase();
  function priceOf(sym) { let p = 0; for (const b of (snap.book || [])) if (upper(b.symbol) === upper(sym)) { const bp = b.price || (b.balance > 0 && b.usd > 0 ? b.usd / b.balance : 0); if (bp > p) p = bp; } if (!p && live) for (const r of (live.rows || live.book || [])) if (upper(r.symbol) === upper(sym) && r.price > p) p = r.price; return p || null; }
  function idleOf(sym) {
    if (sw.live) return sw.live[upper(sym)] || 0;   // live Safe balance when fetched
    return (snap.book || []).filter(b => b.kind === 'idle' && upper(b.symbol) === upper(sym)).reduce((a, b) => a + (b.balance || 0), 0);
  }
  const balLabel = () => sw.live ? `In Safe <span class="dim">(live ${sw.liveAt})</span>:` : sw.liveBusy ? 'In Safe <span class="dim">(refreshing…)</span>:' : 'In Safe <span class="dim">(snapshot)</span>:';
  async function refreshSafeBalances() {
    // fresh balances from the Safe Transaction Service via the executor, so the amount we size is what the Safe holds now
    if (sw.liveBusy) return; sw.liveBusy = true; swapPaint();
    try {
      const j = await loadJSON(EXECUTOR + '/balances/' + snap.client);
      const m = {}; (j.balances || []).forEach(b => { m[upper(b.symbol)] = (m[upper(b.symbol)] || 0) + b.balance; });
      sw.live = m; sw.liveAt = new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) { sw.live = null; $('#swapMsg').textContent = 'Live Safe balance unavailable (' + e.message + '); showing the snapshot figure.'; }
    finally { sw.liveBusy = false; swapPaint(); }
  }
  const sellable = () => [...new Set(sw.groups.flatMap(g => g.sell))].sort();
  const buyableFor = s => [...new Set(sw.groups.filter(g => g.sell.includes(s)).flatMap(g => g.buy))].filter(b => b !== s).sort();
  const pairOk = (s, b) => sw.groups.some(g => g.sell.includes(s) && g.buy.includes(b));
  const buildable = sym => !sw.known || sw.known.includes(upper(sym));
  const isSettle = sym => SETTLEMENT.includes(upper(sym));
  // default counter-asset: USDC, or WETH when selling a dollar stable, else the first buildable settlement asset
  function defaultBuy(sell) { const bs = buyableFor(sell); const pref = ['USDC', 'USDT', 'DAI', 'USDS', 'GHO'].includes(upper(sell)) ? ['WETH', 'ETH', 'USDT', 'DAI'] : ['USDC', 'USDT', 'WETH']; for (const p of pref) { const hit = bs.find(b => upper(b) === p && buildable(b)); if (hit) return hit; } return bs.find(b => isSettle(b) && buildable(b)) || bs.find(buildable) || bs[0] || null; }

  async function renderSwap() {
    clearInterval(sw.timer); clearInterval(sw.tick); sw.quote = null; sw.sell = sw.buy = null;
    try { const r = await loadJSON(EXECUTOR + '/swap-pairs/' + snap.client); sw.groups = r.groups || []; sw.known = r.known_tokens && r.known_tokens.length ? r.known_tokens : null; }
    catch (e) { sw.known = null; try { sw.groups = (await loadJSON('data/' + snap.client + '.strategy.json')).raw_permissions.allPermissions.cowswap.filter(g => g.action === 'swap' && !g.isTWAP).map(g => ({ sell: g.sellAssets, buy: g.buyAssets })); } catch (e2) { sw.groups = []; } }
    renderRewards();
    const sells = sellable();
    if (!sells.length) { $('#swapMsg').textContent = 'No CoW swap permission found for this client in the strategy cache.'; swapPaint(); return; }
    // sensible default: the largest idle settlement asset, into USDC (or the first permitted buy)
    const held = sells.filter(t => idleOf(t) > 0 && buildable(t)).sort((x, y) => idleOf(y) * (priceOf(y) || 0) - idleOf(x) * (priceOf(x) || 0));
    sw.sell = held[0] || sells.find(buildable) || sells[0];
    sw.buy = defaultBuy(sw.sell);
    $('#swapSellBtn').onclick = () => openPicker('sell'); $('#swapBuyBtn').onclick = () => openPicker('buy');
    $('#swapFlip').onclick = swapFlip; $('#swapRate').onclick = () => { sw.inverted = !sw.inverted; swapPaint(); };
    $('#swapAmount').oninput = () => { sw.quote = null; swapPaint(); swapSchedule(); };
    // floor, never round: a figure a few wei above the Safe balance defeats the bot's wrap detection
    $('#swapSellBal').onclick = e => { if (e.target.classList.contains('sw-max')) { $('#swapAmount').value = idleOf(sw.sell) ? (Math.floor(idleOf(sw.sell) * 1e6) / 1e6).toString() : ''; sw.quote = null; swapPaint(); swapQuote(); } };
    $('#swapCta').onclick = swapCtaClick;
    $('#swapPickerX').onclick = closePicker; $('#swapPicker').onclick = e => { if (e.target.id === 'swapPicker') closePicker(); };
    $('#swapPickerQ').oninput = paintPicker;
    $('#swapMsg').textContent = `${sells.length} sellable tokens across ${sw.groups.length} permission group${sw.groups.length === 1 ? '' : 's'}.`;
    sw.live = null; swapPaint(); refreshSafeBalances();
  }
  function swapState() {
    const amt = Number($('#swapAmount').value || 0), bal = sw.sell ? idleOf(sw.sell) : 0;
    if (!sw.sell || !sw.buy) return 'select';
    if (!buildable(sw.sell) || !buildable(sw.buy)) return 'unbuildable';
    if (!(amt > 0)) return 'amount';
    if (!sw.quote) return 'quote';
    if (amt > bal + 1e-9) return 'insufficient';
    if (!executorOn) return 'offline';
    return 'review';
  }
  function swapPaint() {
    const amt = Number($('#swapAmount').value || 0), q = sw.quote;
    const ps = sw.sell ? priceOf(sw.sell) : null, pb = sw.buy ? priceOf(sw.buy) : null;
    $('#swapSellBtn .sym').textContent = sw.sell || 'Select'; $('#swapBuyBtn .sym').textContent = sw.buy || 'Select';
    $('#swapSellBtn').classList.toggle('grey', !!sw.sell && !buildable(sw.sell)); $('#swapBuyBtn').classList.toggle('grey', !!sw.buy && !buildable(sw.buy));
    const bal = sw.sell ? idleOf(sw.sell) : 0;
    $('#swapSellBal').innerHTML = sw.sell ? `${balLabel()} <span class="num">${fmtTok(bal)}</span> ${esc(sw.sell)}${bal > 0 ? ' <button class="sw-max" type="button">Max</button>' : ''}` : '';
    $('#swapBuyBal').innerHTML = sw.buy ? `${balLabel()} <span class="num">${fmtTok(idleOf(sw.buy))}</span> ${esc(sw.buy)}` : '';
    $('#swapSellUsd').textContent = amt > 0 && ps ? usd(amt * ps, 2) : amt > 0 ? 'price unknown' : '';
    const outAmt = q ? q.buy_amount : 0;
    $('#swapBuyAmt').textContent = q ? fmtTok(outAmt) : (amt > 0 && ps && pb ? '≈ ' + fmtTok(amt * ps / pb) : '0');
    $('#swapBuyAmt').classList.toggle('est', !q);
    const buyUsd = q ? (pb ? outAmt * pb : (ps ? (q.sell_amount_after_fee || q.sell_amount) * ps : null)) : null;
    $('#swapBuyUsd').textContent = q && buyUsd != null ? usd(buyUsd, 2) + (pb ? '' : ' (from sell side)') : '';
    // rate line
    const rate = $('#swapRate');
    if (q && q.price) {
      const r = sw.inverted ? 1 / q.price : q.price, a = sw.inverted ? sw.buy : sw.sell, b = sw.inverted ? sw.sell : sw.buy, pu = sw.inverted ? pb : ps;
      rate.hidden = false; rate.innerHTML = `1 ${esc(a)} = <span class="num">${fmtTok(r)}</span> ${esc(b)}${pu ? ` <span class="dim">(${usd(pu, 2)})</span>` : ''} <span class="dim" id="swapCount"></span>`;
    } else { rate.hidden = true; }
    // details
    const det = $('#swapDetails');
    if (q) {
      det.hidden = false;
      const impact = ps && pb ? q.price / (ps / pb) - 1 : null;
      const impactCls = impact == null ? '' : impact < -0.03 ? 'bad' : impact < -0.01 ? 'warn' : 'good';
      $('#swapSummary').innerHTML = `Slippage <b>${(q.slippage_bps / 100).toFixed(2)}%</b> · fee <b>${fmtTok(q.fee)} ${esc(q.sell)}</b>${impact != null ? ` · impact <b class="${impactCls}">${(impact * 100).toFixed(2)}%</b>` : ''}`;
      $('#swapKv').innerHTML = [
        ['Expected output', `${fmtTok(q.buy_amount)} ${esc(q.buy)}`],
        ['Minimum received', `${fmtTok(q.min_receive)} ${esc(q.buy)} <span class="dim">after slippage</span>`],
        ['Slippage tolerance', `${(q.slippage_bps / 100).toFixed(2)}% <span class="dim">CoW dynamic${q.slippage_source ? ' · ' + esc(String(q.slippage_source)) : ''}</span>`],
        ['Network fee', `${fmtTok(q.fee)} ${esc(q.sell)}${ps ? ` <span class="dim">(${usd(q.fee * ps, 2)})</span>` : ''} <span class="dim">paid in the sell token, no gas for the Safe</span>`],
        ['Price impact vs spot', impact == null ? '<span class="dim">no spot price for both tokens</span>' : `<span class="${impactCls}">${(impact * 100).toFixed(2)}%</span> <span class="dim">quote vs Syncrone marks</span>`],
        ['Order type', q.is_wrap ? `${q.sell === 'ETH' || q.sell === 'XDAI' ? 'Wrap' : 'Unwrap'}: direct ${q.sell === 'ETH' || q.buy === 'ETH' ? 'WETH' : 'WXDAI'} contract call under Roles, 1:1` : 'Market sell, fill-or-kill, pre-signed by the Safe'],
        ['Valid for', '30 min from the moment the proposal is built'],
        ['Receiver', `<span class="num">${esc((snap.safes && snap.safes.avatar) || snap.avatar_safe || '')}</span>`],
        ['Route', q.is_wrap ? 'no CoW order: the wrapped-native contract, inside the Roles bundle' : 'CoW Protocol batch auction (solvers)'],
        ['Bot command', `<span class="num">${esc(q.command)}</span>`],
      ].map(([k, v]) => `<div class="r"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
    } else det.hidden = true;
    // notes
    const note = $('#swapNote'); const notes = [];
    if (sw.sell && sw.buy && !isSettle(sw.sell) && !isSettle(sw.buy)) notes.push(`<b>Unusual pair.</b> ${esc(sw.sell)} → ${esc(sw.buy)} is allowed on-chain because both sit in the same Roles group (any sell asset of a group may be swapped into any of its buy assets), but the group's purpose is converting into settlement assets. Confirm the intent before proposing.`);
    if (sw.sell && !buildable(sw.sell)) notes.push(`The bot's token registry for this client has no entry for <b>${esc(sw.sell)}</b>; the order cannot be built from here.`);
    if (sw.buy && !buildable(sw.buy)) notes.push(`The bot's token registry for this client has no entry for <b>${esc(sw.buy)}</b>; the order cannot be built from here.`);
    if (sw.quote && amt > bal + 1e-9) notes.push(`Only <b>${fmtTok(bal)} ${esc(sw.sell)}</b> is idle in the Safe. Withdraw from a position in the Rebalancing section first, or the order will sit unfilled.`);
    note.hidden = !notes.length; note.innerHTML = notes.map(n => `<div>${n}</div>`).join('');
    // CTA
    const st = swapState(), cta = $('#swapCta');
    cta.textContent = { select: 'Select tokens', unbuildable: 'Token not in bot registry', amount: 'Enter an amount', quote: 'Get quote', insufficient: 'Insufficient idle balance', offline: 'Executor offline', review: 'Review swap' }[st];
    cta.disabled = !['quote', 'review'].includes(st); cta.classList.toggle('ghost', st !== 'review');
    $('#swapFlip').disabled = !(sw.sell && sw.buy && pairOk(sw.buy, sw.sell)); $('#swapFlip').title = $('#swapFlip').disabled ? 'Reverse direction is not permitted' : 'Switch sell and buy';
  }
  function swapSchedule() { clearTimeout(sw.debounce); if (Number($('#swapAmount').value || 0) > 0 && token()) sw.debounce = setTimeout(swapQuote, 700); }
  function swapFlip() { if ($('#swapFlip').disabled) return; const s = sw.sell; sw.sell = sw.buy; sw.buy = s; sw.quote = null; $('#swapAmount').value = ''; swapPaint(); refreshSafeBalances(); }
  async function swapQuote() {
    const amount = Number($('#swapAmount').value || 0); if (!(amount > 0) || !sw.sell || !sw.buy) return;
    const seq = ++sw.seq, msg = $('#swapMsg'); msg.textContent = 'Quoting on CoW through the bot…'; $('#swapCta').classList.add('busy');
    const body = JSON.stringify({ client: snap.client, sell: sw.sell, buy: sw.buy, amount });
    try {
      let r = await fetch(EXECUTOR + '/quote', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body });
      if (r.status === 401 && askToken('Executor token needed to quote')) r = await fetch(EXECUTOR + '/quote', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body });
      const j = await r.json(); if (!r.ok || j.error) throw new Error(j.error || r.statusText);
      if (seq !== sw.seq) return;
      sw.quote = j; msg.textContent = `Quote ${new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })} · valid to ${j.valid_to ? new Date(j.valid_to * 1000).toISOString().slice(11, 16) + ' UTC' : 'n/a'}${j.note ? ' · ' + j.note : ''}`;
      swapPaint(); swapArmRefresh();
    } catch (e) { if (seq === sw.seq) { sw.quote = null; msg.textContent = 'Quote failed: ' + e.message; swapPaint(); } }
    finally { $('#swapCta').classList.remove('busy'); }
  }
  function swapArmRefresh() {
    clearInterval(sw.timer); clearInterval(sw.tick); let left = 30;
    const paintCount = () => { const c = $('#swapCount'); if (c) c.textContent = `· refreshes in ${left}s`; };
    paintCount(); sw.tick = setInterval(() => { left -= 1; paintCount(); }, 1000);
    sw.timer = setInterval(() => { if ($('#swapSection').offsetParent && !$('#modal').offsetParent) swapQuote(); }, 30000);
  }
  async function swapCtaClick() { const st = swapState(); if (st === 'quote') { refreshSafeBalances(); swapQuote(); } else if (st === 'review') { await refreshSafeBalances(); if (swapState() === 'review') openSwapExec(sw.quote); } }
  function openPicker(side) {
    sw.picking = side; $('#swapPickerTitle').textContent = side === 'sell' ? 'Sell which token?' : `Buy with ${sw.sell}`; $('#swapPickerQ').value = ''; $('#swapPicker').hidden = false; paintPicker(); $('#swapPickerQ').focus();
  }
  function closePicker() { $('#swapPicker').hidden = true; sw.picking = null; }
  function paintPicker() {
    const q = upper($('#swapPickerQ').value.trim()); const list = sw.picking === 'sell' ? sellable() : buyableFor(sw.sell);
    const rows = list.filter(t => !q || upper(t).includes(q)).map(t => ({ t, bal: idleOf(t), px: priceOf(t), ok: buildable(t) }));
    const section = (title, items) => items.length ? `<div class="sw-grp">${title}</div>` + items.map(i => `<button class="sw-opt ${i.ok ? '' : 'grey'}" type="button" data-t="${esc(i.t)}" ${i.ok ? '' : 'title="not in the bot token registry"'}>
        <span class="sym">${esc(i.t)}</span><span class="bal num">${i.bal > 0 ? fmtTok(i.bal) + (i.px ? ` <span class="dim">${usd(i.bal * i.px, 0)}</span>` : '') : '<span class="dim">–</span>'}</span></button>`).join('') : '';
    const byVal = (x, y) => (y.bal * (y.px || 0)) - (x.bal * (x.px || 0)) || x.t.localeCompare(y.t);
    $('#swapPickerList').innerHTML = section('Settlement assets', rows.filter(r => isSettle(r.t)).sort(byVal)) + section('Other permitted', rows.filter(r => !isSettle(r.t)).sort(byVal)) || '<div class="empty">No permitted token matches.</div>';
    $('#swapPickerList').querySelectorAll('.sw-opt').forEach(b => b.onclick = () => {
      const t = b.dataset.t;
      if (sw.picking === 'sell') { sw.sell = t; if (!sw.buy || !pairOk(t, sw.buy)) sw.buy = defaultBuy(t); }
      else sw.buy = t;
      sw.quote = null; closePicker(); swapPaint(); swapSchedule(); refreshSafeBalances();
    });
  }
  function openSwapExec(q) {
    clearInterval(sw.timer); clearInterval(sw.tick);
    cur = { move: null, plan: null, stage2: { commands: [q.command], label: q.command, wait_for: '' }, claimOnly: true };
    $('#mTitle').textContent = `Execute · ${snap.display_name} · ${q.is_wrap ? (q.sell === 'ETH' || q.sell === 'XDAI' ? 'wrap' : 'unwrap') : 'swap'}`;
    $('#mParams').innerHTML = [['Client / chain', `${snap.client} · ${snap.chain_id}`], ['Avatar Safe', (snap.safes && snap.safes.avatar) || snap.avatar_safe], ['Action', q.is_wrap ? `${q.sell === 'ETH' || q.sell === 'XDAI' ? 'WRAP' : 'UNWRAP'} (direct ${q.sell === 'ETH' || q.buy === 'ETH' ? 'WETH' : 'WXDAI'} call under Roles, no CoW order)` : 'CoW SWAP (market sell, pre-signed order)'],
      ['Sell', `${fmtTok(q.sell_amount)} ${q.sell}`], ['Buy (quote)', `${fmtTok(q.buy_amount)} ${q.buy}`], ['Slippage', `${(q.slippage_bps / 100).toFixed(2)}% (dynamic, CoW)`], ['Min. receive', `${fmtTok(q.min_receive)} ${q.buy}`], ['Fee', `${fmtTok(q.fee)} ${q.sell}`], ['Command', q.command]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(String(v))}</dd></div>`).join('');
    $('#mPlan').hidden = true; $('#mSim').hidden = true; $('#mMsg').className = 'modal-msg';
    $('#mMsg').textContent = 'Build & simulate re-quotes through the bot and builds the approval + setPreSignature steps under Roles. Propose submits the order to CoW and the Safe transaction to the signers.';
    $('#mBuild').disabled = !executorOn; $('#mPropose').disabled = true; steps({}); $('#modal').hidden = false;
  }

  /* ------------------------------------------------------------------ stage 2 (after a CoW fill) */
  async function renderStage2() {
    let box = $('#stage2'); if (!box) { box = document.createElement('div'); box.id = 'stage2'; $('#simMoves').parentNode.insertBefore(box, $('#simMoves')); }
    const pend = JSON.parse(localStorage.getItem('kpk_stage2') || '{}'); const p = pend[snap.client];
    if (!p) { box.innerHTML = ''; return; }
    let status = '';
    if (p.cow_uid) { try { const o = await (await fetch(EXECUTOR + '/cow/' + p.cow_uid, { cache: 'no-store' })).json(); status = o.status ? ` · CoW order <a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.status)}</a>` : ''; } catch (e) { } }
    box.innerHTML = `<div class="refresh-msg warn"><b>Stage 2 pending:</b> ${esc(p.label)} · stage 1 proposed ${new Date(p.created).toISOString().slice(0, 16).replace('T', ' ')} UTC${status}.
      Run it once the Safe has executed stage 1 and the order has filled. <button class="btn" id="stage2Run" type="button" style="margin-left:8px">Run stage 2</button>
      <button class="btn ghost" id="stage2Drop" type="button">Dismiss</button></div>`;
    $('#stage2Run').onclick = () => openStage2(p);
    $('#stage2Drop').onclick = () => { delete pend[snap.client]; localStorage.setItem('kpk_stage2', JSON.stringify(pend)); renderStage2(); };
  }
  function openStage2(p) {
    cur = { move: null, plan: null, stage2: p };
    $('#mTitle').textContent = `Execute · ${snap.display_name} · stage 2`;
    $('#mParams').innerHTML = [['Client / chain', `${snap.client} · ${snap.chain_id}`], ['Avatar Safe', (snap.safes && snap.safes.avatar) || snap.avatar_safe], ['Action', 'DEPOSIT (stage 2)'], ['Command', p.commands.join(' ; ')], ['Waits for', `${p.wait_for} from the CoW fill`]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
    $('#mPlan').hidden = true; $('#mSim').hidden = true; $('#mMsg').className = 'modal-msg';
    $('#mMsg').textContent = 'Build & simulate deposits the whole ' + p.wait_for + ' balance currently in the Safe. If the order has not filled yet the bot reports a zero balance.';
    $('#mBuild').disabled = !executorOn; $('#mPropose').disabled = true; steps({}); $('#modal').hidden = false;
  }

  /* ------------------------------------------------------------------ executor */
  async function pingExecutor() {
    const el = $('#execState');
    try { const r = await fetch(EXECUTOR + '/health', { cache: 'no-store' }); const j = await r.json(); executorOn = !!j.ok; el.textContent = `executor: ${j.ok ? 'connected' : 'error'}${j.host ? ' @ ' + j.host : ''}${j.clients ? ' · ' + j.clients.join(', ') : ''}${j.auth ? (token() ? ' · token set' : ' · token needed') : ''}${j.proposer_code && j.proposer_code.head ? ' · kpk-proposer@' + j.proposer_code.head : ''}`; el.className = 'exec-state ' + (j.ok ? 'on' : 'off'); }
    catch (e) { executorOn = false; el.textContent = 'executor: offline (' + EXECUTOR.replace(/^https?:\/\//, '') + ')'; el.className = 'exec-state off'; }
    $('#refreshBtn').disabled = !executorOn;
    $('#refreshBtn').title = executorOn ? 'Re-run holdings, yields and assessment for this client on ' + ($('#execState').textContent.split('@')[1] || 'the executor') : 'Connect the executor (SSH tunnel to OCI, or scripts/executor.py) to refresh';
    if (snap) simulate();
  }

  async function refreshClient() {
    if (!snap || !executorOn) return;
    const btn = $('#refreshBtn'), msg = $('#refreshMsg');
    btn.classList.add('busy'); btn.textContent = '↻ Refreshing…'; msg.hidden = false; msg.className = 'refresh-msg';
    msg.textContent = `Running holdings (Syncrone, Safe, Etherscan), yields and assessment for ${snap.display_name} on the executor host. Usually 1 to 3 minutes.`;
    try {
      let r = await fetch(EXECUTOR + '/refresh/' + snap.client, { cache: 'no-store', headers: authHeaders() });
      if (r.status === 401 && askToken('Executor token needed to refresh')) r = await fetch(EXECUTOR + '/refresh/' + snap.client, { cache: 'no-store', headers: authHeaders() });
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || 'refresh failed');
      msg.innerHTML = `Refreshed in ${j.seconds}s · NAV ${compact(j.nav_usd)} · ${j.nav_tied ? 'reconciliation ties' : '<b>reconciliation did not tie, read the flags</b>'}` +
        (j.stale_note ? `<br>${esc(j.stale_note)}` : '') + (j.pushed === true ? ' · pushed to the site' : j.pushed ? `<br><b>Site not updated:</b> ${esc(String(j.pushed).split('hint:')[0])}` : '');
      msg.classList.add(j.pushed === true ? 'ok' : 'warn');
      await select(snap.client);
    } catch (e) { msg.className = 'refresh-msg err'; msg.textContent = 'Refresh failed: ' + e.message + (e.message === 'Failed to fetch' ? ' (the executor was unreachable or restarting mid-request; try again in a moment)' : ''); }
    finally { btn.classList.remove('busy'); btn.textContent = '↻ Refresh client'; }
  }

  let cur = null;
  function bindModal() {
    const close = () => { $('#modal').hidden = true; cur = null; };
    $('#mClose').onclick = close; $('#mCancel').onclick = close;
    $('#modal').addEventListener('click', e => { if (e.target === $('#modal')) close(); });
    $('#mBuild').onclick = buildAndSimulate;
    $('#mPropose').onclick = propose;
  }

  function steps(state) { // state: {build, sim, propose} each: '', 'cur', 'done', 'fail'
    $('#mSteps').innerHTML = [['build', '1 · Build Roles transaction'], ['sim', '2 · Tenderly simulation'], ['review', '3 · Your approval'], ['propose', '4 · Propose to Safe']]
      .map(([k, t]) => `<span class="pill ${state[k] || ''}">${t}</span>`).join('');
  }

  function openModal(m) {
    cur = { move: m, plan: null };
    const s = snap.safes || {};
    $('#mTitle').textContent = `Execute · ${snap.display_name}`;
    $('#mParams').innerHTML = [
      ['Client / chain', `${snap.client} · ${snap.chain_id}`], ['Avatar Safe', s.avatar || snap.avatar_safe], ['Roles Modifier', s.roles_modifier || '—'],
      ['Action', m.from.kind === 'idle' ? 'DEPLOY idle' : 'WITHDRAW then DEPOSIT'],
      ['From', `${m.from.protocol} · ${m.from.venue}`], ['To', `${m.to.protocol} · ${m.to.asset} (${m.to.action})${m.to.vault ? ' · ' + m.to.vault : ''}`],
      ['Amount', `${usd(m.amt)} of ${m.from.symbol || m.g}`], ['Yield', `${pct(m.from.apy)} → ${pct(m.toApyDiluted != null ? m.toApyDiluted : m.toApy)} after dilution (${pct(m.toApy)} headline) · +${usd(m.amt * m.pick)}/yr`],
    ].map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('');
    $('#mPlan').hidden = true; $('#mSim').hidden = true; $('#mMsg').className = 'modal-msg'; $('#mMsg').textContent = executorOn ? 'Ready. Build & simulate constructs the transaction through the SafeAgent builders and permission engine, then runs it on Tenderly. Nothing is proposed until you confirm.' : 'Executor offline. Start it locally:  python scripts/executor.py   (needs the client .env with RPC, Tenderly and, for proposing, the agent key).';
    $('#mBuild').disabled = !executorOn; $('#mPropose').disabled = true;
    steps({});
    $('#modal').hidden = false;
  }

  function movePayload(m) {
    return { client: snap.client, chain_id: snap.chain_id, asset_group: m.g, amount_usd: Math.round(m.amt),
      from: { kind: m.from.kind, protocol: m.from.protocol, venue: m.from.venue, symbol: m.from.symbol, vault: m.from.vault || null, usd: m.from.usd },
      to: { protocol: m.to.protocol, asset: m.to.asset, action: m.to.action, vault: m.to.vault || null, apy: m.toApy },
      snapshot_as_of: snap.as_of };
  }

  async function buildAndSimulate() {
    if (!cur) return; steps({ build: 'cur' }); $('#mBuild').disabled = true; $('#mMsg').className = 'modal-msg'; $('#mMsg').textContent = 'Building…';
    try {
      const payload = cur.claimOnly ? { client: snap.client, commands: cur.stage2.commands, notes: ['claim only'] }
        : cur.stage2 ? { client: snap.client, commands: cur.stage2.commands, stage: '2 of 2', notes: ['stage 2: deposit of the CoW fill'] }
        : cur.sweep ? { client: snap.client, rewards_sweep: true, items: cur.sweep.items, to: cur.sweep.to, extra_commands: cur.sweep.extra || [] } : movePayload(cur.move);
      let r = await fetch(EXECUTOR + '/plan', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(payload) });
      if (r.status === 401 && askToken('Executor token needed to build')) r = await fetch(EXECUTOR + '/plan', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(payload) });
      const j = await r.json();
      if (!r.ok || j.error) throw new Error(j.error || r.statusText);
      cur.plan = j;
      $('#mPlan').hidden = false;
      $('#mPlan').innerHTML = (j.stage ? `<span class="pill p-warn">stage ${esc(j.stage)}</span> ` : '') + `<b>Plan</b> · ${esc(j.summary || '')}${j.proposer_code && j.proposer_code.head ? ` · built with kpk-labs/kpk-proposer @ ${esc(j.proposer_code.head)}${j.proposer_code.pulled ? ' (just updated)' : ''}` : ''}<div class="txlist" style="margin-top:6px">${(j.transactions || []).map((t, i) => `<div class="tx"><b>${i + 1}. ${esc(t.label || t.to)}</b><br>to ${esc(t.to)} · value ${esc(String(t.value ?? 0))}<br>${esc(t.data_preview || (t.data || '').slice(0, 74) + (t.data && t.data.length > 74 ? '…' : ''))}</div>`).join('')}</div>` +
        (j.swap_quote ? `<div class="fine" style="margin-top:6px"><b>Swap (${esc(j.swap_quote.venue)}, fee ${j.swap_quote.fee_tier / 1e4}%):</b> ${Number(j.swap_quote.sell_human).toLocaleString('en-US', { maximumFractionDigits: 2 })} ${esc(j.swap_quote.sell_token)} → ${Number(j.swap_quote.quote_out_human).toLocaleString('en-US', { maximumFractionDigits: 2 })} ${esc(j.swap_quote.buy_token)} quoted, min ${Number(j.swap_quote.min_out_human).toLocaleString('en-US', { maximumFractionDigits: 2 })} · price impact ${j.swap_quote.impact_pct}%${j.swap_quote.impact_pct > 0.5 ? ' <span class="pill p-warn">high: consider CoW via the bot</span>' : ''}</div>` : '') +
        (j.cow_orders && j.cow_orders.length ? `<div class="fine" style="margin-top:6px"><b>${j.cow_orders.length} pre-signed CoW order${j.cow_orders.length > 1 ? 's' : ''}</b>, submitted to the CoW API when you propose; each fills after the Safe executes its setPreSignature step.` +
          j.cow_orders.map(o => `<br>· sell <b>${o.sell_human ? esc(String(o.sell_human)) + ' ' : ''}${esc(o.sell_symbol || o.sell_token)}</b> → buy <b>${esc(o.buy_symbol || o.buy_token)}</b>, slippage ${o.slippage_bps} bps, valid to ${o.valid_to ? new Date(o.valid_to * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : 'n/a'}`).join('') + '</div>' : '') +
        (j.next_stage ? `<div class="fine" style="margin-top:6px"><b>Stage 2 (after the order fills):</b> ${esc(j.next_stage.label)} — offered on the Strategies tab once this stage is proposed.</div>` : '') +
        (j.notes && j.notes.length ? `<div class="fine" style="margin-top:6px">${j.notes.map(esc).join('<br>')}</div>` : '') +
        (j.permission_check ? `<div class="fine" style="margin-top:6px">Permissions: ${esc(j.permission_check)}</div>` : '');
      steps({ build: 'done', sim: 'cur' }); $('#mMsg').textContent = 'Simulating on Tenderly…';
      const s = j.simulation || {};
      $('#mSim').hidden = false;
      $('#mSim').innerHTML = `<b>Tenderly</b> · <span class="pill ${s.success ? 'p-good' : 'p-bad'}">${s.success ? 'success' : 'reverted / failed'}</span>` +
        (s.gas_used ? ` · gas ${Number(s.gas_used).toLocaleString()}` : '') +
        (s.url ? ` · <a href="${esc(s.url)}" target="_blank" rel="noopener">open simulation ↗</a>` : '') +
        (s.error ? `<div class="fine" style="margin-top:6px">${esc(s.error)}</div>` : '') +
        (s.balance_changes ? `<div class="fine" style="margin-top:6px">${esc(s.balance_changes)}</div>` : '');
      steps({ build: 'done', sim: s.success ? 'done' : 'fail', review: 'cur' });
      $('#mMsg').textContent = s.success ? (j.can_propose ? 'Review the plan and the simulation. Propose to Safe submits it to the Safe Transaction Service for signers, exactly like the Telegram "approved" step.' : 'Simulation passed. Proposing is disabled on this executor (no agent key configured).') : 'Simulation did not succeed; proposing is blocked.';
      $('#mPropose').disabled = !(s.success && j.can_propose);
    } catch (e) { steps({ build: 'fail' }); $('#mMsg').className = 'modal-msg err'; $('#mMsg').textContent = 'Build failed: ' + e.message; $('#mBuild').disabled = false; }
  }

  async function propose() {
    if (!cur || !cur.plan) return;
    if (!confirm(`Propose this transaction to the ${snap.display_name} Safe? Signers will still have to approve it in the Safe UI.`)) return;
    steps({ build: 'done', sim: 'done', review: 'done', propose: 'cur' }); $('#mPropose').disabled = true; $('#mMsg').textContent = 'Proposing…';
    try {
      const r = await fetch(EXECUTOR + '/propose', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ plan_id: cur.plan.plan_id }) });
      const j = await r.json();
      if (!r.ok || j.error) throw new Error(j.error || r.statusText);
      steps({ build: 'done', sim: 'done', review: 'done', propose: 'done' });
      $('#mMsg').innerHTML = `Proposed. Safe tx hash <span class="num">${esc(j.safe_tx_hash || '')}</span>${j.url ? ` · <a href="${esc(j.url)}" target="_blank" rel="noopener">open in Safe ↗</a>` : ''}` +
        (j.cow_orders && j.cow_orders.length ? `<br>CoW order${j.cow_orders.length > 1 ? 's' : ''} placed: ` + j.cow_orders.map(o => `<a href="${esc(o.url)}" target="_blank" rel="noopener">${esc(o.uid.slice(0, 14))}… ↗</a>`).join(', ') + ' (fill after the Safe executes)' : '') +
        (j.cow_warning ? `<br><b>${esc(j.cow_warning)}</b>` : '');
      if (cur.stage2 && !cur.claimOnly) { const pend = JSON.parse(localStorage.getItem('kpk_stage2') || '{}'); delete pend[snap.client]; localStorage.setItem('kpk_stage2', JSON.stringify(pend)); renderStage2(); }
      if (cur.plan && cur.plan.next_stage) {
        const pend = JSON.parse(localStorage.getItem('kpk_stage2') || '{}');
        pend[snap.client] = { ...cur.plan.next_stage, stage1_tx: j.safe_tx_hash, cow_uid: j.cow_order_uid || null, cow_url: j.cow_url || null, created: Date.now() };
        localStorage.setItem('kpk_stage2', JSON.stringify(pend)); renderStage2();
      }
    } catch (e) { steps({ build: 'done', sim: 'done', review: 'done', propose: 'fail' }); $('#mMsg').className = 'modal-msg err'; $('#mMsg').textContent = 'Propose failed: ' + e.message; }
  }

  init();
})();
