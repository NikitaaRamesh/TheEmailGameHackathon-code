#!/usr/bin/env python3
"""Continuous, transactional self-improvement loop for The Email Game.

Each iteration stability-gates the hosted server, runs exactly one scored match,
audits authenticated telemetry, evaluates a pending candidate, and optionally
asks a constrained model to produce a replacement for only the configured agent
module. Candidates are promoted or rolled back after their next scored match.

Runtime artifacts live under .orchestrator/ and must not be committed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import signal
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = "https://play.theemailgame.com"
WATCH_RE = re.compile(
    r"https?://[^\s]+/watch\?agent=[^&\s]+(?:&|&amp;)token=([A-Za-z0-9._-]+)"
)
BUDGET_RE = re.compile(r"Budget:\s*\$([0-9.]+) left of \$([0-9.]+)")
ROUND_RE = re.compile(r"IN GAME - Round\s+(\d+)", re.IGNORECASE)
TRANSPORT_PATTERNS = (
    "failed to send message",
    "failed to send email",
    "request failed for",
    "submit failed for",
    "read timed out",
    "could not reach the game server",
    "websocket connection failed",
)
AUTH_FAILURE_PATTERNS = (
    "authorization resolution failed closed",
    "fuzzy authorization resolution returned invalid",
    "fuzzy authorization resolution failed closed",
)
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open"}
FORBIDDEN_ATTRIBUTES = {"unlink", "rmdir", "rename", "replace", "write_bytes", "write_text"}


@dataclass
class StabilityReport:
    passed: bool
    successes: int
    failures: list[str]
    median_seconds: Optional[float]
    p95_seconds: Optional[float]
    max_seconds: Optional[float]
    draining: Optional[bool]
    active_games: Optional[int]
    queue_length: Optional[int]
    agent_waiting: Optional[bool]
    agent_connected: Optional[bool]


@dataclass
class TerminalTelemetry:
    exit_code: int
    watch_token: Optional[str]
    rounds_seen: list[int]
    transport_failures: list[str]
    authorization_failures: list[str]
    budget_left: Optional[float]
    budget_cap: Optional[float]
    stop_requested: bool = False


@dataclass
class MatchMetrics:
    game_id: str
    score: int
    rank: int
    field: int
    result: str
    rounds: int
    collected: int
    signed: int
    penalties: int
    abandoned: bool
    round_scores: list[int] = field(default_factory=list)
    transport_failures: int = 0
    authorization_failures: int = 0
    budget_left: Optional[float] = None
    infrastructure_failure: bool = False


@dataclass
class LeaderboardPosition:
    rank: Optional[int]
    elo: Optional[int]
    idle_days: float
    decay: int
    phase: Optional[str]


@dataclass
class OrchestratorState:
    version: int = 1
    matches_run: int = 0
    last_match_id: Optional[str] = None
    best_score: Optional[int] = None
    best_penalties: Optional[int] = None
    best_rank: Optional[int] = None
    best_match_id: Optional[str] = None
    best_snapshot: Optional[str] = None
    best_source_hash: Optional[str] = None
    pending_candidate: Optional[dict[str, Any]] = None
    top_rank_hold_match_id: Optional[str] = None
    top_rank_hold_until: Optional[float] = None


class OrchestratorError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def load_state(path: Path) -> OrchestratorState:
    if not path.exists():
        return OrchestratorState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return OrchestratorState(**data)


def save_state(path: Path, state: OrchestratorState) -> None:
    atomic_write_text(path, json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def percentile_95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def probe_stability(
    server: str,
    agent_id: str,
    samples: int,
    timeout: float,
    max_latency: float,
) -> StabilityReport:
    latencies: list[float] = []
    failures: list[str] = []
    queue_states: list[dict[str, Any]] = []
    session = requests.Session()

    for _ in range(samples):
        for endpoint in ("health", "queue_status"):
            started = time.perf_counter()
            try:
                response = session.get(
                    f"{server.rstrip('/')}/{endpoint}",
                    params={"_orchestrator_probe": time.time_ns()},
                    timeout=timeout,
                )
                elapsed = time.perf_counter() - started
                response.raise_for_status()
                payload = response.json()
                latencies.append(elapsed)
                if endpoint == "queue_status":
                    queue_states.append(payload)
            except Exception as exc:
                failures.append(f"{endpoint}: {type(exc).__name__}: {exc}")

    latest = queue_states[-1] if queue_states else {}
    queue_healthy = bool(queue_states) and all(
        not state.get("draining", True) for state in queue_states
    )
    identity_clean = bool(queue_states) and all(
        agent_id not in state.get("agents_waiting", [])
        and agent_id not in state.get("connected_agents", [])
        for state in queue_states
    )
    passed = bool(latencies) and not failures and queue_healthy and identity_clean
    if latencies:
        passed = passed and max(latencies) <= max_latency

    return StabilityReport(
        passed=passed,
        successes=len(latencies),
        failures=failures,
        median_seconds=round(statistics.median(latencies), 3) if latencies else None,
        p95_seconds=(round(percentile_95(latencies), 3) if latencies else None),
        max_seconds=round(max(latencies), 3) if latencies else None,
        draining=latest.get("draining"),
        active_games=latest.get("active_games"),
        queue_length=latest.get("queue_length"),
        agent_waiting=(agent_id in latest.get("agents_waiting", [])) if latest else None,
        agent_connected=(agent_id in latest.get("connected_agents", [])) if latest else None,
    )


def fetch_leaderboard_position(
    server: str, agent_id: str, timeout: float
) -> LeaderboardPosition:
    response = requests.get(
        f"{server.rstrip('/')}/api/leaderboard",
        params={"_orchestrator_rank_probe": time.time_ns()},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success") or not isinstance(payload.get("leaderboard"), list):
        raise OrchestratorError("Leaderboard API returned an invalid payload")
    row = next(
        (entry for entry in payload["leaderboard"] if entry.get("agent_id") == agent_id),
        None,
    )
    if row is None:
        raise OrchestratorError(f"Agent {agent_id!r} is missing from the leaderboard")
    return LeaderboardPosition(
        rank=int(row["rank"]) if row.get("rank") is not None else None,
        elo=int(row["elo"]) if row.get("elo") is not None else None,
        idle_days=float(row.get("idle_days") or 0.0),
        decay=int(row.get("decay") or 0),
        phase=(payload.get("live") or {}).get("phase"),
    )


def hold_if_top_ranked(
    server: str,
    agent_id: str,
    state: OrchestratorState,
    state_path: Path,
    event_path: Path,
    maximum_rank: int,
    hold_seconds: float,
    poll_seconds: float,
    timeout: float,
    dry_run: bool = False,
) -> LeaderboardPosition:
    position = fetch_leaderboard_position(server, agent_id, timeout)
    append_jsonl(event_path, {"at": utc_now(), "type": "leaderboard", **asdict(position)})
    print("LEADERBOARD", json.dumps(asdict(position), sort_keys=True))

    is_top_ranked = position.rank is not None and 1 <= position.rank <= maximum_rank
    if not is_top_ranked or hold_seconds <= 0:
        if state.top_rank_hold_until is not None:
            state.top_rank_hold_match_id = None
            state.top_rank_hold_until = None
            save_state(state_path, state)
        return position
    if dry_run:
        print(f"Top-{maximum_rank} hold would apply; dry-run will not wait.")
        return position

    now = time.time()
    if (
        state.top_rank_hold_match_id != state.last_match_id
        or state.top_rank_hold_until is None
    ):
        state.top_rank_hold_match_id = state.last_match_id
        state.top_rank_hold_until = now + hold_seconds
        save_state(state_path, state)
        append_jsonl(
            event_path,
            {
                "at": utc_now(),
                "type": "top_rank_hold_started",
                "rank": position.rank,
                "hold_seconds": hold_seconds,
                "match_id": state.last_match_id,
            },
        )

    deadline = state.top_rank_hold_until or now
    while deadline > time.time():
        remaining = deadline - time.time()
        print(
            f"Rank #{position.rank}: holding matchmaking for {math.ceil(remaining)}s "
            f"(top-{maximum_rank} protection)."
        )
        time.sleep(min(max(1.0, poll_seconds), remaining))
        position = fetch_leaderboard_position(server, agent_id, timeout)
        append_jsonl(event_path, {"at": utc_now(), "type": "leaderboard", **asdict(position)})
        if position.rank is None or position.rank > maximum_rank:
            state.top_rank_hold_match_id = None
            state.top_rank_hold_until = None
            save_state(state_path, state)
            append_jsonl(
                event_path,
                {
                    "at": utc_now(),
                    "type": "top_rank_hold_released",
                    "rank": position.rank,
                    "match_id": state.last_match_id,
                },
            )
            print(f"Leaderboard rank is now #{position.rank}; resuming matchmaking.")
            return position

    append_jsonl(
        event_path,
        {
            "at": utc_now(),
            "type": "top_rank_hold_completed",
            "rank": position.rank,
            "match_id": state.last_match_id,
        },
    )
    print("Top-rank hold completed; resuming before the normal idle grace cutoff.")
    return position


def read_gateway_budget() -> dict[str, Any]:
    base = (os.getenv("OPENAI_BASE_URL") or os.getenv("EMAIL_GAME_GATEWAY_URL") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not base or not key:
        return {"available": False}
    response = requests.get(
        f"{base}/key/info",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    response.raise_for_status()
    info = (response.json() or {}).get("info") or {}
    spend = float(info.get("spend") or 0.0)
    cap = info.get("max_budget")
    return {
        "available": cap is not None,
        "spend": spend,
        "cap": float(cap) if cap is not None else None,
        "left": max(0.0, float(cap) - spend) if cap is not None else None,
        "blocked": bool(info.get("blocked")),
    }


def parse_terminal_telemetry(
    output: str,
    exit_code: int,
    stop_requested: bool = False,
) -> TerminalTelemetry:
    watch_matches = WATCH_RE.findall(output)
    budget_matches = BUDGET_RE.findall(output)
    lowered_lines = [(line, line.lower()) for line in output.splitlines()]
    transport = [
        line for line, lowered in lowered_lines
        if any(pattern in lowered for pattern in TRANSPORT_PATTERNS)
    ]
    authorization = [
        line for line, lowered in lowered_lines
        if any(pattern in lowered for pattern in AUTH_FAILURE_PATTERNS)
    ]
    return TerminalTelemetry(
        exit_code=exit_code,
        watch_token=watch_matches[-1] if watch_matches else None,
        rounds_seen=sorted({int(value) for value in ROUND_RE.findall(output)}),
        transport_failures=transport,
        authorization_failures=authorization,
        budget_left=float(budget_matches[-1][0]) if budget_matches else None,
        budget_cap=float(budget_matches[-1][1]) if budget_matches else None,
        stop_requested=stop_requested,
    )


def stream_one_match(
    agent_id: str,
    module: Path,
    server: str,
    model: str,
    temperature: float,
    log_path: Path,
) -> TerminalTelemetry:
    command = [
        sys.executable,
        "scripts/run_custom_agent.py",
        agent_id,
        "--module",
        str(module),
        "--server",
        server,
        "--model",
        model,
        "--temperature",
        str(temperature),
        "--one-match",
    ]
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
    )
    lines: list[str] = []
    stop_requested = False
    assert process.stdout is not None
    try:
        for line in process.stdout:
            print(line, end="", flush=True)
            lines.append(line)
    except KeyboardInterrupt:
        stop_requested = True
        child_signal = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        process.send_signal(child_signal)
        print("\nOrchestrator requested graceful stop; waiting for game boundary.")
        for line in process.stdout:
            print(line, end="", flush=True)
            lines.append(line)
    exit_code = process.wait()
    output = "".join(lines)
    atomic_write_text(log_path, output)
    return parse_terminal_telemetry(output, exit_code, stop_requested)


def authenticated_get(server: str, path: str, token: str) -> dict[str, Any]:
    response = requests.get(
        f"{server.rstrip('/')}/{path.lstrip('/')}",
        params={"token": token},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def audit_latest_match(
    server: str,
    agent_id: str,
    token: str,
    previous_match_id: Optional[str],
    terminal: TerminalTelemetry,
) -> tuple[MatchMetrics, dict[str, Any]]:
    history = authenticated_get(server, f"matches/{agent_id}", token).get("matches", [])
    if not history:
        raise OrchestratorError("Match runner exited but authenticated history is empty")
    latest = history[0]
    game_id = latest.get("game_id")
    if not game_id or game_id == previous_match_id:
        raise OrchestratorError("No new persisted match appeared after the runner exited")

    detail = authenticated_get(server, f"match/{game_id}/{agent_id}", token)
    round_records: list[dict[str, Any]] = []
    seen_rounds: set[int] = set()
    for candidate in walk_dicts(detail):
        number = candidate.get("round_number")
        if not isinstance(number, int) or not isinstance(candidate.get("performance"), dict):
            continue
        if number in seen_rounds:
            continue
        seen_rounds.add(number)
        performance = candidate["performance"]
        round_records.append(
            {
                "round": number,
                "score": int(candidate.get("your_score") or 0),
                "required": performance.get("supposed_to_request_from") or [],
                "collected": performance.get("successfully_submitted_for") or [],
                "authorized": performance.get("authorized_to_sign_for") or [],
                "signed": performance.get("successfully_signed_for") or [],
                "penalties": int(performance.get("unauthorized_signing_penalties") or 0),
            }
        )
    round_records.sort(key=lambda item: item["round"])

    breakdown = latest.get("breakdown") or {}
    infrastructure_failure = bool(terminal.transport_failures) or len(round_records) < 3
    metrics = MatchMetrics(
        game_id=game_id,
        score=int(latest.get("your_score") or 0),
        rank=int(latest.get("rank") or 0),
        field=int(latest.get("field") or 0),
        result=str(latest.get("result") or "unknown"),
        rounds=int(latest.get("rounds") or len(round_records)),
        collected=int(breakdown.get("collected") or 0),
        signed=int(breakdown.get("signed") or 0),
        penalties=int(breakdown.get("penalties") or 0),
        abandoned=bool(latest.get("abandoned")),
        round_scores=[record["score"] for record in round_records],
        transport_failures=len(terminal.transport_failures),
        authorization_failures=len(terminal.authorization_failures),
        budget_left=terminal.budget_left,
        infrastructure_failure=infrastructure_failure,
    )
    return metrics, {"summary": latest, "rounds": round_records}


def audit_latest_match_with_retry(
    server: str,
    agent_id: str,
    token: str,
    previous_match_id: Optional[str],
    terminal: TerminalTelemetry,
    attempts: int,
    retry_seconds: float,
) -> tuple[MatchMetrics, dict[str, Any]]:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return audit_latest_match(
                server,
                agent_id,
                token,
                previous_match_id,
                terminal,
            )
        except (OrchestratorError, requests.RequestException) as exc:
            last_error = exc
            if attempt < attempts:
                print(f"Audit not ready ({attempt}/{attempts}): {exc}")
                time.sleep(retry_seconds)
    raise OrchestratorError(f"Authenticated match audit failed: {last_error}")


def is_degraded(metrics: MatchMetrics, state: OrchestratorState, score_drop: int) -> bool:
    if state.best_score is None or state.best_penalties is None:
        return False
    if metrics.penalties > state.best_penalties:
        return True
    if metrics.score < state.best_score - score_drop:
        return True
    return (
        metrics.score == state.best_score
        and state.best_rank is not None
        and metrics.rank > state.best_rank
    )


def should_refactor(metrics: MatchMetrics, minimum_score: int) -> bool:
    if metrics.infrastructure_failure:
        return False
    return (
        metrics.penalties > 0
        or metrics.authorization_failures > 0
        or metrics.score < minimum_score
        or metrics.signed < 6
    )


def validate_candidate_source(source: str, original_source: str) -> None:
    if len(source) < max(500, len(original_source) // 2):
        raise OrchestratorError("Candidate source is implausibly short")
    if len(source) > len(original_source) * 2:
        raise OrchestratorError("Candidate source is implausibly large")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise OrchestratorError(f"Candidate syntax error: {exc}") from exc

    custom_agent = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CustomAgent"),
        None,
    )
    if custom_agent is None:
        raise OrchestratorError("Candidate removed CustomAgent")
    if not any(isinstance(base, ast.Name) and base.id == "BaseAgent" for base in custom_agent.bases):
        raise OrchestratorError("Candidate CustomAgent no longer subclasses BaseAgent")

    original_tree = ast.parse(original_source)
    candidate_module_nodes = [
        node for node in tree.body
        if not (isinstance(node, ast.ClassDef) and node.name == "CustomAgent")
    ]
    original_module_nodes = [
        node for node in original_tree.body
        if not (isinstance(node, ast.ClassDef) and node.name == "CustomAgent")
    ]
    if ast.dump(ast.Module(body=candidate_module_nodes, type_ignores=[])) != ast.dump(
        ast.Module(body=original_module_nodes, type_ignores=[])
    ):
        raise OrchestratorError("Candidate modified code outside CustomAgent")

    allowed_imports = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(original_tree)
        if isinstance(node, ast.Import) and node.names
    }
    allowed_imports.update(
        node.module.split(".")[0]
        for node in ast.walk(original_tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {name.name.split(".")[0] for name in node.names}
            if not roots <= allowed_imports:
                raise OrchestratorError(f"Candidate added imports outside allowlist: {sorted(roots - allowed_imports)}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root not in allowed_imports:
                raise OrchestratorError(f"Candidate added import outside allowlist: {root}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                raise OrchestratorError(f"Candidate uses forbidden call: {node.func.id}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_ATTRIBUTES:
                raise OrchestratorError(f"Candidate uses forbidden attribute: {node.func.attr}")


def extract_json_object(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise OrchestratorError("Refactor model did not return a JSON object")
    return value


def generate_candidate(
    agent_path: Path,
    metrics: MatchMetrics,
    audit: dict[str, Any],
    terminal: TerminalTelemetry,
    model: str,
) -> tuple[str, str]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise OrchestratorError("openai package is required for self-refactor") from exc

    source = agent_path.read_text(encoding="utf-8")
    prompt = {
        "task": (
            "Repair only evidence-backed strategy defects in this Email Game agent. "
            "Return a JSON object with keys analysis and source. source must be the "
            "complete replacement Python file. Preserve public APIs, deterministic "
            "parsing, fail-closed authorization, bounded retries, and all unrelated logic."
        ),
        "constraints": [
            "Modify only CustomAgent strategy behavior in the supplied source.",
            "Do not add imports, dependencies, file IO, subprocesses, network clients, eval, or exec.",
            "Do not weaken authorization checks or sign ambiguous unauthorized requests.",
            "Do not react to transport-only failures with strategy changes.",
            "Make the smallest change that addresses the telemetry.",
        ],
        "metrics": asdict(metrics),
        "audit": audit,
        "terminal": {
            "rounds_seen": terminal.rounds_seen,
            "authorization_failures": terminal.authorization_failures,
        },
        "source": source,
    }
    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a conservative Python repair engine. Output valid JSON only.",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
        ],
        temperature=0,
        max_tokens=16000,
        response_format={"type": "json_object"},
    )
    result = extract_json_object(response.choices[0].message.content or "")
    candidate = result.get("source")
    analysis = result.get("analysis")
    if not isinstance(candidate, str) or not isinstance(analysis, str):
        raise OrchestratorError("Refactor response is missing string analysis/source fields")
    validate_candidate_source(candidate, source)
    return candidate, analysis


def compile_file(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise OrchestratorError(f"Compile failed for {path}: {result.stderr.strip()}")


def run_playtest(path: Path, model: str) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/playtest.py", str(path), "--model", model],
        cwd=PROJECT_ROOT,
    )
    if result.returncode:
        raise OrchestratorError("Candidate local playtest failed")


def run_regression_tests() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "tests.test_orchestrator",
            "tests.test_agent_regressions",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        details = (result.stdout + "\n" + result.stderr).strip()
        raise OrchestratorError(f"Candidate regression tests failed:\n{details}")


def snapshot_source(agent_path: Path, snapshot_dir: Path, label: str) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    destination = snapshot_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}.py"
    shutil.copy2(agent_path, destination)
    return destination


def promote_current(agent_path: Path, state: OrchestratorState, snapshot_dir: Path, metrics: MatchMetrics) -> None:
    snapshot = snapshot_source(agent_path, snapshot_dir, f"best_{metrics.game_id}")
    state.best_snapshot = str(snapshot.relative_to(PROJECT_ROOT))
    state.best_source_hash = sha256_file(agent_path)
    state.best_score = metrics.score
    state.best_penalties = metrics.penalties
    state.best_rank = metrics.rank
    state.best_match_id = metrics.game_id
    state.pending_candidate = None


def rollback(agent_path: Path, state: OrchestratorState) -> None:
    if not state.best_snapshot:
        raise OrchestratorError("Cannot roll back without a best snapshot")
    source = PROJECT_ROOT / state.best_snapshot
    if not source.exists():
        raise OrchestratorError(f"Best snapshot is missing: {source}")
    atomic_write_bytes(agent_path, source.read_bytes())
    compile_file(agent_path)
    state.pending_candidate = None


def install_candidate(
    agent_path: Path,
    candidate: str,
    analysis: str,
    state: OrchestratorState,
    candidate_dir: Path,
    playtest: bool,
    playtest_model: str,
) -> None:
    candidate_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = candidate_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_candidate.py"
    atomic_write_text(candidate_path, candidate)
    compile_file(candidate_path)
    previous_source = agent_path.read_bytes()
    try:
        atomic_write_bytes(agent_path, candidate_path.read_bytes())
        compile_file(agent_path)
        run_regression_tests()
        if playtest:
            run_playtest(agent_path, playtest_model)
    except Exception:
        atomic_write_bytes(agent_path, previous_source)
        compile_file(agent_path)
        raise
    state.pending_candidate = {
        "candidate_path": str(candidate_path.relative_to(PROJECT_ROOT)),
        "source_hash": sha256_file(agent_path),
        "analysis": analysis,
        "installed_at": utc_now(),
    }


def initialize_state(agent_path: Path, state: OrchestratorState, snapshot_dir: Path) -> None:
    current_hash = sha256_file(agent_path)
    if state.best_snapshot is None:
        snapshot = snapshot_source(agent_path, snapshot_dir, "initial")
        state.best_snapshot = str(snapshot.relative_to(PROJECT_ROOT))
        state.best_source_hash = current_hash
        return
    if state.pending_candidate:
        expected = state.pending_candidate.get("source_hash")
    else:
        expected = state.best_source_hash
    if expected and current_hash != expected:
        raise OrchestratorError(
            "Agent source changed outside the orchestrator. Use --adopt-current to reset "
            "the transactional baseline intentionally."
        )


def adopt_current(agent_path: Path, state: OrchestratorState, snapshot_dir: Path) -> None:
    snapshot = snapshot_source(agent_path, snapshot_dir, "adopted")
    state.best_snapshot = str(snapshot.relative_to(PROJECT_ROOT))
    state.best_source_hash = sha256_file(agent_path)
    state.pending_candidate = None
    state.best_score = None
    state.best_penalties = None
    state.best_rank = None
    state.best_match_id = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuous guarded self-improvement orchestrator")
    parser.add_argument("agent_id")
    parser.add_argument("--module", default="my_agent.py")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--refactor-model", default="gpt-4.1-mini")
    parser.add_argument("--max-matches", type=int, default=0, help="0 means continuous")
    parser.add_argument("--minimum-score", type=int, default=10)
    parser.add_argument("--rollback-score-drop", type=int, default=0)
    parser.add_argument("--minimum-budget-left", type=float, default=5.0)
    parser.add_argument("--stability-samples", type=int, default=8)
    parser.add_argument("--stability-timeout", type=float, default=6.0)
    parser.add_argument("--maximum-latency", type=float, default=2.5)
    parser.add_argument("--gate-retry-seconds", type=float, default=30.0)
    parser.add_argument("--top-rank-hold-rank", type=int, default=2)
    parser.add_argument("--top-rank-hold-seconds", type=float, default=480.0)
    parser.add_argument("--top-rank-poll-seconds", type=float, default=30.0)
    parser.add_argument("--audit-attempts", type=int, default=6)
    parser.add_argument("--audit-retry-seconds", type=float, default=5.0)
    parser.add_argument("--state-dir", default=".orchestrator")
    parser.add_argument("--no-refactor", action="store_true")
    parser.add_argument("--playtest-candidates", action="store_true")
    parser.add_argument("--adopt-current", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Run gates and validation without a match")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    agent_path = (PROJECT_ROOT / args.module).resolve()
    if not agent_path.exists():
        raise OrchestratorError(f"Agent module not found: {agent_path}")
    if PROJECT_ROOT not in agent_path.parents:
        raise OrchestratorError("Agent module must be inside the project")

    state_dir = (PROJECT_ROOT / args.state_dir).resolve()
    state_path = state_dir / "state.json"
    event_path = state_dir / "events.jsonl"
    snapshot_dir = state_dir / "snapshots"
    candidate_dir = state_dir / "candidates"
    log_dir = state_dir / "logs"
    state = load_state(state_path)
    if args.adopt_current:
        adopt_current(agent_path, state, snapshot_dir)
    else:
        initialize_state(agent_path, state, snapshot_dir)
    compile_file(agent_path)
    save_state(state_path, state)

    completed_this_run = 0
    while args.max_matches == 0 or completed_this_run < args.max_matches:
        try:
            hold_if_top_ranked(
                args.server,
                args.agent_id,
                state,
                state_path,
                event_path,
                args.top_rank_hold_rank,
                args.top_rank_hold_seconds,
                args.top_rank_poll_seconds,
                args.stability_timeout,
                args.dry_run,
            )
        except Exception as exc:
            print(f"Leaderboard gate unavailable: {exc}")
            append_jsonl(
                event_path,
                {"at": utc_now(), "type": "leaderboard_error", "error": str(exc)},
            )
            if args.dry_run:
                return 4
            time.sleep(args.gate_retry_seconds)
            continue

        stability = probe_stability(
            args.server,
            args.agent_id,
            args.stability_samples,
            args.stability_timeout,
            args.maximum_latency,
        )
        append_jsonl(event_path, {"at": utc_now(), "type": "stability", **asdict(stability)})
        print("STABILITY", json.dumps(asdict(stability), sort_keys=True))
        if not stability.passed:
            if args.dry_run:
                return 2
            time.sleep(args.gate_retry_seconds)
            continue

        try:
            budget = read_gateway_budget()
        except Exception as exc:
            print(f"Budget gate unavailable: {exc}")
            append_jsonl(
                event_path,
                {"at": utc_now(), "type": "budget_error", "error": str(exc)},
            )
            if args.dry_run:
                return 3
            time.sleep(args.gate_retry_seconds)
            continue
        append_jsonl(event_path, {"at": utc_now(), "type": "budget", **budget})
        if not budget.get("available"):
            raise OrchestratorError("Gateway budget is unavailable; refusing a scored launch")
        if budget.get("blocked"):
            raise OrchestratorError("Gateway key is blocked")
        if budget.get("available") and budget.get("left", 0.0) < args.minimum_budget_left:
            raise OrchestratorError(
                f"Budget gate stopped the loop at ${budget.get('left', 0.0):.2f} remaining"
            )

        if args.dry_run:
            print("Dry-run gates passed; no match launched.")
            return 0

        run_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        terminal = stream_one_match(
            args.agent_id,
            agent_path,
            args.server,
            args.model,
            args.temperature,
            log_dir / f"{run_tag}.log",
        )
        if not terminal.watch_token:
            raise OrchestratorError("Runner did not emit a watch token; cannot audit safely")
        metrics, audit = audit_latest_match_with_retry(
            args.server,
            args.agent_id,
            terminal.watch_token,
            state.last_match_id,
            terminal,
            args.audit_attempts,
            args.audit_retry_seconds,
        )
        state.matches_run += 1
        completed_this_run += 1
        state.last_match_id = metrics.game_id
        append_jsonl(
            event_path,
            {
                "at": utc_now(),
                "type": "match",
                "metrics": asdict(metrics),
                "audit": audit,
                "terminal": {
                    "exit_code": terminal.exit_code,
                    "rounds_seen": terminal.rounds_seen,
                    "transport_failures": terminal.transport_failures,
                    "authorization_failures": terminal.authorization_failures,
                    "budget_left": terminal.budget_left,
                },
            },
        )
        print("MATCH", json.dumps(asdict(metrics), sort_keys=True))

        candidate_rolled_back = False
        if state.pending_candidate:
            if metrics.infrastructure_failure:
                print("Candidate evaluation deferred: infrastructure failure detected.")
            elif is_degraded(metrics, state, args.rollback_score_drop):
                print("Candidate degraded performance; rolling back to best snapshot.")
                rollback(agent_path, state)
                candidate_rolled_back = True
            else:
                print("Candidate accepted; promoting current source.")
                promote_current(agent_path, state, snapshot_dir, metrics)
        elif not metrics.infrastructure_failure and (
            state.best_score is None
            or (
                metrics.penalties
                <= (state.best_penalties if state.best_penalties is not None else metrics.penalties)
                and metrics.score > state.best_score
            )
        ):
            promote_current(agent_path, state, snapshot_dir, metrics)

        if metrics.infrastructure_failure:
            print("Infrastructure failure detected; pausing without strategy edits.")
            save_state(state_path, state)
            time.sleep(args.gate_retry_seconds)
            continue

        if (
            not args.no_refactor
            and not candidate_rolled_back
            and state.pending_candidate is None
            and should_refactor(metrics, args.minimum_score)
        ):
            try:
                candidate, analysis = generate_candidate(
                    agent_path,
                    metrics,
                    audit,
                    terminal,
                    args.refactor_model,
                )
                install_candidate(
                    agent_path,
                    candidate,
                    analysis,
                    state,
                    candidate_dir,
                    args.playtest_candidates,
                    args.model,
                )
                append_jsonl(
                    event_path,
                    {"at": utc_now(), "type": "candidate", "analysis": analysis},
                )
                print(f"Candidate installed for next-match evaluation: {analysis}")
            except Exception as exc:
                append_jsonl(
                    event_path,
                    {"at": utc_now(), "type": "candidate_rejected", "error": str(exc)},
                )
                print(f"Candidate rejected safely: {exc}")

        save_state(state_path, state)
        if terminal.stop_requested:
            print("Graceful stop completed; orchestrator will not launch another match.")
            return 0

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OrchestratorError as exc:
        print(f"ORCHESTRATOR STOPPED: {exc}", file=sys.stderr)
        raise SystemExit(1)
