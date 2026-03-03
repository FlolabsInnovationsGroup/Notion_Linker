import json
import logging
import os
import re
from typing import Dict, List, Optional, Set

import requests
# Basic logging so Lambda output is easy to read
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Notion configuration
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "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").strip()
TIME_ENTRIES_DATABASE_ID = os.getenv(
    "TIME_ENTRIES_DATABASE_ID", "297a157695518075ab6cea94e60e4220"
).strip()
TASKS_DATABASE_ID = os.getenv(
    "TASKS_DATABASE_ID", "b5a15559fffc4a4f97161bf12e6fb400"
).strip()
TASK_RELATION_PROPERTY_NAME = os.getenv("TASK_RELATION_PROPERTY_NAME", "Task")
DESCRIPTION_PROPERTY_NAME = os.getenv("DESCRIPTION_PROPERTY_NAME", "Description")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

NOTION_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)
URL_PATTERN = re.compile(r"https?://\S+")


def _normalize_notion_id(raw: str, hyphenated: bool = False) -> Optional[str]:
    stripped = raw.replace("-", "")
    if len(stripped) != 32 or not re.fullmatch(r"[0-9a-fA-F]{32}", stripped):
        return None
    if not hyphenated:
        return stripped.lower()
    return "-".join(
        [
            stripped[0:8],
            stripped[8:12],
            stripped[12:16],
            stripped[16:20],
            stripped[20:32],
        ]
    ).lower()


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    seen: Set[str] = set()
    deduped: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _find_urls(text: str) -> List[str]:
    if not text:
        return []
    urls: List[str] = []
    for match in URL_PATTERN.findall(text):
        url = match.rstrip(").,;]'\"")
        if url:
            urls.append(url)
    return urls


def _match_task_id_from_text(
    text: str, task_index: Dict[str, Dict[str, str]]
) -> Optional[str]:
    for match in NOTION_ID_PATTERN.findall(text):
        candidate = _normalize_notion_id(match, hyphenated=False)
        if candidate and candidate in task_index.get("by_id", {}):
            return task_index["by_id"][candidate]
    return None


def build_task_indexes(tasks: List[Dict]) -> Dict[str, Dict[str, str]]:
    by_id: Dict[str, str] = {}
    for task in tasks:
        nid = _normalize_notion_id(task["id"], hyphenated=False)
        if nid and nid not in by_id:
            by_id[nid] = task["id"]
    return {"by_id": by_id}


def _safe_raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:  # pragma: no cover - runtime logging
        logging.error("HTTP error %s: %s", response.status_code, response.text)
        raise exc


def fetch_pages_from_database(
    database_id: str, filter_payload: Optional[Dict] = None, limit: int = 100
) -> List[Dict]:
    pages: List[Dict] = []
    cursor: Optional[str] = None
    while len(pages) < limit:
        payload: Dict[str, object] = {"page_size": min(100, limit - len(pages))}
        if filter_payload:
            payload["filter"] = filter_payload
        if cursor:
            payload["start_cursor"] = cursor

        resp = requests.post(
            f"{NOTION_API_BASE}/databases/{database_id}/query",
            headers=NOTION_HEADERS,
            json=payload,
            timeout=30,
        )
        _safe_raise_for_status(resp)
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return pages[:limit]


def extract_title(properties: Dict) -> str:
    for prop in properties.values():
        if prop.get("type") == "title":
            return "".join(item.get("plain_text", "") for item in prop.get("title", []))
    return ""


def extract_rich_text_content(page: Dict, property_name: str) -> str:
    prop = page.get("properties", {}).get(property_name)
    if not prop:
        return ""
    if prop.get("type") == "rich_text":
        return "".join(item.get("plain_text", "") for item in prop.get("rich_text", []))
    if prop.get("type") == "title":
        return "".join(item.get("plain_text", "") for item in prop.get("title", []))
    return ""


