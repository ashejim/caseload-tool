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

  const S = { data: null, v: -1, on: true, queued: false, matched: 0 };
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
    .pill { position: fixed; right: 16px; bottom: 16px; z-index: 2147483000;
      display: flex; align-items: center; gap: 8px; padding: 8px 12px;
      border-radius: 20px; background: #002855; color: #fff;
      font: 600 12px/1 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      box-shadow: 0 4px 14px rgba(0,40,85,.28); cursor: pointer;
      user-select: none; transition: opacity .15s ease; }
    .pill.off { background: #5b6b7f; }
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
    <div class="pill" title="CaseloadNotes overlay — click to show/hide">
      <span class="dot"></span><span class="txt">CaseloadNotes</span></div>`;
  const pill = pillRoot.querySelector('.pill');
  pill.addEventListener('click', () => {
    S.on = !S.on;
    try { localStorage.setItem('cnOverlayOn', S.on ? '1' : '0'); } catch (e) {}
    schedule();
  });
  const mountPill = () => {
    if (document.body && !pillHost.isConnected) document.body.appendChild(pillHost);
  };

  // ---- badge rendering ---------------------------------------------------
  const riskCls = r => r == null ? '' : (r >= 30 ? 'r-high' : r >= 15 ? 'r-mid' : 'r-low');

  function badgeHtml(e) {
    const parts = [];
    for (const t of (e.tasks || []).slice(0, 4)) {
      const tip = `Task ${t.n}: ${t.status || (t.date ? 'submitted' : 'no attempt')}`
        + (t.date ? ` · ${t.date}` : '') + (t.count ? ` (${t.count})` : '');
      parts.push(`<span class="chip t-${esc(t.kind)}" title="${esc(tip)}">T${t.n}</span>`);
    }
    if (e.risk != null)
      parts.push(`<span class="chip ${riskCls(e.risk)}" title="Not-pass risk (CaseloadNotes model)">${Math.round(e.risk)}%</span>`);
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
    for (const tr of document.querySelectorAll(
        'table tbody tr, [role="rowgroup"] > [role="row"], [role="grid"] [role="row"]')) {
      let host = tr.querySelector('[data-cn="badge"]');
      if (!on) { if (host) host.remove(); continue; }
      const cells = tr.querySelectorAll(
        ':scope > td, :scope > [role="cell"], :scope > [role="gridcell"]');
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
    S.matched = matched;
    pill.classList.toggle('off', !S.on);
    pillRoot.querySelector('.txt').textContent = !S.data ? 'CaseloadNotes · waiting for data'
      : S.on ? `CaseloadNotes · ${matched} on this page` : 'CaseloadNotes · hidden';
  }

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
  };
})();
"""
