# Quad Cortex localization automation

This document describes the update pipeline for the Russian Quad Cortex manual. The publication rule is deliberately strict: `manuals/quad-cortex/` contains exactly one current PDF and nothing else.

## What runs and when

`.github/workflows/check-quad-cortex.yml` runs at 03:17 UTC on the 23rd day of every month. The non-round time avoids the busiest part of GitHub Actions' scheduled queue. A maintainer can run the same workflow at any time with **Actions → Check Quad Cortex manual → Run workflow**.

Manual dispatch supports a `dry_run` option. A dry run downloads the state, checks the official page, and writes the result to the workflow summary, but it does not call the translation API or create issues, branches, commits, pull requests, or releases.

The monthly check and the post-merge release use one shared concurrency group. Only one durable-state reader or writer can run at a time, so a new comparison cannot race with state promotion.

[GitHub can automatically disable scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) in a public repository after 60 days without repository activity. This repository intentionally avoids empty heartbeat commits, so GitHub cron is not sufficient by itself. A separate local Codex automation acts as the watchdog: every month it verifies that `check-quad-cortex.yml` is enabled, re-enables it if necessary, and triggers or confirms the monthly check. The maintainer can perform the same recovery with `gh workflow enable check-quad-cortex.yml` followed by a manual dry run.

## End-to-end flow

