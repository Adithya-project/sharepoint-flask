# app/decorators.py — role-based access control for sites
from functools import wraps
from flask import abort
from flask_login import current_user

from app.models import Site, ROLE_RANK


def require_site_role(min_role):
    """
    Decorator for routes that take a site_id (or id) URL param.
    Loads the Site, checks the current user's role against min_role,
    and injects `site` and `role` as kwargs into the view function.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            site_id = kwargs.get("site_id") or kwargs.get("id")
            site = Site.query.get_or_404(site_id)

            role = site.role_for(current_user)
            if not role or ROLE_RANK[role] < ROLE_RANK[min_role]:
                abort(403)

            kwargs["site"] = site
            kwargs["role"] = role
            return view_func(*args, **kwargs)
        return wrapped
    return decorator
