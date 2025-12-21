import json
import logging
import os
import re
from typing import Dict, List, Optional


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
PROJECT_PROPERTY_NAME = os.getenv("PROJECT_PROPERTY_NAME", "Project")
PROJECT_VALUE = os.getenv("PROJECT_VALUE", "Program tasks")
TASK_RELATION_PROPERTY_NAME = os.getenv("TASK_RELATION_PROPERTY_NAME", "Task")
DESCRIPTION_PROPERTY_NAME = os.getenv("DESCRIPTION_PROPERTY_NAME", "Description")

# LLM configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
HF_API_TOKEN = os.getenv("HF_API_TOKEN", "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").strip()
HF_MODEL = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.2").strip()

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json",
}
HF_API_BASE = "https://router.huggingface.co/models"
HF_HEADERS = (
    {"Authorization": f"Bearer {HF_API_TOKEN}", "Content-Type": "application/json"}
    if HF_API_TOKEN
    else {}
)


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


def _parse_markdown_link(text: str) -> Optional[Dict[str, str]]:
    match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", text)
    if not match:
        return None
    return {"label": match.group(1).strip(), "url": match.group(2).strip()}


def build_task_indexes(tasks: List[Dict]) -> Dict[str, Dict[str, str]]:
    by_id: Dict[str, str] = {}
    by_title: Dict[str, str] = {}
    for task in tasks:
        nid = _normalize_notion_id(task["id"], hyphenated=False)
        if nid and nid not in by_id:
            by_id[nid] = task["id"]
        title = task["name"].strip().lower()
        if title and title not in by_title:
            by_title[title] = task["id"]
    return {"by_id": by_id, "by_title": by_title}


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
    description: str, tasks: List[Dict], task_index: Dict[str, Dict[str, str]]
) -> Optional[str]:
    if not tasks:
        logging.warning("No tasks available for matching")
        return None

    # First try deterministic matching using the markdown link contents
    link = _parse_markdown_link(description)
    if link:
        # Match by Notion page ID in the URL
        id_match = re.search(r"([0-9a-fA-F]{32})", link["url"])
        if id_match:
            candidate = _normalize_notion_id(id_match.group(1), hyphenated=False)
            if candidate and candidate in task_index.get("by_id", {}):
                logging.info("Matched by page ID from link")
                return task_index["by_id"][candidate]
        # Match by link label vs task title (case-insensitive)
        label = link["label"].strip().lower()
        if label and label in task_index.get("by_title", {}):
            logging.info("Matched by link label to task title")
            return task_index["by_title"][label]

    task_lines = [f"- {task['name']} (id: {task['id']})" for task in tasks]
    prompt = (
        "You match a time entry to a task by name.\n"
        "Return strictly JSON: {\"task_id\": \"<id>\"} when confident, or {\"task_id\": null} if none match.\n"
        "Only choose from the provided tasks.\n\n"
        f"Time Entry Description: {description}\n\n"
        "Available tasks:\n" + "\n".join(task_lines)
    )

    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You return concise JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 20,
    }
    if OPENAI_API_KEY:
        resp = requests.post(OPENAI_URL, headers=OPENAI_HEADERS, json=body, timeout=30)
        _safe_raise_for_status(resp)
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed.get("task_id")
        except Exception as exc:  # pragma: no cover - runtime logging
            logging.warning("OpenAI LLM parsing failed: %s", exc)
            return None

    if HF_API_TOKEN:
        hf_body = {
            "inputs": prompt,
            "parameters": {"max_new_tokens": 64, "temperature": 0.0},
        }
        resp = requests.post(
            f"{HF_API_BASE}/{HF_MODEL}", headers=HF_HEADERS, json=hf_body, timeout=60
        )
        _safe_raise_for_status(resp)
        try:
            data = resp.json()
            # HF text-generation returns list of dicts with generated_text
            generated = ""
            if isinstance(data, list) and data and "generated_text" in data[0]:
                generated = data[0]["generated_text"]
            elif isinstance(data, dict) and "generated_text" in data:
                generated = data["generated_text"]
            parsed = json.loads(generated.strip().splitlines()[-1])
            return parsed.get("task_id")
        except Exception as exc:  # pragma: no cover - runtime logging
            logging.warning("Hugging Face LLM parsing failed: %s", exc)
            return None

    logging.warning("No LLM credentials set; skipping matching")
    return None


def collect_task_options(limit: int = 100) -> List[Dict]:
    pages = fetch_pages_from_database(TASKS_DATABASE_ID, limit=limit)
    options: List[Dict] = []
    for page in pages:
        title = extract_title(page.get("properties", {}))
        if title:
            options.append({"id": page["id"], "name": title})
    return options


def process_time_entries(limit: int = 100) -> Dict[str, int]:
    time_entries = fetch_pages_from_database(
        TIME_ENTRIES_DATABASE_ID,
        filter_payload={
            "and": [
                {"property": PROJECT_PROPERTY_NAME, "select": {"equals": PROJECT_VALUE}},
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
    task_options = collect_task_options(limit=limit)
    task_index = build_task_indexes(task_options)
    if not task_options:
        logging.warning("No tasks fetched from TASKS_DATABASE_ID=%s", TASKS_DATABASE_ID)
    updated = 0
    skipped = 0
    reasons = {"no_description": 0, "already_linked": 0, "no_match": 0}
    sample_no_match: List[str] = []

    for entry in time_entries:
        description = extract_rich_text_content(entry, DESCRIPTION_PROPERTY_NAME)
        if not description:
            skipped += 1
            reasons["no_description"] += 1
            continue
        if has_task_relation(entry):
            skipped += 1
            reasons["already_linked"] += 1
            continue

        task_id = choose_task_for_description(description, task_options, task_index)
        if task_id:
            update_time_entry_task(entry["id"], task_id)
            updated += 1
        else:
            skipped += 1
            reasons["no_match"] += 1
            if len(sample_no_match) < 5:
                sample_no_match.append(description[:200])

    logging.info(
        "Skip reasons: %s; sample no-match descriptions: %s",
        reasons,
        sample_no_match,
    )
    return {"processed": len(time_entries), "updated": updated, "skipped": skipped}


def lambda_handler(event=None, context=None) -> Dict[str, object]:
    result = process_time_entries(limit=100)
    logging.info("Processing complete: %s", result)
    return {"statusCode": 200, "body": json.dumps(result)}


if __name__ == "__main__":
    lambda_handler()
