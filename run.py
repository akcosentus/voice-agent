"""Entry point — load an agent config and run it over LiveKit transport (for local testing).

Usage:
    python run.py --agent chris/claim_status --case 0
    python run.py --agent cindy/outbound_balance --case 0
"""

import argparse
import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
from pipecat.runner.livekit import configure

from core.config_loader import load_agent_config
from core.pipeline import build_pipeline, build_pipeline_components


async def main(agent_name: str, case_index: int):
    config = load_agent_config(agent_name)

    with open(config.case_data.path) as f:
        cases = json.load(f)
    case_data = cases[case_index]

    components = build_pipeline_components(config, case_data, transport_type="livekit")

    (url, token, room_name) = await configure()

    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    )

    bundle = build_pipeline(components, transport, config=config)

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant_id):
        print(f"Participant joined. {config.display_name} is listening...")
        if bundle.audiobuffer:
            await bundle.audiobuffer.start_recording()

    runner = PipelineRunner()
    await runner.run(bundle.task)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Cosentus voice agent (LiveKit)")
    parser.add_argument("--agent", required=True, help="Agent as category/variant (e.g. chris/claim_status)")
    parser.add_argument("--case", type=int, default=0, help="Case index in the data file")
    args = parser.parse_args()
    asyncio.run(main(args.agent, args.case))
