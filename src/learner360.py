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


_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

TEAMS_ASSIGNED = "/learner360/v1/teams/assigned"
TEAM_MEMBERS = "/learner360/v1/teams/{team}/members"


def team_caseload_query(page: int, owner_ids: list, value_key: str = "values",
                        size: int = 100) -> dict:
    """caseloads/search body filtered to several caseload owners — what the
    UI's Team filter sends (``caseloadOwner IN [...]``). The key holding the
    id list is probed (``values`` vs ``value``) by the dry run."""
    q = caseload_query(page, size)
    q["filters"] = [{"field": "caseloadOwner", "operator": "IN",
                     value_key: list(owner_ids)}]
    return q


_UUID_KEYS = ("wguId", "userWguId", "memberWguId", "id", "uuid", "teamId",
              "userId")


def ids_from(items, prefer=_UUID_KEYS) -> list:
    """UUID-looking ids from a list of dicts (first matching key per item)."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        for k in prefer:
            v = it.get(k)
            if isinstance(v, str) and _UUID_RE.fullmatch(v.strip()):
                out.append(v.strip())
                break
    return out


def json_shape(obj, depth: int = 2) -> object:
    """Structure-only summary (keys / list lengths / scalar TYPES, never
    values) for FERPA-safe discovery logs."""
    if depth < 0:
        return type(obj).__name__
    if isinstance(obj, dict):
        return {k: json_shape(v, depth - 1) for k, v in list(obj.items())[:25]}
    if isinstance(obj, list):
        return [f"list[{len(obj)}]"] + ([json_shape(obj[0], depth - 1)]
                                         if obj else [])
    return type(obj).__name__


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


def ms_until_due(last_captured: Optional[str], days: int, cap_ms: int,
                 now: Optional[datetime] = None) -> int:
    """Milliseconds until a not-yet-due background job comes due (+1 min),
    capped at ``cap_ms`` and at least 1 min — so a scheduler re-checks right
    when the job is due instead of up to ``cap_ms`` later."""
    try:
        last = datetime.fromisoformat(str(last_captured)[:19])
    except (TypeError, ValueError):
        return cap_ms
    secs = days * 86400 - ((now or datetime.now()) - last).total_seconds()
    return int(max(60_000, min(cap_ms, secs * 1000 + 60_000)))


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
        created_src, created = _pick(n, _NOTE_FIELDS["created"])
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
            "modified_at": iso_utc(n.get("lastModifiedDate")),
            "created_src": created_src,
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
      // Notes responses are large and the gateway 503s under load: retry
      // overload statuses with backoff (2s, 5s, 10s) before giving up.
      for (let a = 0; a < 4; a++) {
        try { const r = await fetch(base + id, {headers: h});
              out[id] = {status: r.status, body: await r.text()};
              if (![429, 502, 503, 504].includes(r.status)) break; }
        catch (e) { out[id] = {status: 'fetch-error', body: String(e)}; }
        if (a < 3) await new Promise(res => setTimeout(res, [2000, 5000, 10000][a]));
      }
    }
  };
  await Promise.all(Array.from({length: conc}, run));
  return out;
}"""


def fetch_notes_batch(page, headers: dict, wgu_ids: list,
                      concurrency: int = 3) -> dict:
    """GET studentNotes for many learners, in-page. → {wgu_id: {status, body}}."""
    return page.evaluate(_GET_BATCH_JS,
                         [GATEWAY + NOTES_PATH, headers, list(wgu_ids),
                          max(1, int(concurrency))]) or {}


# ---------------------------------------------------------- academic activity
# GET /learner360/v1/academic-activity/<learner uuid>?startDate=…&endDate=…
# → dated portal / course activity events. The accepted date format isn't
# documented; ACTIVITY_DATE_FORMATS are tried in order on the first learner
# and the first one the gateway accepts is used for the run.
ACTIVITY_PATH = "/learner360/v1/academic-activity/"
ACTIVITY_DATE_FORMATS = ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y")

_ACT_FIELDS = {
    "date": ("date", "activityDate", "eventDate", "createdDate", "timestamp"),
    "category": ("category", "activityCategory"),
    "type": ("typeDescription", "type", "activityType"),
    "context": ("contextDescription", "context", "description"),
    "course": ("courseCode", "course"),
}


def activity_query(start, end, fmt: str) -> str:
    """'?startDate=…&endDate=…' for datetimes ``start``/``end``."""
    return f"?startDate={start.strftime(fmt)}&endDate={end.strftime(fmt)}"


