import json
import os

from ..core import harvest_store, keys

# ==============================================================================
# CONFIGURATION
# ==============================================================================

OUTPUT_FILE = "life_saver_llm.txt"
MAX_DEPTH = 5

# Human-confirmed answers (wizard / --ingest-blindspots). Lives next to the
# harvested sidecar (core.harvest_store) so evidence and decisions share one
# directory; override with LIFESAVER_TYPE_CACHE.
CACHE_FILE = os.path.abspath(
    os.environ.get(
        "LIFESAVER_TYPE_CACHE",
        os.path.join(os.path.dirname(harvest_store.get_sidecar_path()), ".type_cache.json"),
    )
)

# Load existing cache if it exists
DYNAMIC_TYPE_CACHE = {}
DYNAMIC_BLACKLIST = set()

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
            DYNAMIC_TYPE_CACHE = data.get("mappings", {})
            DYNAMIC_BLACKLIST = set(data.get("blacklist", []))
    except Exception as e:
        print(f"⚠️ Warning: Could not load type cache: {e}")


def save_cache():
    """Helper to persist both caches to disk."""
    with open(CACHE_FILE, "w") as f:
        json.dump(
            {"mappings": DYNAMIC_TYPE_CACHE, "blacklist": list(DYNAMIC_BLACKLIST)},
            f,
            indent=4,
        )


def update_type_cache(file_path, func_name, variable_name, model_name):
    """Saves a hyper-specific type mapping so it never collides with other files.
    Key format is owned by core.keys (shared with the dynamic harvester)."""
    key = keys.scoped_key(file_path, func_name, variable_name)

    DYNAMIC_TYPE_CACHE[key] = model_name
    save_cache()


def update_blacklist(file_path, func_name, variable_name):
    """Adds a specific variable in a specific function to the silent ignore list."""
    key = keys.scoped_key(file_path, func_name, variable_name)
    DYNAMIC_BLACKLIST.add(key)
    save_cache()


# --- Runtime-harvested sidecar (evidence layer; see core.harvest_store) ---
# Split by confidence:
#   HARVESTED_TYPES        exactly one observed type -> used for inference, right
#                          below the wizard cache and above MANUAL_VARIABLE_TYPES
#   HARVESTED_AMBIGUOUS    polymorphic across observations -> NEVER inferred;
#                          surfaces as ranked wizard priors only
#   HARVESTED_NON_LOCAL    non-first-party arg values -> NEVER auto-blacklisted;
#                          surfaces as a wizard hint that '-1' is likely right
# Human decisions always win: any key already in DYNAMIC_TYPE_CACHE or
# DYNAMIC_BLACKLIST is skipped entirely. These dicts are deliberately separate
# from DYNAMIC_TYPE_CACHE so save_cache() can never leak evidence into the
# wizard-owned decisions file.
HARVESTED_TYPES = {}
HARVESTED_AMBIGUOUS = {}
HARVESTED_NON_LOCAL = {}
try:
    _sidecar = harvest_store.load()
    for _k, _obs in _sidecar["types"].items():
        if _k in DYNAMIC_TYPE_CACHE or _k in DYNAMIC_BLACKLIST:
            continue
        if len(_obs) == 1:
            HARVESTED_TYPES[_k] = next(iter(_obs))
        else:
            HARVESTED_AMBIGUOUS[_k] = dict(_obs)
    for _k, _obs in _sidecar["non_local"].items():
        if _k in DYNAMIC_TYPE_CACHE or _k in DYNAMIC_BLACKLIST:
            continue
        HARVESTED_NON_LOCAL[_k] = dict(_obs)
except Exception as _exc:
    print(f"⚠️ Warning: Could not load harvested sidecar: {_exc}")

GLOBAL_BLIND_SPOTS = {}

TYPE_SYNONYMS = {}

MANUAL_TYPE_OVERRIDES = {}

MANUAL_VARIABLE_TYPES = {}

DJANGO_IGNORED_METHODS = {}

PYTHON_IGNORED_METHODS = {}

NOISE_CLASSES = {}

GLOBAL_SCHEMA_STUBS = set()
