import json
import os
import re
import shlex
import subprocess

from ..core import keys as core_keys

from .config import (
    OUTPUT_FILE,
    update_type_cache,
    update_blacklist,
    HARVESTED_AMBIGUOUS,
    HARVESTED_NON_LOCAL,
)


# How to reach a Django shell for the ORM-backed inspections. The script is
# appended as the final argument (`shell -c <script>`). Default: a local
# manage.py in the cwd. For a containerised project, e.g. the demo stack:
#   LIFESAVER_SHELL_CMD="docker compose exec -T web python demo/manage.py shell -c"
DEFAULT_SHELL_CMD = "python manage.py shell -c"


def _shell_command(script):
    return shlex.split(os.environ.get("LIFESAVER_SHELL_CMD", DEFAULT_SHELL_CMD)) + [script]


def _parse_location(loc):
    """Parse '[File: path | Func: name]' -> (path, name). Single implementation
    for the three places that need it (prior lookup + both wizard write loops)."""
    clean_loc = loc.strip("[]")
    parts = clean_loc.split(" | ")
    file_path = parts[0].replace("File: ", "").strip()
    func_name = parts[1].replace("Func: ", "").strip()
    return file_path, func_name


def _gather_priors(var_name, locations):
    """Aggregate runtime-harvested observations for this variable across every
    location it blind-spotted in. Returns (local_prior, nonlocal_prior), each
    {TypeName: count}. Empty dicts when the harvester never saw this key."""
    local_prior = {}
    nonlocal_prior = {}
    for loc in locations:
        try:
            file_path, func_name = _parse_location(loc)
        except Exception:
            continue
        key = core_keys.scoped_key(file_path, func_name, var_name)
        for type_name, count in HARVESTED_AMBIGUOUS.get(key, {}).items():
            local_prior[type_name] = local_prior.get(type_name, 0) + count
        for type_name, count in HARVESTED_NON_LOCAL.get(key, {}).items():
            nonlocal_prior[type_name] = nonlocal_prior.get(type_name, 0) + count
    return local_prior, nonlocal_prior


def _assemble_options(models_found, local_prior):
    """Merge the ORM inspection's attribute-match candidates with runtime-observed types.

    The ORM inspection only sees apps.get_models(), so a harvested non-model class
    (service object, DTO, strategy impl) is a candidate it structurally
    cannot propose - append those, most-observed first. Returns (model_names,
    infos) where infos[name] is the display line for that option."""
    model_names = list(models_found.keys())
    infos = dict(models_found)
    for type_name in sorted(local_prior, key=lambda t: -local_prior[t]):
        if type_name not in infos:
            model_names.append(type_name)
            infos[type_name] = "Runtime observation (not a model match for this attr)"
    return model_names, infos


