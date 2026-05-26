import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

load_dotenv()

import ai
import notion
import scraper


@asynccontextmanager
async def lifespan(app: FastAPI):
    required = ["NOTION_TOKEN", "NOTION_DB_ID", "ANTHROPIC_API_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"환경 변수 누락: {', '.join(missing)}")
    yield


app = FastAPI(title="Link Collector", lifespan=lifespan)


class SaveRequest(BaseModel):
    url: HttpUrl


class SaveResponse(BaseModel):
    status: str
    notion_page_id: str
    title: str
    summary: str
    tags: list[str]
    type: str
    source: str


@app.post("/save", response_model=SaveResponse)
async def save_link(req: SaveRequest):
    url = str(req.url)

    source = scraper.detect_source(url)

    try:
        scraped = scraper.scrape(url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"스크래핑 실패: {e}")

    try:
        analysis = ai.analyze(url, scraped["text"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI 분석 실패: {e}")

    try:
        page_id = notion.save(
            url=url,
            source=source,
            title=analysis["title"],
            summary=analysis["summary"],
            type_=analysis["type"],
            tags=analysis["tags"],
            location=analysis.get("location", ""),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"노션 저장 실패: {e}")

    return SaveResponse(
        status="ok",
        notion_page_id=page_id,
        title=analysis["title"],
        summary=analysis["summary"],
        tags=analysis["tags"],
        type=analysis["type"],
        source=source,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
