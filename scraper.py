import time
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

_RETRYABLE = (requests.ConnectionError, requests.Timeout)


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
    source = detect_source(url)
    if source == "YouTube":
        return _scrape_youtube(url)
    if source == "Instagram":
        return _scrape_instagram(url)
    if source == "TikTok":
        return _scrape_oembed(url, "https://www.tiktok.com/oembed?url={url}")
    return _scrape_web(url)


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
            # 5xx는 재시도, 4xx는 즉시 포기
            if e.response is not None and e.response.status_code < 500:
                raise
            last_exc = e
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
    raise last_exc


def _scrape_youtube(url: str) -> dict:
    oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
    try:
        resp = _get_with_retry(oembed_url)
        data = resp.json()
        return {
            "title": data.get("title", ""),
            "description": data.get("author_name", ""),
            "text": f"{data.get('title', '')} by {data.get('author_name', '')}",
        }
    except Exception:
        pass
    return _scrape_web(url)


def _scrape_instagram(url: str) -> dict:
    # Instagram oEmbed는 인증 없이 불안정 — 바로 일반 웹 스크래핑으로 fallback
    return _scrape_web(url)


def _scrape_oembed(url: str, template: str) -> dict:
    oembed_url = template.format(url=url)
    try:
        resp = _get_with_retry(oembed_url, headers=HEADERS)
        data = resp.json()
        title = data.get("title", "")
        author = data.get("author_name", "")
        return {
            "title": title,
            "description": author,
            "text": f"{title} by {author}",
        }
    except Exception:
        pass
    return _scrape_web(url)


def _scrape_web(url: str) -> dict:
    try:
        resp = _get_with_retry(url, timeout=15, headers=HEADERS)
    except Exception as e:
        return {"title": "", "description": "", "text": "", "error": str(e)}

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

    return {
        "title": title,
        "description": description,
        "text": f"{title}\n{description}\n{body}".strip(),
    }


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find(
        "meta", attrs={"name": name}
    )
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""
