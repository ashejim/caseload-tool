"""`l360probe:` — record how the open Learner360 page is BUILT (DOM structure
only) so overlay features (e.g. extra list columns) can be designed against
the real page instead of guesses.

Read-only and value-free: the script reports tag names, ARIA roles, sanitized
class names, layout styles and sizes. It never returns cell text, links, ids or
attribute values — the only text captured is the table's COLUMN HEADER labels
(e.g. "Student ID"), which hold no student data. Body cells are described by
shape only, plus a yes/no "looks like a student id" flag for the cell index
the overlay keys on.
"""
from __future__ import annotations

PROBE_JS = r"""
() => {
  const CLS_OK = /^[A-Za-z_-][\w-]{0,39}$/;
  const cls = el => {
    const raw = (el.getAttribute && el.getAttribute('class')) || '';
    return raw.split(/\s+/).filter(t => CLS_OK.test(t) && !/\d{4,}/.test(t))
      .slice(0, 6).join('.');
  };
  const desc = el => {
    if (!el || !el.tagName) return '';
    const r = el.getAttribute('role');
    const c = cls(el);
    return el.tagName.toLowerCase() + (r ? `[role=${r}]` : '') + (c ? '.' + c : '');
  };
  const st = el => {
    const s = getComputedStyle(el), b = el.getBoundingClientRect();
    return { d: s.display, pos: s.position, ov: s.overflowY,
             tf: s.transform !== 'none' ? 'yes' : '',
             w: Math.round(b.width), h: Math.round(b.height),
             sh: el.scrollHeight, ch: el.clientHeight,
             gtc: s.gridTemplateColumns && s.gridTemplateColumns !== 'none'
                  ? s.gridTemplateColumns.split(' ').length + ' grid cols' : '',
             tl: s.tableLayout || '' };
  };
  const shape = (el, depth) => {
    if (!el || depth < 0) return '';
    const kids = Array.from(el.children || []);
    if (!kids.length) return '';
    const counts = {};
    for (const k of kids.slice(0, 12)) {
      const key = desc(k) + (depth > 0 && k.children.length
        ? '{' + shape(k, depth - 1) + '}' : '');
      counts[key] = (counts[key] || 0) + 1;
    }
    return Object.entries(counts).map(([k, n]) => n > 1 ? `${k}×${n}` : k).join(' ');
  };
  const SID = /^\d{6,10}$/;
  const out = { path: location.pathname.replace(/[0-9a-f-]{36}/gi, '<uuid>')
                       .replace(/\d{5,}/g, '<n>'),
                frames: document.querySelectorAll('iframe').length,
                shadow_hosts: 0, frameworks: [], prefixes: {}, lists: [] };
  const all = document.querySelectorAll('*');
  for (const el of all) {
    if (el.shadowRoot) out.shadow_hosts++;
    const raw = (el.getAttribute('class') || '').split(/\s+/);
    for (const t of raw) {
      const m = /^(mat|mdc|cdk|ag|Mui|p|ant|x|ngx|datatable|rdg|tanstack|v|el|q|chakra|slds|lightning)-/.exec(t)
        || /^(Mui)[A-Z]/.exec(t);      // MUI: MuiDataGrid-root, MuiTable-…
      if (m) out.prefixes[m[1]] = (out.prefixes[m[1]] || 0) + 1;
    }
  }
  if (document.querySelector('[ng-version]'))
    out.frameworks.push('angular ' + document.querySelector('[ng-version]').getAttribute('ng-version'));
  if (window.__NEXT_DATA__) out.frameworks.push('next.js');
  if (document.querySelector('[data-reactroot]') || Array.from(document.querySelectorAll('body *')).slice(0, 50)
        .some(e => Object.keys(e).some(k => k.startsWith('__react')))) out.frameworks.push('react');
  if (Array.from(document.querySelectorAll('body > *')).some(e => e.__vue_app__ || e.__vue__)) out.frameworks.push('vue');
  const badges = document.querySelectorAll('[data-cn="badge"]');
  out.badges = badges.length;

  // Candidate list containers: real tables + ARIA grids/tables.
  const seen = new Set();
  const cands = Array.from(document.querySelectorAll(
    'table, [role="grid"], [role="table"], [role="treegrid"]'));
  for (const b of badges) {
    const g = b.closest('table, [role="grid"], [role="table"], [role="treegrid"]');
    if (g && !cands.includes(g)) cands.unshift(g);
  }
  for (const g of cands) {
    if (seen.has(g)) continue;
    seen.add(g);
    const rows = Array.from(g.querySelectorAll('tr, [role="row"]'))
      .filter(r => r.closest('table, [role="grid"], [role="table"], [role="treegrid"]') === g);
    const heads = Array.from(g.querySelectorAll('th, [role="columnheader"]'))
      .filter(h => h.closest('table, [role="grid"], [role="table"], [role="treegrid"]') === g);
    const body = rows.filter(r => !r.querySelector('th, [role="columnheader"]'));
    const L = { el: desc(g), style: st(g),
                aria: { rowcount: g.getAttribute('aria-rowcount') || '',
                        colcount: g.getAttribute('aria-colcount') || '' },
                rows_rendered: body.length,
                has_badges: !!g.querySelector('[data-cn="badge"]'),
                headers: heads.map(h => ({
                  label: (h.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 30),
                  el: desc(h), w: Math.round(h.getBoundingClientRect().width),
                  sticky: getComputedStyle(h).position === 'sticky' ? 'yes' : '' })),
                header_row: heads[0] ? desc(heads[0].parentElement) + ' ← ' +
                  desc(heads[0].parentElement && heads[0].parentElement.parentElement) : '',
                colgroup: g.querySelectorAll('colgroup col').length,
                row_samples: [], ancestors: [] };
    for (const r of body.slice(0, 3)) {
      const cells = Array.from(r.querySelectorAll(
        ':scope > td, :scope > th, :scope > [role="cell"], :scope > [role="gridcell"]'));
      const s = st(r);
      L.row_samples.push({
        el: desc(r), parent: desc(r.parentElement), style: s,
        rowindex: r.getAttribute('aria-rowindex') ? 'yes' : '',
        inline_style: (r.getAttribute('style') || '').replace(/[\d.]+px/g, 'Npx').slice(0, 120),
        cells: cells.map((c, i) => ({
          i, el: desc(c), w: Math.round(c.getBoundingClientRect().width),
          colindex: c.getAttribute('aria-colindex') ? 'yes' : '',
          pos: getComputedStyle(c).position,
          sid: SID.test((c.textContent || '').trim()) ? 'yes' : '',
          badge: c.querySelector('[data-cn="badge"]') ? 'yes' : '',
          shape: shape(c, 1).slice(0, 160) })),
        direct_children_not_cells: Array.from(r.children).length - cells.length });
    }
    // Row → grid chain: a virtual-scroll viewport / spacer often sits INSIDE
    // the grid, between it and the rows.
    L.row_chain = [];
    if (body[0]) {
      let x = body[0].parentElement, m = 0;
      while (x && x !== g && m < 8) {
        const s = st(x);
        L.row_chain.push({ el: desc(x), d: s.d, pos: s.pos, ov: s.ov, tf: s.tf,
                           h: s.h, sh: s.sh, ch: s.ch });
        x = x.parentElement; m++;
      }
    }
    // Ancestor chain → the scroll container / virtual-scroll viewport.
    let a = g.parentElement, n = 0;
    while (a && a !== document.body && n < 10) {
      const s = st(a);
      L.ancestors.push({ el: desc(a), d: s.d, pos: s.pos, ov: s.ov, tf: s.tf,
                         h: s.h, sh: s.sh, ch: s.ch });
      a = a.parentElement; n++;
    }
    // Paging hints near the list (class names only).
    const near = g.closest('section, main, [role="main"], div') || document;
    L.paging = Array.from(near.querySelectorAll(
      '[class*="paginat" i], [aria-label*="page" i], [role="navigation"]'))
      .slice(0, 3).map(desc);
    out.lists.push(L);
  }
  out.lists.sort((x, y) => (y.has_badges - x.has_badges) || (y.rows_rendered - x.rows_rendered));
  out.lists = out.lists.slice(0, 4);
  return out;
}
"""


