import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def get_llm(settings) -> BaseChatModel:
    """Factory — returns Groq or Vertex AI chat model based on LLM_PROVIDER setting."""
    if settings.llm_provider == "vertex":
        from langchain_google_vertexai import ChatVertexAI
        logger.info("LLM: Vertex AI  model=%s  project=%s",
                    settings.vertex_model, settings.vertex_project_id)
        return ChatVertexAI(
            model_name=settings.vertex_model,
            project=settings.vertex_project_id,
            location=settings.vertex_location,
            temperature=0.2,
            max_output_tokens=2048,
        )
    else:
        from langchain_groq import ChatGroq
        logger.info("LLM: Groq  model=%s", settings.groq_model)
        return ChatGroq(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            temperature=0.2,
        )


def build_agent_graph(llm: BaseChatModel, tools: list, max_tool_calls: int = 6, human_interrupt: bool = False):
    """
    Compile the LangGraph ReAct agent.

    Graph topology:
      START -> [agent] -> (has tool_calls?) -> [tools] -> [agent] -> ...
                                           -> END
    """
    llm_with_tools = llm.bind_tools(tools)

    # ToolNode handles parallel execution natively — same as @Async + CompletableFuture
    tool_node = ToolNode(tools)

    def call_model(state: MessagesState) -> dict:
        """Node: inject system prompt and call the LLM."""
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm_with_tools.invoke(messages)
        tool_call_count = len(response.tool_calls) if response.tool_calls else 0
        logger.info("LLM response: tool_calls=%d  has_text=%s",
                    tool_call_count, bool(response.content))
        return {"messages": [response]}

    def should_continue(state: MessagesState) -> str:
        """Conditional edge: loop to tools or exit to END."""
        last = state["messages"][-1]

        if not (isinstance(last, AIMessage) and last.tool_calls):
            return END

        # Count completed tool rounds (AI messages that had tool_calls)
        rounds_done = sum(
            1 for m in state["messages"]
            if isinstance(m, AIMessage) and m.tool_calls
        )
        if rounds_done < max_tool_calls:
            return "tools"

        logger.warning("Max tool call rounds (%d) reached — forcing exit", max_tool_calls)
        return END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", tool_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    compile_kwargs = {"checkpointer": MemorySaver()}
    if human_interrupt:
        compile_kwargs["interrupt_before"] = ["tools"]
    compiled = graph.compile(**compile_kwargs)
    logger.info("LangGraph agent compiled  tools=%d  max_rounds=%d  human_interrupt=%s",
                len(tools), max_tool_calls, human_interrupt)
    return compiled


def extract_results(messages: list) -> tuple[str, list[str]]:
    """
    Walk the final message list and return:
      (final_answer, deduplicated_tools_used)
    """
    tools_used: list[str] = []
    final_answer: str = ""

    for msg in messages:
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "")
                    if name:
                        tools_used.append(name)
            elif msg.content:
                final_answer = msg.content if isinstance(msg.content, str) else str(msg.content)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique_tools = [t for t in tools_used if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]

    return final_answer, unique_tools
