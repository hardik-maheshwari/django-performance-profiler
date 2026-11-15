"""
core.harvest_store
==================

The sidecar file contract: where runtime type observations live, what shape
they have, and how new observations merge in. Shared by dynamic_life_saver
(writer: TypeHarvester.flush) and static_life_saver (reader: Phase 3), for the
same reason core.keys exists -- one implementation, zero drift.

The sidecar is EVIDENCE, never DECISIONS:
    - it is a separate file from the wizard-owned .type_cache.json, which only
      a human (or the explicit Phase 4 promotion step) writes;
    - conflicts are preserved, not resolved: a key observed with two types keeps
      both, with counts. Resolution happens at read/promotion time.

Schema (version 1):
    {
        "version": 1,
        "types":     { "<scoped_key>": {"TypeName": count, ...}, ... },  # LOCAL classes
        "non_local": { "<scoped_key>": {"TypeName": count, ...}, ... }   # blacklist candidates
    }

Location: LIFESAVER_HARVEST_FILE if set (read per call, so the container can
point at the volume-mounted toolkit); otherwise <toolkit repo root>/
.type_cache.harvested.json. The env var is how host static runs and in-container
dynamic runs agree on one file -- same pattern as LIFESAVER_PROJECT_ROOT.

Write mechanics: read-merge-write with os.replace for atomicity. No file
locking -- the DEBUG workflow is one traced request at a time by design; if
that ever changes, locking is the first thing to add here.

A corrupt sidecar is renamed aside to <path>.corrupt (evidence preserved for
inspection) and treated as empty, rather than crashing the traced request or
silently overwriting the bytes.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

SIDECAR_BASENAME = ".type_cache.harvested.json"
SCHEMA_VERSION = 1

def get_sidecar_path():
    env = os.environ.get("LIFESAVER_HARVEST_FILE")
    if env:
        return os.path.abspath(env)
    # core/ -> django_perf_profiler/ -> repo root
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, SIDECAR_BASENAME)

def _empty():
    return {
        "version": SCHEMA_VERSION,
        "types": {},
        "non_local": {},
    }

def load(path=None):
    """Read the sidecar; tolerate absence; quarantine corruption."""
    path = path or get_sidecar_path()
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("sidecar root is not an object")
        return {
            "version": data.get("version", SCHEMA_VERSION),
            "types": dict(data.get("types", {})),
            "non_local": dict(data.get("non_local", {})),
        }
    except Exception as e:
        quarantine = path + ".corrupt"
        try:
            os.replace(path, quarantine)
            logger.warning("harvest_store: corrupt sidecar quarantined -> %s (%s)", quarantine, e)
        except Exception:
            logger.warning("harvest_store: corrupt sidecar, could not quarantine (%s)", e)
        return _empty()

def _merge_buckets(dst, src):
    for key, observations in src.items():
        per_key = dst.setdefault(key, {})
        for type_name, count in observations.items():
            per_key[type_name] = per_key.get(type_name, 0) + int(count)

def merge_and_save(local_types, non_local, path=None):
    """Union this run's observations into the sidecar. Returns (path, total_keys).
    
    Merge = per-key, per-type count summation, so cross-run polymorphism
    accumulates ({EmailBackend: 3} + {TokenBackend: 1} -> {EmailBackend: 3, TokenBackend: 1}) and nothing a
    previous run observed is ever dropped
    """
    path = path or get_sidecar_path()
    data = load(path)
    _merge_buckets(data["types"], local_types)
    _merge_buckets(data["non_local"], non_local)
    data["version"] = SCHEMA_VERSION

    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path) #atomic or POSIX: readers never see a half-write

    return path, len(data["types"]) + len(data["non_local"])


