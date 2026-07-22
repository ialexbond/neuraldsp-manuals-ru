# Quad Cortex localization automation

This document describes the update process for the Russian Quad Cortex manual. The publication rule is deliberately strict: `manuals/quad-cortex/` contains exactly one current PDF and nothing else.

## Ownership and schedule

The local Codex automation is the **only automatic scheduler and source-change detector**. It runs on the 23rd day of every month at 10:00 in the maintainer's local time zone and owns the complete routine update cycle:

1. fetch the official manual;
2. compare it with the accepted source snapshot;
3. translate only confirmed changed material;
4. rebuild and validate the complete PDF;
5. prepare a dated branch and draft pull request for human review.

The scheduled task is managed in the Codex app, not in a GitHub workflow. Its execution history and result are recorded in the corresponding Codex task. If the computer or Codex task is unavailable at the scheduled time, the maintainer starts that automation manually when the environment is available again; the next comparison still uses full canonical hashes, so no source change is lost.

Translation uses the maintainer's existing Codex plan. The project does **not** use `OPENAI_API_KEY`, `OPENAI_MODEL`, or any separately billed translation API. No paid API credential is required in GitHub or in the repository files.

## Manual GitHub backup check

`.github/workflows/check-quad-cortex.yml` is a diagnostic backup only. It has exactly one trigger: `workflow_dispatch`, which means a maintainer must start it explicitly from **Actions → Check Quad Cortex manual → Run workflow**.

The manual workflow downloads the durable state, fetches the official page, compares canonical hashes, and reports whether the source is unchanged, safely changed, structurally changed, or unavailable. It does not translate text, render or publish a replacement PDF, create an update branch, or open an update pull request. It has no `schedule` or cron trigger and is not a watchdog for the Codex automation.

No repository secret is needed for this backup check. It uses read-only repository access and writes its result only to the GitHub Actions run summary. A manual workflow run may help diagnose a problem, but it never replaces the monthly Codex task.

## Codex end-to-end flow

