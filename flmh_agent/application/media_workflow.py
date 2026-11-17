from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from flmh_agent.domain.media_workflow import MediaWorkflowRequest, MediaWorkflowState
from flmh_agent.ports.workflow import MediaWorkflowPort


def build_media_workflow(tools: MediaWorkflowPort):
    async def validate_request(state: MediaWorkflowState) -> MediaWorkflowState:
        request = MediaWorkflowRequest.model_validate(state["request"])
        if request.mock_execution_confirmed is not True:
            raise ValueError("explicit confirmation is required for the zero-cost media state simulation")
        capabilities = dict(await tools.media_workflow_capabilities())
        if capabilities.get("executionMode") != "MOCK_ONLY" or capabilities.get("producesMediaAssets") is not False:
            raise ValueError("this workflow only supports the local media state simulation with no assets")
        return {"request": request.model_dump(), "capabilities": capabilities}

    async def start_run(state: MediaWorkflowState) -> MediaWorkflowState:
        request = state["request"]
        created = dict(await tools.start_media_workflow(
            {"product_id": request["product_id"], "prompt": request["prompt"], "mock_execution_confirmed": True},
            request["idempotency_key"],
        ))
        if created.get("executionMode") != "MOCK_ONLY" or not created.get("runId"):
            raise ValueError("media workflow service returned an invalid local run")
        return {"created": created}

    async def simulate_nodes(state: MediaWorkflowState) -> MediaWorkflowState:
        return {"result": dict(await tools.simulate_media_workflow(state["created"]["runId"]))}

    async def read_result(state: MediaWorkflowState) -> MediaWorkflowState:
        result = dict(await tools.get_media_workflow(state["created"]["runId"]))
        if result.get("executionMode") != "MOCK_ONLY" or result.get("status") != "MOCKED" or result.get("artifacts") != []:
            raise ValueError("media state simulation has not reached a verified empty-artifact terminal result")
        nodes = result.get("nodes", [])
        if len(nodes) != 2 or {node.get("nodeKey") for node in nodes} != {"image", "video"}:
            raise ValueError("media state simulation is missing its image or video node")
        for node in nodes:
            outcome = node.get("outputSnapshot") or {}
            if (node.get("status") != "MOCKED" or outcome.get("artifactCreated") is not False
                    or "outputUrl" not in outcome or outcome["outputUrl"] is not None):
                raise ValueError("media node must report explicit mock completion without a generated asset")
        return {"result": result, "summary": "媒体状态模拟已完成；图片和视频节点均为 MOCKED，未生成文件或 URL，也未产生供应商费用。"}

    graph = StateGraph(MediaWorkflowState)
    graph.add_node("validate_request", validate_request)
    graph.add_node("start_run", start_run)
    graph.add_node("simulate_nodes", simulate_nodes)
    graph.add_node("read_result", read_result)
    graph.add_edge(START, "validate_request")
    graph.add_edge("validate_request", "start_run")
    graph.add_edge("start_run", "simulate_nodes")
    graph.add_edge("simulate_nodes", "read_result")
    graph.add_edge("read_result", END)
    return graph.compile()
