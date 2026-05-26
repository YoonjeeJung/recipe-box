import os
from notion_client import Client

_client = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(auth=os.environ["NOTION_TOKEN"])
    return _client


def _get_db_id() -> str:
    raw = os.environ["NOTION_DB_ID"]
    # 하이픈 없는 형태(32자)도 허용
    if "-" not in raw and len(raw) == 32:
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return raw


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------

def _rt(content: str) -> list:
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _h3(text: str) -> dict:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rt(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(text)}}


def _numbered(text: str) -> dict:
    return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rt(text)}}


def _callout(text: str, emoji: str = "💡") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rt(text),
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def _image_block(url: str) -> dict:
    return {
        "object": "block",
        "type": "image",
        "image": {
            "type": "external",
            "external": {"url": url},
        },
    }


# ---------------------------------------------------------------------------
# Page body builder
# ---------------------------------------------------------------------------

def _build_children(
    type_: str,
    summary: str,
    ingredients: list,
    steps: list,
    restaurants: list,
    cover_image_url: str = "",
) -> list:
    blocks = []

    # 대표 이미지 (최상단)
    if cover_image_url:
        blocks.append(_image_block(cover_image_url))

    if type_ == "Recipe":
        if summary:
            blocks.append(_callout(summary))
        if ingredients:
            blocks.append(_h3("재료"))
            for ing in ingredients:
                blocks.append(_bullet(ing))
        if steps:
            blocks.append(_h3("조리 순서"))
            for step in steps:
                blocks.append(_numbered(step))

    elif type_ in ("Restaurant", "Cafe"):
        if summary:
            blocks.append(_callout(summary))
        for r in restaurants:
            name = r.get("name", "")
            menu = r.get("menu", "")
            address = r.get("address", "")
            if name:
                blocks.append(_h3(name))
            if menu:
                blocks.append(_paragraph(f"대표메뉴: {menu}"))
            if address:
                blocks.append(_paragraph(f"위치: {address}"))

    else:  # Article, Video, Product, Other
        if summary:
            blocks.append(_callout(summary))

    return blocks


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save(
    url: str,
    source: str,
    title: str,
    summary: str,
    type_: str,
    tags: list[str],
    location: str,
    ingredients: list[str] | None = None,
    steps: list[str] | None = None,
    restaurants: list[dict] | None = None,
    cover_image_url: str = "",
) -> str:
    client = _get_client()
    db_id = _get_db_id()

    properties: dict = {
        "Title": {"title": [{"text": {"content": title or url}}]},
        "Link": {"url": url},
        "Source": {"select": {"name": source}},
        "Type": {"select": {"name": type_}},
        "Summary": {"rich_text": [{"text": {"content": summary}}]},
        "Status": {"select": {"name": "저장됨"}},
    }

    if tags:
        properties["Tags"] = {"multi_select": [{"name": t} for t in tags[:5]]}

    if location:
        properties["Location"] = {"rich_text": [{"text": {"content": location}}]}

    children = _build_children(
        type_,
        summary,
        ingredients or [],
        steps or [],
        restaurants or [],
        cover_image_url,
    )

    page = client.pages.create(
        parent={"database_id": db_id},
        properties=properties,
        children=children,
    )
    return page["id"]