1. Download `quad-cortex-state-v1.zip` from the prerelease tagged `automation-state-quad-cortex-v1`.
2. Download the official [Quad Cortex manual](https://neuraldsp.com/manual/quad-cortex#Global-Features).
3. Extract the 12 chapter introductions and the main `div[id]` sections beneath them.
4. Canonicalize the content. Styled-component class names, formatting whitespace, tracking query parameters, and image resizing parameters are ignored. Text, semantic element order, links, image sources, image alternative text, list structure, and table structure remain significant.
5. Compare each stable unit hash with the saved source snapshot.
6. Finish without repository changes when every hash is unchanged.
7. Stop for human review when a stable unit was added or removed, a semantic tree changed, the chapter or section count is implausible, or the update exceeds the configured size limit.
8. For a safe change, translate only changed text nodes through the local Codex task. Each changed section is considered with its title, previous English text, and previous Russian text so terminology stays consistent. Reject a translation if it loses numbers, protected product or protocol names, IDs, or the exact inline-markup structure.
9. Apply translated fragments to the saved localized HTML, preserve unchanged Russian text byte-for-byte, download changed image assets into the external state, and update the edition date.
10. Render with the same pinned Node Playwright 1.61.1 toolchain used for the accepted edition, derive every table-of-contents destination from the preview PDF's internal links, fill the visible page numbers, and render the tagged final PDF with bookmarks.
11. Validate A4 geometry, page count, visible content on every page, chapter openers, image loading, text bounds and overlaps, links, bookmarks, PDF tags, searchable Cyrillic text, visible table-of-contents page numbers, and exact embedded IBM Plex Sans metrics.
12. Create or refresh `update/quad-cortex/YYYY-MM-DD`, replace the sole PDF, update the README status row, and open a draft pull request.
13. Preserve the candidate state until the pull request is reviewed and merged.
14. After a maintainer merges the pull request, `.github/workflows/release-quad-cortex.yml` verifies that the candidate archive belongs to the merged PDF, promotes that exact archive to the durable state release, and creates the public PDF release.

The update process never pushes directly to `main`. Human review and an explicit merge are mandatory before publication.

## Change classifications

### Unchanged

No stable unit hash or upstream version changed. The Codex task records the successful comparison and exits. README is intentionally not committed merely to advance a check date.

### Safe change

The same stable units and semantic element trees exist, and the number of changed units remains within `.github/manuals/quad-cortex.json` limits. The local Codex task translates only changed text, rebuilds the whole manual, runs all safeguards, and prepares a dated draft pull request.

### Blocked change

Publication stops when the site structure, stable section set, or content volume changes beyond safe limits. The existing PDF and durable state remain untouched. Codex reports the reason to the maintainer. The manual GitHub backup check can independently confirm the comparison result but cannot translate or publish the update.

Changing a threshold merely to force a structurally different document through is not a valid fix. Update the extractor or print template locally, rebuild and inspect the complete PDF, and create a new state schema when necessary.

## Why state is stored as a release asset

The next build needs more than the public PDF: it needs the canonical English snapshot, localized HTML, translation alignment, fonts, and images. Committing those files would make the public repository noisy and would violate the one-PDF manual-directory rule.

The durable archive is therefore an asset on a dedicated prerelease:

- tag: `automation-state-quad-cortex-v1`
- asset: `quad-cortex-state-v1.zip`
- schema: `state.json`, `web/document.html`, and `assets/`

The release asset is mutable but the schema version is explicit. A candidate is not promoted until the matching PDF pull request is merged. This prevents an unsuccessful or rejected localization from becoming the comparison baseline.

The state archive contains no API keys or other credentials. It is publicly downloadable because the repository is public, so it must not contain confidential material.

## Repository secrets and settings

No user-provided repository secret is required for source checking or translation. In particular:

- do not add `OPENAI_API_KEY`;
- do not add `OPENAI_MODEL`;
- do not configure another paid translation service.

GitHub workflows use the built-in `GITHUB_TOKEN` where repository access is required. In **Settings → Actions → General → Workflow permissions**, allow the permissions required by the diagnostic and release workflows. Repository branch protection must require a pull request for `main`; an explicit human merge is the mandatory final publication gate.

Recommended branch protection:

- require pull requests before merging;
- require the `Repository validation` check from `.github/workflows/validate-pull-request.yml`;
- disallow force pushes to `main`;
- keep branch deletion after merge enabled for `update/quad-cortex/*` branches.

## First bootstrap

Bootstrap is a one-time local operation because the completed localized HTML and its assets intentionally do not live in the repository.

1. Make sure the current PDF is at `manuals/quad-cortex/Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf` and the matching completed build files are available locally.
2. Install the pinned Python dependencies:

   ```powershell
   $env:PYTHONUTF8='1'
   python -m pip install -r .github/requirements.txt
   ```

3. Create the archive from the completed edition:

   ```powershell
   $env:PYTHONUTF8='1'
   python .github/scripts/manual_sync.py bootstrap `
     --config .github/manuals/quad-cortex.json `
     --localized-html "..\..\work\web\manual_ru.html" `
     --source-template-html "..\..\work\web\localized_template.html" `
     --asset-directory "..\..\work\assets" `
     --baseline-pdf "manuals\quad-cortex\Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf" `
     --output ".automation\quad-cortex-state-v1.zip" `
     --result ".automation\bootstrap-result.json"
   ```

   Bootstrap refuses to continue unless every source unit has an equivalent semantic tree in the localized HTML.

4. Inspect `bootstrap-result.json`. It must report 12 chapters, at least 60 main sections, successful alignment, and plausible PDF metrics.
5. Upload the durable state asset after the initial repository commit exists:

   ```powershell
   $env:PYTHONUTF8='1'
   gh release create automation-state-quad-cortex-v1 `
     ".automation\quad-cortex-state-v1.zip" `
     --target main `
     --prerelease `
     --title "Quad Cortex automation state v1" `
     --notes "Public service state used by the local Codex automation and manual diagnostic workflow. It contains no secrets."
   ```

6. Run a manual GitHub backup check or the local comparison command below. A baseline check must finish as `unchanged` and must not create a branch. No API key setup is part of bootstrap.

If Neural DSP does not expose a reliable modification date, the baseline fetch time becomes the initial known source date. Later source-change dates are the dates on which Codex first detects a new canonical hash; they are not presented as the publisher's exact edit time.

## PDF and repository safeguards

Before an update branch is pushed, the local Codex automation verifies:

- the renderer is the pinned Node Playwright 1.61.1 toolchain used by the accepted build;
- all images loaded before printing;
- every page is A4, contains visible content, and renders successfully;
- every chapter starts on a new page with its title and substantive content together;
- page count remains within the configured tolerance of the previous edition;
- no text block leaves the page or materially overlaps another text block;
- the PDF remains tagged and every bookmark resolves;
- only the four expected IBM Plex Sans faces are embedded, with exact advance widths and global metrics;
- every visible table-of-contents page number equals its actual link destination;
- `manuals/quad-cortex/` contains one file, it is a PDF, and its name matches the official version;
- repository publication changes are limited to the intended PDF and status metadata.

Any failure leaves `main`, the durable state asset, and public releases unchanged.

## Pull requests, releases, and naming

- Working branch: `update/quad-cortex/YYYY-MM-DD`
- Published file: `Quad_Cortex_User_Manual_RU_v<official-version>_rev<YYYY-MM-DD>.pdf`
- Public release tag: `quad-cortex-v<official-version>-ru.<YYYY-MM-DD>`

The official version and Russian edition date are both kept in the filename. Every update starts as a draft pull request. The `Repository validation` check runs the unit suite and enforces the one-PDF publication policy. A maintainer must inspect the translated changes and rendered PDF, mark the pull request ready, and explicitly merge it. Passing automation is necessary but never substitutes for human review.

## Recovery and failure handling

### The monthly Codex task did not run

Open the local Codex automation and start it manually. Do not add a GitHub cron as a workaround. Optionally run the manual GitHub backup check first to confirm whether the source changed.

### The manual GitHub backup reports a change

Do not attempt to translate inside GitHub Actions. Start the local Codex automation and let it perform the complete comparison, translation, rebuild, and review workflow.

### The state release or asset is missing

Recreate the archive from the last verified localized build or restore a previously downloaded release asset, then upload it to the expected state release. The current PDF remains untouched.

### The Codex update was interrupted

Resume or restart the same Codex task. It must compare against the last accepted state and rerun full PDF validation before proposing a branch. No external API key is needed.

### PDF validation failed

Inspect the changed sections and every page affected by pagination. Structural or layout repairs require a human-reviewed template update and a fresh state archive. Never publish a PDF that only passed a partial page check.

### PDF merged but state promotion failed

Recover the state candidate that was built with the merged PDF, validate their matching identities and hashes, upload it to `automation-state-quad-cortex-v1`, and rerun the release workflow. Do not substitute a state archive from another build.

## Local test and diagnostic commands

Run unit tests without contacting GitHub or a paid API:

```powershell
$env:PYTHONUTF8='1'
python -m unittest discover -s .github/tests -v
```

Create a live source snapshot for diagnosis:

```powershell
$env:PYTHONUTF8='1'
python .github/scripts/manual_sync.py snapshot `
  --config .github/manuals/quad-cortex.json `
  --output .automation/live-snapshot.json
```

Compare a downloaded state archive without translating or rendering:

```powershell
$env:PYTHONUTF8='1'
python .github/scripts/manual_sync.py check `
  --config .github/manuals/quad-cortex.json `
  --state-archive .automation/quad-cortex-state-v1.zip `
  --work-directory .automation/check `
  --snapshot-output .automation/candidate.json `
  --report .automation/report.json
```
