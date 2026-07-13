# Design selection record (Phase 1.5 shadow schema)

This is a **manual, advisory, offline** record.  It is the output of
`scripts/design_shadow_select.py` and is not a prompt hook, runtime router,
embedding index, or plugin-cache enforcement mechanism.

## Input task contract

The selector accepts a YAML or JSON mapping with `task.deliverables`.  Each
deliverable is a separate visual decision:

```yaml
task:
  id: inventory-apple-cjk
  usage_claim: false # true only when a skill-use claim will be published
  evidence: []       # required to permit a published usage claim
  deliverables:
    - id: inventory-ui
      surface: product-ui
      language: cjk
      visual_direction: apple
      erp: true
```

Required per deliverable: `id`, `surface`.  `visual_direction` is required
when an author cannot be selected safely (for example, an unqualified
`deck`).  Valid surfaces are the catalog surfaces.  Recognised explicit
attributes are `language` (`cjk` or `latin`), `visual_direction` (`apple`,
`magazine`, `template`, `branded`), `erp`, `media_export`, and `deck_mode`.
The selector intentionally does not infer any of these from a natural-language
prompt.

## Output record

Each `records[]` item has these fields:

| Field | Meaning |
| --- | --- |
| `deliverable_id` | Stable identifier for exactly one deliverable. |
| `status` | `selected`, `needs_direction`, or `invalid`. |
| `visual_author` | Exactly one author when selected; never an overlay. |
| `baselines` | Constraints applied before style overlays.  Every entry declares `active_facets` and `suppressed_facets`. |
| `overlays` | Optional bounded overlays, also facet-scoped. |
| `gates` | Advisory review gates, not automatically executed. |
| `usage_claim` | Whether a public skill-use claim was requested and whether supplied evidence permits it. |
| `provenance` | Catalog/schema/version references for auditability. |

All statuses have the same record shape.  A `selected` record has one
non-null `visual_author` and may contain baselines, overlays, and gates.  An
`invalid` or `needs_direction` record has `visual_author: null`, empty
`baselines`/`overlays`/`gates`, and a non-empty `reason`; it still carries
`usage_claim` and `provenance.task_id` so a rejected decision is auditable.

### Facet precedence

`local/design-systems` may contribute `cjk-typography`, `cjk-spacing`, and
`erp-structure`.  A record must list only facets relevant to the deliverable;
inactive ownership is expressed in `suppressed_facets`, never silently merged.
For every selected baseline or overlay, `active_facets` plus
`suppressed_facets` must exhaust that skill's catalogued `owns` facets, with no
duplicates.  This makes an omitted facet an auditable decision rather than an
accidental authority leak.

For an Apple-inspired CJK UI the required selection is:

1. `frontend-design` is the only visual author.
2. `design-systems` activates only `cjk-typography` and `cjk-spacing`, and
   suppresses `erp-structure`.
3. `apple-design` is a bounded overlay for interaction/material details.
4. CJK typography wins over `apple-design`'s typography-micro rules where they
   conflict; specifically, CJK text must not receive negative letter-spacing.

For a Latin Apple-inspired UI there is no CJK baseline; the Apple overlay must
say so explicitly and may apply typography-micro without a false CJK
precedence claim.

### Usage evidence

Selection is not proof that a skill was used.  A public `usage_claim` is
permitted only if the input provides at least one evidence item with kind
`read`, `invocation`, or `artifact` **and** a non-empty path that exists on
disk.  Relative paths are resolved from the repository root for validation but
remain repository-relative in the record; absolute paths stay absolute.
Accepted kinds are deduplicated in first-seen order and retained in
`accepted_evidence`.  Otherwise the record remains a valid selection but marks
the claim `permitted: false` and explains the evidence gap.

### Boundary

The record deliberately does **not** choose a model, invoke a skill, mutate a
prompt, download a plugin, or create `DESIGN.md`.  A human or an explicitly
approved workflow consumes it later.
