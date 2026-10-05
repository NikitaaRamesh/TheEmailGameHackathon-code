import unittest
from types import SimpleNamespace

from my_agent import CustomAgent


class AgentRegressionTests(unittest.TestCase):
    def test_signature_json_placeholder_does_not_hide_request(self):
        agent = CustomAgent.__new__(CustomAgent)
        agent.agent_id = "nikitaa_r_r"
        agent.round_number = 1
        agent.assigned_message = "Our message."
        agent.known_agents = set()
        agent.authorized_agents = {"venmani_a_d"}
        agent.agent_claimed_messages = {}
        agent._sent_requests_this_round = {"venmani_a_d"}
        agent._processed_signature_markers = set()
        agent._signed_requests_this_round = set()
        agent._declined_requests_this_round = set()
        signed = []
        agent.sign_and_respond = lambda **kwargs: signed.append(kwargs) or {"success": True}
        agent.submit_signature = lambda payload: {"success": True}
        body = """Agent venmani_a_d requests a signature for Round 1.

Please sign this message for me: The school's flag always flies at half-mast, regardless of occasion.

---BEGIN MESSAGE---
The school's flag always flies at half-mast, regardless of occasion.
---END MESSAGE---

Sign it EXACTLY as written above - do not trim, rephrase, or correct it, or the signature will not verify.
Reply with SIGNED_MESSAGE_JSON:{...} and nothing else."""

        agent._handle_peer_message(
            {"from": "venmani_a_d", "subject": "Signature request - Round 1", "body": body}
        )

        self.assertEqual(1, len(signed))
        self.assertEqual(
            "The school's flag always flies at half-mast, regardless of occasion.",
            signed[0]["message_to_sign"],
        )

    def test_match_9_mixed_explicit_and_fuzzy_authorization(self):
        agent = CustomAgent.__new__(CustomAgent)
        agent.agent_id = "nikitaa_r_r"
        agent.known_agents = {"venmani_a_d", "glen_nfor", "raffi_ardhi_naufal"}
        agent.agent_claimed_messages = {
            "glen_nfor": ["The tailor stitches a single gold thread into every suit lining."],
            "raffi_ardhi_naufal": [
                "The school's flag always flies at half-mast, regardless of occasion."
            ],
        }
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='"raffi_ardhi_naufal"')
                )
            ]
        )
        completions = SimpleNamespace(create=lambda **kwargs: response)
        agent.driver = SimpleNamespace(
            _openai_client=SimpleNamespace(
                chat=SimpleNamespace(completions=completions)
            )
        )
        entries = [
            "venmani_a_d",
            "The agent who mentioned a gesture meant for solemnity becoming routine "
            "(from last round; their message this round may be different)",
        ]

        resolved = agent._resolve_authorized_agents(entries)

        self.assertEqual({"venmani_a_d", "raffi_ardhi_naufal"}, resolved)


if __name__ == "__main__":
    unittest.main()