def activity_list(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("content", "activities", "items", "data", "results"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def parse_activity(resp) -> list:
    """Normalize one learner's activity → [{date (YYYY-MM-DD), at (full ISO
    UTC or ''), category, type, context, course}]."""
    out = []
    for a in activity_list(resp):
        if not isinstance(a, dict):
            continue
        raw = _pick(a, _ACT_FIELDS["date"])[1]
        at = iso_utc(raw)
        day = at[:10] if at else norm_date(str(raw or ""))[:10]
        out.append({
            "date": day, "at": at,
            "category": str(_pick(a, _ACT_FIELDS["category"])[1] or ""),
            "type": str(_pick(a, _ACT_FIELDS["type"])[1] or ""),
            "context": str(_pick(a, _ACT_FIELDS["context"])[1] or ""),
            "course": str(_pick(a, _ACT_FIELDS["course"])[1] or ""),
        })
    return out


def activity_shape(resps: list) -> dict:
    """FERPA-safe summary for the dry-run log: field names (+ counts), which
    candidate each role used, masked date patterns, category / type LABELS
    (catalogue names, not student content), and the date span seen."""
    from collections import Counter
    keys, picked, dates, cats, types = (Counter() for _ in range(5))
    wrappers, days = Counter(), []
    for resp in resps:
        wrappers["list" if isinstance(resp, list) else
                 ("dict:" + ",".join(sorted(resp)[:6]) if isinstance(resp, dict)
                  else type(resp).__name__)] += 1
        for a in activity_list(resp):
            if not isinstance(a, dict):
                continue
            keys.update(a.keys())
            for role, names in _ACT_FIELDS.items():
                name, v = _pick(a, names)
                if name:
                    picked[f"{role}<-{name}"] += 1
                    if role == "date":
                        dates[_mask(v)] += 1
            cats[str(_pick(a, _ACT_FIELDS["category"])[1] or "")] += 1
            types[str(_pick(a, _ACT_FIELDS["type"])[1] or "")] += 1
        days += [x["date"] for x in parse_activity(resp) if x["date"]]
    return {"wrappers": dict(wrappers), "keys": dict(keys.most_common()),
            "picked": dict(picked.most_common()),
            "date_patterns": dict(dates.most_common(3)),
            "categories": dict(cats.most_common(25)),
            "types": dict(types.most_common(30)),
            "span": (min(days), max(days)) if days else None}


_GET_SUFFIX_BATCH_JS = """async ([base, suffix, headers, ids, conc, retry, tries]) => {
  const out = {}; let i = 0;
  const rs = retry || [429, 502, 503, 504]; const nt = tries || 4;
  const h = Object.assign(
    {'Accept': 'application/json', 'Content-Type': 'application/json'}, headers);
  const run = async () => {
    while (i < ids.length) {
      const id = ids[i++];
      for (let a = 0; a < nt; a++) {
        try { const r = await fetch(base + id + suffix, {headers: h});
              out[id] = {status: r.status, body: await r.text()};
              if (!rs.includes(r.status)) break; }
        catch (e) { out[id] = {status: 'fetch-error', body: String(e)}; }
        if (a < nt - 1) await new Promise(res => setTimeout(res, [2000, 5000, 10000][a]));
      }
    }
  };
  await Promise.all(Array.from({length: conc}, run));
  return out;
}"""


def fetch_activity_batch(page, headers: dict, wgu_ids: list, suffix: str,
                         concurrency: int = 3) -> dict:
    """GET academic-activity (with ``suffix`` query) for many learners,
    in-page, retrying overload statuses. → {wgu_id: {status, body}}."""
    return page.evaluate(_GET_SUFFIX_BATCH_JS,
                         [GATEWAY + ACTIVITY_PATH, suffix, headers,
                          list(wgu_ids), max(1, int(concurrency))]) or {}


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


# ---------------------------------------------------------------------------
# Endpoint DISCOVERY (`l360discover:`): while on, the browser worker records
# every gateway response Learner360's own pages fetch as you click around —
# method, a path TEMPLATE (ids → {id}), query-parameter NAMES, status and the
# JSON SHAPE (field names + types, never values). Finds data we don't collect
# yet (degree plan, summaries, enrollments, ...) without guessing URLs. FERPA:
# no values are kept, and id-like object keys are collapsed to {id}.
# ---------------------------------------------------------------------------
_ID_SEG = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F-]{20,}|[0-9a-fA-F]{24,}|\d{3,}"
    r"|[A-Za-z0-9]{15}|[A-Za-z0-9]{18}|\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})$")
