import base64
import json
import os
import requests
import anthropic

_client = None

SYSTEM_PROMPT = """너는 웹 콘텐츠에서 메타데이터를 추출하는 어시스턴트야.
항상 유효한 JSON만 응답하고 다른 텍스트는 절대 포함하지 마.
핵심 원칙: 어떤 소스에도 근거 없는 내용은 절대 만들어내지 마.
단, [영상 화면 텍스트]나 [음성 전사]처럼 인식 오류가 섞일 수 있는 소스는
문맥으로 오탈자를 보정하고 흩어진 정보를 조합해 재구성해도 돼."""

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

    user_message = f"""다음 웹 콘텐츠에서 정보를 추출해서 JSON으로만 응답해:

URL: {url}
콘텐츠: {scraped_text[:4000]}

응답 형식:
{{
  "title": "간결한 제목 (30자 이내)",
  "summary": "콘텐츠 소개 한두 문장 — 원문에서 그대로 발췌. 요약·압축·재작성 금지",
  "type": "{types_hint} 중 하나",
  "tags": ["태그1", "태그2"],
  "location": "큰 행정구역 단위만 (예: 종로구, 의정부시). 없으면 빈 문자열",
  "ingredients": ["재료명 분량 — 원문 표현 그대로"],
  "steps": ["조리 단계 — 원문 표현 그대로"],
  "restaurants": [
    {{
      "name": "식당명",
      "menu": "대표메뉴",
      "address": "상세 주소"
    }}
  ]
}}

태그 참고 목록 (이 중에서 최대 5개, 없으면 새로 만들어도 됨): {tags_hint}

규칙:
- 콘텐츠는 소스별 라벨로 구분될 수 있어: 캡션(라벨 없음), [고정·작성자 댓글],
  [썸네일·이미지 텍스트], [영상 화면 텍스트], [음성 전사].
- 캡션·댓글에 있는 내용은 원문 표현 그대로 추출해.
- [영상 화면 텍스트]와 [음성 전사]는 OCR/음성인식 오류가 섞여 있을 수 있어.
  캡션·댓글에 레시피가 없으면 이 소스들에서 문맥으로 오탈자를 보정하고
  흩어진 재료·조리 과정을 조합해 ingredients와 steps를 재구성해.
  (예: 음성 전사의 "간장 한 스푼 반" → "간장 1.5큰술")
- 어떤 소스에도 근거 없는 재료·분량·단계를 새로 만들어내는 건 금지.
  모든 소스에 재료나 순서가 없으면 빈 배열.
- 조리 과정이 담긴 콘텐츠면(음성·화면 텍스트만 있어도) type은 Recipe로.
- type이 Recipe일 때만 ingredients와 steps를 채워. 나머지는 빈 배열.
- type이 Restaurant 또는 Cafe일 때만 restaurants를 채워. 식당 여러 개면 모두 추출. 나머지는 빈 배열.
- location은 구/시/군 단위만. 상세 주소는 restaurants[].address에.
- 콘텐츠에 없으면 빈 값으로."""

    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
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
    result.setdefault("ingredients", [])
    result.setdefault("steps", [])
    result.setdefault("restaurants", [])

    if result["type"] not in ALLOWED_TYPES:
        result["type"] = "Other"

    # restaurants 각 항목 필드 정제
    result["restaurants"] = [
        {
            "name": r.get("name", ""),
            "menu": r.get("menu", ""),
            "address": r.get("address", ""),
        }
        for r in result["restaurants"]
        if isinstance(r, dict)
    ]

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


def describe_frames(frames_b64: list[str]) -> str:
    """비디오 프레임(base64 JPEG) 목록에서 레시피/텍스트 추출."""
    if not frames_b64:
        return ""

    content: list[dict] = []
    for b64 in frames_b64[:12]:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    content.append({
        "type": "text",
        "text": (
            "요리 영상에서 시간순으로 캡처한 프레임들이야. "
            "화면에 나오는 자막·텍스트를 최우선으로 빠짐없이 옮겨 적어줘 "
            "(재료명, 분량·계량 표기, 조리 단계 자막, 음식명 등). "
            "자막이 없는 프레임은 화면에 보이는 조리 동작을 짧게 서술해."
        ),
    })

    client = _get_client()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()