def resolve_blind_spots(blind_spots):
    if not blind_spots:
        return

    print("\n" + "*" * 10)
    print("🧙 DYNAMIC BLIND SPOT RESOLUTION (WIZARD)")
    print("*" * 10)

    # 1. Map variables to the attributes they called
    var_to_attrs = {}
    attributes_to_check = set()

    for spot in blind_spots.keys():
        parts = spot.split(".", 1)
        if len(parts) == 2:
            var_name, attr_name = parts
            if var_name not in var_to_attrs:
                var_to_attrs[var_name] = set()
            var_to_attrs[var_name].add(attr_name)
            attributes_to_check.add(attr_name)

    if not attributes_to_check:
        return

    print("Sending unresolved attributes to a Django shell for ORM inspection...\n")

    # 2. Build the Django shell script
    django_script = f"""
import inspect
import json
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist

attrs_to_check = {list(attributes_to_check)}
results = {{}}

for attr in attrs_to_check:
    matches = {{}}
    for model in apps.get_models():
        try:
            # 1. Check if it's a database field
            try:
                field = model._meta.get_field(attr)
                field_type = field.get_internal_type()

                # If it's a relation (ForeignKey, M2M), tell us where it goes!
                if field.is_relation and field.related_model:
                    target = field.related_model.__name__ if hasattr(field.related_model, '__name__') else str(field.related_model)
                    matches[model.__name__] = f"{{field_type}} -> Points to '{{target}}'"
                else:
                    matches[model.__name__] = f"Field ({{field_type}})"
                continue
            except FieldDoesNotExist:
                pass

            # 2. Check if it's a property/method/reverse relation
            if hasattr(model, attr):
                cls_attr = getattr(model, attr, None)

                # Dig into reverse relations to find the target model
                if hasattr(cls_attr, 'rel') and hasattr(cls_attr.rel, 'related_model'):
                    target = cls_attr.rel.related_model.__name__
                    matches[model.__name__] = f"Reverse Relation -> Yields '{{target}}' objects"
                elif hasattr(cls_attr, 'rel') and hasattr(cls_attr.rel, 'model'):
                    target = cls_attr.rel.model.__name__
                    matches[model.__name__] = f"Reverse Relation -> Yields '{{target}}' objects"
                elif 'RelatedManager' in str(type(cls_attr)) or 'ManyToOneDescriptor' in str(type(cls_attr)):
                    matches[model.__name__] = "Reverse Relation / Manager"
                elif inspect.isfunction(cls_attr) or inspect.ismethod(cls_attr):
                    matches[model.__name__] = "Method (callable)"
                elif isinstance(cls_attr, property):
                    matches[model.__name__] = "Property (computed value)"
                else:
                    matches[model.__name__] = "Attribute"

        except Exception:
            pass

    if matches:
        results[attr] = matches

# Safely output the payload
print("---JSON_START---")
print(json.dumps(results))
print("---JSON_END---")
"""

    # 3. Run it in the project's Django shell
    try:
        command = _shell_command(django_script)
        result = subprocess.run(command, capture_output=True, text=True, check=True)

        match = re.search(r"---JSON_START---\n(.*)\n---JSON_END---", result.stdout, re.DOTALL)
        if not match:
            print("❌ Failed to parse JSON from the Django shell. Output was:")
            print(result.stdout)
            return

        attr_to_models = json.loads(match.group(1))

    except subprocess.CalledProcessError as e:
        print("\n❌ Failed to run the ORM inspection in the Django shell.")
        print(f"Error output:\n{e.stderr}")
        return
    except Exception as e:
        print(f"\n❌ Error parsing results: {e}")
        return

    # 4. The Interactive Wizard Loop
    for var_name, attrs in var_to_attrs.items():
        sample_attr = list(attrs)[0]
        models_found = attr_to_models.get(sample_attr, {})

        spot_key = f"{var_name}.{sample_attr}"
        locations = blind_spots.get(spot_key, set())

        # Runtime priors: what the dynamic harvester actually saw flowing
        # through this variable at these exact locations.
        local_prior, nonlocal_prior = _gather_priors(var_name, locations)
        model_names, option_infos = _assemble_options(models_found, local_prior)

        print(f"\n❓ The variable '{var_name}' called '.{sample_attr}'.")
        if locations:
            print("📍 Found in:")
            for loc in locations:
                print(f"   {loc}")

        if nonlocal_prior:
            seen = ", ".join(
                f"{t} x{c}" for t, c in sorted(nonlocal_prior.items(), key=lambda kv: -kv[1])
            )
            print(f"\n⚡ Runtime saw non-first-party values here: {seen}")
            print("   -> '-1' (blacklist forever) is likely the right answer.")

        print("\nORM inspection found:")

        if model_names:
            for idx, m_name in enumerate(model_names, 1):
                context_info = option_infos[m_name]
                prior_note = (
                    f"  ⚡ runtime saw x{local_prior[m_name]}" if m_name in local_prior else ""
                )
                print(f"[{idx}] {m_name:<25} : {context_info}{prior_note}")
        else:
            print("    (No Django models found with this attribute)")

        print("[0] Skip for now")
        print(f"[-1] Ignore '{var_name}' in these functions FOREVER 🚫")

        while True:
            choice = input(f"\n   Select (-1 to {len(model_names)}): ").strip()

            if choice == "-1":
                for loc in locations:
                    try:
                        file_path, func_name = _parse_location(loc)
                        update_blacklist(file_path, func_name, var_name)
                    except Exception:
                        pass
                print(f"   🚫 Blacklisted '{var_name}' for these specific locations.\n")
                break
            elif choice.isdigit() and int(choice) <= len(model_names):
                choice_idx = int(choice)
                if choice_idx > 0:
                    selected_model = model_names[choice_idx - 1]
                    for loc in locations:
                        try:
                            file_path, func_name = _parse_location(loc)
                            update_type_cache(file_path, func_name, var_name, selected_model)
                        except Exception:
                            pass
                else:
                    print(f"   ⏭️ Skipped '{var_name}'\n")
                break
            else:
                print("   ❌ Invalid selection, try again.")


def inspect_schema_stubs(stubs):
    if not stubs:
        return

    print("\n" + "=" * 10)
    print(f"🔬 DEEP ORM INSPECTION ({len(stubs)} Schema Stubs)")
    print("=" * 10)
    print("Running the model X-ray in the Django shell...\n")

    # `shell -c` runs a plain script, so the import must be absolute.
    django_script = r"""
from django.apps import apps

from django_perf_profiler.static_life_saver.inspect_model import _core_inspect_model

target_models = TARGET_MODELS_PLACEHOLDER

django_models = {model.__name__: model for model in apps.get_models()}

for stub in target_models:
    if stub in django_models:
        _core_inspect_model(django_models[stub])
"""

    django_script = django_script.replace("TARGET_MODELS_PLACEHOLDER", str(list(stubs)))

    try:
        command = _shell_command(django_script)
        result = subprocess.run(command, capture_output=True, text=True, check=True)

        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write("\n\n# " + "=" * 50 + "\n")
            f.write("# 🔬 DEEP ORM INSPECTION (SCHEMA STUBS)\n")
            f.write("# " + "=" * 50 + "\n")
            f.write(result.stdout)

        print(f"✅ Appended detailed Model X-Rays to {OUTPUT_FILE}")

    except subprocess.CalledProcessError as e:
        print("\n❌ Failed to run the model X-ray in the Django shell.")
        print(f"Error output:\n{e.stderr}")