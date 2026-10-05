"""
The Email Game – your agent. Edit this file.

This is a WORKING agent: it plays a complete game as-is, so you can run it
before changing anything and see what the game looks like. Then start editing.

    python scripts/run_custom_agent.py <your-agent-name> --module my_agent.py --server https://play.theemailgame.com

There is one leaderboard and no practice mode, so every game you play is scored.
Read this file before you connect.

Your class must be named CustomAgent and must subclass BaseAgent.

The runner also accepts (all optional, they stack with your code):
    --prompt my_prompt.md   system prompt for any LLM calls (default: docs/agent_prompt.md)
    --model gpt-4.1         which OpenAI model to use         (default: gpt-4.1)
    --temperature 0.7       LLM randomness 0.0-2.0            (default: 1.0)
See the "Customizing Agents" section of the README for the full rundown.

IMPORTANT - data/sample_agents.json is SAMPLE data and will NOT match the real
game. The round-2+ fuzzy descriptions the live server uses are private and are
not shipped at all, so there is no lookup table to read. Resolve them by
reasoning from the message history you actually receive each round.
"""

import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.base_agent import BaseAgent


class CustomAgent(BaseAgent):
    """
    Your custom The Email Game agent.

    The key method to override is on_message_batch(messages). It is called
    every time a new batch of emails arrives. By default it forwards them to
    an LLM. Override it to replace or augment that behavior with your own logic.

    Available action methods (inherited from BaseAgent):
        self.send_message(to_agent, subject, body)
        self.sign_message(message, for_agent)         -> signed_message dict
        self.sign_and_respond(to_agent, message_to_sign, response_body, subject)
        self.submit_signature(signed_message)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.round_number = 0
        self.assigned_message: str = ""
        self.request_list: List[str] = []
        self.authorization_entries: List[str] = []
        self.authorized_agents: Set[str] = set()
        self.previous_authorized_agents: Set[str] = set()

        # Keep lightweight in-game memory for fuzzy-description resolution.
        self.known_agents: Set[str] = set()
        self.agent_claimed_messages: Dict[str, List[str]] = {}

        # Round-level dedupe.
        self._sent_requests_this_round: Set[str] = set()
        self._processed_signature_markers: Set[str] = set()
        self._signed_requests_this_round: Set[tuple[str, str]] = set()
        self._declined_requests_this_round: Set[tuple[str, str]] = set()

    def on_new_game(self) -> None:
        """Called at the start of each new game (round 1).

        In the live ladder your agent is reused for many back-to-back games.
        The built-in LLM context is reset for you automatically; reset any state
        YOU track here so it does not leak between games. If you skip this, your
        own memory accumulates every game (growing cost if you feed it to an LLM,
        and stale data from previous games).
        """
        self.round_number = 0
        self.assigned_message = ""
        self.request_list = []
        self.authorization_entries = []
        self.authorized_agents = set()
        self.previous_authorized_agents = set()
        self.known_agents = set()
        self.agent_claimed_messages = {}
        self._sent_requests_this_round = set()
        self._processed_signature_markers = set()
        self._signed_requests_this_round = set()
        self._declined_requests_this_round = set()

    def on_message_batch(self, messages: List[Dict]) -> None:
        """
        Called with each batch of fresh incoming emails.

        Each message dict has these keys:
            from        — sender agent ID (or "moderator")
            to          — your agent ID
            subject     — email subject
            body        — email body text
            message_id  — unique ID
            timestamp   — ISO-8601 string

        The default implementation forwards to the LLM. Replace or extend it
        with your own logic. Call super().on_message_batch(messages) to keep
        LLM behavior for messages you don't handle yourself.

        Example: handle moderator messages manually, fall back to LLM for the rest.
        """
        # Process moderator messages first so current round state is ready.
        for msg in [m for m in messages if m.get("from") == "moderator"]:
            self._handle_moderator_message(msg)

        for msg in [m for m in messages if m.get("from") != "moderator"]:
            self._handle_peer_message(msg)

    def _handle_peer_message(self, message: Dict) -> None:
        from_agent = message.get("from", "")
        if not from_agent:
            return

        self.known_agents.add(from_agent)
        body = message.get("body", "")
        subject = message.get("subject", "").lower()

        # The moderator initially names only two peers. When the third opponent
        # first contacts us, immediately ask for their signature too; any agent
        # may sign our message and every valid submission scores.
        if (
            self.assigned_message
            and from_agent not in {"moderator", "system_reminder"}
            and from_agent not in self._sent_requests_this_round
        ):
            self._send_signature_request(from_agent)

        # 1) Always harvest and submit signatures addressed to us.
        self._process_signed_message_if_present(body, from_agent)

        # Replies often quote our original request. They are not new requests.
        if self.extract_signed_message_from_email(body) is not None or any(
            marker in subject
            for marker in ("signed message", "declin", "cannot sign", "unable to sign")
        ) or (subject.startswith("re:") and "signature request" in subject):
            return

        # 2) If this is a signature request, decide whether to sign.
        requested_message = self._extract_requested_message(body)
        if requested_message is None:
            return

        self._remember_agent_message(from_agent, requested_message)

        if self._is_authorized_requester(from_agent):
            request_key = (from_agent, requested_message)
            if request_key in self._signed_requests_this_round:
                print(f"[{self.agent_id}] duplicate signing request ignored for {from_agent}")
                return
            self._signed_requests_this_round.add(request_key)
            print(f"[{self.agent_id}] signing for authorized requester {from_agent}")
            result = self.sign_and_respond(
                to_agent=from_agent,
                message_to_sign=requested_message,
                response_body=(
                    f"Signed as requested for round {self.round_number}."
                ),
                subject=f"Signed Message (Round {self.round_number})",
            )
            if not result.get("success"):
                print(f"[{self.agent_id}] signing send failed for {from_agent}; retrying once")
                result = self.sign_and_respond(
                    to_agent=from_agent,
                    message_to_sign=requested_message,
                    response_body=f"Signed as requested for round {self.round_number}.",
                    subject=f"Signed Message (Round {self.round_number})",
                )
                if not result.get("success"):
                    self._signed_requests_this_round.discard(request_key)
                    print(f"[{self.agent_id}] signing failed for {from_agent}: {result}")
            return

        # Declining unauthorized requests avoids penalties.
        decline_key = (from_agent, requested_message)
        if decline_key in self._declined_requests_this_round:
            print(f"[{self.agent_id}] duplicate unauthorized request ignored for {from_agent}")
            return
        self._declined_requests_this_round.add(decline_key)
        print(f"[{self.agent_id}] declining unauthorized requester {from_agent}")
        result = self.send_message(
            to_agent=from_agent,
            subject=f"Decline: Signature request (Round {self.round_number})",
            body=(
                "I cannot sign this request because you are not on my current "
                "authorization list."
            ),
        )
        if not result.get("success"):
            self._declined_requests_this_round.discard(decline_key)

    def _handle_moderator_message(self, message: Dict) -> None:
        """
        Example: parse a moderator message and act on it directly without the LLM.
        Remove this and call super().on_message_batch([message]) to use LLM instead.
        """
        body = message.get("body", "")
        # Track the round from the moderator's own **ROUND N** marker - never
        # a blind counter. The moderator also sends informational mail (e.g.
        # scoring rejections), which must not advance the round.
        _rm = re.search(r"\*\*ROUND\s+(\d+)\*\*", body, re.IGNORECASE)
        if _rm:
            parsed_round = int(_rm.group(1))
            if parsed_round != self.round_number:
                self._start_new_round(parsed_round)

        # Parse your assigned message. The moderator format (see the round
        # instructions you actually receive) is:
        #   You must get signatures for this EXACT message: "<message>"
        assigned_message = self._parse_assigned_message(body)

        if not assigned_message:
            # Informational moderator message; no action required.
            return

        self.assigned_message = assigned_message
        print(f"[{self.agent_id}] Round {self.round_number} — assigned: {self.assigned_message}")

        # Parse your request list. The moderator format is a single line:
        #   1. You must REQUEST signatures from these agents: alice, bob
        # (comma-separated agent ids after the colon).
        self.request_list = self._parse_request_list(body)
        self.authorization_entries = self._parse_authorization_entries(body)
        self.authorized_agents = self._resolve_authorized_agents(self.authorization_entries)

        self.known_agents.update(self.request_list)
        self.known_agents.update(self.authorized_agents)

        # Anyone may sign our message and every valid signature scores, so ask
        # every identified opponent rather than stopping at the required list.
        collection_targets = self.known_agents | set(self.request_list)
        collection_targets.discard(self.agent_id)
        collection_targets.discard("moderator")
        collection_targets.discard("system_reminder")
        for agent_id in sorted(collection_targets):
            if agent_id in self._sent_requests_this_round:
                continue
            self._send_signature_request(agent_id)

    def _send_signature_request(self, agent_id: str) -> None:
        self._sent_requests_this_round.add(agent_id)
        subject = f"Signature Request (Round {self.round_number})"
        body = (
            f"Please sign this message for me: {self.assigned_message}\n\n"
            f"If your Round {self.round_number} authorization includes "
            f"{self.agent_id}, please use sign_and_respond now and return the "
            "SIGNED_MESSAGE_JSON. I will sign your request whenever my "
            "authorization permits."
        )
        result = self.send_message(to_agent=agent_id, subject=subject, body=body)
        if not result.get("success"):
            print(f"[{self.agent_id}] request failed for {agent_id}; retrying once")
            result = self.send_message(to_agent=agent_id, subject=subject, body=body)
        if not result.get("success"):
            self._sent_requests_this_round.discard(agent_id)
            print(f"[{self.agent_id}] request failed for {agent_id}: {result}")

    def _start_new_round(self, new_round: int) -> None:
        if self.round_number > 0 and new_round != self.round_number:
            self.previous_authorized_agents = set(self.authorized_agents)

        self.round_number = new_round
        self.assigned_message = ""
        self.request_list = []
        self.authorization_entries = []
        self.authorized_agents = set()
        self._sent_requests_this_round = set()
        self._processed_signature_markers = set()
        self._signed_requests_this_round = set()
        self._declined_requests_this_round = set()
        # Scoring deduplication is round-scoped, but BaseAgent's cache is not.
        self._submitted_signature_keys.clear()

    def _parse_assigned_message(self, body: str) -> str:
        m = re.search(r'EXACT\s+message:\s*"([^"]+)"', body, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        for line in body.splitlines():
            if "exact message:" in line.lower():
                return line.split(":", 1)[1].strip().strip('"')
        return ""

    def _parse_request_list(self, body: str) -> List[str]:
        for line in body.splitlines():
            if "request signatures from these agents:" in line.lower():
                names = line.split(":", 1)[1]
                return [n.strip() for n in names.split(",") if n.strip() and n.strip().lower() != "none"]
        return []

    def _parse_authorization_entries(self, body: str) -> List[str]:
        for line in body.splitlines():
            if "authorized to sign messages for these agents:" in line.lower():
                raw = line.split(":", 1)[1].strip()
                if not raw or raw.lower() == "none":
                    return []
                suffix = r"\(from last round; their message this round may be different\)"
                entry_pattern = re.compile(
                    rf"(?:([A-Za-z0-9_\-]+)|(.+?{suffix}))(?:,\s*|$)",
                    re.IGNORECASE,
                )
                entries = [
                    (match.group(1) or match.group(2)).strip()
                    for match in entry_pattern.finditer(raw)
                ]
                return entries
        return []

    def _resolve_authorized_agents(self, entries: List[str]) -> Set[str]:
        explicit: Set[str] = set()
        unresolved: List[str] = []

        for entry in entries:
            maybe_agent = self._extract_explicit_agent_id(entry)
            if maybe_agent:
                explicit.add(maybe_agent)
                continue
            unresolved.append(entry)

        resolved = set(explicit)
        if unresolved:
            candidates = sorted(
                agent for agent in self.known_agents
                if agent not in explicit
                and agent not in {self.agent_id, "moderator", "system_reminder"}
            )
            resolved.update(self._resolve_fuzzy_entries(unresolved, candidates))

        if len(resolved) != len(entries):
            print(
                f"[{self.agent_id}] authorization resolution failed closed: "
                f"resolved {len(resolved)} of {len(entries)} entries"
            )
            return explicit

        if resolved:
            print(f"[{self.agent_id}] authorized this round: {sorted(resolved)}")
        return resolved

    def _extract_explicit_agent_id(self, text: str) -> Optional[str]:
        # Remove common alias suffix from moderator fuzzy text.
        clean = re.sub(r"\s*\(from last round.*$", "", text, flags=re.IGNORECASE).strip()
        if not clean:
            return None

        # Treat single-token identifiers as explicit names.
        if re.fullmatch(r"[A-Za-z0-9_\-]+", clean):
            return clean
        return None

    def _resolve_fuzzy_entries(self, entries: List[str], candidates: List[str]) -> Set[str]:
        if not entries or not candidates:
            return set()

        # Try a free deterministic lexical match first.
        lexical_matches: List[Optional[str]] = [
            self._resolve_fuzzy_by_text(entry, candidates) for entry in entries
        ]
        if all(lexical_matches) and len(set(lexical_matches)) == len(entries):
            return {match for match in lexical_matches if match}

        candidate_history = {
            agent: self.agent_claimed_messages.get(agent, [])
            for agent in candidates
            if self.agent_claimed_messages.get(agent)
        }
        if not candidate_history:
            return set()

        if len(entries) == 1:
            match = self._resolve_single_fuzzy_with_llm(
                entries[0], candidates, candidate_history
            )
            return {match} if match else set()

        prompt = (
            "Match each paraphrased description to the agent whose prior message it "
            "describes. Return ONLY a JSON array of agent IDs in description order. "
            "Use each agent at most once.\nDescriptions:\n"
            + json.dumps(entries, ensure_ascii=True)
            + "\nCandidate prior messages:\n"
            + json.dumps(candidate_history, ensure_ascii=True)
        )
        try:
            response = self.driver._openai_client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "You perform precise semantic entity matching and output JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=80,
            )
            raw = (response.choices[0].message.content or "").strip()
            matches = json.loads(raw)
        except Exception as exc:
            print(f"[{self.agent_id}] fuzzy authorization resolution failed closed: {exc}")
            return set()

        if not isinstance(matches, list) or len(matches) != len(entries):
            print(f"[{self.agent_id}] fuzzy authorization resolution returned invalid count")
            return set()
        if any(not isinstance(agent, str) or agent not in candidates for agent in matches):
            print(f"[{self.agent_id}] fuzzy authorization resolution returned unknown agent")
            return set()
        if len(set(matches)) != len(matches):
            print(f"[{self.agent_id}] fuzzy authorization resolution returned duplicates")
            return set()
        return set(matches)

    def _resolve_single_fuzzy_with_llm(
        self,
        entry: str,
        candidates: List[str],
        candidate_history: Dict[str, List[str]],
    ) -> Optional[str]:
        prompt = (
            "Identify the one agent whose prior message is paraphrased by the "
            "description. Return ONLY the agent ID as a JSON string.\nDescription:\n"
            + json.dumps(entry, ensure_ascii=True)
            + "\nAllowed agent IDs:\n"
            + json.dumps(candidates, ensure_ascii=True)
            + "\nCandidate prior messages:\n"
            + json.dumps(candidate_history, ensure_ascii=True)
        )
        try:
            response = self.driver._openai_client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Resolve one semantic identity and output one JSON string only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=30,
            )
            match = json.loads((response.choices[0].message.content or "").strip())
        except Exception as exc:
            print(f"[{self.agent_id}] fuzzy authorization resolution failed closed: {exc}")
            return None

        if not isinstance(match, str) or match not in candidates:
            print(f"[{self.agent_id}] fuzzy authorization resolution returned unknown agent")
            return None
        return match

    def _resolve_fuzzy_by_text(self, fuzzy: str, candidates: List[str]) -> Optional[str]:
        if not candidates:
            return None

        fuzzy_tokens = self._tokenize(fuzzy)
        best_agent = None
        best_score = 0.0
        second_best = 0.0

        for agent in candidates:
            texts = self.agent_claimed_messages.get(agent, [])
            if not texts:
                continue
            score = max(self._jaccard(fuzzy_tokens, self._tokenize(t)) for t in texts)
            if score > best_score:
                second_best = best_score
                best_score = score
                best_agent = agent
            elif score > second_best:
                second_best = score

        # Require a clear margin to avoid unauthorized-sign penalties.
        if best_agent and best_score >= 0.30 and (best_score - second_best) >= 0.15:
            return best_agent
        return None

    def _tokenize(self, text: str) -> Set[str]:
        stop = {
            "the", "a", "an", "and", "or", "to", "for", "of", "in", "on",
            "from", "last", "round", "their", "this", "that", "with", "who",
            "agent", "message", "may", "be", "different"
        }
        toks = re.findall(r"[a-z0-9]+", text.lower())
        return {t for t in toks if t not in stop}

    def _jaccard(self, a: Set[str], b: Set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        return inter / union if union else 0.0

    def _extract_requested_message(self, body: str) -> Optional[str]:
        # Structured protocol used by some deterministic competitors.
        protocol_match = re.search(
            r"^\s*Exact\s+text\s*:\s*(.+?)\s*$",
            body,
            re.IGNORECASE | re.MULTILINE,
        )
        if protocol_match:
            msg = protocol_match.group(1).strip()
            if len(msg) >= 2 and msg[0] == msg[-1] and msg[0] in "\"'":
                msg = msg[1:-1]
            if msg:
                return msg

        # Multiline request where the exact text is on the next non-empty line.
        multiline_match = re.search(
            r"please\s+sign\s+this\s+EXACT\s+message\s+for\s+me\s*:\s*"
            r"(?:\r?\n\s*)+[\"']?([^\r\n]+?)[\"']?\s*(?:\r?\n|$)",
            body,
            re.IGNORECASE,
        )
        if multiline_match:
            msg = multiline_match.group(1).strip().strip('"\'')
            if msg:
                return msg

        patterns = (
            r"please\s+sign\s+this\s+message\s+for\s+me\s*:\s*(.+)$",
            r"please\s+sign\s+(?:my|this|the)\s+(?:assigned\s+)?message\s*:\s*(.+)$",
            r"sign\s+(?:my|this|the)\s+(?:assigned\s+)?message\s*:\s*(.+)$",
            r"message\s+to\s+sign\s*:\s*(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
            if not match:
                continue
            msg = match.group(1).strip().strip('"')
            if msg:
                return msg

        return None

    def _remember_agent_message(self, agent_id: str, message_text: str) -> None:
        bucket = self.agent_claimed_messages.setdefault(agent_id, [])
        if message_text not in bucket:
            bucket.append(message_text)

    def _process_signed_message_if_present(self, body: str, from_agent: str) -> None:
        marker = "SIGNED_MESSAGE_JSON:"
        if marker not in body:
            return

        raw = body.split(marker, 1)[1].strip()
        if raw in self._processed_signature_markers:
            return
        self._processed_signature_markers.add(raw)

        signed = self.extract_signed_message_from_email(body)
        if not signed:
            return

        original = signed.get("original_message", "")
        signed_for = signed.get("signed_for", "")
        signer = signed.get("signer", from_agent)

        self._remember_agent_message(signer, original)

        # Submit only signatures that can score for us this round.
        if signed_for != self.agent_id:
            return
        if not self.assigned_message:
            return
        if original != self.assigned_message:
            return

        result = self.submit_signature(signed)
        if not result.get("success"):
            print(f"[{self.agent_id}] submit failed for {signer}; retrying once")
            result = self.submit_signature(signed)
            if not result.get("success"):
                print(f"[{self.agent_id}] submit failed for {signer}: {result}")
                return
        print(f"[{self.agent_id}] submitted signature from {signer}")

    def _is_authorized_requester(self, requester: str) -> bool:
        return requester in self.authorized_agents
