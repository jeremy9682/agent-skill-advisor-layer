---
title: "HTTP 417 'module has no attribute X' means stale worker, not code bug"
date: "2026-07-06"
tags: ["bug", "ops"]
source_task: "the example ERP workbench API failures (recurred twice)"
applies_to: ["erp-service", "gunicorn deployments", "any long-lived Python worker"]
---

# HTTP 417 "module has no attribute X" means stale worker, not code bug

## Problem

Workbench API returned 417 with `module has no attribute <new_function>`
right after code that clearly defines the attribute was merged. Two separate
sessions burned time re-reading correct code looking for a nonexistent bug.

## Root Cause Or Decision

The gunicorn worker processes were started before the code change and never
reloaded. The running interpreter held the old module; the filesystem held
the new one. The error message points at code, but the defect is deployment
state.

## Fix Or Pattern

Redeploy the app service (e.g. `make deploy-local`) BEFORE debugging any
"attribute missing" error that appeared immediately after a merge. Rule of
thumb: if the attribute exists in the file on disk, suspect process staleness
first, code second.

## Verification

Both recurrences resolved by redeploy alone, zero code changes.

## Future Trigger

Any plan or debugging session that starts from "API says module has no
attribute X" or an unexplained 417 right after a deploy or merge.
