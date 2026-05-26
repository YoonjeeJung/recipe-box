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


def save(
    url: str,
    source: str,
    title: str,
    summary: str,
    type_: str,
    tags: list[str],
    location: str,
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
    )
    return page["id"]