# A path segment right after one of these is a person's id / username
# ("students/hbaig5", "student/jim.ashe", "mentors/x") → {id}.
_PERSON_SEG_BEFORE = {"students", "student", "mentors", "mentor", "username",
                      "users", "user", "learners", "learner"}
_ID_KEY = re.compile(r"^(?:[0-9a-fA-F-]{16,}|\d{3,}|[A-Z]\d{3}[A-Z0-9]*|.*@.*)$")


def path_template(url: str) -> tuple:
    """(METHOD-less path with id-like segments → '{id}', sorted query keys)."""
    from urllib.parse import parse_qs, urlsplit
    sp = urlsplit(url or "")
    # Non-gateway WGU hosts keep their host so feeds from different sites
    # stay distinct ("//cos.wgu.edu/api/...").
    host = "" if sp.netloc in ("", GATEWAY_HOST, "x") else "//" + sp.netloc
    # Any segment carrying a run of 5+ digits (ids, timestamps, composite
    # note keys like "2026-10-28T12:31:18.588+0000813722205") → {id}.
    raw = sp.path.split("/")
    segs = []
    for i, seg in enumerate(raw):
        prev = raw[i - 1].lower() if i else ""
        idish = (_ID_SEG.match(seg) or re.search(r"\d{5}", seg)
                 or (seg and prev in _PERSON_SEG_BEFORE
                     and seg.lower() not in ("profile", "assigned", "search",
                                             "notifications", "offerings",
                                             "settings")))
        segs.append("{id}" if idish else seg)
    return host + "/".join(segs), sorted(parse_qs(sp.query,
                                                  keep_blank_values=True))


def disc_shape(obj, depth: int = 0, max_depth: int = 7):
    """Structure of a JSON value with no values: dict → {key: shape} (id-like
    keys collapsed to '{id}'), list → [merged item shape] (+ '#n' length
    hint), scalars → 'str' / 'num' / 'bool' / 'null'."""
    if depth > max_depth:
        return "…"
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            key = "{id}" if _ID_KEY.match(str(k)) else str(k)
            sh = disc_shape(v, depth + 1, max_depth)
            out[key] = _merge_shape(out[key], sh) if key in out else sh
        return out
    if isinstance(obj, list):
        merged = None
        for item in obj[:25]:
            sh = disc_shape(item, depth + 1, max_depth)
            merged = sh if merged is None else _merge_shape(merged, sh)
        return [merged if merged is not None else "empty"]
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "bool"
    if isinstance(obj, (int, float)):
        return "num"
    return "str"


