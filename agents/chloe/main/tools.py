from pipecat.frames.frames import EndFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.services.llm_service import FunctionCallParams


async def end_call(params: FunctionCallParams, task: PipelineTask):
    """End the call gracefully after the LLM's goodbye finishes playing."""
    await params.result_callback({"status": "call_ended"})
    await task.queue_frame(EndFrame())
