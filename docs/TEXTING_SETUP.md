# Texting setup (Mongoose)

Texting sends through **Mongoose** (`sms.mongooseresearch.com`), using the
per‑course shared inboxes. This doc explains what works out of the box, and the
one **optional** setup step that makes opt‑in reliable.

## TL;DR

- **Texting works with no extra setup.** Each student is reached by their
  **Salesforce Contact id** (pulled straight from the caseload — no mobile number
  needed), and whether to text them is decided by the Salesforce **opt‑in field**.
- The one thing that field can't do reliably is tell you who is *actually* opted
  in inside Mongoose — the two can disagree. The **optional** Mongoose *segment*
  export fixes that. Recommended, not required.

## How a text gets addressed and gated

For each student the tool needs two things:

1. **Who to send to (addressing).** The tool uses the student's **Salesforce
   Contact id** (e.g. `003…`), which comes from Salesforce with the caseload
   (the `getCaseLoadMainGridData` feed) — authoritative and complete. Mongoose is
   searched by that id, so a student is textable **even if their caseload mobile
   is blank**. (If an id somehow isn't available — e.g. a CSV‑only load — it falls
   back to the mobile number.)
2. **Whether to send (opt‑in).** By default this is the Salesforce
   **TextingPreference** field ("Opted In"). Students marked opted‑in are texted;
   others are skipped.

That's the whole baseline — no Mongoose configuration needed.

## Why the optional segment export helps

The Salesforce opt‑in field and the real Mongoose subscription list **can
disagree**:

- **False "opted in":** Salesforce says opted‑in, but the student isn't actually
  a subscribed contact in Mongoose → texting their Contact id just finds nobody
  (a silent miss).
- **False "not opted in":** the Salesforce field is stale/unchecked while the
  student is actually reachable.

A **Mongoose contacts segment** per course is the *authoritative* "who is really
opted in" list. When it's present, the tool trusts it over the Salesforce field
and marks Salesforce‑only students as **"unverified"** in the text review, so you
can see exactly which sends are on the trusted list vs. the SF field.

**So: the segment is an opt‑in reliability upgrade, not a requirement.**

## One‑time setup (per course/department)

Do this once for each department you text. In **Mongoose**:

1. Switch to that department (top‑left department selector).
2. **Tools → Segments → New Segment.**
3. Add a filter: **Contact ID → is not empty.**
   (Required — this is what makes the list complete: every student who has a
   Contact id is included.)
4. Name the segment **exactly** as the app shows it (copy it verbatim — the
   auto‑export matches the name exactly).
5. **Save.**

The app shows you the exact name(s) to create: run **🔄 Sync Contact IDs**; if any
department has no matching segment, a dialog lists the exact names and these
steps, then re‑checks.

### Prerequisites to confirm before relying on it org‑wide

- **Permissions:** the instructor can create segments in their Mongoose
  department(s).
- **Contact ID field populated:** the department's Mongoose contacts have the
  Salesforce **Contact ID** field synced — the segment filters on it, so if it's
  empty the segment comes back empty.

## Keeping it fresh

Opt‑in changes often, so an export goes stale fast:

- The app **auto‑refreshes** a stale export at startup (when one exists and
  Mongoose is signed in) and **prompts** before a texting fire if it's stale.
- You can refresh manually any time with **🔄 Sync Contact IDs**.
- A stale or missing‑segment state is surfaced in the log; texting still proceeds
  on the Salesforce field in the meantime.

## Quick reference

| Situation | Texting still works? | Notes |
|---|---|---|
| No Mongoose segment set up | ✅ Yes | Addressed by Salesforce Contact id; opt‑in from the SF field (flagged "unverified"). |
| Segment set up + fresh | ✅ Yes | Verified opt‑in from Mongoose; SF‑only students flagged. |
| Segment set up but stale | ✅ Yes | Auto‑refreshed at startup / prompted before a text. |
| Blank caseload mobile | ✅ Yes | Reached by Contact id — mobile not needed. |
| No Contact id available (rare — e.g. CSV‑only load) | ⚠️ Falls back to mobile | A blank‑mobile student here is skipped; set up the segment / reload the grid. |
