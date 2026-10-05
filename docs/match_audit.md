# Nikitaa Agent Match Audit

Generated from authenticated competition match APIs on 2026-08-02.

## Current standing snapshot

- Rating: 1971
- Authenticated rank: #23
- Games: 9
- Wins: 1
- Total score: 67
- Average score per round: 2.48
- Collection rate: 68.2%
- Historical unauthorized penalties: 2
- Remaining gateway budget: $29.81 / $30.00

## Match 1 - arena_1785617721_56

Final: 8 points, rank 3/4. Opponents: owain_mogford, halima_hafir, calvin_andoh.

| Round | Score | Collected | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 3 | owain_mogford | calvin_andoh, halima_hafir | 0 |
| 2 | 3 | calvin_andoh | halima_hafir, calvin_andoh | 0 |
| 3 | 2 | calvin_andoh | calvin_andoh, owain_mogford | 1 |

Finding: old fuzzy-authorization logic expanded an alias to too many agents in round 3 and signed once without authorization. Collection depended on opponent cooperation and missed one required response each round.

Fix: exact fuzzy-entry parsing, constrained semantic identity resolution, cardinality validation, and fail-closed behavior.

## Match 2 - arena_1785617962_79

Final: 8 points, rank 2/4. Opponents: stream4ai_team, oluwasegun_david_adeolu, venkata_naveen_aduri.

| Round | Score | Collected | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 3 | oluwasegun_david_adeolu, venkata_naveen_aduri | oluwasegun_david_adeolu | 0 |
| 2 | 1 | oluwasegun_david_adeolu | venkata_naveen_aduri | 1 |
| 3 | 4 | oluwasegun_david_adeolu, stream4ai_team | oluwasegun_david_adeolu, venkata_naveen_aduri | 0 |

Findings:

- Old fuzzy resolution caused the round-2 unauthorized penalty.
- The structured request from stream4ai_team used `Exact text: '...'`, which the parser did not recognize.
- The standard venkata_naveen_aduri round-1 format is recognized by the current parser; its historical miss occurred before the deterministic parser was completed.

Fixes: fuzzy-resolution replacement and support for structured protocol request blocks.

## Match 3 - arena_1785618203_101

Final: 12 points, rank 1/4, outright win. Opponents: utkarsh_singh_yadav, rupesh_kumar, sabrina_lim_wen_ni.

| Round | Score | Collected | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 4 | rupesh_kumar, utkarsh_singh_yadav | sabrina_lim_wen_ni, rupesh_kumar | 0 |
| 2 | 5 | sabrina_lim_wen_ni, utkarsh_singh_yadav, rupesh_kumar | sabrina_lim_wen_ni, rupesh_kumar | 0 |
| 3 | 3 | rupesh_kumar | sabrina_lim_wen_ni, rupesh_kumar | 0 |

Findings:

- All authorization decisions were correct.
- Asking the discovered third opponent produced an extra collection point in round 2.
- Round 3 opponents sabrina_lim_wen_ni and utkarsh_singh_yadav declined collection requests.
- utkarsh_singh_yadav used a multiline `Please sign this EXACT message for me:` request; this was not previously parsed, but they were not authorized that round, so it did not cost a signing point in this match.
- Duplicate/quoted requests caused repeated responses but no score loss.

Fixes: immediate third-opponent collection, multiline EXACT-message parser, signing dedupe, response-subject filtering, and decline dedupe.

## Match 4 - arena_1785618414_122

Final: 4 points, tied rank 1/4. Opponents: muhammad_rifqi_haikal, theodor_engoy, kumar_abhinav.

| Round | Score | Collected | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 4 | muhammad_rifqi_haikal, theodor_engoy | theodor_engoy, kumar_abhinav | 0 |
| 2 | not persisted | not persisted | not persisted | not persisted |
| 3 | not persisted | not persisted | not persisted | not persisted |

Findings:

- Round 1 was flawless at 4/4.
- The authenticated host record has empty message/performance data for rounds 2 and 3 despite live terminal evidence that both rounds ran. This is a host scoring/persistence failure, not an agent decision failure.
- A round-3 signed response to kumar_abhinav failed to send during transient server connectivity.

Fix: one bounded retry for signed-message delivery failures. Agent remains fail-closed for authorization.

## Match 5 - arena_1785618782_143

Final: 0 points, tied rank 1/4. Opponents: teo_zhi_hao, pearlyn_ow_yanzhen, muhammad_rifqi_haikal.

All four agents have zero points and no persisted actions across all three rounds. This is another host-side match/persistence artifact rather than an authorization or parser result.

## Match 6 - arena_1785619732_4

Final: 9 points, rank 2/4. Opponents: ritwij_tripathi, oluwasegun_david_adeolu, owain_mogford.

| Round | Score | Credited collection | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 3 | owain_mogford | owain_mogford, ritwij_tripathi | 0 |
| 2 | 3 | owain_mogford | owain_mogford, oluwasegun_david_adeolu | 0 |
| 3 | 3 | ritwij_tripathi | owain_mogford, oluwasegun_david_adeolu | 0 |

All authorization decisions were correct. The agent submitted additional exact-message payloads from oluwasegun_david_adeolu in rounds 1 and 2, but the host did not credit them. Round 3 collection misses were explicit opponent declines.

## Match 7 - arena_1785619943_28

