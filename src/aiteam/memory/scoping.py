"""Directory-fingerprint memory bucketing for unregistered project directories.

Direction-layer memory isolation for unregistered directories (a cwd matching no OS project).

Background (one of the root causes of the 2026-07-21 cross-talk incident): memory.py's _resolve_scope_id, when
scope=project and no registered project resolves, silently fell back to the global "system" bucket — equal to
broadcasting "this directory's project
memories" into global memories effective for every session. The fix gives each unregistered directory a temporary bucket
scope_id derived from its normalized cwd fingerprint,
achieving "inherit within the project; memory isolation is a directory's default right, not a registration privilege".

Design points:
- The bucket prefix "dir:" is easy for humans to recognize (distinct from real project ids and the global
"system"/"user"), and also reserves an identification anchor for a future
  regularization at registration time, when the temporary bucket's memories are absorbed into the project bucket.
- The fingerprint's sole authority is the server side: the write path (POST /api/memories) and the read path (GET
/api/memories)
  both derive it from the X-Project-Dir header via this function, avoiding copying the formula into each hook copy and
  causing silent drift
  (silent drift is exactly the category this incident must eradicate). A hook is responsible only for passing the cwd to
  the API.
"""

from __future__ import annotations

import hashlib
import os

# Temporary bucket prefix — the scope_id namespace when scope=project but the directory is unregistered.
DIR_BUCKET_PREFIX = "dir:"


def dir_bucket_scope_id(cwd: str) -> str:
    """Derive the direction-memory temp-bucket scope_id for an unregistered dir.

    Normalization = the absolute path after os.path.realpath (symlinks resolved + relative segments removed, and
    idempotent:
    realpath(realpath(p)) == realpath(p), so passing the raw cwd or an already-normalized path gives the same result).
    Fingerprint = sha1(normalized path UTF-8).hexdigest()[:16], prefixed with "dir:".

    Args:
        cwd: the directory path (usually from the X-Project-Dir header, or the hook's current working directory).

    Returns:
        A bucket scope_id of the form "dir:1a2b3c4d5e6f7a8b"; returns "" when cwd is empty/pure whitespace
        (the caller accordingly refuses the write or falls back to global+user reads only).
    """
    if not cwd or not cwd.strip():
        return ""
    normalized = os.path.realpath(cwd.strip())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{DIR_BUCKET_PREFIX}{digest}"
