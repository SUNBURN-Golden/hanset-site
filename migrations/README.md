# First legacy import

Run `python migrations/0001_import_legacy.py --db <external-path> [--dry-run]`.
The importable function is `data_foundation.legacy.import_legacy(db_path, root,
dry_run)`, also exported by the CLI module. No network requests or builder imports.
Dry-run rolls back all inserted rows; empty schema creation can persist.

Join venues.json keys to CSV name within csv=saunas/boxes, exactly as the builder
does. English copy joins by slug. Source precedence: sauna source_url; box
price_source_url, website, then cf_listing_url. CSV last_verified supplies the
check date; closed_days checked overrides it. Legacy created_at mirrors this
observation date for deterministic intake, not the actual DB insertion time.
Exact hour is unknown: midnight
+09:00 represents day precision. Full explicit date ranges in fact labels provide effective boundaries; an explicit
“closed Mon D, YYYY for maintenance” statement supplies its start date.
valid_from remains null when an effective date
cannot be unambiguously determined. A dated check/reply is not an effective date.
Closed-day until expires at the end of that KST day.

One ven_<slug> entity and one branch_<slug> per accepted entry. Alias languages
include CSV name/name_ko and ko/en titles. Branch geography contains only city,
district and location text. CSV street addresses and address-labeled facts are
not migrated. The configured exclusion policy skips matching venue name/slug/copy;
counts report skips without retaining excluded text. Contacts are minimized before
storage; only the approved public contact can survive. No business details are
published by this internal store.
Naver is not a source.
Excluded-domain URLs become null; the row cannot be higher than source_claim,
and evidence_locator records a reason without retaining the URL. The report's
blocked_source_rows counts affected inputs (including on a repeat run).

## Deterministic status rules

| Input | Status / source_type |
|---|---|
| Official-page reading + high/verified CSV confidence | verified_fact / official_page |
| Venue/hotel/email reply or Korean reply wording | source_claim / venue_reply; URL null |
| Operator-run profile | source_claim / operator_profile |
| Instagram/DM wording | source_claim / official_social |
| Partial CSV confidence or unclear provenance | source_claim / legacy_site_content |
| Explicit third-party / not-official wording | source_claim / third_party |
| Not confirmed / Not stated / not listed / awaiting reply / empty / unverified list | unverified / unknown with null value |
| Not applicable | unverified / not_applicable with null value |
| closed_days source Official page | verified_fact / official_page |
| Other closure sources | source_claim with corresponding source_type |

Unknown overrides all trust rules. Missing source URL downgrades verified status.
No imported row is ai_inference. Source attribution is conservative at the whole
legacy claim level; where row wording combines reply and page evidence, reply rules win.
Channel detection uses row wording and explicit closure source only; CSV
confidence is used only for the official-page verification decision. Precedence:
reply, third-party/unofficial/blog/roundup, operator/Daangn, Instagram/DM, page.
Word boundaries prevent partial English word matches.
The original page section was not captured in legacy data: evidence_locator has
kind legacy_site_content, file and JSON pointer; quoted strings additionally carry
the quote. No CSS selector, publication time or original section is invented.

`value_text` keeps wording (except contact minimization and CSV price placeholder
substitution). Null structured value with known state means a text-only claim,
not an empty known number. Only exact standalone HH:MM ranges, simple ordinal
monthly closure rules and numeric CSV KRW prices are parsed. All remaining prose
stays text-only. CSV price is a separate PriceTicket fact; empty price is unknown.
Ticket/payer/day type are unspecified unless known; no false comparable price.
English facts, tips and unverified items keep language-qualified pointers.
foreigner fields map dropin→public_access and kr_phone→korean_phone_required;
other keys retain their names, including full_price, scrub_fee, last_entry,
closures and age_tattoo_attire. Price fields use PriceTicket; policies use
AccessCondition. Tips use tip; unverified lists use unverified_item.

Official public page facts use public_page_factual / facts_keep_history. Reply
facts use venue_reply_internal / review_365_days, with retention_days 365 and a
review timestamp; this is an internal review obligation, not automatic deletion.

Fact IDs hash file, pointer, attribute, sanitized wording, typed value, state,
source and check date. Entities and aliases insert-or-ignore; fact rows check ID
before inserting. Identical reruns insert nothing. Changed input creates a new
legacy observation, not an automatic temporal resolution: ongoing changes belong
through observe after entity/evidence review. This importer is a first migration,
not a live synchronization job. Return/print counts include inserted rows by status
and source_type, new entities by type, skips, migrated_venues, unknown,
blocked_source_rows and omitted_address_facts. Input coverage counters repeat;
insert counters are zero on the second run.

## Explicit label mapping

Unlisted labels fall back to `note:<label>`. Address labels are omitted. English
rows inherit the paired Korean label's attribute, retaining the English label in
qualifiers. The following table is generated from the explicit implementation map.

