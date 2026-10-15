"""Learner360 (learner360.wgu.edu) — WGU's new caseload app: read-only data
access + change detection.

Hybrid plan: Salesforce stays the caseload source; we ADD what Learner360 has
that Salesforce doesn't. First: weekly NUMERIC MOMI history and MOMI's factor
breakdown (``momentum-indicator``) — the longer-horizon signal the at-risk model
has been starved of (see the data-strategy-ceiling memory).

How it reads: the browser worker opens Learner360 in its own context, harvests
the app's OAuth bearer (+ x-* headers) from the app's OWN gateway calls (exactly
like the Mongoose token), then replays read-only requests in-page. Nothing here
writes to Learner360.

Learner360 is young and changing ("use-new-caseload-endpoint" is a live feature
flag), so every capture also runs a CHANGE WATCH: a contract check of the fields
we depend on, a key diff, the app's release fingerprint, and its feature flags —
diffed against the last run (see ``diff_watch``).

This module holds the browser-free parts (parsing, contract, watch diff) —
unit-tested in tests/test_learner360.py — plus thin Playwright helpers.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

APP_URL = "https://learner360.wgu.edu/caseload"
GATEWAY = "https://faculty-api-gateway.wgu.edu"
GATEWAY_HOST = "faculty-api-gateway.wgu.edu"

CASELOAD_SEARCH = "/learner360/v1/caseloads/search"
MOMI_PATH = "/learner360/v1/momentum-indicator/"

# Facets the Learner360 UI requests with every caseload search — sent verbatim
# so our query looks like the app's own.
_FACETS = ["owner", "status", "program", "momentum", "courses", "isNglx",
           "timezone", "termEndDate", "currentTerm", "termRegistration"]


def caseload_query(page: int, size: int = 100) -> dict:
    """Body for POST caseloads/search (1-based ``page``, as the UI sends)."""
    return {"page": page, "size": size, "filters": [],
            "sort": [{"field": "lastFacultyContact", "direction": "ASC"}],
            "facetFields": list(_FACETS)}


def looks_like_login(url: str) -> bool:
    """True while the tab is on WGU's SSO (PingFederate) rather than the app."""
    u = (url or "").lower()
    return any(s in u for s in ("access.wgu.edu", "pingfed", "pingone",
                                "authorization.oauth2", "/login"))


