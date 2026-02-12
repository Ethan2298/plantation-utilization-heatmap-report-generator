---
id: TASK-3
title: 'Simplified Heatmaps: Appointments (Orange) + Idle Time (Red)'
status: Done
assignee: []
created_date: '2026-02-12 16:42'
updated_date: '2026-02-12 16:45'
labels:
  - heatmap
  - ui
dependencies: []
references:
  - app.py
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Replace the current 4-tab analysis with 2 focused heatmaps:

**Heatmap 1 — Appointments (Orange)**
- Shows cumulative massage appointment volume across all time slots (day × time)
- Source: Appointments report
- Filter: Only include services where the service category is "Massage" — exclude enhancements
- Color: Orange gradient

**Heatmap 2 — Idle Time (Red)**
- Formula: Massage Therapist Shift Hours − Blocked Out Time − Appointment Time = Idle Time
- Shows idle therapist time per slot (day × time)
- Color: Red gradient

Both heatmaps use the same day × 30-min-slot grid as the existing heatmaps.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Appointments heatmap only includes massage services (no enhancements) from the Appointments report
- [x] #2 Appointments heatmap uses an orange color gradient
- [x] #3 Idle time heatmap = shift hours - block out time - appointment time
- [x] #4 Idle time heatmap uses a red color gradient
- [x] #5 Both heatmaps render on the same day × 30-min time slot grid
- [x] #6 Existing parsing logic for attendance, block-outs, and appointments is reused
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Replaced the 4-tab analysis with 2 focused heatmaps:\n\n1. **Appointments Heatmap (Orange)** — `calculate_appointment_heatmap()` counts massage appointment-slots per day×time. Uses orange gradient (`#fff8f0` → `#e65100`). Only massage services (no enhancements) via existing `parse_appointments()` filter.\n\n2. **Idle Time Heatmap (Red)** — existing `calculate_idle_heatmap()` computes Shift - BlockOuts - Appointments = Idle. Uses red gradient (unchanged `#fef0ef` → `#67000d`).\n\nBoth render on the same 7-day × 30-min slot grid. Existing parsing logic fully reused.
<!-- SECTION:FINAL_SUMMARY:END -->
