import base64
import json
import os
import requests
import anthropic

_client = None

SYSTEM_PROMPT = """너는 웹 콘텐츠를 분석해서 노션 DB에 저장할 메타데이터를 추출하는 어시스턴트야.
항상 유효한 JSON만 응답하고 다른 텍스트는 절대 포함하지 마."""

ALLOWED_TYPES = ["Recipe", "Restaurant", "Cafe", "Product", "Article", "Video", "Other"]
ALLOWED_TAGS = [
    "Korean", "Japanese", "Chinese", "Italian", "French", "American", "Thai", "Vietnamese",
    "Coffee", "Dessert", "Vegan", "Vegetarian", "Seafood", "Meat", "Noodle", "Rice",
    "Brunch", "Bakery", "Bar", "FastFood", "FineAdining",
    "Seoul", "Busan", "Jeju", "Tokyo", "Osaka", "NewYork", "Paris",
    "Travel", "DIY", "Shopping", "Tech", "Health", "Beauty",
]


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def analyze(url: str, scraped_text: str) -> dict:
    tags_hint = ", ".join(ALLOWED_TAGS)
    types_hint = " | ".join(ALLOWED_TYPES)

    user_message = f"""다음 웹 콘텐츠를 분석해서 JSON으로만 응답해:

URL: {url}
콘텐츠: {scraped_text[:4000]}

응답 형식:
{{
  "title": "간결한 제목 (30자 이내)",
  "summary": "3줄 요약 (각 줄은 핵심 정보 위주)",
  "type": "{types_hint} 중 하나",
  "tags": ["태그1", "태그2"],
  "location": "장소명 또는 주소 (없으면 빈 문자열)"
}}

태그 참고 목록 (이 중에서 최대 5개, 없으면 새로 만들어도 됨): {tags_hint}"""

    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    # JSON 블록이 마크다운 코드펜스로 감싸진 경우 제거
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    result = json.loads(raw)

    # 필수 필드 기본값 보장
    result.setdefault("title", "")
    result.setdefault("summary", "")
    result.setdefault("type", "Other")
    result.setdefault("tags", [])
    result.setdefault("location", "")

    if result["type"] not in ALLOWED_TYPES:
        result["type"] = "Other"

    return result


def describe_images(image_urls: list[str]) -> str:
    """이미지 URL 목록을 받아 Claude Vision으로 텍스트/핵심 내용 추출."""
    content: list[dict] = []

    for url in image_urls[:3]:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if not media_type.startswith("image/"):
                continue
            b64 = base64.standard_b64encode(resp.content).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
        except Exception:
            continue

    if not content:
        return ""

    content.append({
        "type": "text",
        "text": (
            "이미지에서 텍스트와 핵심 내용을 추출해줘. "
            "음식명, 메뉴, 가격, 장소명, 주소, 브랜드, 상품명 등 구체적 정보 위주로 서술해."
        ),
    })

    client = _get_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()
