/* Treasury rebalancer page. Loads data/index.json + data/<client>.json (written by scripts/publish.py)
   and re-runs the move sizing client-side with the user's thresholds. Nothing is sent anywhere. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const usd = (v, d = 0) => '$' + Number(v || 0).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d });
  const pct = (v, d = 2) => v == null ? 'n/a' : (Number(v) * 100).toFixed(d) + '%';
  const compact = v => { v = Number(v || 0); return v >= 1e9 ? '$' + (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? '$' + (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? '$' + (v / 1e3).toFixed(0) + 'k' : usd(v); };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const GROUP_COLORS = { USD: '#2D8561', ETH: '#1D1D1D', EURO: '#8E6710', OTHER: '#706E66' };

  let index = null, snap = null, sim = { pickupBps: 50, moveUsd: 250000, tvlCapPct: 10, basis: 'apy', exclude: new Set() };

  async function loadJSON(p) { const r = await fetch(p, { cache: 'no-store' }); if (!r.ok) throw new Error(p + ' ' + r.status); return r.json(); }

  async function init() {
    try { index = await loadJSON('data/index.json'); }
    catch (e) { $('#stamp').textContent = 'No snapshot yet. Run `python rebalancer/scripts/publish.py`.'; return; }
    const tabs = $('#clientTabs');
    tabs.innerHTML = index.clients.map((c, i) => `<button role="tab" data-c="${c.client}" aria-pressed="${i === 0}">${esc(c.display_name)}</button>`).join('');
    tabs.addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; [...tabs.children].forEach(x => x.setAttribute('aria-pressed', x === b)); select(b.dataset.c); });
    const first = new URLSearchParams(location.search).get('client') || index.clients[0]?.client;
    if (first) { [...tabs.children].forEach(x => x.setAttribute('aria-pressed', x.dataset.c === first)); select(first); }
  }

  let live = null;
  async function select(slug) {
    snap = await loadJSON('data/' + slug + '.json');
    try { live = await loadJSON('data/' + slug + '.live.json'); } catch (e) { live = null; }
    history.replaceState(null, '', '?client=' + slug);
    sim.pickupBps = snap.thresholds.min_pickup_bps; sim.moveUsd = snap.thresholds.min_move_usd; sim.tvlCapPct = snap.thresholds.venue_tvl_cap_pct;
    sim.exclude = new Set(snap.thresholds.exclude || []);
    render();
    renderLive();
  }

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
    $('#stamp2').textContent = `data as of ${new Date(snap.as_of).toUTCString().replace(':00 GMT', ' UTC')} · APY period ${snap.period} · vaults.fyi ${snap.vault_data_fetched_at ? new Date(snap.vault_data_fetched_at).toISOString().slice(0, 16).replace('T', ' ') + ' UTC' : 'n/a'}`;

    // NAV cards
    const groups = {}; snap.book.forEach(b => groups[b.asset_group] = (groups[b.asset_group] || 0) + b.usd);
    const nav = snap.nav_usd; const stables = (groups.USD || 0) + (groups.EURO || 0);
    const blended = weightedApy(snap.book.filter(b => b.kind === 'position' && b.apy != null));
    const idle = snap.book.filter(b => b.kind === 'idle').reduce((s, b) => s + b.usd, 0);
    const diffOk = r.diff == null || r.diff <= 0.01;
    $('#navCards').innerHTML = [
      card('NAV (book)', compact(nav), `Syncrone ${compact(r.syncrone_nav_usd)}`),
      card('Stables / volatile', `${(100 * stables / nav).toFixed(1)}<span class="u">/ ${(100 * (groups.ETH || 0) / nav).toFixed(1)}%</span>`, 'by market value'),
      card('Blended APY (priced positions)', pct(blended), `${snap.book.filter(b => b.kind === 'position' && b.apy != null).length} positions`),
      card('Idle at 0%', compact(idle), idle > 0 ? 'deployable' : 'none'),
      card('In-flight', compact(r.in_flight_usd), 'withdrawal queues'),
      card('Untracked by optimizer', compact(r.untracked_usd), 'no APY feed'),
      card('Reconciliation', r.diff == null ? 'n/a' : pct(r.diff), `<span class="pill ${diffOk ? 'p-good' : 'p-bad'}">${diffOk ? 'ties' : 'STOP'}</span> · Safe=Etherscan ${r.tokens_checked - r.tokens_disagree}/${r.tokens_checked}`),
    ].join('');
    const others = Object.entries(r.other_wallets || {});
    $('#reconBox').innerHTML = `<b>Bridge.</b> Syncrone NAV ${usd(r.syncrone_nav_usd)} against Strategy API positions ${usd(r.strategy_api_usd)} + untracked ${usd(r.untracked_usd)} + in-flight ${usd(r.in_flight_usd)} + idle ${usd(r.idle_usd)} = ${usd(r.bridge_usd)}. Tolerance 1%.` +
      (others.length ? `<div class="fine" style="margin-top:6px">Other wallets/chains in the same Syncrone org, not in this view: ${others.map(([k, v]) => `${esc(k)} ${compact(v)}`).join(', ')}.</div>` : '');

    // Policy
    const ps = $('#policySection');
    if (snap.policy) {
      ps.hidden = false; $('#policyTitle').textContent = snap.policy.name;
      $('#policyTable tbody').innerHTML = snap.policy_checks.map(k => `<tr><td class="k">${esc(k.check)}</td><td><span class="pill ${{ breached: 'p-bad', near: 'p-warn', 'in-bounds': 'p-good' }[k.status]}">${k.status}</span></td><td>${esc(k.value)}${k.effective_stable_target_pct ? ` · effective stable target <b>${k.effective_stable_target_pct}%</b>` : ''}</td></tr>`).join('');
    } else { ps.hidden = true; }

    // Groups / positions
    $('#groups').innerHTML = Object.keys(groups).sort().map(g => {
      const rows = snap.book.filter(b => b.asset_group === g).sort((a, b) => b.usd - a.usd);
      const tot = groups[g]; const ap = weightedApy(rows.filter(b => b.kind === 'position' && b.apy != null));
      return `<div class="grp"><div class="grp-h"><h3>${g}</h3><div class="tot"><b>${compact(tot)}</b> · ${(100 * tot / nav).toFixed(1)}% of NAV${ap != null ? ` · blended <b>${pct(ap)}</b>` : ''}</div></div>
        <div class="bar">${rows.map(b => `<span title="${esc(b.venue)} ${compact(b.usd)}" style="width:${100 * b.usd / tot}%;background:${b.kind === 'idle' ? 'var(--warn)' : b.kind === 'in_flight' ? 'var(--hair)' : b.untracked ? 'var(--muted)' : GROUP_COLORS[g] || '#999'};opacity:${b.kind === 'position' && !b.untracked ? 0.55 + 0.45 * (rows.indexOf(b) % 2) : 1}"></span>`).join('')}</div>
        <div class="scroll"><table><thead><tr><th>Protocol</th><th>Venue</th><th class="n">Value</th><th class="n">Share</th><th class="n">APY</th><th class="n">30d</th></tr></thead><tbody>
        ${rows.map(b => `<tr class="${b.kind === 'idle' ? 'idle' : b.kind === 'in_flight' ? 'inflight' : ''}"><td class="k">${esc(b.protocol)}</td><td>${esc(b.venue)}${b.untracked ? ' <span class="pill p-warn">untracked</span>' : ''}</td><td class="n num">${usd(b.usd)}</td><td class="n num">${(100 * b.usd / nav).toFixed(1)}%</td><td class="n num">${b.apy == null ? '<span class="dim">n/a</span>' : pct(b.apy)}</td><td class="n num dim">${apy30(b)}</td></tr>`).join('')}
        </tbody></table></div></div>`;
    }).join('');

    // Simulator controls
    const protos = [...new Set(snap.permitted.map(p => p.protocol))].sort();
    $('#exclude').innerHTML = protos.map(p => `<button data-p="${esc(p)}" aria-pressed="${sim.exclude.has(p)}">${esc(p)}</button>`).join('');
    $('#exclude').onclick = e => { const b = e.target.closest('button'); if (!b) return; sim.exclude.has(b.dataset.p) ? sim.exclude.delete(b.dataset.p) : sim.exclude.add(b.dataset.p); b.setAttribute('aria-pressed', sim.exclude.has(b.dataset.p)); simulate(); };
    $('#basis').onclick = e => { const b = e.target.closest('button'); if (!b) return; [...$('#basis').children].forEach(x => x.setAttribute('aria-pressed', x === b)); sim.basis = b.dataset.b; simulate(); };
    bindRange('#pickup', 'pickupBps', v => v + ' bps'); bindRange('#move', 'moveUsd', v => compact(v)); bindRange('#tvl', 'tvlCapPct', v => v + '% of venue TVL');
    simulate();

    // ops-tools
    $('#opsCaveat').textContent = snap.ops_tools_caveat || '';
    $('#ops').innerHTML = snap.ops_tools.length ? snap.ops_tools.map(o => `<div class="opsrow"><b>${o.asset_group}</b>: ${pct(o.current_apy)} → ${pct(o.recommended_apy)} by moving ${compact(o.changed_usd)}. ${o.allocations.map(a => `${esc(a.protocol)}/${esc(a.venue)} → ${compact(a.usd)} @ ${pct(a.apy)}`).join('; ')}</div>`).join('') : '<p class="empty">No optimizer output for this client/chain.</p>';

    // flags
    $('#flags').innerHTML = snap.flags.length ? snap.flags.map(f => `<li class="${/^NAV:|STOP/.test(f) ? 'stop' : ''}">${esc(f)}</li>`).join('') : '<li>No flags.</li>';
  }

  function apy30(b) { const p = snap.permitted.find(p => p.protocol === b.protocol && p.priced && p.asset_group === b.asset_group && (p.asset === b.symbol || (p.vault && (b.venue || '').toLowerCase().includes((p.asset || '').toLowerCase())))); return p && p.apy_30d != null ? pct(p.apy_30d) : ''; }
  function card(t, v, foot) { return `<div class="card"><div class="t">${t}</div><div class="v num">${v}</div><div class="foot">${foot || ''}</div></div>`; }
  function weightedApy(rows) { const d = rows.reduce((s, b) => s + b.usd, 0); return d ? rows.reduce((s, b) => s + b.usd * b.apy, 0) / d : null; }
  function bindRange(sel, key, fmt) { const el = $(sel); el.value = sim[key]; const lab = $(sel + 'Val'); lab.textContent = fmt(sim[key]); el.oninput = () => { sim[key] = Number(el.value); lab.textContent = fmt(sim[key]); simulate(); }; }

  /* Same method as scripts/assess.py: per asset group, fill the best permitted venues from idle
     balances and laggards, bounded by venue TVL cap and policy protocol-cap headroom. */
  function simulate() {
    const nav = snap.nav_usd; const cap = snap.policy?.protocol_cap_pct_nav;
    const byProto = {}; snap.book.forEach(b => { if (b.kind !== 'idle') byProto[b.protocol] = (byProto[b.protocol] || 0) + b.usd; });
    const minPick = sim.pickupBps / 1e4; const moves = []; let before = 0, after = 0, den = 0, idleDeployed = 0;
    const apyOf = p => sim.basis === 'apy_30d' && p.apy_30d != null ? p.apy_30d : p.apy;
    const groups = [...new Set(snap.book.map(b => b.asset_group))];
    for (const g of groups) {
      const pos = snap.book.filter(b => b.asset_group === g && b.kind === 'position' && b.apy != null);
      const idle = snap.book.filter(b => b.asset_group === g && b.kind === 'idle' && b.usd > 1000);
      const venues = snap.permitted.filter(p => p.asset_group === g && p.priced && apyOf(p) != null && !sim.exclude.has(p.protocol)).sort((a, b) => apyOf(b) - apyOf(a));
      pos.forEach(b => { before += b.usd * b.apy; den += b.usd; });
      if (!venues.length) continue;
      const best = apyOf(venues[0]);
      const headroom = new Map(venues.map(v => [v, Math.max(0, Math.min(cap ? cap / 100 * nav - (byProto[v.protocol] || 0) : Infinity, v.tvl_usd ? sim.tvlCapPct / 100 * v.tvl_usd : Infinity))]));
      const sources = [...idle.map(b => ({ ...b, apy: 0 })), ...pos.filter(b => sim.exclude.has(b.protocol) || (best - b.apy >= minPick && b.usd >= sim.moveUsd)).sort((a, b) => a.apy - b.apy)];
      for (const src of sources) {
        let rem = src.usd;
        for (const v of venues) {
          if (rem < sim.moveUsd && src.kind !== 'idle') break;
          if (v.protocol === src.protocol && v.asset === src.symbol) continue;
          const pick = apyOf(v) - src.apy;
          if (pick < minPick && src.kind === 'position' && !sim.exclude.has(src.protocol)) break;
          const amt = Math.min(rem, headroom.get(v));
          if (amt < Math.min(sim.moveUsd, rem) || amt <= 0) continue;
          moves.push({ g, from: src, to: v, amt, pick, forced: sim.exclude.has(src.protocol) });
          headroom.set(v, headroom.get(v) - amt); rem -= amt; if (src.kind === 'idle') idleDeployed += amt;
          if (rem <= 0) break;
        }
      }
    }
    after = before + moves.reduce((s, m) => s + m.amt * m.pick, 0); den += idleDeployed;
    const pickup = moves.reduce((s, m) => s + m.amt * m.pick, 0);
    $('#simOut').innerHTML = [
      ['Candidate moves', moves.length], ['Capital moved', compact(moves.reduce((s, m) => s + m.amt, 0))],
      ['Blended APY before', den ? pct(before / (den - idleDeployed)) : 'n/a'], ['Blended APY after', den ? pct(after / den) : 'n/a'],
      ['Pickup per year', compact(pickup)],
    ].map(([t, v]) => `<div class="o"><div class="t">${t}</div><div class="v num">${v}</div></div>`).join('');
    $('#simMoves').innerHTML = moves.length ? moves.map(m => `<div class="mv ${m.from.kind === 'idle' ? 'idle' : ''}"><div class="path"><b>${m.g}</b> · ${esc(m.from.protocol)} ${esc(m.from.venue)} <span class="arr">→</span> ${esc(m.to.protocol)} ${esc(m.to.asset)} <small>(${m.to.action})</small><br><small>${pct(m.from.apy)} → ${pct(apyOf(m.to))}${m.to.tvl_usd ? ` · venue TVL ${compact(m.to.tvl_usd)}` : ''}${m.forced ? ' · excluded venue: exit is mandatory' : ''}</small></div><div class="amt num">${usd(m.amt)}<small>+${usd(m.amt * m.pick)}/yr</small></div></div>`).join('')
      : '<p class="empty">No move clears these thresholds. Laggards are noted, not traded.</p>';
  }

  init();
})();
