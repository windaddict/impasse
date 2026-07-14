# Changelog

All notable changes to Impasse are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) for its schemas and skill.

## [Unreleased]

### Added
- Schemas: `reviewer-response.v1.json` and `reconciliation-result.v1.json` — the
  reviewer emits observations with anchored evidence; reconciliation records the
  per-finding disposition and escalated deadlocks. Domain-general via an evidence
  *anchor* union (`file_range | text_quote | section | source | record | generic`).
