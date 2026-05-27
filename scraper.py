import os
import re
import time
import base64
import tempfile
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# 이 길이 이상이면 다음 단계(이미지/영상) 건너뜀
SUFFICIENT_TEXT_LEN = 150

_RETRYABLE = (requests.ConnectionError, requests.Timeout)

# faster-whisper 모델은 첫 호출 시 한 번만 로드
_whisper_model = None

# instaloader 인스턴스 — 서버 시작 시 한 번 로그인, 메모리에 유지
_insta_loader = None


def _get_insta_loader():
    """INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD 환경변수가 있으면 로그인해서 반환."""
    global _insta_loader
    if _insta_loader is not None:
        return _insta_loader
    username = os.environ.get("INSTAGRAM_USERNAME")
    password = os.environ.get("INSTAGRAM_PASSWORD")
    if not (username and password):
        return None
    try:
        import instaloader
        import logging
        logger = logging.getLogger(__name__)
        L = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
        )
        L.login(username, password)
        _insta_loader = L
        logger.info("instaloader login success: %s", username)
        return _insta_loader
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("instaloader login failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_source(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        return "YouTube"
    if "instagram.com" in host:
        return "Instagram"
    if "tiktok.com" in host:
        return "TikTok"
    if "twitter.com" in host or "x.com" in host:
        return "Twitter"
    return "Web"


def scrape(url: str) -> dict:
    """
    3단계 파이프라인:
      1. 텍스트 추출
      2. 이미지 → Claude Vision OCR   (텍스트 부족 시)
      3. 영상 → 자막/STT              (1+2 모두 부족 시)
    """
    source = detect_source(url)

    # ── Stage 1: 텍스트 ─────────────────────────────────────────────────────
    result = _scrape_text(url, source)
    image_urls: list[str] = result.pop("_image_urls", [])
    cover_image_url: str = image_urls[0] if image_urls else ""

    if _is_sufficient(result["text"]):
        result["cover_image_url"] = cover_image_url
        return result

    # ── Stage 2: 이미지 OCR ─────────────────────────────────────────────────
    if image_urls:
        try:
            from ai import describe_images
            ocr = describe_images(image_urls[:3])
            if ocr:
                result["text"] = _join(result["text"], ocr)
                if _is_sufficient(result["text"]):
                    result["cover_image_url"] = cover_image_url
                    return result
        except Exception:
            pass

    # ── Stage 3: 영상 STT ────────────────────────────────────────────────────
    try:
        transcript = _transcribe(url)
        if transcript:
            result["text"] = _join(result["text"], transcript)
    except Exception:
        pass

    result["cover_image_url"] = cover_image_url
    return result


# ---------------------------------------------------------------------------
# Stage 1: Text helpers
# ---------------------------------------------------------------------------

def _scrape_text(url: str, source: str) -> dict:
    if source == "YouTube":
        return _scrape_youtube(url)
    if source == "Instagram":
        return _scrape_instagram(url)
    if source == "TikTok":
        return _scrape_oembed(url, "https://www.tiktok.com/oembed?url={url}")
    return _scrape_web(url)


def _scrape_instagram(url: str) -> dict:
    """instaloader로 캡션 + 썸네일 추출. 로그인 정보 없으면 _scrape_web으로 폴백."""
    shortcode_match = re.search(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)", url)
    if not shortcode_match:
        return _scrape_web(url)

    shortcode = shortcode_match.group(1)
    L = _get_insta_loader()
    if L is None:
        return _scrape_web(url)

    try:
        import instaloader
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        caption = post.caption or ""
        thumbnail = ""
        if post.is_video:
            thumbnail = post.video_thumbnail_url or ""
        if not thumbnail:
            thumbnail = post.url or ""
        return {
            "title": post.owner_username,
            "description": caption[:200],
            "text": caption,
            "_image_urls": [thumbnail] if thumbnail else [],
        }
    except Exception:
        return _scrape_web(url)


def _scrape_youtube(url: str) -> dict:
    oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
    try:
        resp = _get_with_retry(oembed_url)
        data = resp.json()
        title = data.get("title", "")
        author = data.get("author_name", "")
        thumbnail = data.get("thumbnail_url", "")
        return {
            "title": title,
            "description": author,
            "text": f"{title}\n{author}",
            "_image_urls": [thumbnail] if thumbnail else [],
        }
    except Exception:
        pass
    return _scrape_web(url)


def _scrape_oembed(url: str, template: str) -> dict:
    oembed_url = template.format(url=url)
    try:
        resp = _get_with_retry(oembed_url, headers=HEADERS)
        data = resp.json()
        title = data.get("title", "")
        author = data.get("author_name", "")
        thumbnail = data.get("thumbnail_url", "")
        return {
            "title": title,
            "description": author,
            "text": f"{title}\n{author}",
            "_image_urls": [thumbnail] if thumbnail else [],
        }
    except Exception:
        pass
    return _scrape_web(url)


def _scrape_web(url: str) -> dict:
    try:
        resp = _get_with_retry(url, timeout=15, headers=HEADERS)
    except Exception as e:
        return {"title": "", "description": "", "text": "", "_image_urls": [], "error": str(e)}

    soup = BeautifulSoup(resp.text, "html.parser")

    title = (
        _meta(soup, "og:title")
        or _meta(soup, "twitter:title")
        or (soup.title.string.strip() if soup.title else "")
    )
    description = (
        _meta(soup, "og:description")
        or _meta(soup, "twitter:description")
        or _meta(soup, "description")
        or ""
    )

    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    body = " ".join(p for p in paragraphs if len(p) > 40)[:3000]

    image_urls = _extract_image_urls(soup)

    return {
        "title": title,
        "description": description,
        "text": f"{title}\n{description}\n{body}".strip(),
        "_image_urls": image_urls,
    }


def _extract_image_urls(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []

    for attr in ("og:image", "twitter:image"):
        val = _meta(soup, attr)
        if val and val not in urls:
            urls.append(val)

    for img in soup.find_all("img", src=True):
        src = str(img["src"])
        if src.startswith("http") and src not in urls:
            urls.append(src)
        if len(urls) >= 5:
            break

    return urls


# ---------------------------------------------------------------------------
# Stage 3: Video transcription
# ---------------------------------------------------------------------------

def _transcribe(url: str) -> str:
    # 자막 우선 (빠르고 가벼움)
    captions = _get_captions(url)
    if _is_sufficient(captions):
        return captions

    # 자막 없으면 오디오 다운로드 + Whisper STT
    stt = _whisper_stt(url)
    return stt


def _get_captions(url: str) -> str:
    try:
        import yt_dlp
    except ImportError:
        return ""

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "writeautomaticsub": True,
            "writesubtitles": True,
            "subtitleslangs": ["ko", "en"],
            "skip_download": True,
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
            "quiet": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception:
            return ""

        for fname in os.listdir(tmpdir):
            if fname.endswith(".vtt") or fname.endswith(".srt"):
                with open(os.path.join(tmpdir, fname), encoding="utf-8") as f:
                    return _parse_subtitle(f.read())

    return ""


def _whisper_stt(url: str) -> str:
    try:
        import faster_whisper
    except ImportError:
        return ""

    try:
        import yt_dlp
    except ImportError:
        return ""

    global _whisper_model

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = os.path.join(tmpdir, "audio")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": audio_path + ".%(ext)s",
            "quiet": True,
            "no_warnings": True,
            # 5분까지만 다운로드 (Railway 무료 플랜 고려)
            "external_downloader_args": {"ffmpeg_i": ["-t", "300"]},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception:
            return ""

        # 다운로드된 파일 찾기
        audio_file = next(
            (os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.startswith("audio.")),
            None,
        )
        if not audio_file:
            return ""

        try:
            if _whisper_model is None:
                _whisper_model = faster_whisper.WhisperModel(
                    "base", device="cpu", compute_type="int8"
                )
            segments, _ = _whisper_model.transcribe(audio_file, beam_size=1)
            return " ".join(seg.text.strip() for seg in segments)[:3000]
        except Exception:
            return ""


def _parse_subtitle(text: str) -> str:
    # WEBVTT 헤더 제거
    text = re.sub(r"WEBVTT.*?\n\n", "", text, flags=re.DOTALL)
    # 타임스탬프 라인 제거
    text = re.sub(r"\d{2}:\d{2}(:\d{2})?[.,]\d{3} --> .+", "", text)
    # HTML/XML 태그 제거
    text = re.sub(r"<[^>]+>", "", text)
    # 시퀀스 번호 제거
    text = re.sub(r"^\d+\s*$", "", text, flags=re.MULTILINE)

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # 중복 라인 제거 (자막은 같은 문장이 반복되는 경우 많음)
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique.append(line)

    return " ".join(unique)[:3000]


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _is_sufficient(text: str) -> bool:
    return len((text or "").strip()) >= SUFFICIENT_TEXT_LEN


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p).strip()


def _get_with_retry(url: str, *, timeout: int = 10, **kwargs) -> requests.Response:
    delay = 1
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except _RETRYABLE as e:
            last_exc = e
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise
            last_exc = e
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
    raise last_exc


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find(
        "meta", attrs={"name": name}
    )
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""