# ---------------------------------------------------------------- parsing
def _num(v) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def norm_date(s) -> str:
    """Normalize a date string to YYYY-MM-DD; return the raw string if unknown."""
    s = str(s or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    for fmt in ("%m-%d-%Y", "%m/%d/%Y", "%m/%d/%y"):   # L360 sends MM-DD-YYYY
        try:
            return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def parse_caseload_page(resp: dict) -> tuple[list[dict], int]:
    """One caseloads/search page → (rows, total_pages). Rows keep only what we
    need to fetch + join: student_id (matches Salesforce StudentID), wgu_id
    (Learner360's learner UUID), pidm, momentum label, courses."""
    rows = []
    for c in (resp or {}).get("content") or []:
        if not isinstance(c, dict):
            continue
        sid = str(c.get("studentId") or "").strip()
        wid = str(c.get("studentWguId") or "").strip()
        if not sid or not wid:
            continue
        rows.append({
            "student_id": sid, "wgu_id": wid,
            "pidm": str(c.get("studentPidm") or ""),
            "momentum": str(c.get("momentum") or ""),
            "courses": [str(x) for x in (c.get("courses") or [])],
        })
    page = (resp or {}).get("page") or {}
    try:
        total = int(page.get("totalPages") or 1)
    except (TypeError, ValueError):
        total = 1
    return rows, max(1, total)


def page_meta(resp: dict) -> dict:
    """{content, total_elements, total_pages, number} from a caseloads/search
    page — used for robust paging (works whether pages are 0- or 1-based) and
    to report a shortfall vs the server's own total."""
    resp = resp or {}
    page = resp.get("page") or {}

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    content = resp.get("content") or []
    return {"content": len(content) if isinstance(content, list) else 0,
            "total_elements": _i(page.get("totalElements")),
            "total_pages": _i(page.get("totalPages")),
            "number": _i(page.get("number")),
            "no_id": sum(1 for c in content if isinstance(c, dict)
                         and (not c.get("studentWguId") or not c.get("studentId")))}


def parse_momi(resp: dict) -> dict:
    """momentum-indicator response → {current, weekly[], factors[]}. ``weekly``
    rows: week_date, momentum (label), score (numeric), wow, term_start.
    ``factors``: category, impact."""
    resp = resp or {}
    weekly = []
    for w in resp.get("weeklyMomiScores") or []:
        if not isinstance(w, dict):
            continue
        d = norm_date(w.get("date"))
        if not d:
            continue
        weekly.append({
            "week_date": d,
            "momentum": str(w.get("momentum") or ""),
            "score": _num(w.get("momentumScore")),
            "wow": _num(w.get("weekOverWeekDifference")),
            "term_start": _num(w.get("fromTermStartDifference")),
        })
    factors = []
    for f in resp.get("factorBreakdowns") or []:
        if isinstance(f, dict) and f.get("category"):
            factors.append({"category": str(f["category"]),
                            "impact": _num(f.get("impactScore"))})
    return {"current": str(resp.get("currentMomentum") or ""),
            "weekly": weekly, "factors": factors}


def momi_capture_due(last_captured: Optional[str], days: int,
                     now: Optional[datetime] = None) -> bool:
    """Should the background auto-capture run? ``last_captured`` is the ISO
    timestamp of the newest stored MOMI capture (history.momi_capture_status),
    None if never. ``days`` <= 0 = auto-capture off."""
    if days <= 0:
        return False
    if not last_captured:
        return True
    try:
        last = datetime.fromisoformat(str(last_captured)[:19])
    except ValueError:
        return True
    return ((now or datetime.now()) - last).total_seconds() >= days * 86400


# ---------------------------------------------------------- student notes
# GET /studentNotes/v1/notes/<learner uuid> → the learner's FULL note history
# (all authors / courses — like SF's Notes History). Field names below are
# candidates; the first `l360notes: dry` run reports which ones are really used
# (notes_shape) so the mapping can be pinned down.
NOTES_PATH = "/studentNotes/v1/notes/"

_NOTE_FIELDS = {
    # Live 2026-10-15: no plain 'id'; candidates seen = compositeKey, newId,
    # oldId, noteNumber, interactionId (which is unique is checked by dry run).
    "id": ("compositeKey", "newId", "oldId", "noteNumber", "id", "noteId"),
    # ~3% of notes lack createdDate → fall back to lastModifiedDate.
    "created": ("wguCreationDateTime", "createdDate", "createdAt",
                "creationDate", "noteDate", "date", "lastModifiedDate"),
    "type": ("type", "noteType"),
    "subtype": ("subType", "subtype", "noteSubType"),
    "course": ("courseCode", "course"),
    "author": ("authorName", "createdByName", "author", "createdBy"),
    "subject": ("subject", "title"),
    "body": ("text", "body", "noteText", "description", "shortText"),
}


def _pick(d: dict, names) -> tuple:
    """(field name, value) of the first candidate present & non-empty."""
    for n in names:
        v = d.get(n)
        if v not in (None, "", [], {}):
            return n, v
    return "", None


def iso_utc(s) -> str:
    """Normalize a timestamp to the stored notes format
    'YYYY-MM-DDTHH:MM:SS.000Z' (UTC). Naive values are taken as UTC (the
    gateway's convention); unparseable → ''."""
    from datetime import timezone
    t = str(s or "").strip()
    if not t:
        return ""
    if t.isdigit() and len(t) >= 12:            # epoch millis
        dt = datetime.fromtimestamp(int(t) / 1000, tz=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            dt = None
            for fmt in ("%m-%d-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                        "%m-%d-%Y %I:%M %p", "%m/%d/%Y %I:%M %p"):
                try:
                    dt = datetime.strptime(t, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# Learner360 sends a mix of SF labels ("Live Call and Email to Student") and
# snake_case codes ("email_from_student"). Codes are mapped to the SF label so
# the SF-tuned channel/direction classifiers and the cross-source type match
# work unchanged.
_TYPE_LABELS = {
    "instant_message_text": "Instant Message (IM) / Text",
}
_SMALL_WORDS = {"to", "from", "with", "and", "or", "of", "on", "in"}


def sf_type_label(t: str) -> str:
    """'email_from_student' → 'Email from Student'; SF-style labels pass."""
    t = (t or "").strip()
    if not t or "_" not in t:
        return t
    if t.lower() in _TYPE_LABELS:
        return _TYPE_LABELS[t.lower()]
    words = t.lower().split("_")
    return " ".join(w if (i and w in _SMALL_WORDS) else w.capitalize()
                    for i, w in enumerate(words))


def _author_text(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or " ".join(
            x for x in (v.get("firstName"), v.get("lastName")) if x) or "")
    return str(v or "")


def notes_list(resp) -> list:
    """The note items in a notes response (a bare list or wrapped)."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("content", "notes", "items", "data", "results"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def parse_notes(resp) -> list:
    """Normalize one learner's notes → [{l360_id, created_at, type, course,
    author, subject, body, author_is_ci, author_is_pm, from_sms}]. ``type``
    is the SF-style label (sf_type_label) so the existing channel/direction
    classifiers apply; subType (a topic like 'Goals and Tasks') is not merged
    in — SF's Type__c doesn't carry it either."""
    out = []
    for n in notes_list(resp):
        if not isinstance(n, dict):
            continue
        _, nid = _pick(n, _NOTE_FIELDS["id"])
        _, created = _pick(n, _NOTE_FIELDS["created"])
        _, typ = _pick(n, _NOTE_FIELDS["type"])
        typ = sf_type_label(str(typ or ""))
        _, body = _pick(n, _NOTE_FIELDS["body"])
        out.append({
            "l360_id": str(nid or ""),
            "created_at": iso_utc(created),
            "type": typ,
            "course": str(_pick(n, _NOTE_FIELDS["course"])[1] or ""),
            "author": _author_text(_pick(n, _NOTE_FIELDS["author"])[1]),
            "subject": str(_pick(n, _NOTE_FIELDS["subject"])[1] or ""),
            "body": str(body or ""),
            "author_is_ci": bool(n.get("authorIsCI") or n.get("authorIsCi")),
            "author_is_pm": bool(n.get("authorIsPM") or n.get("authorIsPm")),
            "from_sms": bool(n.get("createdFromSms")),
        })
    return out


def _mask(v) -> str:
    """Shape of a value without its content: digits→9, letters→a."""
    return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", str(v)))[:40]


def notes_shape(resps: list) -> dict:
    """FERPA-safe summary of raw notes responses for the dry-run log: field
    names (with how many notes carry each), which candidate the parser picked
    per role, masked id/date patterns, and the type / subType LABELS (category
    names, not student content)."""
    from collections import Counter
    keys, picked, ids, dates, types = (Counter() for _ in range(5))
    wrappers = Counter()
    id_vals = {f: [] for f in _NOTE_FIELDS["id"]}
    dup_same_event = 0
    for resp in resps:
        wrappers["list" if isinstance(resp, list) else
                 ("dict:" + ",".join(sorted(resp)[:6]) if isinstance(resp, dict)
                  else type(resp).__name__)] += 1
        seen_events = set()
        for n in notes_list(resp):
            if not isinstance(n, dict):
                continue
            keys.update(n.keys())
            for f in id_vals:
                if n.get(f) not in (None, ""):
                    id_vals[f].append(str(n[f]))
            ev = (str(_pick(n, _NOTE_FIELDS["created"])[1] or ""),
                  " ".join(str(_pick(n, _NOTE_FIELDS["body"])[1] or "").split())[:60])
            if ev[0] and ev in seen_events:
                dup_same_event += 1
            seen_events.add(ev)
            for role, names in _NOTE_FIELDS.items():
                name, v = _pick(n, names)
                if name:
                    picked[f"{role}<-{name}"] += 1
                    if role == "id":
                        ids[_mask(v)] += 1
                    elif role == "created":
                        dates[_mask(v)] += 1
            t = str(_pick(n, _NOTE_FIELDS["type"])[1] or "")
            s = str(_pick(n, _NOTE_FIELDS["subtype"])[1] or "")
            types[f"{t} | {s}" if s else t] += 1
    return {"wrappers": dict(wrappers), "keys": dict(keys.most_common()),
            "picked": dict(picked.most_common()),
            # {field: "present/distinct pattern"} — which id is truly unique.
            "id_uniqueness": {f: f"{len(v)}/{len(set(v))} "
                              f"{_mask(v[0]) if v else ''}"
                              for f, v in id_vals.items() if v},
            "dup_same_event": dup_same_event,
            "id_patterns": dict(ids.most_common(3)),
            "date_patterns": dict(dates.most_common(3)),
            "types": dict(types.most_common(25))}


_GET_BATCH_JS = """async ([base, headers, ids, conc]) => {
  const out = {}; let i = 0;
  const h = Object.assign(
    {'Accept': 'application/json', 'Content-Type': 'application/json'}, headers);
  const run = async () => {
    while (i < ids.length) {
      const id = ids[i++];
      try { const r = await fetch(base + id, {headers: h}); out[id] = {status: r.status, body: await r.text()}; }
      catch (e) { out[id] = {status: 'fetch-error', body: String(e)}; }
    }
  };
  await Promise.all(Array.from({length: conc}, run));
  return out;
}"""


def fetch_notes_batch(page, headers: dict, wgu_ids: list,
                      concurrency: int = 5) -> dict:
    """GET studentNotes for many learners, in-page. → {wgu_id: {status, body}}."""
    return page.evaluate(_GET_BATCH_JS,
                         [GATEWAY + NOTES_PATH, headers, list(wgu_ids),
                          max(1, int(concurrency))]) or {}


# ---------------------------------------------------------- change watch
# REQUIRED fields per endpoint — what our parsers depend on. A missing one is a
# breaking change (logged loudly; data from that endpoint can't be trusted).
CONTRACT = {
    "caseloads/search": {
        "top": {"content", "page"},
        "item": {"studentId", "studentWguId", "studentPidm", "momentum"},
    },
    "momentum-indicator": {
        "top": {"currentMomentum", "weeklyMomiScores", "factorBreakdowns"},
        "weeklyMomiScores": {"date", "momentum", "momentumScore"},
        "factorBreakdowns": {"category", "impactScore"},
    },
}

# Which list in the response holds the per-row items for each endpoint.
_ITEM_LISTS = {
    "caseloads/search": {"item": "content"},
    "momentum-indicator": {"weeklyMomiScores": "weeklyMomiScores",
                           "factorBreakdowns": "factorBreakdowns"},
}


def observed_keys(name: str, resp) -> dict:
    """{'top': [...], <item group>: [...]} — the field names actually present,
    for diffing release-to-release (new fields hint at coming features)."""
    out = {"top": sorted(resp.keys()) if isinstance(resp, dict) else []}
    for group, list_key in _ITEM_LISTS.get(name, {}).items():
        items = (resp or {}).get(list_key) if isinstance(resp, dict) else None
        keys: set = set()
        for it in (items or [])[:25]:
            if isinstance(it, dict):
                keys.update(it.keys())
        out[group] = sorted(keys)
    return out


def check_contract(name: str, resp) -> list[str]:
    """Missing REQUIRED fields → human-readable breaking-change messages."""
    spec = CONTRACT.get(name)
    if not spec:
        return []
    if not isinstance(resp, dict):
        return [f"{name}: response is no longer a JSON object"]
    seen = observed_keys(name, resp)
    msgs = []
    for group, required in spec.items():
        have = set(seen.get(group, []))
        if group != "top" and not have:
            continue        # empty list (e.g. a student with no factors) — not drift
        missing = sorted(required - have)
        if missing:
            where = "response" if group == "top" else group
            msgs.append(f"{name}: {where} is missing {', '.join(missing)}")
    return msgs


def diff_watch(old: Optional[dict], new: dict) -> list[str]:
    """Compare two watch snapshots ({bundles, flags, keys{endpoint:{group:[]}}})
    → change messages. ``old`` None = first run (baseline)."""
    if not old:
        return ["baseline recorded (first Learner360 check)"]
    msgs = []
    if sorted(old.get("bundles") or []) != sorted(new.get("bundles") or []) \
            and (new.get("bundles") or []):
        msgs.append("🆕 Learner360 has released a new version since the last "
                    f"check ({old.get('checked_at', 'earlier')}) — worth a look "
                    "for new features (EA, scheduled texting, …).")
    of, nf = old.get("flags") or {}, new.get("flags") or {}
    added = sorted(set(nf) - set(of))
    removed = sorted(set(of) - set(nf))
    changed = sorted(k for k in set(of) & set(nf) if of[k] != nf[k])
    if added:
        msgs.append(f"feature flags added: {', '.join(added[:15])}"
                    + (" …" if len(added) > 15 else ""))
    if removed:
        msgs.append(f"feature flags removed: {', '.join(removed[:15])}"
                    + (" …" if len(removed) > 15 else ""))
    for k in changed[:15]:
        msgs.append(f"feature flag '{k}' changed: {of[k]} → {nf[k]}")
    ok, nk = old.get("keys") or {}, new.get("keys") or {}
    for ep in sorted(set(ok) | set(nk)):
        for group in sorted(set(ok.get(ep, {})) | set(nk.get(ep, {}))):
            a, b = set(ok.get(ep, {}).get(group, [])), set(nk.get(ep, {}).get(group, []))
            if not a or not b:
                continue
            if b - a:
                msgs.append(f"{ep} {group}: new field(s) {', '.join(sorted(b - a))}")
            if a - b:
                msgs.append(f"{ep} {group}: field(s) gone {', '.join(sorted(a - b))}")
    return msgs


# ------------------------------------------------------- browser helpers
# Release fingerprint (the app's own JS bundle names change per deploy), the
# LaunchDarkly feature flags (simplified to name → JSON value), and the user's
# WGU id. Read-only; skips analytics keys.
SNAPSHOT_JS = """() => {
  const flags = {};
  let best = 0;
  for (const k of Object.keys(localStorage)) {
    if (!k.startsWith('LaunchDarkly_') || /_freshness$|_ContextIndex$/.test(k)) continue;
    try {
      const o = JSON.parse(localStorage.getItem(k));
      if (!o || typeof o !== 'object') continue;
      const names = Object.keys(o).filter(n => !n.startsWith('$'));
      if (names.length <= best) continue;
      best = names.length;
      for (const n of Object.keys(flags)) delete flags[n];
      for (const n of names) {
        const v = o[n];
        const val = (v && typeof v === 'object' && 'value' in v) ? v.value : v;
        flags[n] = JSON.stringify(val === undefined ? null : val).slice(0, 200);
      }
    } catch (e) {}
  }
  const bundles = [...document.scripts].map(s => s.src)
    .filter(s => s && s.startsWith(location.origin))
    .map(s => s.split('/').pop().split('?')[0]).sort();
  return {wguId: localStorage.getItem('wgu-id') || '', bundles, flags};
}"""

_API_JS = """async ([url, headers, method, body]) => {
  // The gateway answers 415 (Unsupported Media Type) to a request WITHOUT a
  // JSON Content-Type — even a GET — so always send it.
  const opts = {method, headers: Object.assign(
    {'Accept': 'application/json', 'Content-Type': 'application/json'}, headers)};
  if (body !== null) opts.body = JSON.stringify(body);
  try { const r = await fetch(url, opts); let t = ''; try { t = await r.text(); } catch (e) {}
        return {status: r.status, body: t}; }
  catch (e) { return {status: 'fetch-error', body: String(e)}; }
}"""

_MOMI_BATCH_JS = """async ([base, headers, ids, conc]) => {
  const out = {}; let i = 0;
  const h = Object.assign(
    {'Accept': 'application/json', 'Content-Type': 'application/json'}, headers);
  const run = async () => {
    while (i < ids.length) {
      const id = ids[i++];
      try { const r = await fetch(base + id, {headers: h}); out[id] = {status: r.status, body: await r.text()}; }
      catch (e) { out[id] = {status: 'fetch-error', body: String(e)}; }
    }
  };
  await Promise.all(Array.from({length: conc}, run));
  return out;
}"""


def api(page, headers: dict, method: str, path: str, body=None) -> dict:
    """One gateway call from inside the Learner360 tab (CORS origin satisfied).
    Returns {status, text, json (or None)}."""
    r = page.evaluate(_API_JS, [GATEWAY + path, headers, method, body]) or {}
    text = r.get("body") or ""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    return {"status": r.get("status"), "text": text, "json": data}


def fetch_momi_batch(page, headers: dict, wgu_ids: list, concurrency: int = 5) -> dict:
    """GET momentum-indicator for many learners, ``concurrency`` at a time,
    in-page. → {wgu_id: {status, body}}."""
    return page.evaluate(_MOMI_BATCH_JS,
                         [GATEWAY + MOMI_PATH, headers, list(wgu_ids),
                          max(1, int(concurrency))]) or {}