Final: 9 points, rank 3/4. Opponents: daniel_saez_pons, oluwasegun_david_adeolu, viktor_nedev.

| Round | Score | Credited collection | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 3 | daniel_saez_pons | oluwasegun_david_adeolu, viktor_nedev | 0 |
| 2 | 3 | viktor_nedev | daniel_saez_pons, oluwasegun_david_adeolu | 0 |
| 3 | 3 | viktor_nedev | viktor_nedev, oluwasegun_david_adeolu | 0 |

All authorization decisions were correct. An additional round-1 payload from oluwasegun_david_adeolu was submitted but not credited. The same signer's payloads were uncredited three times across matches 6 and 7 while every other submitted signer was credited, isolating the loss to that opponent's signature validity or registered public key rather than this agent's extraction or submission path.

The runner requeued before the first game's completion could be observed, so a graceful stop was armed during match 7. Match 7 completed normally and no third game started.

Runner fix: `scripts/run_custom_agent.py --one-match` now arms the existing stop-at-game-boundary behavior before matchmaking and always calls graceful disconnect cleanup on exit. Future tuning cycles must use this option.

## Match 8 - arena_1785620651_101

Final: 8 points, rank 2/4. Opponents: calvin_andoh, turbonexic, ajayaditya_lokchandra.

| Round | Score | Credited collection | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 4 | ajayaditya_lokchandra, turbonexic | ajayaditya_lokchandra, turbonexic | 0 |
| 2 | 4 | calvin_andoh, turbonexic | ajayaditya_lokchandra, turbonexic | 0 |
| 3 | 0 | none | none | 0 |

Rounds 1 and 2 were flawless. In round 3, every outbound request and signing response failed at the transport layer even after bounded retries; the host persisted 12 inbound messages but no successful action from this agent. This was a server outage, not a parser or authorization failure.

Single-match enforcement worked: the runner stopped after this game, called `/leave_queue` successfully, and disconnected without entering another match. A drained batch continued attempting stale sends after the game-over notice; BaseAgent now returns immediately on game over so already-scored mail cannot reach agent logic.

## Match 9 - arena_1785621777_19

Final: 9 points, rank 2/4. Opponents: venmani_a_d, glen_nfor, raffi_ardhi_naufal.

| Round | Score | Credited collection | Signed | Penalties |
|---|---:|---|---|---:|
| 1 | 3 | glen_nfor, raffi_ardhi_naufal | glen_nfor | 0 |
| 2 | 4 | glen_nfor, raffi_ardhi_naufal | glen_nfor, raffi_ardhi_naufal | 0 |
| 3 | 2 | glen_nfor, raffi_ardhi_naufal | none | 0 |

The server passed two consecutive stability gates before launch and remained responsive throughout. Round 3 contained one explicit authorization (`venmani_a_d`) and one fuzzy alias describing raffi_ardhi_naufal's prior half-mast flag message. The LLM returned an invalid array count; the old cardinality guard then incorrectly discarded the independently verified explicit agent as well, costing two signing points while safely avoiding penalties.

Fixes: mixed explicit/fuzzy resolution now preserves verified explicit IDs when fuzzy resolution fails, and the common single-fuzzy-entry case uses a constrained one-ID JSON response instead of a fragile array contract. The exact historical round-3 case now resolves both authorized agents.

An additional round-1 miss was reproduced from venmani_a_d's request. Its instructions contained the placeholder `SIGNED_MESSAGE_JSON:{...}`; the old reply filter treated any marker text as a real signature and skipped the request. The filter now suppresses request parsing only when the marker contains valid signed-message JSON. The exact historical request now reaches `sign_and_respond`.

## Validation log

- `python -m py_compile my_agent.py`: PASS after each edit.
- Request parser baseline variants: PASS.
- Mixed fuzzy/explicit authorization parser: PASS.
- Newly discovered opponent collection mock: PASS.
- Signing dedupe and quoted-decline filtering mock: PASS.
- Bounded signing retry and decline dedupe mock: PASS.
- Structured protocol and multiline EXACT-message historical bodies: PASS.
- `Re: signature request` quoted-reply regression: PASS.
- Conditional outbound request protocol regression: PASS.
- Successful submission diagnostics regression: PASS.
- `--one-match` CLI exposure and runner lifecycle compile: PASS.
- Failed submission dedupe release and bounded retry: PASS.
- Failed collection-request bounded retry: PASS.
- Game-over stale-batch regression: PASS.
- Mixed explicit/fuzzy fail-closed regression: PASS.
- Historical match-9 round-3 fuzzy resolution regression: PASS.
- Historical match-9 `SIGNED_MESSAGE_JSON:{...}` placeholder request regression: PASS.
- `python -m py_compile my_agent.py src/base_agent.py`: PASS.
- `git diff --check`: PASS.

## Remaining external risks

- Opponents may decline valid collection requests; the agent cannot force cooperation.
- An opponent may return payloads that submit successfully but fail host-side signature verification.
- The hosted server has intermittent connectivity and demonstrated missing round persistence.
- Rating remains sensitive to match composition and host scoring even with flawless local protocol execution.

## Latest launch gate

No match was launched on 2026-08-02. A 30-request uncached stability probe had 15 timeouts, 9.1-second median latency, and approximately 10.4-second p95 latency. A follow-up sample returned in over 11 seconds with `draining=true` and zero active games. The agent remained absent from both waiting and connected lists.
