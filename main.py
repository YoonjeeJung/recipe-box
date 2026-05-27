import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

load_dotenv()

import ai
import notion
import scraper

logger = logging.getLogger(__name__)


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
    warning: str = ""


@app.post("/save", response_model=SaveResponse)
async def save_link(req: SaveRequest):
    url = str(req.url)
    source = scraper.detect_source(url)
    warning = ""

    # 스크래핑 — 실패해도 URL만으로 저장 계속 진행
    scraped_text = ""
    cover_image_url = ""
    try:
        scraped = scraper.scrape(url)
        scraped_text = scraped.get("text", "")
        cover_image_url = scraped.get("cover_image_url", "")
        if scraped.get("error"):
            warning = f"스크래핑 부분 실패: {scraped['error']}"
            logger.warning("scrape partial failure url=%s error=%s", url, scraped["error"])
    except Exception as e:
        warning = f"스크래핑 실패, URL만 저장: {e}"
        logger.error("scrape failed url=%s error=%s", url, e)

    # AI 분석 — 텍스트가 없으면 URL만 전달, 실패 시 기본값으로 계속 진행
    analysis: dict = {"title": "", "summary": "", "type": "Other", "tags": [], "location": "", "ingredients": [], "steps": [], "restaurants": []}
    try:
        analysis = ai.analyze(url, scraped_text or url)
    except Exception as e:
        if not warning:
            warning = f"AI 분석 실패, 기본값으로 저장: {e}"
        logger.error("ai analyze failed url=%s error=%s", url, e)

    # 노션 저장 — 여기서 실패하면 502
    try:
        page_id = notion.save(
            url=url,
            source=source,
            title=analysis["title"] or url,
            summary=analysis["summary"],
            type_=analysis["type"],
            tags=analysis["tags"],
            location=analysis.get("location", ""),
            ingredients=analysis.get("ingredients", []),
            steps=analysis.get("steps", []),
            restaurants=analysis.get("restaurants", []),
            cover_image_url=cover_image_url,
        )
    except Exception as e:
        logger.error("notion save failed url=%s error=%s", url, e)
        raise HTTPException(status_code=502, detail=f"노션 저장 실패: {e}")

    return SaveResponse(
        status="ok",
        notion_page_id=page_id,
        title=analysis["title"],
        summary=analysis["summary"],
        tags=analysis["tags"],
        type=analysis["type"],
        source=source,
        warning=warning,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug/instagram")
async def debug_instagram():
    """인스타 세션 설정 확인용 엔드포인트."""
    return {
        "session_id_set": bool(os.environ.get("INSTAGRAM_SESSION_ID")),
        "username": os.environ.get("INSTAGRAM_USERNAME", ""),
    }


class ScrapeDebugResponse(BaseModel):
    text: str
    text_length: int
    cover_image_url: str
    warning: str = ""


@app.post("/debug/scrape")
async def debug_scrape(req: SaveRequest):
    """스크래핑 결과 원문 확인용 엔드포인트."""
    try:
        result = scraper.scrape(str(req.url))
        return ScrapeDebugResponse(
            text=result.get("text", ""),
            text_length=len(result.get("text", "")),
            cover_image_url=result.get("cover_image_url", ""),
            warning=result.get("error", ""),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
