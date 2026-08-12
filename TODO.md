# TODO.md

Task tracking for the Codeberg/Forgejo to GitHub Migration Tool.

---

## 🛠️ Phase 1: Core Reliability & Simple Resumption
- [ ] **State Persistence & Resume System**
  - [ ] Implement `state.json` tracking to record migrated Codeberg issue IDs and corresponding GitHub issue numbers.
  - [ ] Implement atomic file writes (`os.replace`) so state isn't corrupted on `Ctrl+C` or unexpected exits.
  - [ ] Add simple resumption logic to skip issues already present in `state.json`.
- [ ] **Basic Rate Limit Safety**
  - [ ] Add `time.sleep()` delays between POST operations to respect GitHub's secondary rate limits.
  - [ ] Inspect HTTP `403` / `429` responses and pause execution if limits are encountered.
- [ ] **Dry-Run Mode**
  - [ ] Add a `--dry-run` flag to print formatted issue payloads without hitting external APIs.

---

## 📝 Phase 2: Content & Formatting (Keep It Simple)
- [ ] **Header Attribution**
  - [ ] Format issue bodies and comments with blockquotes detailing original Codeberg username and creation date.
- [ ] **Label Transfer**
  - [ ] Fetch original issue labels and attach them during issue creation on GitHub.
- [ ] **Comment Threading**
  - [ ] Fetch comments sequentially per issue and post them to GitHub before closing closed issues.

---

## 💻 Phase 3: Post-Validation & Polish (Optional for Sharing)
- [ ] **CLI Argument Parsing**
  - [ ] Use `argparse` for flags (`--source`, `--target`, `--resume`, `--dry-run`).
- [ ] **Summary Log**
  - [ ] Output a simple terminal count of total issues migrated, skipped, or failed.
