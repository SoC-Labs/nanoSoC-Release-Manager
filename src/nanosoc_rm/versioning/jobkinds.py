"""The versioning job kinds (`versions-refresh`, `version-draft`), registered on import so the job runner finds
them through jobs.KIND_MODULES as it finds the quality and release kinds."""
from __future__ import annotations

from nanosoc_rm.versioning.commands import ensure_job_kinds

ensure_job_kinds()
