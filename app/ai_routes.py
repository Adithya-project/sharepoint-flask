"""
app/ai_routes.py — the natural-language command bar.

POST /ai/command   body={"text": "...", "site_id": 7 (optional)}
  - Calls Gemini to parse the text into a structured action.
  - Resolves names to real IDs (site, folder, file, member).
  - Re-runs the same permission checks the regular routes use — the AI layer
    cannot bypass roles.
  - Performs the action by calling the same DB code as the regular routes.
  - Returns a short JSON message the UI can show.

GET  /ai/embeddings/backfill   re-embed all accessible files (admin helper).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import or_

from app import db
from app.ai import parse_command, embed_text
from app.emailer import send_invite_email
from app.decorators import require_site_role
from app.models import (
    File, Folder, Site, SiteMember, User, ROLE_RANK,
)

ai_bp = Blueprint("ai", __name__, url_prefix="/ai")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_accessible_sites() -> list[Site]:
    member_site_ids = [m.site_id for m in SiteMember.query.filter_by(user_id=current_user.id)]
    return Site.query.filter(
        or_(Site.owner_id == current_user.id, Site.id.in_(member_site_ids))
    ).order_by(Site.name).all()


def _resolve_site(name: str | None) -> Site | None:
    """Find a site by name that the current user is allowed to see.
    Returns None if name is None OR the user doesn't have access."""
    if not name:
        return None
    accessible = _user_accessible_sites()
    needle = name.lower()
    # Prefer exact match, then substring, in alphabetical order.
    for s in sorted(accessible, key=lambda s: s.name):
        if s.name.lower() == needle:
            return s
    for s in sorted(accessible, key=lambda s: s.name):
        if needle in s.name.lower() or s.name.lower() in needle:
            return s
    return None


def _resolve_folder(site: Site, name: str | None) -> Folder | None:
    if not name:
        return None
    needle = name.lower()
    folders = Folder.query.filter_by(site_id=site.id).all()
    for f in folders:
        if f.name.lower() == needle:
            return f
    for f in folders:
        if needle in f.name.lower() or f.name.lower() in needle:
            return f
    return None


def _resolve_file(site: Site, name: str | None) -> File | None:
    if not name:
        return None
    needle = name.lower()
    files = File.query.filter_by(site_id=site.id).all()
    for f in files:
        if f.original_name.lower() == needle:
            return f
    for f in files:
        if needle in f.original_name.lower() or f.original_name.lower() in needle:
            return f
    return None


def _resolve_user_by_email(email: str | None) -> User | None:
    if not email:
        return None
    return User.query.filter_by(email=email.strip().lower()).first()


def _assert_role(site: Site, min_role: str) -> tuple[bool, str | None]:
    """Same check the @require_site_role decorator does. Returns (ok, role)."""
    role = site.role_for(current_user)
    if not role or ROLE_RANK[role] < ROLE_RANK[min_role]:
        return False, role
    return True, role


# ---------------------------------------------------------------------------
# Command bar entry point
# ---------------------------------------------------------------------------

