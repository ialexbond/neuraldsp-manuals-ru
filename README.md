# Neural DSP manuals in Russian

[![Quad Cortex manual check](https://github.com/ialexbond/neuraldsp-manuals-ru/actions/workflows/check-quad-cortex.yml/badge.svg?branch=main)](https://github.com/ialexbond/neuraldsp-manuals-ru/actions/workflows/check-quad-cortex.yml)

Carefully formatted Russian editions of official Neural DSP manuals. The manual directory intentionally publishes only the current PDF for each product; source snapshots, translation memory, images, and build files are kept outside that directory.

## Current manuals

<!-- MANUAL_STATUS:START -->
| Product | Upstream manual | Russian edition | Status | Download |
| --- | --- | --- | --- | --- |
| Quad Cortex | 4.0.0 | 2026-07-23 | Current at the initial automation baseline | [PDF](manuals/quad-cortex/Quad_Cortex_User_Manual_RU_v4.0.0_rev2026-07-23.pdf) |
<!-- MANUAL_STATUS:END -->

The badge above shows the result of the latest **manually requested GitHub backup check**. It is not a monthly-status indicator. The regular update history belongs to the local Codex automation. An open issue with the [`update-detected`](https://github.com/ialexbond/neuraldsp-manuals-ru/issues?q=is%3Aissue+is%3Aopen+label%3Aupdate-detected) label means either that an update was detected or that the automation needs attention. Read the issue before deciding whether the PDF is outdated.

## File and release versioning

- Exactly one current PDF is kept in `manuals/quad-cortex/`.
- Its name is `Quad_Cortex_User_Manual_RU_v<official-version>_rev<YYYY-MM-DD>.pdf`.
- A corrected Russian edition removes the prior filename and replaces it with the new dated revision, never creating a second current copy.
- Every published revision also has a GitHub Release tagged `quad-cortex-v<official-version>-ru.<YYYY-MM-DD>`. Release tags preserve the Russian revision history without cluttering the manual directory.

## Update policy

The local Codex automation is the **only automatic scheduler and source-change detector**. On the 23rd day of every month it checks the official [Quad Cortex manual](https://neuraldsp.com/manual/quad-cortex#Global-Features), compares stable chapter and section identifiers after presentation-only markup is removed, and records the result in its Codex task.

When nothing changed, no branch, commit, pull request, or release is created. For a safe text-only change, the same Codex automation translates only the changed material using the user's existing Codex plan, rebuilds and validates the complete PDF, and prepares an `update/quad-cortex/YYYY-MM-DD` branch with a **draft** pull request. It does not use `OPENAI_API_KEY` or any separately billed translation API. Human review and an explicit merge are mandatory before publication. Section additions, removals, layout changes, unexpectedly large updates, or failed PDF checks stop publication for human review.

`.github/workflows/check-quad-cortex.yml` has no schedule. It can be started explicitly from GitHub Actions as a diagnostic backup, but it neither translates nor publishes an update. Its run history only describes manual backup checks; it is not the authoritative monthly log.

The current single-repository design stores durable automation state as a dedicated prerelease asset. That ZIP is outside `manuals/quad-cortex/`, contains no credentials, and is publicly downloadable because this repository is public. Public manual releases contain the PDF; the state prerelease exists only to support future comparisons and rebuilds.

Implementation, recovery, and first-run instructions are documented in [docs/AUTOMATION.md](docs/AUTOMATION.md).
