# Visitor questions and evidence coverage

All questions concern internal answer capability; no answers are invented here.

## Q1. Which Seoul saunas open today accept tattoos and have a cold plunge at 15C or below, with a day-pass price?

Entities: Venue, Branch, Bath, PriceTicket, OpeningHours, Closure, AccessCondition.

Attributes: city; tattoo acceptance; bath kind/presence/temperature/unit; explicit day-pass price; schedule and dated closure.

Relations: Branch contains Bath; Branch offers ticket; access applies to Branch.

Time: today KST; closure overrides schedule; temperature observation/check date.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q2. Can a non-guest enter this hotel sauna as a drop-in?

Entities: Venue, Branch, AccessCondition, PriceTicket.

Attributes: public/non-guest access; reservation; ticket payer_class non-guest; guest distinction.

Relations: hotel Branch has access policy and non-guest ticket.

Time: policy validity and ticket period for visit date.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q3. Can I book with a foreign card and passport, without a Korean phone?

Entities: Venue, Branch, AccessCondition.

Attributes: foreign card acceptance; passport required; korean_phone_required; reservation method; foreigners policy.

Relations: booking/access conditions apply to the selected Branch.

Time: current policy; booking day and latest verification.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q4. Is it open today, and what is the last entry time?

Entities: Branch, OpeningHours, Closure.

Attributes: weekday hours; last_entry; holiday exception; maintenance closure.

Relations: schedule and closures apply to Branch.

Time: today KST; intersect intervals; overnight ambiguity requires confirmation.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q5. Which facilities are closed this month for maintenance or regular rest days?

Entities: Venue, Branch, Closure, Facility.

Attributes: recurring rule text/structured rule; dated closure; maintenance; affected facility.

Relations: Closure affects Branch or Facility.

Time: month boundaries KST; recurring dates plus temporary exceptions.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q6. How do weekday and weekend prices compare for the same ticket, guest versus non-guest, and when were they checked?

Entities: Branch, PriceTicket.

Attributes: amount; KRW; unit; ticket_type; payer_class; day_type; check date.

Relations: Branch offers precisely scoped tickets.

Time: price effective interval; weekday/weekend visit date; latest confirmation.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q7. Are there age or gender limits, and what attire is required?

Entities: Branch, AccessCondition, Space.

Attributes: minimum/maximum age; gender; attire; zone scope.

Relations: AccessCondition applies to Branch or Space.

Time: policy validity at visit date and checked date.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q8. Are there wet, dry and steam rooms, and what are their published temperatures?

Entities: Branch, Space, SaunaRoom.

Attributes: kind wet/dry/steam/other; presence; temperature; unit; availability.

Relations: Branch contains Space contains SaunaRoom.

Time: published measurement/check date; no real-time sensor implication.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q9. What is the cold plunge temperature, and is there a separate rapid-cold pool?

Entities: Branch, Space, Bath.

Attributes: kind cold/rapid_cold; temperature/unit; presence; availability.

Relations: Space contains distinct Bath identities; no inferred rapid-cold equivalence.

Time: published observation date; availability interval.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q10. Can one person use a private room?

Entities: Branch, Space, AccessCondition, PriceTicket.

Attributes: private room presence; capacity one; reservation; ticket scope.

Relations: private Space has occupancy access and scoped ticket.

Time: current capacity/booking policy and price period.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q11. Which HYROX or CrossFit boxes allow a drop-in, at what price and with what booking method?

Entities: Venue, Branch, AccessCondition, PriceTicket, Event.

Attributes: program type; public access; drop-in price/currency/unit; reservation; language.

Relations: Branch offers program and drop-in ticket; access applies to program.

Time: session/visit date; current ticket and reservation rules.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Q12. Which events are scheduled during my trip, and are their dates still confirmed?

Entities: Venue, Branch, Event, Closure.

Attributes: event title/type; start/end; status; venue; cancellation.

Relations: Event occurs at Branch; cancellation/Closure affects occurrence.

Time: trip interval KST; event status validity; source publication and verification dates.

Verification: require current, non-conflicting verified_fact for every decisive
attribute, official-page evidence plus source and checked date, within 90 days.
A source_claim is an attributed lead only: answer “Not confirmed” / “확인 중”
and request official confirmation; unknown stays unknown. Older facts show
“오래됨” and cannot support a definitive current answer. Latest append-only
confirmation may refresh the check date.

Unknown is not “no limit”: missing permission, restriction, temperature, presence,
price or schedule does not imply allowed, unrestricted, free, absent or open.
Return unknown separately from matching and explicitly excluded candidates.

## Coverage matrix

| Question | Venue | Branch | Space | Bath | SaunaRoom | Facility | PriceTicket | OpeningHours | Closure | AccessCondition | Event |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Q1 | ✓ | ✓ | — | ✓ | — | — | ✓ | ✓ | ✓ | ✓ | — |
| Q2 | ✓ | ✓ | — | — | — | — | ✓ | — | — | ✓ | — |
| Q3 | ✓ | ✓ | — | — | — | — | — | — | — | ✓ | — |
| Q4 | — | ✓ | — | — | — | — | — | ✓ | ✓ | — | — |
| Q5 | ✓ | ✓ | — | — | — | ✓ | — | — | ✓ | — | — |
| Q6 | — | ✓ | — | — | — | — | ✓ | — | — | — | — |
| Q7 | — | ✓ | ✓ | — | — | — | — | — | — | ✓ | — |
| Q8 | — | ✓ | ✓ | — | ✓ | — | — | — | — | — | — |
| Q9 | — | ✓ | ✓ | ✓ | — | — | — | — | — | — | — |
| Q10 | — | ✓ | ✓ | — | — | — | ✓ | — | — | ✓ | — |
| Q11 | ✓ | ✓ | — | — | — | — | ✓ | — | — | ✓ | ✓ |
| Q12 | ✓ | ✓ | — | — | — | — | — | — | ✓ | — | ✓ |
