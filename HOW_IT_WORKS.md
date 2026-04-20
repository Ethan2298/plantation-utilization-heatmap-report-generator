# How It Works

A plain-English walkthrough of what this app does, **why each number in the report exists**, and how each one is calculated.

---

## 0. Why These Specific Numbers?

The report is trying to answer one question: **are we staffing the floor efficiently?** Every metric in the report exists to break that question into a piece you can act on.

| Metric                    | Question it answers                               | Why it's there                                                                                                                                                       |
| ------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Scheduled Hours**       | How much labor did we *pay for*?                  | The ceiling. You can't be more productive than the hours you put on the schedule.                                                                                    |
| **Block Time**            | How much of that labor was unavailable to guests? | Training, meetings, admin. Not bad — but not revenue-producing.                                                                                                      |
| **Net Available Hours**   | How much labor *could* have been booked?          | The real denominator. Comparing appointments to scheduled hours would unfairly penalize days with heavy training.                                                    |
| **Appointment Hours**     | How much labor actually *was* booked?             | The numerator. The thing the business gets paid for.                                                                                                                 |
| **Utilization %**         | What fraction of available time did we sell?      | The single headline number. Low = overstaffed or under-marketed. High = at capacity, possibly turning guests away.                                                   |
| **Hourly heatmap**        | *When* are we under- or over-utilized?            | A daily total hides the truth. A Tuesday at 82% could be 100% at 6 PM and 40% at 11 AM. The heatmap shows where to add or cut shifts.                                |
| **Weekday vs. weekend**   | Are the two halves of the week balanced?          | Weekends and weekdays usually need different staffing models. Bundling them hides both problems.                                                                     |
| **Member %**              | How much of our demand comes from members?        | Members have different economics (recurring revenue, higher lifetime value). Tracking this tells you whether membership growth is actually showing up in chair time. |
| **Week-over-week deltas** | Is the trend improving or getting worse?          | One week is noise. A delta tells you direction.                                                                                                                      |

Everything downstream in this doc is just *how* each of those is computed.

---

## 1. The Big Picture

You upload 3 (or 4) Zenoti exports. The app:

1. Parses them into tidy tables (Python / pandas).
2. Packages them as a single JSON blob.
3. Injects that blob into a self-contained HTML file with embedded CSS + JS.

The HTML file is the report. Open it in any browser — no server, no internet, no dependencies. All filtering, heatmap toggling, and recalculation happens client-side in JavaScript against the embedded JSON.

```
 Uploads ──► Python parse ──► JSON payload ──► HTML template ──► Report.html
  (CSV/XLS)     (app.py)      (build_data_payload)  (generate_html_report)
```

---

## 2. The Inputs

| File                      | What it tells us                                                             | Key columns used                                                   |
| ------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **Attendance**            | When each therapist was *scheduled* to work                                  | `Schedule Status`, `Date`, `Schedule` (e.g. `10:00 AM - 03:00 PM`) |
| **Appointments**          | When a therapist was *actually in a session*                                 | `Start Time`, `End Time`                                           |
| **Block Out Time**        | When a therapist was on the schedule but unavailable (break, training, etc.) | `Date`, `StartTime`, `EndTime`, `BlockOutTimeType`                 |
| **Membership** (optional) | Which guests are members, and when their membership was active               | `GuestCode`, `StartDate`, `EndDate`                                |

### Parsing gotchas the code handles

- Attendance uses `10:00 AM` (space). Block-outs use `10:00AM` (no space). Multiple formats are tried.
- If a block-out's end time is earlier than its start, we assume AM/PM wrapped and add 12 hours.
- Appointments with `start == end` are zero-duration enhancements (add-ons). They are filtered out — they'd skew utilization.
- Attendance rows are only kept when `Schedule Status == "Scheduled"`.

---

## 3. How Everything Is Stored Internally

Every time entry — scheduled shift, appointment, block-out — is reduced to four things:

- `date` (YYYY-MM-DD)
- `dayOfWeek` (0=Mon … 6=Sun)
- `startMinute` (minutes since midnight, e.g. 9:30 AM = 570)
- `endMinute` (minutes since midnight)

This uniform shape is what makes the math simple.

---

## 4. The One Core Calculation

Everything in the report — the scorecard, the heatmap, the daily table — is built from one primitive: **how many minutes of a time range fall inside a specific clock hour?**

```js
function minsInHour(startMin, endMin, hourStart) {
  return Math.max(0, Math.min(endMin, hourStart + 60) - Math.max(startMin, hourStart));
}
```

Translated: clip the range `[startMin, endMin]` to the hour bucket `[hourStart, hourStart+60]`, return the width. Zero if they don't overlap.

**Example.** A shift from 9:30 AM (570) to 11:15 AM (675), checked against the 10 AM hour (hourStart = 600):

```
min(675, 660) - max(570, 600) = 660 - 600 = 60 minutes
```

The 10 AM hour is fully covered. Checking the 11 AM hour (hourStart = 660):

```
min(675, 720) - max(570, 660) = 675 - 660 = 15 minutes
```

Every hour of every day gets three of these overlaps computed — one from the scheduled shifts, one from the appointments, one from the block-outs.

---

## 5. Building the Heatmap Grid

The operating window is **9 AM – 9 PM** (12 hours) × **7 days of week** = **84 cells**.

