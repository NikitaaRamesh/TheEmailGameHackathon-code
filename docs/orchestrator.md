# Autonomous Orchestrator

`scripts/orchestrator.py` runs a guarded execute-audit-refactor-evaluate loop around `my_agent.py`.

## Safety model

- Every hosted run uses `scripts/run_custom_agent.py --one-match`.
- Health and queue endpoints must pass repeated uncached probes before launch.
- The issued-key budget must remain above the configured reserve.
- Match results come from the authenticated `/matches`, `/match`, and `/api/agent` APIs.
- Runtime artifacts and byte-for-byte source snapshots live in `.orchestrator/`.
- Self-refactoring may replace only the configured agent module.
- Candidate source must parse, compile, retain `CustomAgent(BaseAgent)`, use no new imports, avoid file mutation/dynamic execution, and leave all module-level code unchanged.
- Every candidate must pass orchestrator safety tests and historical agent regressions; failure restores the previous source immediately.
- A candidate is not accepted when generated. It is marked pending and evaluated in the next scored match.
- A score regression or new unauthorized-signing penalty restores the last accepted source snapshot.
- Transport failures and incomplete host persistence pause evaluation and suppress strategy edits.
- Delayed match persistence is retried before the loop treats an audit as failed.
- Unknown or blocked gateway budget state prevents a scored launch.
- A global rank of 1 or 2 pauses matchmaking for up to eight minutes after each match, with live rank rechecks every 30 seconds. Falling below second place resumes immediately.
- Local candidate playtests are opt-in because they use the same limited gateway budget and run four agents.

The watch URL printed by the match runner is streamed directly to the terminal. Open it to watch the live match and first-visit rules walkthrough.

## Commands

Validate all gates without entering matchmaking:

```powershell
python scripts/orchestrator.py nikitaa_r_r --dry-run
```

Run a bounded two-match candidate cycle:

```powershell
python scripts/orchestrator.py nikitaa_r_r --max-matches 2
```

Run continuously until a gate stops it or you press `Ctrl+C`:

```powershell
python scripts/orchestrator.py nikitaa_r_r --max-matches 0
```

Pressing `Ctrl+C` during a game asks the child runner to finish that game, then stops the orchestrator before another launch.

To require a local sandbox match before installing each candidate:

```powershell
python scripts/orchestrator.py nikitaa_r_r --playtest-candidates
```

This is intentionally disabled by default to conserve the $30 shared gateway budget.

## Important options

| Option | Default | Purpose |
|---|---:|---|
| `--minimum-budget-left` | `5.0` | Stop before the gateway balance falls below this amount. |
| `--minimum-score` | `10` | Consider a clean lower score for self-refactoring. |
| `--rollback-score-drop` | `0` | Allowed drop from the accepted best before rollback. |
| `--maximum-latency` | `2.5` | Reject a launch if any stability request exceeds this many seconds. |
| `--stability-samples` | `8` | Number of health/queue sample pairs before each match. |
| `--gate-retry-seconds` | `30` | Delay before retrying an unhealthy gate. |
| `--top-rank-hold-rank` | `2` | Highest global rank that activates the post-match hold. |
| `--top-rank-hold-seconds` | `480` | Maximum hold per completed match; eight minutes stays inside the normal published idle grace. |
| `--top-rank-poll-seconds` | `30` | Live leaderboard recheck interval during a top-rank hold. |
| `--audit-attempts` | `6` | Authenticated post-match persistence attempts. |
| `--audit-retry-seconds` | `5` | Delay between persistence attempts. |
| `--no-refactor` | off | Execute and audit continuously without source changes. |
| `--adopt-current` | off | Intentionally reset transactional history after a manual source edit. |

## State and recovery

`.orchestrator/state.json` records the accepted snapshot, pending candidate, best score, penalties, and last match ID. `.orchestrator/events.jsonl` contains stability, budget, match, candidate, and rejection events. Per-match terminal logs are stored under `.orchestrator/logs/`.

The orchestrator refuses to start if `my_agent.py` changed outside its transaction. Review the edit, then explicitly use `--adopt-current` to create a new baseline.

Deleting `.orchestrator/` resets orchestrator history but does not affect hosted ratings or match records.