@ai_bp.route("/command", methods=["POST"])
@login_required
def command():
    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    raw_site_id = data.get("site_id")
    try:
        current_site_id = int(raw_site_id) if raw_site_id not in (None, "") else None
    except (TypeError, ValueError):
        current_site_id = None

    if not text:
        return jsonify({"ok": False, "message": "Empty command."}), 400

    # Figure out which site context (if any) the user is in. We pass the
    # name to Gemini so it can resolve "here" / "this site".
    current_site: Site | None = None
    if current_site_id:
        current_site = Site.query.get(current_site_id)
        if current_site and not current_site.has_role_at_least(current_user, "viewer"):
            current_site = None  # user can't actually see it — don't leak name

    accessible = _user_accessible_sites()
    try:
        action = parse_command(
            text,
            current_site=current_site.name if current_site else None,
            accessible_sites=[s.name for s in accessible],
        )
    except Exception as e:
        current_app.logger.exception("AI command parsing failed")
        return jsonify({
            "ok": False,
            "message": f"AI parsing failed: {type(e).__name__}: {e}",
        }), 500

    kind = (action.get("action") or "").lower()
    try:
        result = _dispatch(kind, action, current_site, accessible)
    except PermissionError as e:
        return jsonify({"ok": False, "message": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        current_app.logger.exception("AI action failed")
        return jsonify({
            "ok": False,
            "message": f"Action failed: {type(e).__name__}: {e}",
        }), 500

    return jsonify({"ok": True, **result})


def _dispatch(
    kind: str,
    action: dict[str, Any],
    current_site: Site | None,
    accessible: list[Site],
) -> dict[str, Any]:
    """Run the parsed action. Returns {"message": ..., "redirect": ...}."""

    if kind == "help":
        return {"message": _help_text()}

    if kind == "unsupported":
        return {"message": action.get("message") or "I couldn't figure that out."}

    if kind == "list":
        return _do_list(action, accessible)

    # Everything else resolves to a site first.
    site_name = action.get("site") or (current_site.name if current_site else None)
    site = _resolve_site(site_name)
    if site is None:
        raise ValueError(
            f"I couldn't find a site called {site_name!r} that you have access to."
        )

    if kind == "create_folder":
        return _do_create_folder(site, action)
    if kind == "share":
        return _do_share(site, action)
    if kind == "search":
        return _do_search(site, action)
    if kind == "open":
        return _do_open(site, action)
    if kind == "comment":
        return _do_comment(site, action)
    if kind == "summarize":
        return _do_summarize(site, action)

    raise ValueError(f"Unknown action kind: {kind!r}")


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------

def _do_list(action: dict, accessible: list[Site]) -> dict:
    target_site_name = action.get("site")
    if target_site_name:
        site = _resolve_site(target_site_name)
        if site is None:
            raise ValueError(f"No accessible site called {target_site_name!r}.")
        folders = Folder.query.filter_by(site_id=site.id, parent_id=None).order_by(Folder.name).all()
        files = File.query.filter_by(site_id=site.id, folder_id=None).order_by(File.created_at.desc()).limit(20).all()
        lines = [f"Top of {site.name}:"]
        for f in folders:
            lines.append(f"  📁 {f.name}")
        for fl in files:
            lines.append(f"  📄 {fl.original_name}")
        return {"message": "\n".join(lines)}
    if not accessible:
        return {"message": "You don't belong to any sites yet."}
    return {
        "message": "Your sites: " + ", ".join(s.name for s in accessible)
    }


def _do_create_folder(site: Site, action: dict) -> dict:
    ok, _ = _assert_role(site, "editor")
    if not ok:
        raise PermissionError(f"You need editor role on {site.name} to create folders.")
    name = (action.get("name") or "").strip()
    if not name:
        raise ValueError("I need a name for the new folder.")
    parent = _resolve_folder(site, action.get("folder"))
    f = Folder(site_id=site.id, parent_id=parent.id if parent else None, name=name, created_by=current_user.id)
    db.session.add(f)
    db.session.commit()
    target = url_for("sites.view_site", id=site.id, folder=f.id)
    return {
        "message": action.get("message") or f'Created folder "{name}" in {site.name}.',
        "redirect": target,
    }


def _do_share(site: Site, action: dict) -> dict:
    ok, _ = _assert_role(site, "owner")
    if not ok:
        raise PermissionError(f"Only owners of {site.name} can share files or invite members.")
    email = (action.get("share_with_email") or "").strip().lower()
    if not email:
        raise ValueError("I need an email to share with.")
    role = (action.get("share_role") or "viewer").lower()
    if role not in ("viewer", "editor", "owner"):
        role = "viewer"

    file = _resolve_file(site, action.get("file"))
    if file:
        # Sharing a file: ensure the user is a site member; if not, add them.
        user = _resolve_user_by_email(email)
        if not user:
            raise ValueError(f"No registered user with email {email!r}.")
        membership = SiteMember.query.filter_by(site_id=site.id, user_id=user.id).first()
        if not membership:
            db.session.add(SiteMember(site_id=site.id, user_id=user.id, role=role))
        elif ROLE_RANK[role] > ROLE_RANK[membership.role]:
            membership.role = role
        db.session.commit()
        send_invite_email(
            email, site_name=site.name, role=role,
            inviter_name=current_user.name, file_name=file.original_name,
            attachment_path=os.path.join(current_app.config["UPLOAD_DIR"], file.stored_name),
        )
        return {
            "message": action.get("message")
                or f'Ensured {email} has {role} access to {site.name} (file: {file.original_name}).',
            "redirect": url_for("files.view_file", site_id=site.id, file_id=file.id),
        }
    else:
        # No file named — interpret as site-level invitation.
        user = _resolve_user_by_email(email)
        if not user:
            raise ValueError(f"No registered user with email {email!r}.")
        membership = SiteMember.query.filter_by(site_id=site.id, user_id=user.id).first()
        if membership:
            membership.role = role
        else:
            db.session.add(SiteMember(site_id=site.id, user_id=user.id, role=role))
        db.session.commit()
        send_invite_email(
            email, site_name=site.name, role=role, inviter_name=current_user.name
        )
        return {
            "message": action.get("message")
                or f'Invited {email} to {site.name} as {role}.',
            "redirect": url_for("sites.view_site", id=site.id),
        }


def _do_search(site: Site, action: dict) -> dict:
    q = (action.get("search_query") or "").strip()
    if not q:
        raise ValueError("I need something to search for.")
    return {
        "message": action.get("message") or f'Searching {site.name} for "{q}".',
        "redirect": url_for("sites.search") + f"?q={q}&site_id={site.id}",
    }


def _do_open(site: Site, action: dict) -> dict:
    file = _resolve_file(site, action.get("file"))
    if not file:
        raise ValueError(f"I couldn't find that file in {site.name}.")
    return {
        "message": action.get("message") or f'Opening {file.original_name}.',
        "redirect": url_for("files.view_file", site_id=site.id, file_id=file.id),
    }


def _do_comment(site: Site, action: dict) -> dict:
    ok, _ = _assert_role(site, "viewer")
    if not ok:
        raise PermissionError(f"You need at least viewer access on {site.name} to comment.")
    file = _resolve_file(site, action.get("file"))
    if not file:
        raise ValueError(f"I couldn't find that file in {site.name}.")
    body = (action.get("comment_body") or "").strip()
    if not body:
        raise ValueError("I need some text for the comment.")
    db.session.add(Comment(file_id=file.id, user_id=current_user.id, body=body))
    db.session.commit()
    return {
        "message": action.get("message") or f'Commented on {file.original_name}.',
        "redirect": url_for("files.view_file", site_id=site.id, file_id=file.id),
    }


def _do_summarize(site: Site, action: dict) -> dict:
    # Feature 4 uses the same route — just opens the file page, where the
    # summary is generated on demand and cached.
    file = _resolve_file(site, action.get("file"))
    if not file:
        raise ValueError(f"I couldn't find that file in {site.name}.")
    return {
        "message": action.get("message") or f'Opening {file.original_name} with a summary.',
        "redirect": url_for("files.view_file", site_id=site.id, file_id=file.id) + "?ai_summary=1",
    }


# Late import to avoid circular: Comment is referenced by _do_comment.
from app.models import Comment, FileEmbedding  # noqa: E402,F401


def _help_text() -> str:
    return (
        "Try things like:\n"
        '  • "create a folder called Invoices in Finance"\n'
        '  • "share budget.xlsx with john@office.com as editor in Marketing"\n'
        '  • "find the Q3 contract in Finance"\n'
        '  • "open Design Spec.pdf in Marketing"\n'
        '  • "comment \'looks good\' on report.pdf in Marketing"\n'
        '  • "summarize Design Spec.pdf in Marketing"\n'
        '  • "list my sites"'
    )
