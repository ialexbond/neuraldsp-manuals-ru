from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .canonical import unit_map


def classify_change(
    baseline: dict[str, Any], candidate: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    baseline_units = unit_map(baseline)
    candidate_units = unit_map(candidate)
    baseline_keys = set(baseline_units)
    candidate_keys = set(candidate_units)
    added = sorted(candidate_keys - baseline_keys)
    removed = sorted(baseline_keys - candidate_keys)
    common = sorted(baseline_keys & candidate_keys)
    changed = [
        key for key in common if baseline_units[key]["hash"] != candidate_units[key]["hash"]
    ]
    skeleton_changes = [
        key
        for key in changed
        if baseline_units[key]["skeleton"] != candidate_units[key]["skeleton"]
    ]
    attribute_shape_changes = []
    for key in changed:
        old_shape = {
            (row["path"], row["tag"], row["name"])
            for row in baseline_units[key]["attributes"]
        }
        new_shape = {
            (row["path"], row["tag"], row["name"])
            for row in candidate_units[key]["attributes"]
        }
        if old_shape != new_shape:
            attribute_shape_changes.append(key)
    changed_ratio = len(changed) / max(1, len(baseline_units))

    reasons: list[str] = []
    if candidate.get("chapter_count") != config["expected_chapter_count"]:
        reasons.append(
            f"chapter count changed from {config['expected_chapter_count']} "
            f"to {candidate.get('chapter_count')}"
        )
    if candidate.get("section_count", 0) < config["minimum_section_count"]:
        reasons.append(
            f"only {candidate.get('section_count', 0)} stable sections were found; "
            f"at least {config['minimum_section_count']} are required"
        )
    if added:
        reasons.append(f"stable units were added: {', '.join(added[:8])}")
    if removed:
        reasons.append(f"stable units were removed: {', '.join(removed[:8])}")
    if skeleton_changes:
        reasons.append(
            "semantic layout changed in: " + ", ".join(skeleton_changes[:8])
        )
    if attribute_shape_changes:
        reasons.append(
            "semantic attributes were added or removed in: "
            + ", ".join(attribute_shape_changes[:8])
        )
    if len(changed) > config["maximum_changed_unit_count"]:
        reasons.append(
            f"{len(changed)} units changed; the automatic limit is "
            f"{config['maximum_changed_unit_count']}"
        )
    if changed_ratio > config["maximum_changed_unit_ratio"]:
        reasons.append(
            f"{changed_ratio:.1%} of units changed; the automatic limit is "
            f"{config['maximum_changed_unit_ratio']:.1%}"
        )

    metadata_changed = baseline.get("upstream_version") != candidate.get("upstream_version")
    source_url_changed = baseline.get("source_url") != candidate.get("source_url")
    has_changes = bool(
        changed or metadata_changed or source_url_changed or added or removed
    )
    if not has_changes:
        status = "unchanged"
    elif config.get("source_kind") == "pdf" or config.get("monitor_only"):
        reasons.append(
            "the source uses monitor-only mode and requires a manually reviewed translation"
        )
        status = "review_required"
    elif reasons:
        status = "blocked"
    else:
        status = "safe_change"

    return {
        "schema_version": 1,
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source_url": candidate.get("source_url"),
        "source_url_changed": source_url_changed,
        "baseline_version": baseline.get("upstream_version"),
        "upstream_version": candidate.get("upstream_version"),
        "baseline_hash": baseline.get("content_hash"),
        "candidate_hash": candidate.get("content_hash"),
        "baseline_integrity_hash": baseline.get("integrity_hash"),
        "candidate_integrity_hash": candidate.get("integrity_hash"),
        "changed_units": changed,
        "added_units": added,
        "removed_units": removed,
        "skeleton_changes": skeleton_changes,
        "attribute_shape_changes": attribute_shape_changes,
        "changed_ratio": changed_ratio,
        "reasons": reasons,
    }
