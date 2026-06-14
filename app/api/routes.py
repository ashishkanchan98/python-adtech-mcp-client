import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from langchain_core.messages import HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import extract_results
from app.api.schemas import HealthResponse, QueryRequest, QueryResponse, ResumeRequest, TicketResponse
from app.config import settings
from app.db.database import get_db
from app.db.models import Ticket

router = APIRouter()
logger = logging.getLogger(__name__)


def _build_user_message(req: QueryRequest) -> str:
    """Inject context fields into the user message — same pattern as Java AgentOrchestrator."""
    parts = [req.query]
    ctx: list[str] = []
    if req.advertiser_id:
        ctx.append(f"Advertiser ID: {req.advertiser_id}")
    if req.campaign_id:
        ctx.append(f"Campaign ID: {req.campaign_id}")
    if req.segment_id:
        ctx.append(f"Segment ID: {req.segment_id}")
    if req.account_tier:
        ctx.append(f"Account Tier: {req.account_tier}")
    if ctx:
        parts.append("\n[Context]\n" + "\n".join(ctx))
    return "\n".join(parts)


def _determine_status(answer: str) -> str:
    keywords = ["escalat", "human review", "cannot resolve", "unable to determine"]
    return "ESCALATED" if any(k in answer.lower() for k in keywords) else "RESOLVED_BY_AGENT"


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("static/index.html") as f:
        return HTMLResponse(content=f.read())


@router.get("/support", response_class=HTMLResponse)
async def support():
    with open("static/support.html") as f:
        return HTMLResponse(content=f.read())


@router.get("/api/v1/support/health-check", response_model=HealthResponse)
async def health_check(request: Request):
    tools = getattr(request.app.state, "tools", [])
    return HealthResponse(
        status="UP",
        service="python-adtech-mcp-client",
        llm_provider=settings.llm_provider,
        kb_provider=settings.kb_provider,
        tools_loaded=len(tools),
        mcp_server_url=settings.mcp_server_url,
    )


@router.post("/api/v1/support/query", response_model=QueryResponse)
async def submit_query(
    body: QueryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Human interrupt mode uses a separate compiled graph (interrupt_before=["tools"])
    if body.human_interrupt:
        agent = getattr(request.app.state, "agent_hi", None)
    else:
        agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized — MCP server may be unreachable")

    user_text = _build_user_message(body)
    effective_thread_id = body.thread_id or str(uuid.uuid4())
    logger.info("Processing query: advertiser=%s campaign=%s thread_id=%s memory=%s interrupt=%s",
                body.advertiser_id, body.campaign_id, effective_thread_id,
                bool(body.thread_id), body.human_interrupt)

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_text)]},
        config={"configurable": {"thread_id": effective_thread_id}},
    )

    # Check if graph paused at tools node (human interrupt fired)
    if body.human_interrupt:
        state = await agent.aget_state({"configurable": {"thread_id": effective_thread_id}})
        if state.next and "tools" in state.next:
            last_msg = state.values["messages"][-1]
            pending = [tc["name"] for tc in last_msg.tool_calls] if (
                hasattr(last_msg, "tool_calls") and last_msg.tool_calls) else []
            logger.info("Graph paused — pending_tools=%s  thread_id=%s", pending, effective_thread_id)
            return QueryResponse(
                status="AWAITING_APPROVAL",
                thread_id=effective_thread_id,
                pending_tools=pending,
            )

    return await _save_and_return(result["messages"], body.query,
                                  body.advertiser_id, body.campaign_id,
                                  effective_thread_id, db)


async def _save_and_return(messages, query, advertiser_id, campaign_id, thread_id, db):
    """Extract results, persist ticket, return QueryResponse."""
    final_answer, tools_used = extract_results(messages)
    if not final_answer:
        final_answer = "Unable to process the query. Please try again or contact support."

    rounds = sum(1 for m in messages if hasattr(m, "tool_calls") and m.tool_calls)
    status = _determine_status(final_answer)
    ticket_id = str(uuid.uuid4())

    ticket = Ticket(
        id=ticket_id,
        query=query or "",
        advertiser_id=advertiser_id,
        campaign_id=campaign_id,
        answer=final_answer,
        tools_used=",".join(tools_used),
        status=status,
        created_at=datetime.utcnow(),
    )
    db.add(ticket)
    await db.commit()

    logger.info("Ticket %s saved  status=%s  tools=%s  rounds=%d",
                ticket_id, status, tools_used, rounds)

    return QueryResponse(
        ticket_id=ticket_id,
        answer=final_answer,
        tools_used=tools_used,
        status=status,
        rounds=rounds,
        thread_id=thread_id,
    )


@router.post("/api/v1/support/query/resume", response_model=QueryResponse)
async def resume_query(
    body: ResumeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    agent = getattr(request.app.state, "agent_hi", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    config = {"configurable": {"thread_id": body.thread_id}}

    if body.approved:
        logger.info("Human approved  thread_id=%s", body.thread_id)
        result = await agent.ainvoke(None, config=config)
    else:
        logger.info("Human rejected  thread_id=%s — injecting KB-only override", body.thread_id)
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="Skip tool execution. Answer using the knowledge base only.")]},
            config=config,
        )

    # Graph might pause again on a second tool round — return AWAITING_APPROVAL again
    state = await agent.aget_state(config)
    if state.next and "tools" in state.next:
        last_msg = state.values["messages"][-1]
        pending = [tc["name"] for tc in last_msg.tool_calls] if (
            hasattr(last_msg, "tool_calls") and last_msg.tool_calls) else []
        logger.info("Graph paused again — pending_tools=%s", pending)
        return QueryResponse(
            status="AWAITING_APPROVAL",
            thread_id=body.thread_id,
            pending_tools=pending,
        )

    return await _save_and_return(result["messages"], body.query,
                                  body.advertiser_id, body.campaign_id,
                                  body.thread_id, db)


@router.get("/api/v1/support/tickets", response_model=list[TicketResponse])
async def list_tickets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Ticket).order_by(Ticket.created_at.desc()))
    tickets = result.scalars().all()
    return [
        TicketResponse(
            id=t.id,
            query=t.query,
            advertiser_id=t.advertiser_id,
            campaign_id=t.campaign_id,
            answer=t.answer,
            tools_used=t.tools_used.split(",") if t.tools_used else [],
            status=t.status,
            created_at=t.created_at,
        )
        for t in tickets
    ]


@router.get("/api/v1/support/tickets/{ticket_id}", response_model=TicketResponse)
async def get_ticket(ticket_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    return TicketResponse(
        id=ticket.id,
        query=ticket.query,
        advertiser_id=ticket.advertiser_id,
        campaign_id=ticket.campaign_id,
        answer=ticket.answer,
        tools_used=ticket.tools_used.split(",") if ticket.tools_used else [],
        status=ticket.status,
        created_at=ticket.created_at,
    )