| Korean label | Attribute |
|---|---|
| 객실 고객 | guest_discount |
| 공식 영문 안내 | english |
| 공식 채널 | note |
| 공휴일 | opening_hours |
| 구성(공식 소개) | facility |
| 그룹 세션 | program |
| 노천탕 이용 조건 | access_public |
| 도로명 주소 | omit_address |
| 드롭인 | price |
| 드롭인 10회권 | price |
| 드롭인 1회 | price |
| 드롭인 가격 | price |
| 문신 | tattoo |
| 문신 규정 | tattoo |
| 복장·입장 규정 | attire |
| 분류 | identity |
| 비투숙객 | access_public |
| 사우나 | sauna_room |
| 세신 | scrub_fee |
| 세신(바디클린) | scrub_fee |
| 소요시간 | note |
| 수업 | program |
| 수업 시간 | program |
| 시간표 | program |
| 시민의 숲 지점 | location |
| 시설 | facility |
| 시설·서비스 | facility |
| 신청 | booking |
| 실내풀 | facility |
| 언어 | english |
| 여행자 패스 | price |
| 연간 회원권 | price |
| 연령 | age |
| 연령 안내 | age |
| 연령·복장 규정 | age |
| 연령·성별 규정 | age |
| 영업 | opening_hours |
| 영업시간 | opening_hours |
| 예약 | booking |
| 예약 방법 | booking |
| 예약(공식) | booking |
| 오픈짐 | program |
| 외국인 | foreigners |
| 외국인(공식) | foreigners |
| 외국인·결제 | foreigners |
| 요금 | price |
| 요금 기록 차이 | note |
| 요금 단서 추가 | note |
| 요금(2026.09.01~10.31) | price |
| 요금(2026.11.01~11.30) | price |
| 요금(대인, 평일 / 주말·공휴일) | price |
| 요금(소인, 평일 / 주말·공휴일) | price |
| 운영 | opening_hours |
| 운영(공식) | opening_hours |
| 운영시간 | opening_hours |
| 운영시간 기록 차이 | note |
| 운영시간 재확인 | note |
| 운영시간(2026.09.01~11.30) | opening_hours |
| 운영시간(본점) | opening_hours |
| 운영시간·일요일 | opening_hours |
| 위치 | location |
| 이용 대상 | access_public |
| 이용 방법(후불제) | access_public |
| 이용 안내 | access_public |
| 이용 연령 | age |
| 이용 인원 | private_room |
| 이용 조건 | access_public |
| 이용시간 | opening_hours |
| 인허가 | identity |
| 일요일 | opening_hours |
| 일요일 운영 | opening_hours |
| 일일 입장권 | price |
| 일일권 | price |
| 임시휴장 | closure |
| 입장 규정 | access_public |
| 입장 방식 | access_public |
| 장비(공식 페이지) | facility |
| 정기휴관 | closure |
| 정식 상호·주소 | omit_address |
| 종류·시간 | sauna_room |
| 주소 | omit_address |
| 주소(공식) | omit_address |
| 주차 | note |
| 지점·주소(공식) | omit_address |
| 찜질복 | attire |
| 찜질시설 | facility |
| 참고 | note |
| 찾아가기 | location |
| 체험 | program |
| 초보자 | program |
| 층 안내(공식 편의시설) | facility |
| 층별 시설(공식) | facility |
| 콘셉트 | note |
| 크로스핏 공식 어필리에이트 여부 | identity |
| 크로스핏 공식 어필리에이트 확인 | identity |
| 토요일 | opening_hours |
| 투숙객 할인가 | guest_discount |
| 특징 | facility |
| 편의 | facility |
| 프라이빗룸(공식) | private_room |
| 하이브리드 수업 | program |
| 회원 문의 | note |
| 휴관 | closure |
| 휴관일 | closure |
| 휴무 | closure |
| 휴무·조정 | closure |
| 휴무·휴장 | closure |


## Attribution, absence wording and stable state subjects

Official-page rows keep the CSV URL. Official-social rows keep it only for an
instagram.com path handle present in row wording. Operator-profile rows keep it
only when the host matches the named platform (currently Daangn). Third-party
rows keep it only when its host differs from the CSV website host. Reply rows
have no URL. A mismatched URL is retained as evidence_locator.venue_level_source_url
with source_scope="venue_level_legacy"; status is at most source_claim. Nullable
social/operator URLs are allowed only for non-verified rows.

For price, full_price, scrub_fee, guest_discount and last_entry, absence wording
(not listed/stated/shown/on the official page) without digits means unknown,
unverified and null value. Included/free wording is exempt. With digits, mixed
absence wording stays known but at most source_claim, and evidence_locator has
mixed_absence_clause=true. validate_fact also rejects known digit-free absence.

Tracked subjects are hours_<slug>, closure_<slug>, price_<slug>, access_<slug>,
all children of branch_<slug>. Korean and English renderings are separate rows,
sharing subject and attribute; canonical label and language qualifiers distinguish
facts. Tips/unverified items have pointer_key qualifiers. No room is invented.
by_language reports inserted ko/en/none row counts. distinct_fact_keys counts
inserted distinct (subject, attribute, label-or-pointer-key) ignoring language;
for rows without those qualifiers the original pointer is used. Both counters
are zero on an identical rerun. These counts separate renderings from fact keys.