def _merge_shape(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge_shape(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        if a == ["empty"]:
            return b
        if b == ["empty"]:
            return a
        return [_merge_shape(a[0], b[0])]
    if a == b or b == "null":
        return a
    if a == "null":
        return b
    return f"{a}|{b}" if isinstance(a, str) and isinstance(b, str) else a


def discovery_add(store: dict, method: str, url: str, status, body) -> None:
    """Fold one gateway response into ``store`` {"METHOD path": entry}."""
    path, qkeys = path_template(url)
    key = f"{(method or 'GET').upper()} {path}"
    e = store.setdefault(key, {"count": 0, "statuses": [], "query": [],
                               "shape": None})
    e["count"] += 1
    if status not in e["statuses"]:
        e["statuses"].append(status)
    e["query"] = sorted(set(e["query"]) | set(qkeys))
    if body is not None:
        sh = disc_shape(body)
        e["shape"] = sh if e["shape"] is None else _merge_shape(e["shape"], sh)


def discovery_known(key: str) -> bool:
    """Is this endpoint one CaseloadNotes already reads?"""
    return any(p.rstrip("/") in key for p in (
        CASELOAD_SEARCH, MOMI_PATH, TEAMS_ASSIGNED, "/learner360/v1/teams/",
        NOTES_PATH, ACTIVITY_PATH))


def discovery_summary(store: dict) -> list:
    """Log lines: new endpoints first, each with its top-level fields."""
    lines = []
    for key in sorted(store, key=lambda k: (discovery_known(k), k)):
        e = store[key]
        sh = e["shape"]
        top = sh[0] if isinstance(sh, list) and sh else sh
        fields = (", ".join(list(top)[:14]) + (" …" if len(top) > 14 else "")
                  if isinstance(top, dict) else str(top))
        tag = "known" if discovery_known(key) else "NEW"
        q = f" ?{'&'.join(e['query'])}" if e["query"] else ""
        lines.append(f"  [{tag}] {key}{q}  ×{e['count']} {e['statuses']}"
                     f"  → {fields}")
    return lines


def shape_paths(shape, prefix: str = "") -> set:
    """Dotted field paths of a disc_shape ('a.b[].c'), for new-field diffs."""
    out = set()
    if isinstance(shape, dict):
        for k, v in shape.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.add(p)
            out |= shape_paths(v, p)
    elif isinstance(shape, list) and shape:
        out |= shape_paths(shape[0], prefix + "[]")
    return out


def discovery_diff(store: dict, seen: dict) -> list:
    """What ``store`` has that the ``seen`` baseline ({key: [paths]}) lacks:
    [(key, None)] for a new endpoint, [(key, [new field paths])] for new
    fields on a known one."""
    out = []
    for key in sorted(store):
        paths = shape_paths(store[key].get("shape"))
        if key not in seen:
            out.append((key, None))
        else:
            new = sorted(paths - set(seen[key]))
            if new:
                out.append((key, new))
    return out


def discovery_merge_seen(seen: dict, store: dict) -> None:
    """Fold ``store``'s endpoints + field paths into the ``seen`` baseline."""
    for key, e in store.items():
        seen[key] = sorted(set(seen.get(key, [])) |
                           shape_paths(e.get("shape")))


def discovery_diff_lines(diff: list, store: dict) -> list:
    """Human log lines for discovery_diff output."""
    lines = []
    for key, new in diff:
        if new is None:
            one = discovery_summary({key: store[key]})[0].strip()
            lines.append(f"  🆕 new data feed: {one.replace('[NEW] ', '')}")
        else:
            more = f" (+{len(new) - 8} more)" if len(new) > 8 else ""
            lines.append(f"  🆕 new field(s) on {key}: "
                         f"{', '.join(new[:8])}{more}")
    return lines


# ---------------------------------------------------------------------------
# Degree plan + community of care (found by `l360discover:` 2026-11-03).
#   GET /learner360/v1/degree-plan/<id>   current term: courses (status,
#       planned/actual dates, CUs, incomplete), weeksRemaining, planned vs
#       completed CUs
#   GET /learner360/v2/degree-plan/<id>   whole plan: completed/current/future
#       terms + metadata (percentComplete, remainingCUs, ...)
#   GET /relationships/v1/learners/<id>/community-of-care
#       programMentor {name, mentorEmail}, instructors [{name, courseCode}]
# Which learner id the path takes (Learner360 uuid or PIDM) is probed on the
# first learner of a run. Only plan/progress fields are kept — no contact
# details beyond the program mentor's (staff) name + email.
# ---------------------------------------------------------------------------
PLAN_V1_PATH = "/learner360/v1/degree-plan/"
PLAN_V2_PATH = "/learner360/v2/degree-plan/"
COC_PATH = "/relationships/v1/learners/"
COC_SUFFIX = "/community-of-care"

# Course status words that mean "done" (case-insensitive substring match).
_DONE_WORDS = ("pass", "complete", "graded", "transfer", "waive", "earned")


def course_done(status: str) -> bool:
    s = str(status or "").lower()
    if any(w in s for w in ("incomplete", "not ", "fail", "in progress")):
        return False
    return any(w in s for w in _DONE_WORDS)


def fetch_get_batch(page, headers: dict, base_path: str, ids: list,
                    suffix: str = "", concurrency: int = 3,
                    retry_500: bool = False) -> dict:
    """GET GATEWAY+base_path+<id>+suffix for many ids in-page (overload
    retries). ``retry_500`` also retries a 500 once (2 s) — for an endpoint
    that intermittently 500s. → {id: {status, body}}."""
    extra = ([[429, 500, 502, 503, 504], 2] if retry_500 else [None, None])
    return page.evaluate(_GET_SUFFIX_BATCH_JS,
                         [GATEWAY + base_path, suffix, headers,
                          [str(i) for i in ids], max(1, int(concurrency))]
                         + extra) or {}


def parse_plan(v1, v2, coc) -> dict:
    """Compact per-student plan summary from the three responses (any may be
    None). Dates are YYYY-MM-DD; missing values are ''/None."""
    v1 = v1 if isinstance(v1, dict) else {}
    v2 = v2 if isinstance(v2, dict) else {}
    coc = coc if isinstance(coc, dict) else {}
    courses = []
    for c in v1.get("termCourses") or []:
        if not isinstance(c, dict):
            continue
        courses.append({
            "code": str(c.get("code") or ""),
            "name": str(c.get("name") or ""),
            "cus": _num(c.get("cus")),
            "status": str(c.get("status") or ""),
            "planned_start": norm_date(c.get("plannedStartDate")),
            "planned_end": norm_date(c.get("plannedEndDate")),
            "start": norm_date(c.get("actualStartDate")),
            "end": norm_date(c.get("actualEndDate")),
            "incomplete": bool(c.get("incomplete")),
        })
    future = []
    for t in v2.get("futureTerms") or []:
        for c in (t or {}).get("courses") or []:
            if isinstance(c, dict) and c.get("courseCode"):
                future.append(str(c["courseCode"]))
    md = v2.get("metadata") if isinstance(v2.get("metadata"), dict) else {}
    pm = coc.get("programMentor") if isinstance(coc.get("programMentor"),
                                                dict) else {}
    return {
        "term_code": str(v1.get("termCode") or ""),
        "term_start": norm_date(v1.get("termStartDate")),
        "term_end": norm_date(v1.get("termEndDate")),
        "term_number": _num(v1.get("currentTermNumber")),
        "total_terms": _num(v1.get("totalNumberOfTerms")),
        "weeks_remaining": _num(v1.get("weeksRemaining")),
        "term_planned_cus": _num(v1.get("termPlannedCus")),
        "term_completed_cus": _num(v1.get("termCompletedCus")),
        "courses": courses,
        "future_courses": future,
        "percent_complete": _num(md.get("percentComplete")),
        "remaining_cus": _num(md.get("remainingCUs")),
        "total_cus": _num(md.get("totalCUs")),
        "pm_name": str(pm.get("name") or ""),
        "pm_email": str(pm.get("mentorEmail") or ""),
        "instructors": [
            {"course": str(i.get("courseCode") or ""),
             "name": str(i.get("name") or "")}
            for i in (coc.get("instructors") or []) if isinstance(i, dict)],
    }


def plan_view(plan: dict, my_course: str) -> dict:
    """What the viewer shows for one caseload row (student + MY course):
    courses left this term (not done, incl. mine), whether mine is the last
    one, other courses left, next planned course, CUs left, degree %."""
    mine = str(my_course or "").strip().upper()
    left = [c["code"] for c in plan.get("courses") or []
            if c.get("code") and not course_done(c.get("status"))]
    others = [c for c in left if c.upper() != mine]
    in_term = any(c.upper() == mine for c in left)
    # Next: a not-done term course planned to start after mine, else the
    # first course of the next planned term.
    mine_c = next((c for c in plan.get("courses") or []
                   if c.get("code", "").upper() == mine), None)
    nxt = ""
    if mine_c:
        after = sorted((c for c in plan.get("courses") or []
                        if c.get("code", "").upper() != mine
                        and not course_done(c.get("status"))
                        and (c.get("planned_start") or "")
                        > (mine_c.get("planned_start") or "")),
                       key=lambda c: c.get("planned_start") or "")
        nxt = after[0]["code"] if after else ""
    if not nxt and plan.get("future_courses"):
        nxt = plan["future_courses"][0]
    pc, cc = plan.get("term_planned_cus"), plan.get("term_completed_cus")
    return {
        "courses_left": ", ".join(left),
        "other_courses_left": ", ".join(others),
        "last_course": bool(in_term and not others),
        "next_course": nxt,
        "term_cus_left": (pc - cc) if pc is not None and cc is not None
        else None,
        "weeks_left": plan.get("weeks_remaining"),
        "degree_pct": plan.get("percent_complete"),
        "pm_name": plan.get("pm_name") or "",
        "pm_email": plan.get("pm_email") or "",
    }


def plan_vocab(plans: list) -> dict:
    """Value LABELS seen across plans (course statuses) — for a dry run."""
    from collections import Counter
    st = Counter()
    for p in plans:
        for c in p.get("courses") or []:
            st[c.get("status") or "(blank)"] += 1
    return {"course_status": dict(st.most_common())}
