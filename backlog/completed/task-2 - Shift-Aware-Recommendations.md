---
id: TASK-2
title: Shift-Aware Recommendations
status: Done
assignee: []
created_date: '2026-02-12 16:17'
updated_date: '2026-02-12 17:21'
labels:
  - enhancement
  - recommendations
dependencies:
  - TASK-1
references:
  - 'app.py:378-407 (build_recommendations)'
  - 'app.py:519-541 (tab4 UI)'
priority: medium
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Replace slot-level (30-min) recommendations with shift-aware recommendations that suggest realistic shift additions/cuts for **massage therapists only**, using average shift duration to size recommendations.

**Key principle:** Use average shift duration (~6 hrs) ONLY to determine recommended shift length. Place the shift window based on demand/utilization data (where the need is), NOT based on historical start/end time patterns.

**Data confirms (681 shifts, all Massage Therapists):**
- Mean: 6.06 hrs → round to 6-hr standard shift
- 5-hr shifts: 33.0%, 6-hr shifts: 48.5% (combined 81.5%)

**Approach:**
1. Compute average shift duration from attendance data → use as standard shift length (~6 hrs)
2. For ADD: find contiguous windows of high-utilization slots spanning ~6 hrs → recommend "Add 1 massage therapist Tue 10 AM - 4 PM (6-hr shift)" where the time window comes from demand data
3. For CUT: find contiguous windows of overstaffing spanning ~6 hrs → recommend "Cut 1 massage therapist Wed 11 AM - 5 PM (6-hr shift)"
4. Show estimated impact (e.g., utilization change)

**Scope:** Massage therapists only. Modify `build_recommendations()` and recommendations UI in app.py. Keep existing heatmap tabs untouched.
<!-- SECTION:DESCRIPTION:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Replaced slot-level (30-min) recommendations with shift-aware recommendations in `app.py`.

**Changes:**
- Added `compute_avg_shift_duration()` - calculates average shift length from attendance data
- Added `find_shift_windows()` - finds contiguous qualifying slot runs and groups them into shift-sized windows
- Rewrote `build_recommendations()` - produces shift-level ADD/CUT recommendations with time windows sized to average shift duration (~6 hrs), estimated utilization impact, and priority levels
- Added "Shift Recommendations" tab to the UI with supply/demand calculation, showing add-staff and cut-staff tables
- Existing heatmap tabs untouched
<!-- SECTION:FINAL_SUMMARY:END -->
