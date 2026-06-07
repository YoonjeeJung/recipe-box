import logging
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

_logger = logging.getLogger(__name__)


class _YtDlpLogger:
    """yt-dlp 콘솔 출력을 Python logger(DEBUG)로 전환 — Railway 로그 오염 방지."""
    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        _logger.debug("yt-dlp: %s", msg)
    def info(self, msg): _logger.debug("yt-dlp: %s", msg)
    def warning(self, msg): _logger.debug("yt-dlp warn: %s", msg)
    def error(self, msg): _logger.debug("yt-dlp err: %s", msg)


def _ydl_base_opts(**extra) -> dict:
    """공통 yt-dlp 옵션 — format 에러 억제 + 로거 설정."""
    return {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "format": "best",
        "ignore_no_formats_error": True,  # Shorts 등 format 검증 실패해도 메타데이터 추출 계속
        "logger": _YtDlpLogger(),
        **extra,
    }


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
    is_carousel: bool = result.pop("_is_carousel", False)
    cover_image_url: str = image_urls[0] if image_urls else ""

    # 카드뉴스(캐러셀)는 이미지에 내용이 있으므로 텍스트가 충분해도 OCR 진행
    if _is_sufficient(result["text"]) and not is_carousel:
        result["cover_image_url"] = cover_image_url
        return result

    # ── Stage 1.5: 고정댓글 (YouTube Shorts / Instagram Reels) ─────────────
    if source in ("YouTube", "Instagram"):
        try:
            comments = _get_comments(url, source)
            _logger.info("stage1.5 comments len=%d source=%s url=%s", len(comments), source, url)
            if comments:
                result["text"] = _join(result["text"], comments)
                if _is_sufficient(result["text"]) and not is_carousel:
                    result["cover_image_url"] = cover_image_url
                    return result
        except Exception as e:
            _logger.warning("stage1.5 failed url=%s error=%s", url, e)

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
    if source == "Twitter":
        return _scrape_twitter(url)
    return _scrape_web(url)


def _instagram_media_id(url: str) -> int:
    """Instagram URL의 shortcode를 media_id(int)로 변환."""
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    shortcode = parts[1] if len(parts) >= 2 else ""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        if char in alphabet:
            media_id = media_id * 64 + alphabet.index(char)
    return media_id


def _instagram_carousel_images(url: str, session_id: str) -> list[str]:
    """Instagram 내부 API로 캐러셀 슬라이드 이미지 URL 직접 추출."""
    media_id = _instagram_media_id(url)
    if not media_id:
        return []

    api_url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
    headers = {
        "Cookie": f"sessionid={session_id}",
        "User-Agent": "Instagram 219.0.0.12.117 Android",
        "Accept": "*/*",
        "X-IG-App-ID": "936619743392459",
    }
    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        item = (resp.json().get("items") or [{}])[0]
        if item.get("carousel_media"):
            urls = []
            for m in item["carousel_media"]:
                candidates = m.get("image_versions2", {}).get("candidates", [])
                if candidates:
                    urls.append(candidates[0]["url"])
            return urls[:5]
        candidates = item.get("image_versions2", {}).get("candidates", [])
        return [candidates[0]["url"]] if candidates else []
    except Exception:
        return []


def _scrape_instagram(url: str) -> dict:
    """yt-dlp로 인스타 캡션 추출 + Instagram API로 캐러셀 이미지 수집."""
    session_id = os.environ.get("INSTAGRAM_SESSION_ID", "")

    try:
        import yt_dlp
        ydl_opts = _ydl_base_opts()
        if session_id:
            ydl_opts["http_headers"] = {
                "Cookie": f"sessionid={session_id}",
                "User-Agent": HEADERS["User-Agent"],
            }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        is_carousel = info.get("_type") == "playlist"
        caption = info.get("description") or ""
        uploader = info.get("uploader") or ""
        title = info.get("title") or uploader

        # 캐러셀이면 Instagram API로 슬라이드 이미지 직접 수집
        if is_carousel and session_id:
            thumbnails = _instagram_carousel_images(url, session_id)
        else:
            thumbnail = info.get("thumbnail") or ""
            if not thumbnail:
                thumbs = info.get("thumbnails") or []
                thumbnail = thumbs[-1].get("url", "") if thumbs else ""
            thumbnails = [thumbnail] if thumbnail else []

        text = "\n".join(filter(None, [title, caption]))
        _logger.info("yt-dlp instagram ok url=%s is_carousel=%s caption_len=%d images=%d",
                     url, is_carousel, len(caption), len(thumbnails))
        return {
            "title": title,
            "description": caption[:200],
            "text": text,
            "_image_urls": thumbnails,
            "_is_carousel": is_carousel,
        }
    except Exception as e:
        _logger.error("yt-dlp instagram failed url=%s error=%s", url, e)
        return _scrape_web(url)


