# HANSET venue twin — representative questions

The twin exists to answer these questions with evidence. Each answer is built only from open
`fact` / `source_claim` values; any `unverified` slot makes the criterion "unknown" (never "yes").

| # | Question | Entities · attributes needed | Time rule | Verification rule |
|---|----------|------------------------------|-----------|-------------------|
| Q1 | Which saunas have a **rapid-cold plunge** and let **foreigners** in on a **day pass today**? | facility[rapid_cold_plunge].exists · usage_condition[foreigner].policy · usage_condition[access].policy · price_plan[day_pass/drop_in].amount · hours[closure].closure_rule | facts open at query time; closure rule evaluated for the date | every criterion needs a cited value; guests-only/members access = no; unknown access is not assumed open |
| Q2 | What does a **day pass** cost at branch X on a weekday vs weekend, adult vs child? | price_plan(ticket, scope, audience, duration).amount KRW | price valid on the date (effective_from ≤ d < effective_to) | CSV/directory prices are `source_claim`; ambiguous multi-price matches go to review, never a value |
| Q3 | Is branch X **open now / closed on date D** (regular hours, monthly closure, temporary closure)? | hours[regular, scope].open_hours · hours[closure].closure_rule | same-source change closes the previous period | maintenance windows extracted as hours are flagged by quality check |
| Q4 | Can I enter with a **tattoo**? (allowed / cover_required / not_allowed / conditional) | usage_condition[tattoo].policy | open value | absence of a rule ≠ allowed; stays unverified |
| Q5 | What are the **sauna and cold-plunge temperatures**, and which baths exist (dry/wet sauna, warm, hot, cold, rapid-cold, layout)? | facility[sauna|dry_sauna|wet_sauna].temperature_c / exists · facility[cold_plunge].temperature_c · facility[warm_bath|hot_bath|cold_plunge|ice_bath].exists · facility[layout].bath_list | open value; changes keep history | out-of-range temperatures flagged; no "typical" defaults |
| Q6 | What is the **minimum age** / who may enter (guests only, members, reservation)? | usage_condition[age].min_age · usage_condition[access].policy | open value | from explicit text only |
| Q7 | What **changed** at branch X since date D (price, hours, closure, facilities)? | corrections · facts with effective_to | correction history | each change links old → new fact |

Run Q1: `python3 -m hanset_twin ask --date YYYY-MM-DD`. Summary: `python3 -m hanset_twin report`.
