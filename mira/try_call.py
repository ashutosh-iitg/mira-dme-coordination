"""Run one mock call + real LLM extraction without Temporal.

uv run python -m mira.try_call --party lincoln --purpose supplier_screen --attempt 2
"""

import argparse
import asyncio
import sys

from mira.config import ConfigError, scenario_path
from mira.extraction import interpret_call
from mira.llm import LlmError
from mira.models import CallPurpose, CallRequest
from mira.voice.agent import MockVoiceAgent, load_scenario


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--party", required=True, help="case-insensitive substring of a scripted party name")
    parser.add_argument("--purpose", required=True, choices=[p.value for p in CallPurpose])
    parser.add_argument("--attempt", type=int, default=1)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    scenario = load_scenario(scenario_path())
    matches = [(phone, p) for phone, p in scenario.parties.items() if args.party.lower() in p.name.lower()]
    if len(matches) != 1:
        names = [p.name for _, p in matches] or [p.name for p in scenario.parties.values()]
        print(f"--party must match exactly one of: {names}", file=sys.stderr)
        return 2

    phone, party = matches[0]
    request = CallRequest(
        case_id="try-call",
        call_id=f"try-{args.purpose}-{args.attempt}",
        purpose=CallPurpose(args.purpose),
        party_name=party.name,
        phone=phone,
        attempt=args.attempt,
    )
    raw = await MockVoiceAgent(scenario).call(request)
    outcome = await interpret_call(request, raw)
    print(outcome.model_dump_json(indent=2))
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_run(_parse_args())))
    except (ConfigError, LlmError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