For every cell `(dow, hour)` we accumulate, across every record that falls in the selected period:

```
scheduled[dow][h]   += minsInHour(shift)        / 60
blocked[dow][h]     += minsInHour(blockout)     / 60
appointment[dow][h] += minsInHour(appointment)  / 60
```

All in hours.

From those three, the derived numbers in each cell:

```
netAvailable = scheduled - blocked
utilization  = appointment / netAvailable × 100%
```

`netAvailable` is the hours the therapist was on the floor and *not* in a break/meeting — i.e. the hours they could have been in a session. `utilization` is the share of that time they actually were.

If `netAvailable == 0`, utilization is `N/A` (no denominator).

---

## 6. The Scorecard (top-of-report totals)

For the selected period:

| Metric                | Formula                                                          |
| --------------------- | ---------------------------------------------------------------- |
| **Scheduled Hours**   | Sum of `scheduledHours` across all attendance rows in the period |
| **Block Time**        | Sum of `blocked[dow][h]` across every cell in the grid¹          |
| **Net Available**     | `Scheduled − Block Time`                                         |
| **Appointment Hours** | Sum of `appointment[dow][h]` across every cell                   |
| **Utilization**       | `Appointment Hours / Net Available × 100%`                       |

¹ Block time is intentionally summed *from the grid*, not directly from the block-out records. This clips it to the 9 AM – 9 PM operating window — block-outs before 9 or after 9 don't get counted against utilization, because the shifts wouldn't either.

The deltas (`▲ 2.3pp`, `▼ 0.5h`) compare the selected period to the one immediately before it.

---

## 7. The Daily Table

Same formulas as the scorecard, but rolled up per day-of-week instead of over the whole period:

```
for each dow 0..6:
    scheduled_day   = Σ scheduled[dow][h]   for h in 9..20
    blocked_day     = Σ blocked[dow][h]     for h in 9..20
    appointment_day = Σ appointment[dow][h] for h in 9..20
    netAvailable    = scheduled_day − blocked_day
    utilization     = appointment_day / netAvailable × 100%
```

The "trend" column for each day compares its utilization this period vs. the same day last period (so Monday vs. last Monday, not vs. Sunday).

---

## 8. Weekend vs. Weekday Card

Exactly the daily table logic, grouped differently:

- **Weekday** = Mon–Fri (dow 0–4)
- **Weekend** = Sat–Sun (dow 5–6)

All four numbers (scheduled, block, net available, appointment) are summed across the group; utilization is recomputed from those sums. Not an average-of-utilizations — that would weight a slow Sunday the same as a packed Saturday.

---

## 9. Member %

Only appears if a Membership file was uploaded.

During Python load, each appointment gets tagged `isMember = True` if the guest had an active membership on the appointment date:

```python
isMember = any(
    guest_code matches AND
    appointment_date >= membership.startDate AND
    appointment_date <= membership.endDate
)
```

Then:

```
member %  =  member appointment hours  /  total appointment hours  ×  100
```

---

## 10. Periods

The 4 periods in the report roll *backwards* from the latest date in the uploaded data.

- Each period is exactly 7 days.
- The most recent period ends on the last date seen in any file.
- The previous period ends 1 day before that, etc.

So if the last data date is 2026-04-19, the periods are:

- `2026-04-13 → 2026-04-19` (current)
- `2026-04-06 → 2026-04-12`
- `2026-03-30 → 2026-04-05`
- `2026-03-23 → 2026-03-29`

---

## 11. The Filter Bar

The filter bar lets the user exclude certain block-out types (e.g. `Training`, `Meeting`). When a block type is unchecked:

- Those block-out records are dropped from the `blocked` accumulator.
- `netAvailable = scheduled − blocked` goes up.
- `utilization` goes down.

Everything is recomputed in the browser in JavaScript — no reload.

---

## 12. Validation Scripts

Two scripts independently re-parse the source files and compare to the JSON embedded in the generated HTML:

- `scripts/quick_check.py` — ~2 seconds, sanity check on counts and totals.
- `scripts/validate_report.py` — 17 sections of adversarial checks: record counts, field accuracy, dayOfWeek correctness, period boundaries, recomputed utilization per cell, etc.

If either script passes, the report is arithmetically consistent with its inputs.

---

## 13. File Map

```
app.py                        Everything: Streamlit UI + parsers + HTML template + embedded JS
requirements.txt              streamlit, pandas, openpyxl, lxml
.streamlit/config.toml        Streamlit theming
scripts/quick_check.py        Fast validation
scripts/validate_report.py    Exhaustive validation
CLAUDE.md                     Short architecture note (also covers parsing quirks)
```

Key functions inside `app.py`:

| Function                                                                    | What it does                                                           |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `load_attendance`, `load_appointments`, `load_blockouts`, `load_membership` | Parse each file into a normalized DataFrame                            |
| `tag_members`                                                               | Adds `isMember` flag to appointments                                   |
| `build_data_payload`                                                        | Assembles the JSON blob (ATTENDANCE / APPOINTMENTS / BLOCKOUTS / META) |
| `generate_html_report`                                                      | Injects the payload into the HTML template                             |
| `minsInHour` (JS, in the template)                                          | The one primitive — minute overlap between a range and an hour bucket  |
| `computeMetrics` (JS)                                                       | Rolls `minsInHour` over every record into the heatmap grid + scorecard |
