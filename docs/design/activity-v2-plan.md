# Activity page v2 — "Chicago Radio Activity"

**Status:** planning only. No code written. Branch `activity-v2` off `77a547e`.
**Reference mock:** [`activity-v2-reference.png`](activity-v2-reference.png) (supplied by Eric, 2026-09-29)

The current Activity page (shipped 2026-09-28, PRs #11/#12) is a single 780px
column: counts line, health line, and a flat list of repeater rows. The mock is a
full dashboard — masthead, hero, stat cards, side-by-side service tables, public
service cards, and a map.

This document records what the mock asks for, what our data can actually back,
and what has to change elsewhere before the rest can be built honestly.

---

## 1. Mock inventory

1. **Masthead bar** — skyline logo + "Chicago Offline | RF Feed", tagline
   "REAL RADIO ACTIVITY. A STRONGER CHICAGO." Nav: Live · Repeaters ·
   Public Service · Map · Observers · About. Right-aligned green pill:
   "Chicago RF Network • Live / 4 observers online".
2. **Hero** — Chicago skyline photo background, h1 "Chicago Radio Activity",
   sub "What's on the air around Chicago right now." plus a one-line
   description. Right-side kicker "COMMUNITY + PREPAREDNESS + PUBLIC SERVICE /
   PEOPLE · RADIOS · A MORE RESILIENT CHICAGO".
3. **Three stat cards** — AMATEUR RADIO (HAM) green "18 active", GMRS blue
   "7 active", PUBLIC SERVICE amber "12 systems heard". Each: "in last 24
   hours", a bar sparkline, and a "↑ +3 vs previous day" delta.
4. **"Repeaters on the air"** — filter pills [Active now | Today | All
   repeaters]. Two side-by-side tables, HAM REPEATERS and GMRS REPEATERS.
   Columns: Callsign/Name (two-line), Frequency with offset sign ("146.940 -"),
   Area, Last heard (colored dot + relative time), 24h activity (bar chart).
   Footer link "View all ham repeaters →".
5. **"Major Public Service"** — horizontal cards with agency seals: Chicago
   Police, Chicago Fire/EMS, CTA, Illinois State Police, Chicago-area Interop.
   Each card: activity phrase, dot + relative time, mini bars, and a band line
   ("Citywide (P25) · 700/800 MHz"). Caption clarifies this is RF activity, not
   decoded audio.
6. **"Chicago Area Map"** — dark basemap with colored dots, plus a legend panel
   "What does this mean?" (green = heard in the last hour, amber = heard today,
   grey = not recently heard).
7. **"How we know" panel** — observers online, receiver types, latest
   observation age, and a "View observer network →" button.

---

## 2. Backed by data we already have

These need only UI work against existing accessors in `ssrfobs/ui.py` /
`ssrfobs/web.py`:

- **Masthead, hero, nav chrome** — pure presentation.
- **HAM vs GMRS split** — `REPEATER_SERVICES` is already `("amateur", "gmrs")`,
  so the two-table layout is a regroup of rows we already render.
- **Per-service active counts** — same freshness buckets the counts line uses now.
- **Last heard dot + relative time** — already rendered, just restyled into a cell.
- **24h activity bars** — `sparks()` already unions `observations` and
  `monitor_checks` and buckets per hour. Widening the window from 12 to 24
  buckets is a constant change.
- **"How we know" panel** — `stations()` and `receivers()` supply observer
  count, receiver roles, and the latest observation timestamp.
- **Filter pills** — Active now / Today / All are client-side filters over rows
  that are already in the DOM.
- **"↑ +3 vs previous day" deltas** — not computed today, but derivable: count
  distinct repeaters heard in the last 24h vs the prior 24h from the same two
  tables `sparks()` reads.
- **Offset sign in the frequency cell** — derivable from `rx_mhz` vs `tx_mhz`
  in ssrf-lite's `codeplug.json`; we just don't render it.

## 3. Not backed — do not fabricate

| Mock element | Blocker | Path to unblock |
|---|---|---|
| **Area** column ("Near North Side", "Elmhurst") | ssrf-lite has no locality field. `codeplug.json` carries callsign, rx/tx, ctcss, dcs, dcs_polarity, color_code, timeslots, lat, lon, service, mode, bandwidth_khz, name — no city or neighborhood. | `ssrf-lite-spec-change` to add `locality`. Until then read `ident.get("locality")` defensively and omit the column when absent. |
| **Major Public Service** section, and the PUBLIC SERVICE stat card | We monitor nothing on 700/800 MHz P25. There is no observation behind "12 systems heard" or any of the five agency cards. | Needs actual receiver coverage on 700/800. Until then: omit, or render an explicit "not yet monitored" state. **Open question for Eric.** |
| **Chicago Area Map** dots | `lat`/`lon` exist in the schema but were null on sampled records. Unknown real coverage. | Audit coordinate coverage across the monitored set before promising pins. **Open question for Eric.** |
| Skyline photo, agency seals | Image assets we do not have and may not be licensed to use. Agency seals in particular imply endorsement. | Eric to supply or approve substitutes. |

## 4. Known-bad live state at time of writing

Worth fixing before or alongside this work — a redesigned page full of zeros is
worse than the current one:

```
0 active recently · 0 heard today · 57 monitored
⚠ Monitoring degraded · 0/57 repeaters checked within 1 h
```

Every row reported "last checked 17h ago" and the newest hearing was the previous
morning. That is a stalled monitor ingest on the rf-survey side, not a UI bug.

## 5. Sequencing

1. Triage the stalled ingest (separate, rf-survey).
2. Decide the Public Service and Map question (omit vs. explicit empty state).
3. Build the section-3 items: chrome, hero, stat cards, HAM/GMRS tables,
   deltas, offset signs, "How we know" panel.
4. `ssrf-lite` `locality` spec change, then light up the Area column.
5. Public Service and Map only once there is coverage behind them.

## 6. Ground rule

Every number on this page must trace to an observation. Where we have no data we
say so plainly rather than rendering a plausible-looking placeholder. The v1 page
earned its credibility by distinguishing OBSERVED from VERIFIED; v2 must not
spend that on a nicer layout.
