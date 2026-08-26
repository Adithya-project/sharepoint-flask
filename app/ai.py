"""
app/ai.py — shared Gemini helpers used by the command bar, semantic search,
auto-folder suggestion, and file summarization.

One place so all four features use the same client, the same retry handling,
and the same model configuration. If GEMINI_API_KEY is missing, every helper
raises a clear RuntimeError that the routes catch and turn into a flash
message instead of a 500.
"""

import os
import json
import logging
from typing import Any

import google.genai as genai
from flask import current_app

log = logging.getLogger(__name__)

_client: genai.Client | None = None


def get_client() -> genai.Client:
    """Lazy-init the Gemini client. Fails loudly if the key isn't set."""
    global _client
    if _client is None:
        api_key = current_app.config.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. "
                "Copy .env.example to .env and add your key, "
                "or set it in your shell environment."
            )
        _client = genai.Client(api_key=api_key)
    return _client


def reset_client() -> None:
    """For tests / hot-reload — drop the cached client."""
    global _client
    _client = None


# ---------- command bar (Feature 1) ----------

# Schema we ask Gemini to return. We deliberately use a flat dict (not nested)
# so a small model can output it reliably as JSON.
COMMAND_SCHEMA_DESCRIPTION = """
Return a JSON object with these fields (use null for any you cannot determine):

  action:           one of "create_folder", "share", "search", "comment",
                    "open", "list", "summarize", "help", "unsupported"
  site:             name of the site the user is referring to, if any
  folder:           name of a folder inside that site, if any
  file:             name (or partial name) of a file, if any
  name:             name to use when creating a folder
  share_with_email: email address to share with
  share_role:       "viewer", "editor", or "owner"
  search_query:     what to search for
  comment_body:     the body of a comment to add
  summary_kind:     "short" (one sentence) or "detailed" (a paragraph)
  message:          one short human-friendly sentence describing what you did
                    (or what you need clarified). Use first person ("I created…")

Rules:
  - Resolve "this site" or "here" to the current site if one is given.
  - If a name is ambiguous, pick the closest match but mention it in `message`.
  - If you genuinely cannot do the request, set action to "unsupported" and
    explain in `message`.
"""


def parse_command(
    user_text: str,
    *,
    current_site: str | None = None,
    accessible_sites: list[str] | None = None,
) -> dict[str, Any]:
    """
    Turn a free-form English command into a structured action dict.
    `current_site` is the site the user is currently viewing (if any) — used
    to resolve "this site" / "here".
    `accessible_sites` is a list of site names the user is allowed to see,
    passed in so Gemini can pick a real one rather than hallucinating.
    """
    client = get_client()
    model = current_app.config["GEMINI_MODEL"]

    context_bits = []
    if current_site:
        context_bits.append(f'The user is currently viewing the site "{current_site}".')
    if accessible_sites:
        context_bits.append(
            "Sites the user has access to: "
            + ", ".join(f'"{s}"' for s in accessible_sites)
        )

    prompt = (
        "You are the command parser for a file-sharing app called TeamSpace.\n"
        f"User command: {user_text!r}\n\n"
        + ("\n".join(context_bits) + "\n" if context_bits else "")
        + COMMAND_SCHEMA_DESCRIPTION
    )

    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "temperature": 0.0,
        },
    )
    return json.loads(resp.text)


# ---------- semantic search (Feature 2) ----------

def embed_text(text: str) -> list[float]:
    """Return a vector embedding for `text` using the configured Gemini model."""
    client = get_client()
    model = current_app.config["GEMINI_EMBED_MODEL"]
    resp = client.models.embed_content(model=model, contents=text)
    # The SDK returns embeddings as a list of Embedding objects; we want the
    # first one's values, which is a list[float].
    return list(resp.embeddings[0].values)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain-Python cosine similarity. No numpy — keeps the dep list small."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------- auto-folder suggestion (Feature 3) ----------

FOLDER_SUGGESTION_PROMPT = """\
You suggest folders for newly uploaded files in a file-sharing app.

The file is named: {filename}
The site name is: {site_name}
Existing folders in this site (in this folder):
{folder_list}

Reply with JSON only:
  {{"folder": "<best matching folder name, or null if none fit>",
   "confidence": "high" | "medium" | "low",
   "reason": "<one short sentence>"}}

Pick null when the file clearly doesn't belong in any of the existing folders.
Don't invent folder names — only use names from the list, or null.
"""


def suggest_folder(
    filename: str,
    site_name: str,
    existing_folder_names: list[str],
) -> dict[str, Any]:
    client = get_client()
    model = current_app.config["GEMINI_MODEL"]
    folder_list = (
        "\n".join(f"- {n}" for n in existing_folder_names)
        if existing_folder_names
        else "(no folders yet — root level)"
    )
    prompt = FOLDER_SUGGESTION_PROMPT.format(
        filename=filename, site_name=site_name, folder_list=folder_list
    )
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0.0},
    )
    return json.loads(resp.text)


# ---------- file summarization (Feature 4) ----------

SUMMARIZE_PROMPT = """\
You summarize files for a file-sharing app called TeamSpace.

File: {filename}
Kind: {kind}    (short = one sentence, detailed = a short paragraph)
Text content (may be truncated):
---
{content}
---

Reply with JSON only:
  {{"summary": "<the summary>"}}
"""


def summarize_text(filename: str, text: str, kind: str = "short") -> str:
    """Return a one-sentence or short-paragraph summary of `text`."""
    client = get_client()
    model = current_app.config["GEMINI_MODEL"]
    # Cap input — large PDFs would otherwise blow the context window.
    truncated = text[:20_000]
    prompt = SUMMARIZE_PROMPT.format(
        filename=filename, kind=kind, content=truncated
    )
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0.2},
    )
    return json.loads(resp.text)["summary"]


# ---------- text extraction (Feature 4) ----------

# Extensions we can read as plain text. Anything else returns None and the
# UI shows a "cannot summarize" message.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".rb",
    ".go", ".rs", ".sh", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".log", ".sql",
}


def extract_text(file_path: str, original_name: str) -> str | None:
    """Return extracted text from a file on disk, or None if we don't know
    how to read it. Raises on read errors after the file exists."""
    import os
    from pathlib import Path

    ext = Path(original_name).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return None
        reader = PdfReader(file_path)
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                chunks.append("")
        return "\n".join(chunks)
    return None
