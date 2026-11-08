"""What other pages need to offer "+ Mesa" (ADR-067). No routes here.

A route module may not import another one (``tests/test_web_package.py``), so
the projects list every "add to a desk" form reads lives in this helper.
"""

from __future__ import annotations

from fastapi import Request

from zettel.web.rendering import service


def desk_choices(request: Request) -> list[dict[str, str]]:
    """``{"project_id", "title"}`` for each desk, most recently edited first."""
    from zettel.projects import list_projects

    return [
        {"project_id": p.project_id, "title": p.title} for p in list_projects(service(request).cfg)
    ]
