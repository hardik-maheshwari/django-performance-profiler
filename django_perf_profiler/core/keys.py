"""
core.keys
---------
Single source of truth for the scoped type-cache / blacklist key:

    {rel_path}::{func_name}::{var_name}

This is the shared contract between static_life_saver (reader: the
DYNAMIC_TYPE_CACHE / DYNAMIC_BLACKLIST lookups in visitors.py, and the wizard
writes in config.py) and dynamic_life_saver (writer: runtime type harvesting,
Phase 1). Extracted so a third drifting key format can never appear -- the
same disease core.boundaries cures for first-party classification.

Contract (do not renegotiate silenetly; existing caches depend on it):
    - rel_path is relative to the repo root, forward-slash separated.
    - func_name is the BARE function name (frame code.co_name on the dynamic
    side, the post-'Class.method'-split target_func_name on the static side),
    never a qualname. This deliberately inherits static's same-file
    name-collision weakness: two functions with the same name in one file
    share a key. Fixing that would orphan every existing cache entry, so it
    stays until a cache migration is worth it.
    - The root is core.boundaries.PROJECT_ROOT (cwd unless LIFESAVER_PROJECT_ROOT
    is set). A host-side run and an in-container dynamic run produce
    identical keys iff both anchor at the repo at the repo root -- set
    LIFESAVER_PROJECT_ROOT in the container to make that explicit rather than
    accidental.
"""

import os

from . import boundaries

SEP = "::"


def rel_to_root(path):
    """Repo-root-relative, forward-slash path.
    
    Mirrors the `os.path.relpath(file_path, os.getcwd())` the static visitors
    used inline, but anchored on boundaries.PROJECT_ROOT so the container and
    the host can be aligned via LIFESAVER_PROJECT_ROOT. Behavior is byte-
    identical to the old inline form when the env var is unset and the tool
    runs from the repo root (PROJECT_ROOT defaults to cwd at import time).
    """
    try:
        rel = os.path.relpath(path, boundaries.PROJECT_ROOT)
    except ValueError:
        # e.g. different drive letter on Windows -- fall back to the raw path
        # rather than guessing; a non-matching key is inert, a wrong one isn't.
        rel = path
    return rel.replace("\\", "/")


def scoped_key(rel_path, func_name, var_name):
    """Build the key from an ALREADY root-relative path.
    
    Deliberately does not relativize or normalize: a call site passing an
    absolute path here has drifted, and silently fixing it would hide that.
    Use scoped_key_for_file when starting from an absolute path.
    """
    return f"{rel_path}{SEP}{func_name}{SEP}{var_name}"


def scoped_key_for_file(abs_or_rel_path, func_name, var_name):
    """Relativize + build in one step (the writer-side convenience)."""
    return scoped_key(rel_to_root(abs_or_rel_path), func_name, var_name)

