# Schedule Optimization Software - Backlog

## Active Items

### Priority 1: Recommendation Logic Refinement
- **Status:** In Progress
- **Description:** Current recommendation thresholds (85% add, 1.2 idle cut) need tuning based on real data
- **Notes:** 
  - Need to validate 85% utilization threshold with Plantation management
  - Consider business context: 85% might be too aggressive for customer experience
  - Factor in seasonal variations

### Priority 2: Data Validation & Edge Cases
- **Status:** TODO
- **Description:** Add input validation for malformed Zenoti exports
- **Examples:**
  - Missing therapist names
  - Overlapping appointments (same therapist double-booked)
  - Block-outs that exceed shift boundaries
  - Appointments marked "Massages" but zero duration

### Priority 3: Historical Trend Analysis
- **Status:** Idea
- **Description:** Compare week-over-week or month-over-month patterns
- **Value:** Identify seasonal trends, not just single-period snapshots

### Priority 4: Cost Modeling
- **Status:** Idea
- **Description:** Attach dollar values to idle time recommendations
- **Inputs:** Hourly therapist wage, overhead costs
- **Output:** "Cutting 2 hours Tuesday 2-4pm saves $X/week"

### Priority 5: Export Automation
- **Status:** Future
- **Description:** Integrate with Zenoti API instead of manual file uploads
- **Blockers:** Need Zenoti API access (if available)

## Completed
- ✅ Basic heatmap visualization
- ✅ Supply/demand/utilization calculations
- ✅ Block-out type filtering
- ✅ PDF export functionality

## Notes
- Created: 2026-02-11
- Last updated: 2026-02-11
