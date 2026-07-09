# Contributing to caseload-tool

A complete, beginner-friendly walkthrough for contributing to
**[`ashejim/caseload-tool`](https://github.com/ashejim/caseload-tool)**. It
assumes you have never used Git before.

> **Note:** This project is **proprietary / all-rights-reserved** (the source is
> public, but it is not open-licensed). Please check with the maintainer before
> starting large changes.

---

## 0. One-time setup

**a) Make a GitHub account** — go to [github.com](https://github.com) and sign up (free).

**b) Install Git**
- **Windows:** download from [git-scm.com/download/win](https://git-scm.com/download/win)
  and run the installer (accept the defaults). This gives you **Git Bash**, a
  terminal you'll type commands into.
- **Mac:** `git` is usually already there; if not, install from
  [git-scm.com/download/mac](https://git-scm.com/download/mac).

**c) Tell Git who you are** (open Git Bash and run, using *your* name/email):
```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

**d) (Recommended) Install the GitHub CLI** — [cli.github.com](https://cli.github.com).
It handles login and pull requests without wrestling with passwords:
```bash
gh auth login
```
Choose **GitHub.com → HTTPS → login with a browser**, and follow the prompts.
(If you skip this, GitHub will ask for a Personal Access Token instead of a
password the first time you push — the CLI is much easier.)

---

## 1. Fork the project (make your own copy on GitHub)

A **fork** is your personal copy of the repo, living under your account.

1. Go to **https://github.com/ashejim/caseload-tool**
2. Click **Fork** (top-right) → **Create fork**.
3. You now have `https://github.com/YOUR-USERNAME/caseload-tool`.

---

## 2. Clone your fork (download it to your computer)

In Git Bash, go to where you keep projects, then clone **your fork** (replace
`YOUR-USERNAME`):
```bash
cd ~/Documents          # or wherever you want it
git clone https://github.com/YOUR-USERNAME/caseload-tool.git
cd caseload-tool
```

## 3. Link back to the original ("upstream")

This lets you pull in updates the maintainer makes later:
```bash
git remote add upstream https://github.com/ashejim/caseload-tool.git
```
Check it worked — you should see `origin` (your fork) and `upstream` (the
original):
```bash
git remote -v
```

---

## 4. Make a branch for your change

**Never work directly on `main`.** Create a branch named for what you're doing:
```bash
git checkout main
git pull upstream main          # start from the latest code
git checkout -b fix-typo-in-readme
```
(`git checkout -b NAME` creates and switches to a new branch. Use a short,
descriptive name like `add-dark-mode` or `fix-note-crash`.)

## 5. Make your changes

Edit files in your normal editor. As you go, see what you've changed:
```bash
git status          # which files changed
git diff            # the exact line-by-line changes
```

## 6. Commit your changes (save a snapshot)

```bash
git add -A                              # stage all your changes
git commit -m "Fix typo in the README setup section"
```
Write the message in the present tense, describing *what* the change does. Make
several small commits rather than one giant one if it's a bigger change.

## 7. Push your branch to your fork

```bash
git push -u origin fix-typo-in-readme
```
(The first push of a branch needs `-u origin BRANCHNAME`; after that, just
`git push`.) If you didn't set up the `gh` CLI, GitHub will prompt for your
username and a **Personal Access Token** here — create one at
**GitHub → Settings → Developer settings → Personal access tokens**.

---

## 8. Open a Pull Request (propose your change)

A **Pull Request (PR)** asks the maintainer to merge your branch into their
project.

**Easiest — with the GitHub CLI:**
```bash
gh pr create --fill
```
It opens a PR from your branch to `ashejim/caseload-tool`'s `main`. Add `--web`
to finish in the browser.

**Or on the website:**
1. Go to your fork `https://github.com/YOUR-USERNAME/caseload-tool`.
2. You'll see a banner: **"Compare & pull request"** → click it. (Or go to the
   original repo's **Pull requests** tab → **New pull request** →
   **compare across forks**.)
3. Confirm the arrow points
   **`ashejim/caseload-tool : main` ← `YOUR-USERNAME/caseload-tool : your-branch`**.
4. Give it a clear **title** and a **description** (what changed and why). Click
   **Create pull request**.

## 9. Respond to review feedback

The maintainer may ask for changes. You don't open a new PR — just keep working
on the **same branch**:
```bash
# make edits...
git add -A
git commit -m "Address review: rename variable"
git push
```
The new commits appear on the existing PR automatically.

---

## 10. Keeping your fork up to date

Before starting each new piece of work, sync with the original so you're not
building on stale code:
```bash
git checkout main
git pull upstream main          # get the maintainer's latest
git push origin main            # update your fork too (optional)
```
Then branch again for the next change (step 4).

---

## Quick mental model

- **`upstream`** = the real project (read-only for you)
- **`origin`** = your fork (you push here)
- **branch** = one isolated change
- **PR** = the request to merge your branch into the real project

## The 6 commands you'll use 90% of the time

```bash
git status
git add -A
git commit -m "message"
git push
git checkout -b new-branch
git pull upstream main
```

---

## Project-specific notes

- It's a **Python** app; the main file is `scripts/launcher.py`. After changing
  it, sanity-check that it still compiles:
  ```bash
  python -m py_compile scripts/launcher.py
  ```
- **Don't commit local data / PII files** (caseload CSVs, `*_probe*.txt`,
  `history.db`, `settings.json`, exported segment CSVs) — they're gitignored for
  a reason. Run `git status` before committing to be sure nothing sensitive is
  staged.
- Keep changes focused: one topic per branch / PR makes review much easier.