1. Download `quad-cortex-state-v1.zip` from the prerelease tagged `automation-state-quad-cortex-v1`.
2. Download the official [Quad Cortex manual](https://neuraldsp.com/manual/quad-cortex#Global-Features).
3. Extract the 12 chapter introductions and the main `div[id]` sections beneath them.
4. Canonicalize the content. Styled-component class names, formatting whitespace, tracking query parameters, and image resizing parameters are ignored. Text, semantic element order, links, image sources, image alternative text, list structure, and table structure remain significant.
5. Compare each stable unit hash with the saved source snapshot.
6. Finish without repository changes when every hash is unchanged.
7. Stop and create or update an `update-detected` issue when a stable unit was added or removed, a semantic tree changed, the chapter/section count is implausible, or the update exceeds the configured size limit.
8. For a safe change, send only changed text nodes to the OpenAI API. Each request includes the section title, previous English text, and previous Russian text so terminology remains consistent. The response is rejected if it loses numbers, protected product/protocol names, IDs, or the exact inline-markup structure.
9. Apply translated fragments to the saved localized HTML, preserve unchanged Russian text byte-for-byte, download changed image assets into the external state, and update the edition date.
10. Render with the same pinned Node Playwright 1.61.1 toolchain used for the accepted edition, derive every table-of-contents destination from the preview PDF's internal links, fill the visible page numbers, and render the tagged final PDF with bookmarks.
11. Validate A4 geometry, page count, blank pages, every page render, image loading, text bounds and overlaps, links, bookmarks, PDF tags, searchable Cyrillic text, visible TOC page numbers, and exact embedded IBM Plex Sans metrics.
12. Create or refresh `update/quad-cortex/YYYY-MM-DD`, replace the sole PDF, update the README status row, and open a pull request.
13. Keep the candidate state as a 90-day GitHub Actions artifact while the pull request is open.
14. After a maintainer merges the pull request, `.github/workflows/release-quad-cortex.yml` verifies that the candidate archive belongs to the merged PDF, promotes that exact archive to the durable state release, and creates the public PDF release.

The one-time bootstrap pull request is also recognized by the release workflow. After it is merged, the workflow validates the already uploaded baseline state against the initial PDF and creates the first public PDF release without replacing the baseline archive.

The workflow never pushes directly to `main`. Automated update pull requests are opened as drafts. Human review and an explicit merge are mandatory before publication.

## Why state is stored as a release asset

The next build needs more than the public PDF: it needs the canonical English snapshot, localized HTML, translation alignment, fonts, and images. Committing those files would make the public repository noisy and would violate the one-PDF manual-directory rule.

The durable archive is therefore an asset on a dedicated prerelease:

- tag: `automation-state-quad-cortex-v1`
- asset: `quad-cortex-state-v1.zip`
- schema: `state.json`, `web/document.html`, and `assets/`

The release asset is mutable but the schema version is explicit. A state candidate is not promoted until the matching PDF pull request is merged. This prevents an unsuccessful or rejected translation from becoming the comparison baseline.

The state archive contains no API keys or other credentials. In the current single-repository design it is a service-data prerelease asset outside `manuals/quad-cortex/`, but it is still publicly downloadable because the repository is public. It contains source templates, fonts, images, and translation alignment data; do not put confidential material in it. Moving the state to private storage later would allow the same public manual directory policy without exposing this service data.

## Repository secrets and settings

Required repository secret:

| Name | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Translates only source fragments whose canonical content changed. |

Optional repository variable:

| Name | Default | Purpose |
| --- | --- | --- |
| `OPENAI_MODEL` | `gpt-5-mini` | Selects an available OpenAI model without editing the workflow. |

The workflows use GitHub's built-in `GITHUB_TOKEN`. In **Settings → Actions → General → Workflow permissions**, allow read and write permissions. Repository branch protection must require a pull request for `main`; an explicit human merge is the mandatory final publication gate.

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

3. Create the archive from the completed edition. From the repository root, a workspace matching the original localization project can use:

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

   Bootstrap refuses to continue unless every source unit has an equivalent semantic tree in the localized HTML. This is important: future text-node replacement would be unsafe without that alignment.

4. Inspect `bootstrap-result.json`. It must report 12 chapters, at least 60 main sections, successful alignment, and plausible PDF metrics.
5. Upload the durable state asset after the initial repository commit exists:

   ```powershell
   $env:PYTHONUTF8='1'
   gh release create automation-state-quad-cortex-v1 `
     ".automation\quad-cortex-state-v1.zip" `
     --target main `
     --prerelease `
     --title "Quad Cortex automation state v1" `
     --notes "Publicly downloadable service state used by the monthly localization workflow. It contains no secrets."
   ```

6. Add `OPENAI_API_KEY`, optionally set `OPENAI_MODEL`, and run a manual dry check. A baseline check should finish as `unchanged` and must not create a branch.

If Neural DSP does not expose a reliable modification date, the baseline fetch time becomes the initial known source date. Later source-change dates are the dates on which this workflow first detects a new canonical hash; they are not presented as the publisher's exact edit time.

## Change classifications

### Unchanged

No unit hash or upstream version changed. The workflow writes a summary and exits. README is intentionally not committed merely to advance a check date; the workflow badge and Actions history are the authoritative check log.

### Safe change

The same stable units and semantic element trees exist, and the number of changed units remains within `.github/manuals/quad-cortex.json` limits. Only changed text and supported attributes are updated. A dated update branch, pull request, candidate state artifact, and `update-detected` issue are created. The issue tells readers that the PDF on `main` is temporarily outdated.

### Blocked change

Publication stops when the site structure, stable section set, or content volume changes beyond safe limits. The existing PDF and durable state remain untouched. The open `update-detected` issue links to the failed run and is the repository-wide stale-content signal.

Changing a threshold to force a structurally different document through is not a valid fix. Update the extractor or print template locally, rebuild and inspect the whole PDF, then create a new state schema if necessary.

## PDF and repository safeguards

Before a branch is pushed, the automation verifies:

- the renderer is the pinned Node Playwright 1.61.1 toolchain used by the accepted build;
- all images loaded before printing;
- the PDF has the configured minimum number of pages;
- page count remains within the configured percentage of the previous edition;
- every page contains content and renders successfully;
- every page is A4;
- no text block leaves the page or materially overlaps another text block;
- the PDF remains tagged and every bookmark resolves;
- only the four expected IBM Plex Sans faces are embedded, and every used glyph retains the source font's exact advance width and global metrics;
- the minimum expected internal-link count is present;
- no link has an invalid destination;
- every visible table-of-contents page number equals its actual link destination;
- stable source structure keeps the same bookmark and image-placement counts;
- `manuals/quad-cortex/` contains one file, it is a PDF, and its name matches the official version;
- the commit contains changes only to `README.md` and `manuals/quad-cortex/*.pdf`.

Any failure leaves `main`, the durable state asset, and public releases unchanged.

## Pull requests, releases, and naming

- Working branch: `update/quad-cortex/YYYY-MM-DD`
- Published file: `Quad_Cortex_User_Manual_RU_v<official-version>_rev<YYYY-MM-DD>.pdf`
- Public release tag: `quad-cortex-v<official-version>-ru.<YYYY-MM-DD>`

The official version and Russian edition date are both kept in the filename. If Neural DSP edits version 4.0.0 without changing its displayed version, the next accepted edition removes the prior dated filename, publishes one new dated PDF, and receives a new release tag.

The pull request body contains a hidden `state-run-id` marker. The merge workflow uses it to retrieve the exact state archive built with that PDF, then verifies the archive identity, edition date, version, PDF hash, and PDF metrics before promotion. Do not remove this marker while editing an automated pull request.

Every automated update starts as a draft. The `Repository validation` pull-request check runs the unit suite and enforces the one-PDF publication policy. A maintainer must inspect the translated changes and rendered PDF, mark the pull request ready, and explicitly merge it. Passing automation is necessary but never substitutes for human review.

## Recovery and failure handling

### The state release or asset is missing

The check creates an `update-detected` issue and fails before comparison. Recreate the archive from the last verified localized build or restore a previously downloaded release asset, then upload it to the expected state release.

### The candidate artifact expired before merge

Close the old pull request and rerun the monthly workflow. Do not merge a PDF whose candidate state can no longer be recovered; doing so would make the next comparison use the wrong baseline.

### Translation API failure

No branch is published. Check the secret, model variable, API availability, and failed run. Rerun after correcting the cause. Existing translations are not sent again unless their English fragments changed.

### PDF validation failure

Download the workflow logs and reproduce the update locally. Inspect the changed sections and all pages affected by pagination. Structural or layout repairs require a human-reviewed template update and a fresh state archive.

### PDF merged but state promotion failed

The release workflow opens an `update-detected` issue and does not claim a successful release. Download the state candidate from the source run while it is retained, upload it to `automation-state-quad-cortex-v1`, rerun repository validation, and create the public release manually if necessary.

### Scheduled workflow did not run exactly on the 23rd

GitHub schedules can be delayed. The run time is the check time. The next successful check compares full canonical hashes, so a delay does not lose changes. A maintainer can run a dry check manually at any time.

### Scheduled workflow was disabled after repository inactivity

Public-repository schedules may be disabled after 60 days without repository activity. The local Codex automation is the watchdog and should re-enable the workflow without creating a heartbeat commit. To recover manually:

```powershell
$env:PYTHONUTF8='1'
gh workflow enable check-quad-cortex.yml
gh workflow run check-quad-cortex.yml -f dry_run=true
```

## Local test and dry-check commands

Run unit tests without contacting GitHub or OpenAI:

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
