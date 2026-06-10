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


@app.post("/debug/instagram-raw")
async def debug_instagram_raw(req: SaveRequest):
    """yt-dlp + Instagram API 동작 확인용."""
    import os as _os
    import requests as _requests
    import yt_dlp
    from urllib.parse import urlparse as _up

    url = str(req.url)
    session_id = _os.environ.get("INSTAGRAM_SESSION_ID", "")
    ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    if session_id:
        ydl_opts["http_headers"] = {
            "Cookie": f"sessionid={session_id}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = list(info.get("entries") or [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {e}")

    # Instagram 내부 API 직접 테스트
    api_status = None
    api_images = []
    if session_id:
        path = _up(url).path
        parts = [p for p in path.split("/") if p]
        shortcode = parts[1] if len(parts) >= 2 else ""
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        media_id = 0
        for char in shortcode:
            if char in alphabet:
                media_id = media_id * 64 + alphabet.index(char)
        api_url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
        try:
            resp = _requests.get(api_url, headers={
                "Cookie": f"sessionid={session_id}",
                "User-Agent": "Instagram 219.0.0.12.117 Android",
                "X-IG-App-ID": "936619743392459",
            }, timeout=10)
            api_status = resp.status_code
            if resp.status_code == 200:
                item = (resp.json().get("items") or [{}])[0]
                if item.get("carousel_media"):
                    api_images = [
                        m.get("image_versions2", {}).get("candidates", [{}])[0].get("url", "")
                        for m in item["carousel_media"]
                    ]
                else:
                    candidates = item.get("image_versions2", {}).get("candidates", [])
                    api_images = [candidates[0]["url"]] if candidates else []
        except Exception as ex:
            api_status = f"error: {ex}"

    return {
        "yt_dlp_type": info.get("_type"),
        "yt_dlp_entries": len(entries),
        "ig_api_status": api_status,
        "ig_api_images_count": len([u for u in api_images if u]),
        "ig_api_first_image": (api_images[0][:80] + "...") if api_images and api_images[0] else "",
    }


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

