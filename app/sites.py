# app/sites.py — dashboard, sites, folders, members, search
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import or_

from app import db
from app.models import Site, SiteMember, Folder, File, User, FileEmbedding
from app.decorators import require_site_role
from app.ai import embed_text, cosine_similarity
from app.emailer import send_invite_email

sites_bp = Blueprint("sites", __name__)


@sites_bp.route("/")
@login_required
def dashboard():
    # sites the user owns OR is a member of
    member_site_ids = [m.site_id for m in SiteMember.query.filter_by(user_id=current_user.id)]
    sites = Site.query.filter(
        or_(Site.owner_id == current_user.id, Site.id.in_(member_site_ids))
    ).order_by(Site.created_at.desc()).all()

    # attach a member_count for the template
    for s in sites:
        s.member_count = SiteMember.query.filter_by(site_id=s.id).count()

    return render_template("dashboard.html", sites=sites)


@sites_bp.route("/sites", methods=["POST"])
@login_required
def create_site():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        flash("Site name is required.", "danger")
        return redirect(url_for("sites.dashboard"))

    site = Site(name=name, description=description, owner_id=current_user.id)
    db.session.add(site)
    db.session.flush()  # get site.id before commit

    db.session.add(SiteMember(site_id=site.id, user_id=current_user.id, role="owner"))
    db.session.commit()

    return redirect(url_for("sites.view_site", id=site.id))


@sites_bp.route("/sites/<int:id>")
@login_required
@require_site_role("viewer")
def view_site(id, site, role):
    folder_id = request.args.get("folder", type=int)

    folders = Folder.query.filter_by(site_id=site.id, parent_id=folder_id).order_by(Folder.name).all()
    files = File.query.filter_by(site_id=site.id, folder_id=folder_id).order_by(File.created_at.desc()).all()

    current_folder = None
    breadcrumbs = []
    if folder_id:
        current_folder = Folder.query.get_or_404(folder_id)
        f = current_folder
        while f:
            breadcrumbs.insert(0, f)
            f = Folder.query.get(f.parent_id) if f.parent_id else None

    members = SiteMember.query.filter_by(site_id=site.id).order_by(SiteMember.role.desc()).all()

    return render_template(
        "site.html",
        site=site, role=role, folders=folders, files=files,
        current_folder=current_folder, breadcrumbs=breadcrumbs, members=members
    )


@sites_bp.route("/sites/<int:site_id>/folders", methods=["POST"])
@login_required
@require_site_role("editor")
def create_folder(site_id, site, role):
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", type=int)

    if name:
        db.session.add(Folder(site_id=site.id, parent_id=parent_id, name=name, created_by=current_user.id))
        db.session.commit()

    if parent_id:
        return redirect(url_for("sites.view_site", id=site.id, folder=parent_id))
    return redirect(url_for("sites.view_site", id=site.id))


@sites_bp.route("/sites/<int:site_id>/members", methods=["POST"])
@login_required
@require_site_role("owner")
def add_member(site_id, site, role):
    raw_emails = request.form.get("email", "")
    new_role = request.form.get("role", "viewer")
    if new_role not in ("viewer", "editor", "owner"):
        new_role = "viewer"

    # Accept multiple emails separated by commas, semicolons, or whitespace.
    import re
    emails = [e.strip().lower() for e in re.split(r"[,;\s]+", raw_emails) if e.strip()]
    if not emails:
        flash("Enter at least one email address.", "danger")
        return redirect(url_for("sites.view_site", id=site.id))

    added, emailed, not_found = [], [], []
    for email in emails:
        user = User.query.filter_by(email=email).first()
        if not user:
            not_found.append(email)
            continue

        membership = SiteMember.query.filter_by(site_id=site.id, user_id=user.id).first()
        if membership:
            membership.role = new_role
        else:
            db.session.add(SiteMember(site_id=site.id, user_id=user.id, role=new_role))
        db.session.commit()
        added.append(user.email)

        if send_invite_email(
            user.email, site_name=site.name, role=new_role, inviter_name=current_user.name
        ):
            emailed.append(user.email)

    if added:
        msg = f"Added: {', '.join(added)}."
        if len(emailed) < len(added):
            msg += " (Some invite emails failed to send — check mail settings.)"
        flash(msg, "success")
    if not_found:
        flash(f"No user found for: {', '.join(not_found)}.", "danger")

    return redirect(url_for("sites.view_site", id=site.id))


@sites_bp.route("/sites/<int:site_id>/members/<int:user_id>/remove", methods=["POST"])
@login_required
@require_site_role("owner")
def remove_member(site_id, user_id, site, role):
    SiteMember.query.filter_by(site_id=site.id, user_id=user_id).delete()
    db.session.commit()
    return redirect(url_for("sites.view_site", id=site.id))


@sites_bp.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    files, folders = [], []
    semantic_used = False

    if q:
        member_site_ids = [m.site_id for m in SiteMember.query.filter_by(user_id=current_user.id)]
        accessible = or_(Site.owner_id == current_user.id, Site.id.in_(member_site_ids))
        accessible_site_ids = [s.id for s in Site.query.filter(accessible)]

        # Feature 2: try semantic search first if the user has at least one
        # embedded file in their accessible set. We compute the query embedding,
        # then score every accessible embedded file by cosine similarity, and
        # keep the top 50 above a small threshold.
        try:
            embedded_rows = (
                FileEmbedding.query
                .join(File, File.id == FileEmbedding.file_id)
                .filter(File.site_id.in_(accessible_site_ids))
                .all()
            )
        except Exception:
            embedded_rows = []

        if embedded_rows:
            try:
                qvec = embed_text(q)
                scored = [
                    (cosine_similarity(qvec, row.vector), row.file)
                    for row in embedded_rows
                ]
                scored.sort(key=lambda x: x[0], reverse=True)
                # Threshold of 0.5 is conservative — Gemini embeddings are
                # usually 0.6+ for clearly-related, <0.4 for unrelated.
                top = [f for s, f in scored if s >= 0.5][:50]
                if top:
                    files = top
                    semantic_used = True
                else:
                    # No good semantic matches — fall back to LIKE so we
                    # don't return zero results when the user typed a
                    # filename fragment.
                    like = f"%{q}%"
                    files = (
                        File.query.filter(
                            File.site_id.in_(accessible_site_ids),
                            File.original_name.ilike(like),
                        )
                        .order_by(File.created_at.desc()).limit(50).all()
                    )
            except Exception as e:
                current_app.logger.warning("Semantic search failed: %s", e)

        if not semantic_used:
            like = f"%{q}%"
            files = (
                File.query.filter(
                    File.site_id.in_(accessible_site_ids),
                    File.original_name.ilike(like),
                )
                .order_by(File.created_at.desc()).limit(50).all()
            )

        # Folders still use LIKE — they're short named and embedding them
        # adds little value.
        folders = (
            Folder.query.filter(Folder.site_id.in_(accessible_site_ids), Folder.name.ilike(f"%{q}%"))
            .order_by(Folder.created_at.desc()).limit(50).all()
        )

    return render_template(
        "search.html", q=q, files=files, folders=folders, semantic_used=semantic_used
    )
