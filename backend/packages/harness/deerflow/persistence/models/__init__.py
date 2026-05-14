"""ORM model registration entry point.

Importing this module ensures all ORM models are registered with
``Base.metadata`` so Alembic autogenerate detects every table.

The actual ORM classes have moved to entity-specific subpackages:
- ``deerflow.persistence.thread_meta``
- ``deerflow.persistence.run``
- ``deerflow.persistence.feedback``
- ``deerflow.persistence.user``
- ``deerflow.persistence.workspace``  (Stage 0 PR3)
- ``deerflow.persistence.workspace_membership``  (Stage 0 PR3)
- ``deerflow.persistence.service_account``  (Stage 0 PR8)
- ``deerflow.persistence.api_key``  (Stage 0 PR8)

``RunEventRow`` remains in ``deerflow.persistence.models.run_event`` because
its storage implementation lives in ``deerflow.runtime.events.store.db`` and
there is no matching entity directory.
"""

from deerflow.persistence.api_key.model import ApiKeyRow
from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.models.run_event import RunEventRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.service_account.model import ServiceAccountRow
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.user.model import UserRow
from deerflow.persistence.workspace.model import WorkspaceRow
from deerflow.persistence.workspace_membership.model import WorkspaceMembershipRow

__all__ = [
    "ApiKeyRow",
    "FeedbackRow",
    "RunEventRow",
    "RunRow",
    "ServiceAccountRow",
    "ThreadMetaRow",
    "UserRow",
    "WorkspaceMembershipRow",
    "WorkspaceRow",
]
