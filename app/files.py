# app/files.py — file upload / download / delete / comments
import os
import uuid

from flask import Blueprint, request, redirect, url_for, send_from_directory, current_app, render_template, flash
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db
from app.models import File, Comment, FileEmbedding, Folder
from app.decorators import require_site_role
from app.ai import embed_text, suggest_folder, summarize_text, extract_text

files_bp = Blueprint("files", __name__)


@files_bp.route("/sites/<int:site_id>/upload", methods=["POST"])
@login_required
@require_site_role("editor")
def upload_file(site_id, site, role):
    uploaded = request.files.get("file")
    folder_id = request.form.get("folder_id", type=int)

    if not uploaded or uploaded.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("sites.view_site", id=site.id, folder=folder_id) if folder_id
                         else url_for("sites.view_site", id=site.id))

    original_name = secure_filename(uploaded.filename)
    ext = os.path.splitext(original_name)[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(current_app.config["UPLOAD_DIR"], stored_name)
    uploaded.save(save_path)

    file_record = File(
        site_id=site.id,
        folder_id=folder_id,
        stored_name=stored_name,
        original_name=original_name,
        size=os.path.getsize(save_path),
        mime_type=uploaded.mimetype,
        uploaded_by=current_user.id,
    )
    db.session.add(file_record)
    db.session.commit()

    # Feature 2: embed the file's name so semantic search works on it. Done
    # after commit so the file has an id. Failure to embed shouldn't fail
    # the upload — just log and move on.
    try:
        vec = embed_text(original_name)
        db.session.add(FileEmbedding(
            file_id=file_record.id,
            vector=vec,
            source_text=original_name,
        ))
        db.session.commit()
    except Exception as e:
        current_app.logger.warning("Embedding failed for %s: %s", original_name, e)
        db.session.rollback()

    # Feature 3: ask Gemini which folder this file should live in. Only
    # meaningful when the file is in the root of the site (i.e. user didn't
    # already pick a folder). If a folder was chosen, the user already knows
    # where it goes — don't second-guess them.
    if not folder_id:
        try:
            existing = Folder.query.filter_by(site_id=site.id, parent_id=None).all()
            existing_names = [f.name for f in existing]
            suggestion = suggest_folder(original_name, site.name, existing_names)
            target_name = (suggestion.get("folder") or "").strip()
            if target_name and target_name.lower() != "null":
                target = next(
                    (f for f in existing if f.name.lower() == target_name.lower()),
                    None,
                )
                if target and target.id != file_record.folder_id:
                    file_record.ai_suggested_folder_id = target.id
                    file_record.ai_suggestion_reason = suggestion.get("reason") or ""
                    db.session.commit()
        except Exception as e:
            current_app.logger.warning("Folder suggestion failed for %s: %s", original_name, e)
            db.session.rollback()

    if folder_id:
        return redirect(url_for("sites.view_site", id=site.id, folder=folder_id))
    return redirect(url_for("sites.view_site", id=site.id))


@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/download")
@login_required
@require_site_role("viewer")
def download_file(site_id, file_id, site, role):
    file_record = File.query.filter_by(id=file_id, site_id=site.id).first_or_404()
    return send_from_directory(
        current_app.config["UPLOAD_DIR"],
        file_record.stored_name,
        as_attachment=True,
        download_name=file_record.original_name,
    )


@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/delete", methods=["POST"])
@login_required
@require_site_role("editor")
def delete_file(site_id, file_id, site, role):
    file_record = File.query.filter_by(id=file_id, site_id=site.id).first_or_404()

    file_path = os.path.join(current_app.config["UPLOAD_DIR"], file_record.stored_name)
    if os.path.exists(file_path):
        os.remove(file_path)

    folder_id = file_record.folder_id
    db.session.delete(file_record)
    db.session.commit()

    if folder_id:
        return redirect(url_for("sites.view_site", id=site.id, folder=folder_id))
    return redirect(url_for("sites.view_site", id=site.id))


@files_bp.route("/sites/<int:site_id>/files/<int:file_id>")
@login_required
@require_site_role("viewer")
def view_file(site_id, file_id, site, role):
    file_record = File.query.filter_by(id=file_id, site_id=site.id).first_or_404()
    comments = Comment.query.filter_by(file_id=file_record.id).order_by(Comment.created_at.asc()).all()
    return render_template("file.html", site=site, role=role, file=file_record, comments=comments)


@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/comments", methods=["POST"])
@login_required
@require_site_role("viewer")
def add_comment(site_id, file_id, site, role):
    body = request.form.get("body", "").strip()
    if body:
        db.session.add(Comment(file_id=file_id, user_id=current_user.id, body=body))
        db.session.commit()
    return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))


# --- Feature 3: accept / dismiss a folder suggestion ---

@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/accept-suggestion", methods=["POST"])
@login_required
@require_site_role("editor")
def accept_suggestion(site_id, file_id, site, role):
    file_record = File.query.filter_by(id=file_id, site_id=site.id).first_or_404()
    if file_record.ai_suggested_folder_id is None:
        flash("No suggestion to accept.", "warning")
        return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))
    file_record.folder_id = file_record.ai_suggested_folder_id
    file_record.ai_suggested_folder_id = None
    file_record.ai_suggestion_reason = None
    db.session.commit()
    flash(f"Moved {file_record.original_name} to the suggested folder.", "success")
    return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))


@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/dismiss-suggestion", methods=["POST"])
@login_required
@require_site_role("editor")
def dismiss_suggestion(site_id, file_id, site, role):
    file_record = File.query.filter_by(id=file_id, site_id=site.id).first_or_404()
    file_record.ai_suggested_folder_id = None
    file_record.ai_suggestion_reason = None
    file_record.ai_suggestion_dismissed = True
    db.session.commit()
    return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))


# --- Feature 4: AI summary ---

@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/generate-summary", methods=["POST"])
@login_required
@require_site_role("viewer")
def generate_summary(site_id, file_id, site, role):
    file_record = File.query.filter_by(id=file_id, site_id=site.id).first_or_404()
    kind = (request.form.get("kind") or "short").lower()
    if kind not in ("short", "detailed"):
        kind = "short"

    file_path = os.path.join(current_app.config["UPLOAD_DIR"], file_record.stored_name)
    text = extract_text(file_path, file_record.original_name)
    if text is None:
        flash(
            f"Can't summarize {file_record.original_name} — file type not supported "
            "(works for .txt, .md, .csv, code files, and .pdf).",
            "warning",
        )
        return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))
    if not text.strip():
        flash("The file looks empty — nothing to summarize.", "warning")
        return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))

    try:
        summary = summarize_text(file_record.original_name, text, kind=kind)
    except Exception as e:
        current_app.logger.exception("Summarize failed")
        flash(f"Summary failed: {e}", "danger")
        return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))

    file_record.summary = summary
    file_record.summary_kind = kind
    db.session.commit()
    flash("Summary generated.", "success")
    return redirect(url_for("files.view_file", site_id=site.id, file_id=file_id))


@files_bp.route("/sites/<int:site_id>/files/<int:file_id>/regenerate-summary", methods=["POST"])
@login_required
@require_site_role("viewer")
def regenerate_summary(site_id, file_id, site, role):
    # Identical to generate, but the user has already seen a summary — we
    # just overwrite it with a different kind. Reuses the same code path.
    return generate_summary(site_id=site_id, file_id=file_id, site=site, role=role)