def extract_rich_text_links(page: Dict, property_name: str) -> List[str]:
    prop = page.get("properties", {}).get(property_name)
    if not prop:
        return []
    links: List[str] = []
    if prop.get("type") == "rich_text":
        for item in prop.get("rich_text", []):
            href = item.get("href")
            if href:
                links.append(href)
            links.extend(_find_urls(item.get("plain_text", "")))
        return _dedupe_preserve_order(links)
    if prop.get("type") == "title":
        text = "".join(item.get("plain_text", "") for item in prop.get("title", []))
        return _dedupe_preserve_order(_find_urls(text))
    return []


def has_task_relation(page: Dict) -> bool:
    prop = page.get("properties", {}).get(TASK_RELATION_PROPERTY_NAME)
    if not prop or prop.get("type") != "relation":
        return False
    return bool(prop.get("relation"))


def update_time_entry_task(page_id: str, task_id: str) -> None:
    payload = {
        "properties": {
            TASK_RELATION_PROPERTY_NAME: {
                "relation": [{"id": task_id}],
            }
        }
    }
    resp = requests.patch(
        f"{NOTION_API_BASE}/pages/{page_id}", headers=NOTION_HEADERS, json=payload, timeout=30
    )
    _safe_raise_for_status(resp)
    logging.info("Linked time entry %s to task %s", page_id, task_id)


def choose_task_for_description(
    description_links: List[str],
    tasks: List[Dict],
    task_index: Dict[str, Dict[str, str]],
) -> Optional[str]:
    if not tasks:
        logging.warning("No tasks available for matching")
        return None
    search_text = " ".join(description_links)
    match = _match_task_id_from_text(search_text, task_index)
    if match:
        logging.info("Matched by page ID in description link")
    return match


def collect_task_options(limit: int = 100) -> List[Dict]:
    pages = fetch_pages_from_database(TASKS_DATABASE_ID, limit=limit)
    options: List[Dict] = []
    for page in pages:
        title = extract_title(page.get("properties", {}))
        if title:
            options.append({"id": page["id"], "name": title})
    return options


def process_time_entries(limit: int = 100) -> Dict[str, object]:
    time_entries = fetch_pages_from_database(
        TIME_ENTRIES_DATABASE_ID,
        filter_payload={
            "and": [
                {
                    "property": DESCRIPTION_PROPERTY_NAME,
                    "rich_text": {"is_not_empty": True},
                },
                {
                    "property": DESCRIPTION_PROPERTY_NAME,
                    "rich_text": {"does_not_contain": "No Description"},
                },
            ]
        },
        limit=limit,
    )
    # Grab a wider set of tasks to improve deterministic link-to-ID matching
    task_options = collect_task_options(limit=max(limit, 1000))
    task_index = build_task_indexes(task_options)
    if not task_options:
        logging.warning("No tasks fetched from TASKS_DATABASE_ID=%s", TASKS_DATABASE_ID)
    updated = 0
    skipped = 0
    reasons = {
        "no_description": 0,
        "no_link": 0,
        "already_linked": 0,
        "no_match": 0,
    }
    sample_no_match: List[str] = []

    for entry in time_entries:
        description = extract_rich_text_content(entry, DESCRIPTION_PROPERTY_NAME)
        description_links = extract_rich_text_links(entry, DESCRIPTION_PROPERTY_NAME)
        if not description and not description_links:
            skipped += 1
            reasons["no_description"] += 1
            continue
        if not description_links:
            skipped += 1
            reasons["no_link"] += 1
            continue
        if has_task_relation(entry):
            skipped += 1
            reasons["already_linked"] += 1
            continue

        task_id = choose_task_for_description(description_links, task_options, task_index)
        if task_id:
            update_time_entry_task(entry["id"], task_id)
            updated += 1
        else:
            skipped += 1
            reasons["no_match"] += 1
            if len(sample_no_match) < 5:
                sample_no_match.append(description[:200] or description_links[0][:200])

    logging.info(
        "Skip reasons: %s; sample no-match descriptions: %s",
        reasons,
        sample_no_match,
    )
    return {
        "processed": len(time_entries),
        "updated": updated,
        "skipped": skipped,
    }


def lambda_handler(event=None, context=None) -> Dict[str, object]:
    result = process_time_entries(limit=1000)
    logging.info("Processing complete: %s", result)
    return {"statusCode": 200, "body": json.dumps(result)}


if __name__ == "__main__":
    lambda_handler()
