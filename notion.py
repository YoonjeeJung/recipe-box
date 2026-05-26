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


def _rt(content: str) -> list:
    return [{"type": "text", "text": {"content": content[:2000]}}]


def _h2(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(text)}}


def _numbered(text: str) -> dict:
    return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rt(text)}}


def _bookmark(url: str) -> dict:
    return {"object": "block", "type": "bookmark", "bookmark": {"url": url}}


def _build_children(type_: str, summary: str, url: str, ingredients: list, steps: list) -> list:
    blocks = []

    if summary:
        blocks.append(_h2("요약"))
        blocks.append(_paragraph(summary))

    if type_ == "Recipe":
        if ingredients:
            blocks.append(_h2("재료"))
            for ing in ingredients:
                blocks.append(_bullet(ing))
        if steps:
            blocks.append(_h2("조리 순서"))
            for step in steps:
                blocks.append(_numbered(step))

    blocks.append(_h2("원본 링크"))
    blocks.append(_bookmark(url))

    return blocks


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

    page = client.pages.create(
        parent={"database_id": db_id},
        properties=properties,
        children=_build_children(type_, summary, url, ingredients or [], steps or []),
    )
    return page["id"]
