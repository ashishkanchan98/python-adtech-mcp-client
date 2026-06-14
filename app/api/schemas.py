from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, field_serializer


class QueryRequest(BaseModel):
    query: str = Field(..., description="The support question or issue description")
    advertiser_id: Optional[str] = Field(None, alias="advertiserId")
    campaign_id: Optional[str] = Field(None, alias="campaignId")
    segment_id: Optional[str] = Field(None, alias="segmentId")
    account_tier: Optional[str] = Field(None, alias="accountTier")
    thread_id: Optional[str] = Field(None, alias="threadId")
    human_interrupt: bool = Field(False, alias="humanInterrupt")

    model_config = {"populate_by_name": True}


class QueryResponse(BaseModel):
    # Present when graph completes normally
    ticket_id: Optional[str] = Field(None, alias="ticketId")
    answer: Optional[str] = None
    tools_used: list[str] = Field(default_factory=list, alias="toolsUsed")
    rounds: int = 0
    # Always present
    status: str
    thread_id: Optional[str] = Field(None, alias="threadId")
    # Present only when status == "AWAITING_APPROVAL"
    pending_tools: list[str] = Field(default_factory=list, alias="pendingTools")

    model_config = {"populate_by_name": True}


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., alias="threadId")
    approved: bool = True
    query: Optional[str] = None
    advertiser_id: Optional[str] = Field(None, alias="advertiserId")
    campaign_id: Optional[str] = Field(None, alias="campaignId")

    model_config = {"populate_by_name": True}


class TicketResponse(BaseModel):
    id: str
    query: str
    advertiser_id: Optional[str] = Field(None, alias="advertiserId")
    campaign_id: Optional[str] = Field(None, alias="campaignId")
    answer: str
    tools_used: list[str] = Field(..., alias="toolsUsed")
    status: str
    created_at: datetime = Field(..., alias="createdAt")

    model_config = {"populate_by_name": True}

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        # Always emit UTC ISO string with Z suffix so browsers parse it correctly
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # trim to milliseconds + Z


class HealthResponse(BaseModel):
    status: str
    service: str
    llm_provider: str = Field(..., alias="llmProvider")
    kb_provider: str = Field(..., alias="kbProvider")
    tools_loaded: int = Field(..., alias="toolsLoaded")
    mcp_server_url: str = Field(..., alias="mcpServerUrl")

    model_config = {"populate_by_name": True}
