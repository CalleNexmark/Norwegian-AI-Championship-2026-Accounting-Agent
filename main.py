"""FastAPI app — /solve and /health endpoints."""

import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

from agent import parse_prompt
from task_handlers import HANDLERS
from tripletex_client import TripletexClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Tripletex AI Agent")


class TripletexCredentials(BaseModel):
    base_url: str
    session_token: str


class FileAttachment(BaseModel):
    filename: str
    content_base64: str
    mime_type: str


class SolveRequest(BaseModel):
    prompt: str
    files: Optional[list[FileAttachment]] = None
    tripletex_credentials: TripletexCredentials


class SolveResponse(BaseModel):
    status: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/", response_model=SolveResponse)
@app.post("/solve", response_model=SolveResponse)
def solve(request: SolveRequest):
    creds = request.tripletex_credentials
    client = TripletexClient(creds.base_url, creds.session_token)

    logger.info(f"Received prompt: {request.prompt}")

    # 1. Parse intent with Gemini
    try:
        parsed = parse_prompt(request.prompt, request.files)
        logger.info(f"Parsed: {parsed}")
    except Exception as e:
        logger.error(f"LLM parse error: {e}")
        return SolveResponse(status="completed")

    task_type = parsed.get("task_type", "unknown")
    handler = HANDLERS.get(task_type)

    if not handler:
        logger.warning(f"No handler for task_type={task_type}")
        return SolveResponse(status="completed")

    # 2. Execute handler
    try:
        result = handler(parsed, client)
        logger.info(f"Handler result: {result}")
    except Exception as e:
        logger.error(f"Handler error for {task_type}: {e}")

    return SolveResponse(status="completed")
