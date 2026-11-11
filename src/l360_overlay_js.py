"""The injected CaseloadNotes overlay script for Learner360 (Stage 2, P0).

Kept as a Python string so it ships inside the app with no packaging step.
Installed into the app's own browser with ``context.add_init_script`` and
re-evaluated into already-open Learner360 pages. Activates ONLY on
learner360.wgu.edu. Read-only: draws badges, never changes Learner360 data,
never touches its sign-in tokens.

Contract with the worker (push model): ``window.__cnOverlay.version()`` →
snapshot version the page has (-1 = none); ``setData(snapshot)`` installs a
l360_overlay.build_snapshot() dict.
"""

OVERLAY_JS = r"""
(() => {
  if (location.host !== 'learner360.wgu.edu' || window.__cnOverlay) return;

  const S = { data: null, v: -1, on: true, queued: false, matched: 0,
              outbox: [], colTables: new Set() };
  try { S.on = localStorage.getItem('cnOverlayOn') !== '0'; } catch (e) {}

  // ---- design tokens (matched to Learner360's palette) -------------------
  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .wrap { display: inline-flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;
      font: 500 11px/1.2 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      color: #1b2a3a; }
    .chip { display: inline-flex; align-items: center; gap: 3px;
      height: 18px; padding: 0 6px; border-radius: 9px;
      background: #eef2f7; border: 1px solid #d5dde8; white-space: nowrap; }
    .t-pass   { background: #e3f4ea; border-color: #9fd3b1; color: #135c32; }
    .t-return { background: #fdecea; border-color: #f2b1aa; color: #8a1f14; }
    .t-pending{ background: #e8f0fb; border-color: #a9c3ea; color: #1d4f91; }
    .t-none   { background: #f4f6f9; border-color: #e1e6ee; color: #8a97a8; }
    .r-low  { background: #e3f4ea; border-color: #9fd3b1; color: #135c32; }
    .r-mid  { background: #fff4dc; border-color: #f0d08b; color: #7a5600; }
    .r-high { background: #fdecea; border-color: #f2b1aa; color: #8a1f14; }
    .step { background: #002855; border-color: #002855; color: #fff; }
    .ea   { background: #fff; border-color: #c2410c; color: #c2410c; }
    .muted { color: #5b6b7f; background: transparent; border-color: transparent;
      padding: 0 2px; }
    .pill { position: fixed; left: 16px; bottom: 16px; z-index: 2147483000;
      display: flex; align-items: center; gap: 6px; padding: 6px 9px;
      border-radius: 20px; background: #002855; color: #fff;
      font: 600 12px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      box-shadow: 0 4px 14px rgba(0,40,85,.28); cursor: pointer;
      user-select: none; transition: opacity .15s ease; }
    .pill.off { background: #5b6b7f; }
    .pill .txt { max-width: 0; overflow: hidden; white-space: nowrap;
      transition: max-width .2s ease; }
    .pill:hover .txt, .pill.drag .txt { max-width: 320px; }
    .pill.drag { cursor: grabbing; opacity: .85; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #5dd39e; }
    .pill.off .dot { background: #c7d0db; }
  `;

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // ---- floating on/off pill ---------------------------------------------
  const pillHost = document.createElement('div');
  pillHost.setAttribute('data-cn', 'pill');
  const pillRoot = pillHost.attachShadow({ mode: 'open' });
  pillRoot.innerHTML = `<style>${CSS}</style>
    <div class="pill" title="CaseloadNotes overlay — click to show/hide, drag to move">
      <span class="dot"></span><b>CN</b><span class="txt">CaseloadNotes</span></div>`;
  const pill = pillRoot.querySelector('.pill');
  // Draggable (position remembered) so it never sits on the page's own
  // buttons; a click without movement toggles the overlay.
  const placePill = (x, y) => {
    const w = pill.offsetWidth || 60, h = pill.offsetHeight || 28;
    x = Math.max(4, Math.min(window.innerWidth - w - 4, x));
    y = Math.max(4, Math.min(window.innerHeight - h - 4, y));
    Object.assign(pill.style, {left: x + 'px', top: y + 'px', bottom: 'auto',
                               right: 'auto'});
  };
  try {
    const pos = JSON.parse(localStorage.getItem('cnPillPos') || 'null');
    if (pos) requestAnimationFrame(() => placePill(pos.x, pos.y));
  } catch (e) {}
  let drag = null;
  pill.addEventListener('pointerdown', ev => {
    const r = pill.getBoundingClientRect();
    drag = {dx: ev.clientX - r.left, dy: ev.clientY - r.top,
            x0: ev.clientX, y0: ev.clientY, moved: false};
    pill.setPointerCapture(ev.pointerId);
  });
  pill.addEventListener('pointermove', ev => {
    if (!drag) return;
    if (!drag.moved && Math.hypot(ev.clientX - drag.x0, ev.clientY - drag.y0) < 4) return;
    drag.moved = true;
    pill.classList.add('drag');
    placePill(ev.clientX - drag.dx, ev.clientY - drag.dy);
  });
  let justDragged = false;
  pill.addEventListener('pointerup', () => {
    const d = drag; drag = null;
    pill.classList.remove('drag');
    if (d && d.moved) {
      justDragged = true;               // swallow the click that follows
      const r = pill.getBoundingClientRect();
      try { localStorage.setItem('cnPillPos', JSON.stringify({x: r.left, y: r.top})); } catch (e) {}
    }
  });
  pill.addEventListener('click', () => {
    if (justDragged) { justDragged = false; return; }
    S.on = !S.on;
    try { localStorage.setItem('cnOverlayOn', S.on ? '1' : '0'); } catch (e) {}
    schedule();
  });
  const mountPill = () => {
    if (document.body && !pillHost.isConnected) document.body.appendChild(pillHost);
  };

  // ---- badge rendering ---------------------------------------------------
  const RISK_TIP = 'Estimated chance this student does not pass the course, from '
    + 'their momentum history, calibrated on your past results. Typical is about '
    + '8%; 20%+ stands out. A ranking aid (moderate accuracy), not a verdict.';
  const riskCls = r => r == null ? '' : (r >= 30 ? 'r-high' : r >= 15 ? 'r-mid' : 'r-low');

  function badgeHtml(e) {
    const parts = [];
    for (const t of (e.tasks || []).slice(0, 4)) {
      const tip = `Task ${t.n}: ${t.status || (t.date ? 'submitted' : 'no attempt')}`
        + (t.date ? ` · ${t.date}` : '') + (t.count ? ` (${t.count})` : '');
      parts.push(`<span class="chip t-${esc(t.kind)}" title="${esc(tip)}">T${t.n}</span>`);
    }
    if (e.risk != null)
      parts.push(`<span class="chip ${riskCls(e.risk)}" title="${esc(RISK_TIP)}">${Math.round(e.risk)}%</span>`);
    const oa = e.assess && e.assess.oa;
    if (oa && (oa.used || oa.passed)) {
      const last = (oa.sittings || []).slice(-1)[0];
      const tip = (oa.sittings || []).map(t => `#${t.n} ${t.date}${t.score != null ? ' · ' + t.score : ''}${t.passed ? ' ✓' : ''}`).join('\n')
        + (e.assess.cut ? `\ncut ${e.assess.cut}` : '');
      const cls = oa.passed ? 't-pass' : (oa.used >= 3 ? 'risk-hi' : 't-return');
      parts.push(`<span class="chip ${cls}" title="${esc(tip)}">OA ${oa.passed ? '✓' : oa.used + '/' + oa.max}${last && last.score != null ? ' · ' + last.score : ''}</span>`);
    }
    if (e.next_step)
      parts.push(`<span class="chip step" title="Success path: next step">${esc(e.next_step)}</span>`);
    if (e.ea)
      parts.push(`<span class="chip ea" title="Open Essential Action: ${esc(e.ea)}">EA</span>`);
    const bits = [];
    if (e.days_contact != null) bits.push(`contact ${Math.round(e.days_contact)}d`);
    if (e.days_course_activity != null) bits.push(`activity ${Math.round(e.days_course_activity)}d`);
    if (bits.length) {
      const tip = [e.last_action ? `Last action: ${e.last_action}` : '',
                   e.followup_date ? `Follow-up ${e.followup_date}` : '',
                   e.contact_pref ? `Prefers ${e.contact_pref}` : ''].filter(Boolean).join(' · ');
      parts.push(`<span class="chip muted" title="${esc(tip)}">${esc(bits.join(' · '))}</span>`);
    }
    return `<style>${CSS}</style><div class="wrap">${parts.join('')}</div>`;
  }

  const SID_RE = /^\d{6,10}$/;

  function pickEntry(entries, rowText) {
    if (!entries || !entries.length) return null;
    return entries.find(e => e.course && rowText.includes(e.course)) || entries[0];
  }

  function decorate() {
    S.queued = false;
    mountPill();
    const on = S.on && S.data;
    let matched = 0;
    const rowEntry = new Map();          // tr → {e: entry, k: sid:course}
    for (const tr of document.querySelectorAll(
        'table tbody tr, [role="rowgroup"] > [role="row"], [role="grid"] [role="row"]')) {
      let host = tr.querySelector('[data-cn="badge"]');
      if (!on) { if (host) host.remove(); continue; }
      const cells = tr.querySelectorAll(
        ':scope > td:not([data-cn]), :scope > [role="cell"], :scope > [role="gridcell"]');
      if (cells.length < 2) continue;
      let sid = '';
      for (const td of cells) {
        const t = (td.textContent || '').trim();
        if (SID_RE.test(t)) { sid = t.replace(/^0+/, ''); break; }
      }
      if (!sid) continue;
      const entry = pickEntry((S.data.students || {})[sid], tr.textContent || '');
      if (!entry) { if (host) host.remove(); continue; }
      matched++;
      const key = S.v + ':' + sid + ':' + entry.course;
      if (tr.tagName === 'TR') {
        rowEntry.set(tr, { e: entry, k: sid + ':' + entry.course });
        const tb = tr.closest('table');
        if (tb) S.colTables.add(tb);
      }
      if (host && host.dataset.key === key) continue;
      if (!host) {
        host = document.createElement('div');
        host.setAttribute('data-cn', 'badge');
        host.attachShadow({ mode: 'open' });
        cells[0].appendChild(host);
      }
      host.dataset.key = key;
      host.shadowRoot.innerHTML = badgeHtml(entry);
    }
    for (const tb of Array.from(S.colTables)) {
      if (!tb.isConnected) { S.colTables.delete(tb); continue; }
      syncTable(tb, rowEntry);
      if (!on) S.colTables.delete(tb);
    }
    S.matched = matched;
    pill.classList.toggle('off', !S.on);
    pillRoot.querySelector('.txt').textContent = !S.data ? 'CaseloadNotes · waiting for data'
      : S.on ? `CaseloadNotes · ${matched} on this page` : 'CaseloadNotes · hidden';
    renderPanel();
  }


  // ---- extra list columns (⚙ Columns) -----------------------------------
  // Learner360's list is a plain Angular Material table (probed: no virtual
  // scroll, 20 rows/page). We append real <th>/<td> cells AFTER Angular's
  // own (its cell outlet inserts before its anchor, so ours stay last),
  // borrowing the native cell classes so they look native. Every body row
  // gets cells (blank when not ours) so the columns line up.
  const KIND_STYLE = {
    pass: 'background:#e3f4ea;color:#135c32;border:1px solid #9fd3b1',
    fail: 'background:#fdecea;color:#8a1f14;border:1px solid #f2b1aa',
    mid: 'background:#fff4dc;color:#7a5600;border:1px solid #f0d08b' };
  const stripCols = cn => String(cn || '').split(/\s+/).filter(t => t &&
    !/^(cdk-column-|mat-column-|mat-sort-header|cdk-.*sticky|mat-.*sticky)/.test(t)).join(' ');

  function colTd(proto, v) {
    const td = document.createElement('td');
    td.setAttribute('data-cn', 'col');
    td.className = stripCols(proto.className);
    if (v) {
      const span = document.createElement('span');
      span.textContent = v[0];
      const st = KIND_STYLE[v[1]];
      span.style.cssText = (st ? st + ';border-radius:9px;padding:1px 6px;' : '')
        + 'white-space:nowrap;font-size:12px';
      if (v[2]) td.title = v[2];
      td.appendChild(span);
    }
    return td;
  }

  function syncTable(table, rowEntry) {
    const show = !!(S.on && S.data);
    const cols = show ? (S.data.list_cols || []) : [];
    const sig = show ? S.v + '|' + cols.map(c => c[0]).join(',') : '';
    const hr = table.tHead && table.tHead.rows[0];
    if (hr && (hr.dataset.cnCols || '') !== sig) {
      hr.querySelectorAll(':scope > [data-cn="col"]').forEach(x => x.remove());
      const proto = hr.querySelector(':scope > th:not([data-cn])');
      if (show && proto) {
        for (const [, label] of cols) {
          const th = document.createElement('th');
          th.setAttribute('data-cn', 'col');
          th.className = stripCols(proto.className);
          th.textContent = label;
          th.title = 'CaseloadNotes column';
          hr.appendChild(th);
        }
        const g = document.createElement('th');
        g.setAttribute('data-cn', 'col');
        g.className = stripCols(proto.className);
        g.innerHTML = `<button type="button" title="Choose CaseloadNotes columns"
          style="border:1px solid #d5dde8;background:#fff;border-radius:6px;cursor:pointer;
          padding:2px 7px;font:600 12px/1.2 inherit;color:#002855;white-space:nowrap">⚙ Columns${cols.length ? ' (' + cols.length + ')' : ''}</button>`;
        g.querySelector('button').addEventListener('click', ev => {
          ev.stopPropagation();
          openColPicker();
        });
        hr.appendChild(g);
      }
      hr.dataset.cnCols = sig;
    }
    for (const body of table.tBodies) {
      for (const tr of body.rows) {
        const re = rowEntry.get(tr);
        const key = sig + '#' + (re ? re.k : '');
        if ((tr.dataset.cnCols || '') === key) continue;
        tr.querySelectorAll(':scope > td[data-cn="col"]').forEach(x => x.remove());
        const proto = tr.querySelector(':scope > td:not([data-cn])');
        // Skip placeholder rows ("No data" spanning the table).
        if (show && proto && proto.colSpan <= 1) {
          for (const [k] of cols) tr.appendChild(colTd(proto, re && (re.e.cols || {})[k]));
          tr.appendChild(colTd(proto, null));          // under ⚙ Columns
        }
        tr.dataset.cnCols = key;
      }
    }
  }

  const pickHost = document.createElement('div');
  pickHost.setAttribute('data-cn', 'colpick');
  const pickRoot = pickHost.attachShadow({ mode: 'open' });
  const KCSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .back { position: fixed; inset: 0; background: rgba(0,20,45,.25);
      z-index: 2147483001; display: flex; align-items: center; justify-content: center;
      font: 13px/1.35 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #1b2a3a; }
    .dlg { background: #fff; border-radius: 10px; width: 440px; max-width: 92vw;
      max-height: 78vh; display: flex; flex-direction: column;
      box-shadow: 0 12px 40px rgba(0,40,85,.3); }
    .hd { padding: 12px 14px 6px; font-weight: 700; color: #002855; }
    .sub { padding: 0 14px 6px; color: #5b6b7f; font-size: 12px; }
    .q { margin: 0 14px 6px; height: 28px; border: 1px solid #d5dde8;
      border-radius: 6px; padding: 0 8px; font: inherit; }
    .ls { overflow: auto; padding: 0 14px; flex: 1; }
    .grp { margin: 8px 0 2px; font-size: 11px; font-weight: 700; color: #5b6b7f;
      text-transform: uppercase; letter-spacing: .04em; }
    label { display: flex; gap: 6px; align-items: center; padding: 2px 0; cursor: pointer; }
    label .n { margin-left: auto; color: #8a97a8; font-size: 11px; }
    .ft { display: flex; gap: 6px; padding: 10px 14px; border-top: 1px solid #eef2f7; }
    .ft .sp { flex: 1; }
    button { height: 28px; padding: 0 12px; border-radius: 6px; cursor: pointer;
      border: 1px solid #d5dde8; background: #fff; font: inherit; }
    button.pri { background: #002855; border-color: #002855; color: #fff; }
  `;

  function openColPicker() {
    if (!S.data) return;
    const cat = S.data.list_catalog || [];
    const label = Object.fromEntries(cat);
    let chosen = (S.data.list_cols || []).map(c => c[0]).filter(k => k in label);
    const MAX = 12;
    const row = ([k, d]) => {
      const i = chosen.indexOf(k);
      return `<label><input type="checkbox" value="${esc(k)}"${i >= 0 ? ' checked' : ''}${i < 0 && chosen.length >= MAX ? ' disabled' : ''}> ${esc(d)}${i >= 0 ? `<span class="n">#${i + 1}</span>` : ''}</label>`;
    };
    const draw = q => {
      const m = ([, d]) => !q || d.toLowerCase().includes(q);
      const bi = cat.filter(([k]) => k.startsWith('@')).filter(m);
      const fl = cat.filter(([k]) => !k.startsWith('@')).filter(m);
      return (bi.length ? `<div class="grp">CaseloadNotes</div>${bi.map(row).join('')}` : '')
        + (fl.length ? `<div class="grp">Caseload fields</div>${fl.map(row).join('')}` : '');
    };
    pickRoot.innerHTML = `<style>${KCSS}</style><div class="back"><div class="dlg" role="dialog" aria-label="CaseloadNotes columns">
      <div class="hd">CaseloadNotes columns</div>
      <div class="sub">Added to the right of Learner360's own columns, in the order you tick them (max ${MAX}).</div>
      <input class="q" placeholder="Search fields…">
      <div class="ls">${draw('')}</div>
      <div class="ft"><button data-b="clear">Clear all</button><span class="sp"></span>
        <button data-b="cancel">Cancel</button><button class="pri" data-b="save">Save</button></div></div></div>`;
    if (!pickHost.isConnected) document.body.appendChild(pickHost);
    const ls = pickRoot.querySelector('.ls');
    const q = pickRoot.querySelector('.q');
    const close = () => { pickHost.remove(); };
    ls.addEventListener('change', ev => {
      const k = ev.target.value;
      chosen = ev.target.checked ? chosen.concat([k]).slice(0, MAX) : chosen.filter(x => x !== k);
      ls.innerHTML = draw(q.value.trim().toLowerCase());
    });
    q.addEventListener('input', () => { ls.innerHTML = draw(q.value.trim().toLowerCase()); });
    pickRoot.querySelector('.back').addEventListener('click', ev => {
      if (ev.target.classList.contains('back')) close();
    });
    pickRoot.querySelector('.dlg').addEventListener('keydown', ev => {
      if (ev.key === 'Escape') close();
    });
    pickRoot.querySelector('[data-b="cancel"]').addEventListener('click', close);
    pickRoot.querySelector('[data-b="clear"]').addEventListener('click', () => {
      chosen = [];
      ls.innerHTML = draw(q.value.trim().toLowerCase());
    });
    pickRoot.querySelector('[data-b="save"]').addEventListener('click', () => {
      S.outbox.push({ type: 'list_cols', keys: chosen });
      // Headers now (the column keys are part of each cell's signature, so
      // rows redraw); the values arrive with the app's next snapshot.
      S.data.list_cols = chosen.map(k => [k, label[k]]);
      close();
      schedule();
    });
    q.focus();
  }

  // ---- student panel (learner pages) ------------------------------------
  const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
  const PCSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .panel { position: fixed; top: 72px; right: 0; bottom: 16px; width: 340px;
      z-index: 2147482999; display: flex; flex-direction: column;
      background: #fff; border: 1px solid #d5dde8; border-right: none;
      border-radius: 10px 0 0 10px; box-shadow: -6px 0 24px rgba(0,40,85,.12);
      font: 400 12.5px/1.4 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      color: #1b2a3a; transform: translateX(0);
      transition: transform .18s ease, opacity .18s ease; }
    .panel.collapsed { transform: translateX(312px); }
    .tab { position: absolute; left: 0; top: 0; bottom: 0; width: 28px;
      display: flex; align-items: center; justify-content: center;
      background: #002855; color: #fff; cursor: pointer; border: none;
      border-radius: 10px 0 0 10px; writing-mode: vertical-rl;
      font: 600 11px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      letter-spacing: .06em; }
    .body { margin-left: 28px; flex: 1; overflow-y: auto; padding: 14px 14px 18px;
      scrollbar-width: thin; }
    h2 { font-size: 15px; margin: 0 0 2px; color: #002855; }
    .sub { color: #5b6b7f; font-size: 11.5px; margin-bottom: 10px; }
    .sec { border-top: 1px solid #e8edf3; padding: 10px 0 6px; }
    .asm { margin-top: 8px; padding-top: 6px; border-top: 1px dashed #e8edf3; }
    .tabs { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0 6px; }
    .flds { margin-top: 6px; }
    .fbtn { margin-left: 6px; padding: 3px 6px; }
    .fsearch { width: 100%; box-sizing: border-box; margin: 4px 0 6px;
      padding: 5px 7px; border: 1px solid #c9d4e2; border-radius: 6px;
      font: 12px -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
    .flist { max-height: 46vh; overflow: auto; border: 1px solid #e8edf3;
      border-radius: 6px; padding: 4px 6px; }
    .fopt { display: block; font-size: 12px; padding: 2px 0; cursor: pointer; }
    .tb { font: 600 11px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      padding: 4px 7px; border-radius: 12px; border: 1px solid #c9d4e2;
      background: #fff; color: #3d4b5c; cursor: pointer; }
    .tb.on { background: #002855; border-color: #002855; color: #fff; }
    .grid { border-collapse: collapse; width: 100%; margin: 6px 0 2px;
      font-size: 11px; }
    .grid th { font-weight: 700; color: #5b6b7f; padding: 2px 3px;
      text-align: center; white-space: nowrap; }
    .grid td { border: 1px solid #e8edf3; padding: 3px; text-align: center; }
    .grid td.rl { text-align: left; color: #3d4b5c; max-width: 110px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .g-ok { background: #e3f4ea; color: #145c32; }
    .g-mid { background: #fff4e5; color: #8a4b00; }
    .g-bad { background: #fde8e8; color: #9b1c1c; }
    .hint { font-size: 10.5px; color: #7a8796; margin: 2px 0 4px; }
    .warn { margin: 6px 0; padding: 6px 8px; border-radius: 6px;
      background: #fff4e5; color: #8a4b00; font-size: 12px; line-height: 1.35; }
    .topics { font-size: 12px; color: #3d4b5c; margin: 4px 0; line-height: 1.35; }
    .links { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .lnk { font: 600 11.5px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      padding: 5px 8px; border-radius: 6px; border: 1px solid #c9d4e2;
      background: #fff; color: #002855; text-decoration: none; cursor: pointer; }
    .lnk:hover { background: #eef3f9; }
    .sec:first-of-type { border-top: none; }
    .lbl { font-size: 10.5px; font-weight: 700; letter-spacing: .05em;
      text-transform: uppercase; color: #5b6b7f; margin-bottom: 6px; }
    .row { display: flex; justify-content: space-between; gap: 8px;
      padding: 2px 0; }
    .row .k { color: #5b6b7f; }
    .chips { display: flex; flex-wrap: wrap; gap: 4px; }
    .chip { display: inline-flex; align-items: center; height: 20px;
      padding: 0 7px; border-radius: 10px; font-size: 11px; font-weight: 600;
      background: #eef2f7; border: 1px solid #d5dde8; white-space: nowrap; }
    .t-pass { background:#e3f4ea; border-color:#9fd3b1; color:#135c32; }
    .t-return { background:#fdecea; border-color:#f2b1aa; color:#8a1f14; }
    .t-pending { background:#e8f0fb; border-color:#a9c3ea; color:#1d4f91; }
    .t-none { background:#f4f6f9; border-color:#e1e6ee; color:#8a97a8; }
    .r-low { background:#e3f4ea; border-color:#9fd3b1; color:#135c32; }
    .r-mid { background:#fff4dc; border-color:#f0d08b; color:#7a5600; }
    .r-high { background:#fdecea; border-color:#f2b1aa; color:#8a1f14; }
    .step { display: flex; gap: 6px; align-items: baseline; padding: 2px 0; }
    .mark { width: 14px; text-align: center; font-weight: 700; }
    .s-done .mark { color: #1a7f43; } .s-skipped .mark { color: #8a97a8; }
    .s-due .mark { color: #002855; } .s-blocked .mark { color: #b45309; }
    .s-due { font-weight: 600; }
    .note { padding: 6px 0; border-bottom: 1px dashed #e8edf3; }
    .note:last-child { border-bottom: none; }
    .note .meta { color: #5b6b7f; font-size: 11px; }
    .ea { color: #c2410c; font-weight: 600; }
    svg { display: block; }
    .pron { display: flex; align-items: center; gap: 6px; margin: -4px 0 10px; }
    .pron .unk { color: #8a97a8; font-style: italic; }
    .icon { border: 1px solid #d5dde8; background: #fff; color: #002855;
      border-radius: 6px; height: 22px; min-width: 22px; padding: 0 6px;
      cursor: pointer; font: 600 11px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
    .icon:hover { background: #eef2f7; }
    .editor { display: flex; flex-wrap: wrap; gap: 4px; margin: -4px 0 10px; }
    .editor .opt { border: 1px solid #d5dde8; background: #fff; border-radius: 11px;
      height: 22px; padding: 0 9px; cursor: pointer; color: #1b2a3a;
      font: 500 11.5px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
    .editor .opt:hover, .editor .opt.sel { background: #002855; color: #fff;
      border-color: #002855; }
    .editor input { height: 22px; width: 110px; border: 1px solid #d5dde8;
      border-radius: 6px; padding: 0 6px;
      font: 400 11.5px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
    .saved { color: #1a7f43; font-size: 11px; }
  `;
  const panelHost = document.createElement('div');
  panelHost.setAttribute('data-cn', 'panel');
  const panelRoot = panelHost.attachShadow({ mode: 'open' });
  let panelCollapsed = false;
  try { panelCollapsed = localStorage.getItem('cnPanelCollapsed') === '1'; } catch (e) {}
  let panelKey = '';

  const MARK = { done: '✓', skipped: '–', due: '●', blocked: '!' };

  function spark(points) {
    if (!points || points.length < 2) return '';
    const W = 300, H = 44, P = 4;
    const xs = (i) => P + i * (W - 2 * P) / (points.length - 1);
    const ys = (v) => H - P - v * (H - 2 * P);
    const d = points.map((p, i) => (i ? 'L' : 'M') + xs(i).toFixed(1) + ' ' + ys(p[1]).toFixed(1)).join(' ');
    const bands = [0.2, 0.5, 0.7, 0.9].map(b =>
      `<line x1="${P}" x2="${W - P}" y1="${ys(b)}" y2="${ys(b)}" stroke="#eef2f7" stroke-width="1"/>`).join('');
    const last = points[points.length - 1];
    return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="MOMI score, last ${points.length} weeks">${bands}
      <path d="${d}" fill="none" stroke="#002855" stroke-width="2" stroke-linejoin="round"/>
      <circle cx="${xs(points.length - 1)}" cy="${ys(last[1])}" r="3" fill="#002855"/></svg>
      <div class="row"><span class="k">${esc(points[0][0])}</span>
      <span>now ${Math.round(last[1] * 100)}</span></div>`;
  }

  // ---- Assessments box (src/assess_summary): PA tasks, OA, pre-A -------
  const PRE_TIP = 'Pre-assessment scores did not predict OA scores in the C949 '
    + 'analysis (students can retake it and learn the items) — not a readiness signal.';
  const tabSel = {};                       // "sid|course" -> selected tab
  const rateCls = r => r === 'Competent' || r === 'Exemplary' ? 'g-ok'
    : (r === 'Unsatisfactory' ? 'g-bad' : (r ? 'g-mid' : ''));
  const pctCls = (p, cut) => p == null ? '' : (p >= (cut || 74) ? 'g-ok' : (p >= (cut || 74) - 10 ? 'g-mid' : 'g-bad'));
  const pctTxt = p => p == null ? '' : p + '%';

  function boxFlags(e) {
    const a = e.assess || {}, ty = a.types || {}, oa = a.oa || {};
    const tasks = (e.tasks || []).length > 0;
    return {
      pa: tasks && ty.pa !== false,
      oa: ty.oa === true || (ty.oa == null && !!(oa.used || (a.links || {}).coaching)),
      pre: ty.oa !== false && !!((a.pre || {}).sittings),
    };
  }

  function taskChips(e) {
    return (e.tasks || []).map(t =>
      `<span class="chip t-${esc(t.kind)}" title="${esc(t.status || '')}">T${t.n}${t.date ? ' · ' + esc(t.date.slice(5)) : ''}${t.count ? ' (' + esc(t.count) + ')' : ''}</span>`).join('');
  }

  // Competency × attempt grid: Pre-A first/last + every OA attempt.
  function gridHtml(a) {
    const cols = [], pre = (a.pre && a.pre.rows) || [], cut = a.cut;
    const preWith = pre.filter(p => p.pct != null);
    if (preWith.length) cols.push({ lab: 'Pre ' + preWith[0].n, kind: 'pre', d: preWith[0] });
    if (preWith.length > 1) cols.push({ lab: 'Pre ' + preWith[preWith.length - 1].n, kind: 'pre', d: preWith[preWith.length - 1] });
    for (const t of a.attempts || []) cols.push({ lab: 'OA ' + t.n, kind: 'oa', d: t });
    if (!cols.length) return '';
    const nums = Object.keys(a.titles || {});
    for (const c of cols) for (const k of Object.keys((c.d.comps && !Array.isArray(c.d.comps)) ? c.d.comps : {}))
      if (!nums.includes(k)) nums.push(k);
    nums.sort();
    let h = `<table class="grid"><tr><th></th>${cols.map(c => `<th>${esc(c.lab)}</th>`).join('')}</tr>`;
    h += `<tr><td class="rl">Score</td>` + cols.map(c => {
      const v = c.kind === 'pre' ? c.d.pct : c.d.score;
      const cls = c.kind === 'oa' && c.d.passed ? 'g-ok' : pctCls(v, cut);
      return `<td class="${cls}" title="${esc(c.d.date || '')}">${v == null ? '—' : esc(v) + (c.kind === 'pre' ? '%' : '')}${c.kind === 'oa' && c.d.passed ? ' ✓' : ''}</td>`;
    }).join('') + `</tr>`;
    for (const n of nums) {
      const title = (a.titles || {})[n] || n;
      h += `<tr><td class="rl" title="${esc(n)}">${esc(title)}</td>` + cols.map(c => {
        if (c.kind === 'pre') {
          const p = (c.d.comps || {})[n];
          return `<td class="${pctCls(p, cut)}">${pctTxt(p) || '—'}</td>`;
        }
        const x = (c.d.comps || []).find(z => z.num === n);
        if (!x) return `<td>—</td>`;
        return `<td class="${rateCls(x.rating)}" title="${esc(x.rating)}">${x.pct != null ? x.pct + '%' : esc((x.rating || '').slice(0, 5))}</td>`;
      }).join('') + `</tr>`;
    }
    return h + `</table><div class="hint">Cells: pre-A % correct per competency; OA = coaching rating${a.attempts && a.attempts.some(t => (t.comps || []).some(z => z.pct != null)) ? ' and %' : ''}. Green = competent / ≥ cut.</div>`;
  }

  function oaTab(a, t) {
    let h = `<div class="row"><span class="k">OA ${esc(t.n)}</span><span>${esc(t.date || '')} · score ${t.score != null ? esc(t.score) : '—'}${a.cut ? ' (cut ' + esc(a.cut) + ')' : ''}${t.passed ? ' ✓ passed' : ''}${t.minutes != null ? ' · ' + esc(Math.round(t.minutes)) + ' min' : ''}</span></div>`;
    if ((t.comps || []).length)
      h += `<div class="chips">` + t.comps.map(x =>
        `<span class="chip ${rateCls(x.rating)}" title="${esc(x.num)}">${esc(x.title || x.num)}: ${esc(x.rating)}${x.pct != null ? ' · ' + x.pct + '%' : ''}</span>`).join('') + `</div>`;
    else h += `<div class="hint">No coaching report stored for this attempt.</div>`;
    if ((t.topics || []).length) h += `<div class="topics">Suggested study: ${esc(t.topics.join('; '))}</div>`;
    return h;
  }

  function preTab(a) {
    const pre = a.pre || {}, rows = pre.rows || [];
    let h = `<div class="row" title="${esc(PRE_TIP)}"><span class="k">Pre-A</span><span>${esc(pre.sittings || 0)} sitting(s)${pre.since_last_oa ? ' · ' + esc(pre.since_last_oa) + ' since last OA' : ''} ⓘ</span></div>`;
    const nums = Object.keys(a.titles || {});
    for (const p of rows) for (const k of Object.keys(p.comps || {})) if (!nums.includes(k)) nums.push(k);
    nums.sort();
    if (rows.length) {
      h += `<table class="grid"><tr><th>#</th><th>Date</th><th>Total</th>${nums.map(n => `<th title="${esc(n)}">${esc(((a.titles || {})[n] || n).split(' ')[0])}</th>`).join('')}</tr>`
        + rows.map(p => `<tr><td>${esc(p.n)}</td><td>${esc(p.date || '')}</td><td class="${pctCls(p.pct, a.cut)}">${pctTxt(p.pct) || (p.passed ? 'passed' : '—')}</td>`
          + nums.map(n => { const v = (p.comps || {})[n]; return `<td class="${pctCls(v, a.cut)}">${pctTxt(v) || '—'}</td>`; }).join('') + `</tr>`).join('')
        + `</table>`;
    }
    return h;
  }

  function assessBox(e, sid) {
    const a = e.assess || {}, f = boxFlags(e), course = e.course;
    if (!f.pa && !f.oa && !f.pre && !((a.links || {}).cos)) return '';
    const oa = a.oa || {}, key = sid + '|' + course;
    const tabs = [['sum', 'Summary']];
    if (f.pa) tabs.push(['pa', 'Tasks']);
    if (f.pre) tabs.push(['pre', 'Pre-A']);
    if (f.oa) for (const t of a.attempts || []) tabs.push(['oa' + t.n, 'OA ' + t.n]);
    const sel = tabs.some(t => t[0] === tabSel[key]) ? tabSel[key] : 'sum';
    let h = `<div class="asm"><div class="lbl">Assessments${a.oa_code && f.oa ? ' · ' + esc(a.oa_code) : ''}${a.cut && f.oa ? ' · cut ' + esc(a.cut) : ''}</div>`;
    if (tabs.length > 1)
      h += `<div class="tabs">` + tabs.map(([k, lab]) =>
        `<button class="tb${k === sel ? ' on' : ''}" data-tab="${esc(k)}" data-key="${esc(key)}">${esc(lab)}</button>`).join('') + `</div>`;
    if (sel === 'sum') {
      if (f.pa) h += `<div class="row"><span class="k">Tasks</span><span></span></div><div class="chips">${taskChips(e)}</div>`;
      if (f.oa) {
        h += `<div class="row"><span class="k">OA</span><span>${oa.passed ? 'passed' : esc(oa.used || 0) + ' of ' + esc(oa.max || 4) + ' used'}</span></div>`;
        if (oa.next && (oa.next.scheduled || oa.next.approval))
          h += `<div class="row"><span class="k">Next</span><span>${oa.next.scheduled ? 'scheduled ' + esc(oa.next.scheduled) : ''}${oa.next.scheduled && oa.next.approval ? ' · ' : ''}${esc(oa.next.approval || '')}</span></div>`;
        if (a.prep)
          h += `<div class="warn">Next is attempt 4 — it repeats the form from attempt 2. `
            + (a.prep.not_competent && a.prep.not_competent.length ? `Not competent then: ${esc(a.prep.not_competent.join(', '))}. ` : '')
            + (a.prep.suggested && a.prep.suggested.length ? `Attempt-2 study topics: ${esc(a.prep.suggested.join('; '))}` : '') + `</div>`;
      }
      if (f.pre) {
        const pre = a.pre || {};
        h += `<div class="row" title="${esc(PRE_TIP)}"><span class="k">Pre-A</span><span>${esc(pre.sittings)} sitting(s) · first ${pctTxt(pre.first_pct) || '—'} · last ${pctTxt(pre.last_pct) || '—'}${pre.since_last_oa ? ' · ' + esc(pre.since_last_oa) + ' since last OA' : ''} ⓘ</span></div>`;
      }
      if (f.oa || f.pre) h += gridHtml(a);
    } else if (sel === 'pa') {
      h += `<div class="chips">${taskChips(e)}</div>`;
    } else if (sel === 'pre') {
      h += preTab(a);
    } else {
      const t = (a.attempts || []).find(x => 'oa' + x.n === sel);
      if (t) h += oaTab(a, t);
    }
    const L = a.links || {}, btns = [];
    if (L.cos) btns.push(`<a class="lnk" href="${esc(L.cos)}" target="_blank" rel="noopener">Course page</a>`);
    if (f.oa && L.coaching) btns.push(`<a class="lnk" href="${esc(L.coaching)}" target="_blank" rel="noopener">Coaching report</a>`);
    if (f.pre && L.preA) btns.push(`<a class="lnk" href="${esc(L.preA)}" target="_blank" rel="noopener">Pre-A report</a>`);
    if (e.assess) btns.push(`<button class="lnk" data-act="refresh-assess" data-course="${esc(course)}" title="Re-import this student's ${esc(course)} attempts, scores and coaching">↻ refresh</button>`);
    if (btns.length) h += `<div class="links">${btns.join('')}</div>`;
    return h + `</div>`;
  }

  // ---- ⚙ Fields picker: which caseload columns the panel shows ---------
  function openFieldPicker() {
    const cat = S.data.field_catalog || [];
    const chosen = new Set(S.data.panel_fields || []);
    const body = panelRoot.querySelector('.body');
    if (!body || !cat.length) return;
    const list = q => cat.filter(([k, d]) => !q || d.toLowerCase().includes(q))
      .map(([k, d]) => `<label class="fopt"><input type="checkbox" value="${esc(k)}"${chosen.has(k) ? ' checked' : ''}> ${esc(d)}</label>`).join('');
    body.innerHTML = `<div class="sec"><div class="lbl">Fields to show (${cat.length} available)</div>
      <input class="fsearch" placeholder="Search fields…">
      <div class="flist">${list('')}</div>
      <div class="links"><button class="lnk" data-f="save">Save</button><button class="lnk" data-f="cancel">Cancel</button><button class="lnk" data-f="none">Clear all</button></div></div>`;
    const flist = body.querySelector('.flist');
    const keep = () => flist.querySelectorAll('input').forEach(i =>
      i.checked ? chosen.add(i.value) : chosen.delete(i.value));
    body.querySelector('.fsearch').addEventListener('input', ev => {
      keep();
      flist.innerHTML = list(ev.target.value.trim().toLowerCase());
    });
    body.querySelector('[data-f="save"]').addEventListener('click', () => {
      keep();
      const keys = cat.map(([k]) => k).filter(k => chosen.has(k));  // catalog order
      S.outbox.push({ type: 'panel_fields', keys: keys });
      S.data.panel_fields = keys;
      panelKey = '';
      renderPanel();
    });
    body.querySelector('[data-f="cancel"]').addEventListener('click', () => { panelKey = ''; renderPanel(); });
    body.querySelector('[data-f="none"]').addEventListener('click', () => {
      chosen.clear();
      flist.querySelectorAll('input').forEach(i => { i.checked = false; });
    });
  }

  function panelHtml(sid, entries) {
    const e0 = entries[0];
    const courses = entries.map(e => e.course).filter(Boolean).join(' · ');
    let h = `<h2>${esc(e0.name || 'Student')}</h2>
      <div class="sub">${esc(courses)}${e0.contact_pref ? ' · prefers ' + esc(e0.contact_pref) : ''}</div>
      <div class="pron">${e0.pronouns ? `<span>${esc(e0.pronouns)}</span>`
        : '<span class="unk">pronouns unknown</span>'}
        <button class="icon" data-act="edit-pron" title="Set pronouns (saved in CaseloadNotes only)">✎</button>
        <button class="lnk fbtn" data-act="pick-fields" title="Choose extra caseload columns to show here">⚙ Fields${(S.data.panel_fields || []).length ? ' (' + S.data.panel_fields.length + ')' : ''}</button></div>`;
    for (const e of entries) {
      const box = assessBox(e, sid);
      const tasks = box ? '' : taskChips(e);   // tasks live in the box when shown
      h += `<div class="sec"><div class="lbl">${esc(e.course)}${e.term_end ? ' · ends ' + esc(e.term_end) : ''}</div>
        <div class="chips">${tasks}
        ${e.risk != null ? `<span class="chip ${riskCls(e.risk)}" title="${esc(RISK_TIP)}">risk ${Math.round(e.risk)}%</span>` : ''}
        ${e.momentum ? `<span class="chip">${esc(e.momentum)}</span>` : ''}</div>
        ${e.ea ? `<div class="row ea"><span>Open EA</span><span>${esc(e.ea)}</span></div>` : ''}`;
      if ((e.fields || []).length)
        h += `<div class="flds">` + e.fields.map(([k, v]) =>
          `<div class="row"><span class="k">${esc(k)}</span><span>${esc(v)}</span></div>`).join('') + `</div>`;
      if (e.cohort)
        h += `<div class="sub">${esc(e.cohort.team || 'team')} student · CI ${esc(e.cohort.owner || '—')}${e.cohort.program ? ' · ' + esc(e.cohort.program) : ''}${e.cohort.departed ? ' · left the roster ' + esc(e.cohort.departed) : ''}</div>`;
      h += box;
      if ((e.steps || []).length) {
        h += `<div style="margin-top:8px">` + e.steps.map(st =>
          `<div class="step s-${esc(st.status)}"><span class="mark">${MARK[st.status] || '·'}</span>${esc(st.label)}</div>`).join('') + `</div>`;
      }
      h += `</div>`;
    }
    const facts = [];
    if (e0.followup_date) facts.push(['Follow-up', e0.followup_date + (e0.followup_note ? ' — ' + e0.followup_note : '')]);
    if (e0.last_action) facts.push(['Last action', e0.last_action + (e0.days_action != null ? ` (${Math.round(e0.days_action)}d ago)` : '')]);
    if (e0.days_contact != null) facts.push(['Last contact', Math.round(e0.days_contact) + ' days ago']);
    if (e0.days_course_activity != null) facts.push(['Course activity', Math.round(e0.days_course_activity) + ' days ago']);
    if (e0.signals) facts.push(['Signals', e0.signals]);
    if (e0.term_left) facts.push(['Left this term', e0.term_left
      + (e0.term_cus_left != null ? ` (${e0.term_cus_left} CUs)` : '')]);
    if (e0.next_course) facts.push(['Next planned', e0.next_course]);
    if (e0.degree_pct != null) facts.push(['Degree', Math.round(e0.degree_pct) + '% complete']);
    if (e0.pm_email) facts.push(['Program mentor', e0.pm_email]);
    if (e0.pa_returns != null) facts.push(['PA returns / passes', `${e0.pa_returns} / ${e0.pa_passes != null ? e0.pa_passes : 0}`]);
    if (facts.length)
      h += `<div class="sec"><div class="lbl">At a glance</div>` + facts.map(([k, v]) =>
        `<div class="row"><span class="k">${esc(k)}</span><span>${esc(v)}</span></div>`).join('') + `</div>`;
    const m = (S.data.momi || {})[sid];
    if (m && m.length > 1) h += `<div class="sec"><div class="lbl">MOMI trend</div>${spark(m)}</div>`;
    const notes = (S.data.notes || {})[sid] || [];
    if (notes.length)
      h += `<div class="sec"><div class="lbl">Recent notes</div>` + notes.map(n =>
        `<div class="note"><div class="meta">${esc(n.date)} · ${esc(n.type)}${n.direction === 'inbound' ? ' · from student' : ''}${n.author ? ' · ' + esc(n.author) : ''}</div>${esc(n.text)}</div>`).join('') + `</div>`;
    return `<style>${PCSS}</style><div class="panel${panelCollapsed ? ' collapsed' : ''}">
      <button class="tab" title="Show/hide CaseloadNotes">CASELOADNOTES</button>
      <div class="body">${h}</div></div>`;
  }

  function renderPanel() {
    const m = location.pathname.match(UUID_RE) || location.href.match(UUID_RE);
    const sid = (m && S.on && S.data) ? (S.data.uuid_map || {})[m[0].toLowerCase()] : null;
    const entries = sid ? (S.data.students || {})[sid] : null;
    if (!entries || !entries.length) {
      if (panelHost.isConnected) panelHost.remove();
      panelKey = '';
      return;
    }
    if (!panelHost.isConnected && document.body) document.body.appendChild(panelHost);
    const key = S.v + ':' + sid;
    if (key === panelKey) return;
    panelKey = key;
    panelRoot.innerHTML = panelHtml(sid, entries);
    panelRoot.querySelector('.tab').addEventListener('click', () => {
      panelCollapsed = !panelCollapsed;
      try { localStorage.setItem('cnPanelCollapsed', panelCollapsed ? '1' : '0'); } catch (e) {}
      panelRoot.querySelector('.panel').classList.toggle('collapsed', panelCollapsed);
    });
    const fb = panelRoot.querySelector('[data-act="pick-fields"]');
    if (fb) fb.addEventListener('click', () => openFieldPicker());
    panelRoot.querySelectorAll('.tb[data-tab]').forEach(b =>
      b.addEventListener('click', () => {
        tabSel[b.dataset.key] = b.dataset.tab;
        panelKey = '';
        renderPanel();
      }));
    panelRoot.querySelectorAll('[data-act="refresh-assess"]').forEach(b =>
      b.addEventListener('click', () => {
        S.outbox.push({ type: 'refresh_assess', sid: sid, course: b.dataset.course });
        b.textContent = 'refreshing…';
        b.disabled = true;
      }));
    const editBtn = panelRoot.querySelector('[data-act="edit-pron"]');
    if (editBtn) editBtn.addEventListener('click', () => openPronEditor(sid, entries));
  }

  // ---- pronoun editor (saved in CaseloadNotes via the outbox) ----------
  const PRON_CHOICES = ['he/him', 'she/her', 'they/them'];

  function setPronouns(sid, entries, value) {
    for (const e of entries) e.pronouns = value;          // optimistic
    S.outbox.push({ type: 'pronouns', sid: sid, value: value });
    panelKey = '';                                         // force re-render
    renderPanel();
    const line = panelRoot.querySelector('.pron');
    if (line) line.insertAdjacentHTML('beforeend', '<span class="saved">saved</span>');
  }

  function openPronEditor(sid, entries) {
    const line = panelRoot.querySelector('.pron');
    if (!line) return;
    const cur = entries[0].pronouns || '';
    const opts = PRON_CHOICES.map(c =>
      `<button class="opt${c === cur ? ' sel' : ''}" data-v="${esc(c)}">${esc(c)}</button>`).join('');
    line.outerHTML = `<div class="editor">${opts}
      <input maxlength="30" placeholder="other…" value="${PRON_CHOICES.includes(cur) ? '' : esc(cur)}">
      <button class="icon" data-act="ok" title="Save">Save</button>
      ${cur ? '<button class="icon" data-act="clear" title="Clear">Clear</button>' : ''}
      <button class="icon" data-act="cancel" title="Cancel">✕</button></div>`;
    const ed = panelRoot.querySelector('.editor');
    const inp = ed.querySelector('input');
    ed.querySelectorAll('.opt').forEach(b =>
      b.addEventListener('click', () => setPronouns(sid, entries, b.dataset.v)));
    ed.querySelector('[data-act="ok"]').addEventListener('click', () => {
      const v = inp.value.trim();
      if (v) setPronouns(sid, entries, v.slice(0, 30));
    });
    inp.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && inp.value.trim()) setPronouns(sid, entries, inp.value.trim().slice(0, 30));
      if (ev.key === 'Escape') { panelKey = ''; renderPanel(); }
    });
    const clr = ed.querySelector('[data-act="clear"]');
    if (clr) clr.addEventListener('click', () => setPronouns(sid, entries, ''));
    ed.querySelector('[data-act="cancel"]').addEventListener('click',
      () => { panelKey = ''; renderPanel(); });
    inp.focus();
  }

  // Learner360 is a single-page app: follow in-page navigation.
  for (const fn of ['pushState', 'replaceState']) {
    const orig = history[fn];
    history[fn] = function () { const r = orig.apply(this, arguments); schedule(); return r; };
  }
  window.addEventListener('popstate', () => schedule());

  function schedule() {
    if (S.queued) return;
    S.queued = true;
    requestAnimationFrame(decorate);
  }

  // Re-decorate when Learner360 re-renders its table (paging, sorting,
  // filtering) — ignoring mutations inside our own badges.
  const mo = new MutationObserver(muts => {
    for (const m of muts) {
      const t = m.target;
      if (t && t.closest && t.closest('[data-cn]')) continue;
      schedule();
      return;
    }
  });
  const start = () => {
    mountPill();
    mo.observe(document.body, { childList: true, subtree: true });
    schedule();
  };
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);

  window.__cnOverlay = {
    version: () => S.v,
    setData: d => { S.data = d; S.v = (d && d.v != null) ? d.v : 0; schedule(); return S.v; },
    matched: () => S.matched,
    // Edits made in the panel, collected by the app's worker each tick.
    takeOutbox: () => { const o = S.outbox; S.outbox = []; return o; },
  };
})();
"""
