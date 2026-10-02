"""Provenance snapshot format version — bump when the snapshot layout
(§3 of docs/classification-provenance-spec.md) changes, so a later AI-judge job
can correlate a decision with the snapshot shape that produced it, the same way
TaskActionEvent correlates on METADATA_FORMAT_VERSION."""

PROVENANCE_FORMAT_VERSION = "v1"