def _scrape_twitter(url: str) -> dict:
    """Twitter/X oEmbed API로 트윗 텍스트 추출. 인증 불필요."""
    oembed_url = f"https://publish.twitter.com/oembed?url={url}&omit_script=true"
    try:
        resp = _get_with_retry(oembed_url, headers=HEADERS)
        data = resp.json()
        author = data.get("author_name", "")
        # HTML에서 트윗 본문 파싱
        html = data.get("html", "")
        soup = BeautifulSoup(html, "html.parser")
        # blockquote > p 가 트윗 본문
        p = soup.find("p")
        tweet_text = p.get_text(" ", strip=True) if p else ""
        text = "\n".join(filter(None, [author, tweet_text]))
        return {
            "title": author,
            "description": tweet_text[:200],
            "text": text,
            "_image_urls": [],
        }
    except Exception:
        pass
    return _scrape_web(url)


def _scrape_youtube(url: str) -> dict:
    """YouTube 영상 정보 추출 — yt-dlp(전체 설명)로 시도, 실패 시 oEmbed 폴백."""
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_base_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get("title") or ""
        description = info.get("description") or ""
        thumbnail = info.get("thumbnail") or ""
        if not thumbnail:
            thumbs = info.get("thumbnails") or []
            thumbnail = thumbs[-1].get("url", "") if thumbs else ""
        return {
            "title": title,
            "description": description[:200],
            "text": "\n".join(filter(None, [title, description])),
            "_image_urls": [thumbnail] if thumbnail else [],
        }
    except Exception:
        pass
    # oEmbed 폴백 (title + author_name만 가져옴)
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


def _get_comments(url: str, source: str) -> str:
    """YouTube/Instagram 고정댓글·인기댓글 텍스트 반환 (상위 3개)."""
    if source == "YouTube":
        return _get_youtube_comments(url)
    if source == "Instagram":
        session_id = os.environ.get("INSTAGRAM_SESSION_ID", "")
        if session_id:
            return _get_instagram_comments(url, session_id)
    return ""


def _get_youtube_comments(url: str) -> str:
    try:
        import yt_dlp
        ydl_opts = _ydl_base_opts(
            getcomments=True,
            extractor_args={"youtube": {"max_comments": ["20"]}},
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        comments = info.get("comments") or []
        _logger.info("youtube comments total=%d url=%s", len(comments), url)
        # 고정댓글만 사용
        pinned = [c.get("text", "") for c in comments if c.get("is_pinned") and c.get("text")]
        _logger.info("youtube pinned=%d", len(pinned))
        return "\n\n".join(pinned)
    except Exception as e:
        _logger.warning("youtube comments failed url=%s error=%s", url, e)
        return ""


def _get_instagram_comments(url: str, session_id: str) -> str:
    media_id = _instagram_media_id(url)
    if not media_id:
        return ""
    api_url = (
        f"https://www.instagram.com/api/v1/media/{media_id}/comments/"
        "?can_support_threading=true&permalink_enabled=false"
    )
    headers = {
        "Cookie": f"sessionid={session_id}",
        "User-Agent": "Instagram 219.0.0.12.117 Android",
        "Accept": "*/*",
        "X-IG-App-ID": "936619743392459",
    }
    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        _logger.info("instagram comments api status=%d url=%s", resp.status_code, url)
        if resp.status_code != 200:
            return ""
        data = resp.json()
        comments = data.get("comments") or []
        _logger.info("instagram comments total=%d", len(comments))
        # 고정댓글만 사용
        pinned = [c.get("text", "") for c in comments if c.get("is_pinned_comment") and c.get("text")]
        _logger.info("instagram pinned=%d", len(pinned))
        return "\n\n".join(pinned)
    except Exception as e:
        _logger.warning("instagram comments failed url=%s error=%s", url, e)
        return ""


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
        ydl_opts = _ydl_base_opts(
            writeautomaticsub=True,
            writesubtitles=True,
            subtitleslangs=["ko", "en"],
            outtmpl=os.path.join(tmpdir, "%(id)s"),
        )
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
            "logger": _YtDlpLogger(),
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