def summarize(res: dict) -> list:
    """Human-readable log lines for a probe result (structure only)."""
    lines = [f"page {res.get('path', '?')} · frameworks: "
             f"{', '.join(res.get('frameworks') or []) or '?'} · class prefixes: "
             + (", ".join(f"{k}-×{v}" for k, v in sorted(
                 (res.get("prefixes") or {}).items(), key=lambda kv: -kv[1])[:6])
                or "none")
             + f" · iframes {res.get('frames', 0)} · shadow hosts "
             f"{res.get('shadow_hosts', 0)} · overlay badges {res.get('badges', 0)}"]
    lists = res.get("lists") or []
    if not lists:
        lines.append("no table / grid found on this page — open the caseload list "
                     "and try again")
    for k, L in enumerate(lists, 1):
        s = L.get("style") or {}
        lines.append(f"[{k}] {L['el']}  display={s.get('d')} layout={s.get('tl')} "
                     f"{s.get('gtc', '')}  {s.get('w')}×{s.get('h')}px  rows "
                     f"rendered={L['rows_rendered']} aria-rowcount="
                     f"{L['aria']['rowcount'] or '-'} colgroup={L['colgroup']}"
                     + ("  ← overlay badges here" if L.get("has_badges") else ""))
        if L.get("header_row"):
            lines.append(f"    header row: {L['header_row']}")
        if L.get("headers"):
            lines.append("    headers: " + " | ".join(
                f"{h['label'] or '∅'} ({h['w']}px{', sticky' if h['sticky'] else ''})"
                for h in L["headers"][:20]))
            lines.append(f"    header cell: {L['headers'][0]['el']}")
        for j, r in enumerate(L.get("row_samples") or [], 1):
            rs = r.get("style") or {}
            lines.append(f"    row {j}: {r['el']} in {r['parent']}  display="
                         f"{rs.get('d')} pos={rs.get('pos')} transform="
                         f"{rs.get('tf') or 'no'} h={rs.get('h')} aria-rowindex="
                         f"{r['rowindex'] or 'no'} extra children="
                         f"{r['direct_children_not_cells']}"
                         + (f" style='{r['inline_style']}'" if r.get('inline_style') else ""))
            if j == 1:
                for c in r.get("cells") or []:
                    flags = ", ".join(x for x in (
                        "SID" if c.get("sid") else "", "badge" if c.get("badge") else "",
                        "colindex" if c.get("colindex") else "",
                        c["pos"] if c.get("pos") not in ("static", "") else "") if x)
                    lines.append(f"      cell {c['i']}: {c['el']} {c['w']}px"
                                 + (f" [{flags}]" if flags else "")
                                 + (f"  {c['shape']}" if c.get("shape") else ""))
        for tag, chain in (("between rows+list", L.get("row_chain") or []),
                           ("ancestor", L.get("ancestors") or [])):
            for a in chain:
                if a["ov"] in ("auto", "scroll") or a["pos"] in ("absolute", "relative") \
                        or a["tf"] or a["sh"] > a["ch"] + 4:
                    lines.append(f"    {tag} {a['el']}  display={a['d']} pos={a['pos']} "
                                 f"overflow={a['ov']} h={a['h']} scrollH={a['sh']}"
                                 + (" transform" if a["tf"] else ""))
        if L.get("paging"):
            lines.append("    paging: " + " · ".join(L["paging"]))
    return lines
