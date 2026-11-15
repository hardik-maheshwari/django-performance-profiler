import ast
from pathlib import Path

from ..core import boundaries, keys

from .config import (
    DYNAMIC_TYPE_CACHE,
    HARVESTED_TYPES,
    TYPE_SYNONYMS,
    DJANGO_IGNORED_METHODS,
    PYTHON_IGNORED_METHODS,
    MANUAL_TYPE_OVERRIDES,
    MANUAL_VARIABLE_TYPES,
    GLOBAL_BLIND_SPOTS,
    DYNAMIC_BLACKLIST,
)


def _module_file(base_dir, parts):
    """<base>/a/b.py, else <base>/a/b/__init__.py, else None."""
    candidate = base_dir.joinpath(*parts).with_suffix(".py")
    if candidate.exists():
        return candidate
    candidate = base_dir.joinpath(*parts, "__init__.py")
    return candidate if candidate.exists() else None


def resolve_module_path(base_file, module_name, level=0):
    """Map an import to the first-party file it names, or None (third-party,
    stdlib, or unresolvable). Absolute imports resolve against
    core.boundaries.PROJECT_ROOT, the same root the scoped keys use; relative
    imports (level >= 1) resolve against the importing file's package."""
    if level == 0:
        return _module_file(Path(boundaries.PROJECT_ROOT), module_name.split("."))

    search_dir = Path(base_file).resolve().parent
    for _ in range(level - 1):
        search_dir = search_dir.parent
    if module_name:
        return _module_file(search_dir, module_name.split("."))
    candidate = search_dir.joinpath("__init__.py")
    return candidate if candidate.exists() else None


class FileScanner(ast.NodeVisitor):
    """
    Scans a file to map Imports, Definitions, and Class Hierarchies.
    """

    def __init__(self, file_path):
        self.file_path = file_path
        self.import_map = {}
        self.definition_map = set()
        # { "ClassName": { "bases": [], "methods": set(), "decorators": [] } }
        self.class_metadata = {}

    def visit_Import(self, node):
        for alias in node.names:
            path = resolve_module_path(self.file_path, alias.name, 0)
            if path:
                self.import_map[alias.asname or alias.name] = (str(path), None)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if not node.module and node.level == 0:
            return
        path = resolve_module_path(self.file_path, node.module, node.level)
        if path:
            for alias in node.names:
                self.import_map[alias.asname or alias.name] = (str(path), alias.name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        # Standalone functions
        self.definition_map.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        # Async standalone functions (Celery/Channels often use these)
        self.definition_map.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.definition_map.add(node.name)

        # 1. Capture Base Classes (Inheritance)
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(base.attr)  # e.g., models.Model -> Model

        # 2. Capture Methods defined INSIDE this class
        methods = set()
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(item.name)

        # 3. Capture Class Decorators (e.g., @api_view)
        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)
                elif isinstance(dec.func, ast.Attribute):
                    decorators.append(dec.func.attr)

        self.class_metadata[node.name] = {
            "bases": bases,
            "methods": methods,
            "decorators": decorators,
        }

        # IMPORTANT: We do NOT generic_visit(node) here because we handled
        # the methods inside logic above. We don't want to double-count
        # class methods as global definitions.


class FunctionDetailsVisitor(ast.NodeVisitor):
    """
    Extracts source code, local imports, decorators, and class-member usages.
    """

    def __init__(self, target_func_name, source_text, file_path):
        if "." in target_func_name:
            self.expected_class, self.target_func_name = target_func_name.split(".", 1)
        else:
            self.expected_class = None
            self.target_func_name = target_func_name

        self.source_text = source_text
        self.file_path = file_path

        self.found = False
        self.source_code = None
        self.is_inside_target = False

        # Dependency Tracking
        self.used_names = set()  # Global/Local vars (e.g., timezone, models)
        self.local_imports = {}  # Imports defined inside the function
        self.class_member_uses = set()  # Attributes accessed via self (e.g., self.process)
        self.decorators = set()  # Decorators used on the function

        # Scoping & Inference
        self.local_defs = set()  # Local variables/args
        self.class_context = None  # Name of the class we are currently visiting
        self.instance_alias = None  # The variable name for 'self' (e.g., 'self', 'cls')
        self.target_class = None  # The class name WHERE the target function was found
        self.local_var_types = {}  # Heuristic map: { "order": "Order", "user": "User" }

    def visit_ClassDef(self, node):
        # Track which class we are entering
        if not self.expected_class and node.name == self.target_func_name:
            self.found = True
            self.is_inside_target = True
            self.target_class = node.name
            self.source_code = ast.get_source_segment(self.source_text, node)

            # We still visit children to catch decorators/imports at the class level
            self.generic_visit(node)
            self.is_inside_target = False
            return

        # Not the target: just track which class we are inside
        prev_context = self.class_context
        self.class_context = node.name
        self.generic_visit(node)
        self.class_context = prev_context

    def visit_FunctionDef(self, node):
        if node.name == self.target_func_name:
            if self.expected_class and self.class_context != self.expected_class:
                self.generic_visit(node)
                return
            self.found = True
            self.is_inside_target = True
            self.target_class = self.class_context  # Record where we found it
            self.source_code = ast.get_source_segment(self.source_text, node)

            # 1. Identify 'self' variable name (First argument)
            if node.args.args and self.class_context:
                self.instance_alias = node.args.args[0].arg

            # Relative path computed once per function.
            # Anchored on core.boundaries.PROJECT_ROOT (== cwd unless
            # LIFESAVER_PROJECT_ROOT is set) so keys match the dynamic harvester's.
            rel_path = keys.rel_to_root(self.file_path)

            # Positional-only, standard and keyword-only arguments
            all_args = []
            if hasattr(node.args, "posonlyargs"):
                all_args.extend(node.args.posonlyargs)
            all_args.extend(node.args.args)
            all_args.extend(node.args.kwonlyargs)

            # 2. Register arguments and infer types
            for arg in all_args:
                arg_name = arg.arg
                self.local_defs.add(arg_name)

                # Skip 'self' or 'cls'
                if hasattr(self, "instance_alias") and arg_name == self.instance_alias:
                    continue
                elif arg_name in ["self", "cls"]:
                    continue

                if arg.annotation and isinstance(arg.annotation, ast.Name):
                    self.local_var_types[arg_name] = arg.annotation.id
                    continue

                # --- Type Inference Hierarchy ---
                # Level 1: Scoped type cache (answers saved by the type wizard)
                scoped_key = keys.scoped_key(rel_path, self.target_func_name, arg_name)
                if scoped_key in DYNAMIC_TYPE_CACHE:
                    self.local_var_types[arg_name] = DYNAMIC_TYPE_CACHE[scoped_key]
                    continue

                # Level 1.5: Runtime-harvested single type (evidence, never a
                # human decision; ambiguous keys deliberately don't resolve here)
                if scoped_key in HARVESTED_TYPES:
                    self.local_var_types[arg_name] = HARVESTED_TYPES[scoped_key]
                    continue

                # Level 2: Global Manual Overrides
                if arg_name in MANUAL_VARIABLE_TYPES:
                    self.local_var_types[arg_name] = MANUAL_VARIABLE_TYPES[arg_name]
                    continue

                # Level 3: Explicit Synonyms
                if arg_name in TYPE_SYNONYMS and len(TYPE_SYNONYMS[arg_name]) > 0:
                    self.local_var_types[arg_name] = TYPE_SYNONYMS[arg_name][0]
                    continue

                # Level 4: The CamelCase Fallback
                guessed_type = "".join(word.capitalize() for word in arg_name.split("_"))
                self.local_var_types[arg_name] = guessed_type

            # Don't forget *args and **kwargs (We add to local_defs but don't usually type-infer them)
            if node.args.vararg:
                self.local_defs.add(node.args.vararg.arg)
            if node.args.kwarg:
                self.local_defs.add(node.args.kwarg.arg)

            # 3. Capture Decorators
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name):
                    self.decorators.add(dec.id)
                    self.used_names.add(dec.id)
                elif isinstance(dec, ast.Call):
                    if isinstance(dec.func, ast.Name):
                        self.decorators.add(dec.func.id)
                        self.used_names.add(dec.func.id)
                    elif isinstance(dec.func, ast.Attribute):
                        if isinstance(dec.func.value, ast.Name):
                            self.used_names.add(dec.func.value.id)

            self.generic_visit(node)
            self.is_inside_target = False
        else:
            if self.is_inside_target:
                # --- THE METHOD BOUNDARY FENCE ---
                # If target_class == target_func_name, we are taking a snapshot of a full Class.
                # DO NOT traverse into its methods and trigger false positives!
                if self.target_class and self.target_class == self.target_func_name:
                    return
                self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        # Route async functions through the exact same logic as normal functions
        self.visit_FunctionDef(node)

    # --- Capture Local Imports ---
    def visit_Import(self, node):
        if self.is_inside_target:
            for alias in node.names:
                path = resolve_module_path(self.file_path, alias.name, 0)
                if path:
                    self.local_imports[alias.asname or alias.name] = (str(path), None)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if self.is_inside_target:
            if not node.module and node.level == 0:
                return
            path = resolve_module_path(self.file_path, node.module, node.level)
            if path:
                for alias in node.names:
                    self.local_imports[alias.asname or alias.name] = (
                        str(path),
                        alias.name,
                    )
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        """Reads explicit Python type hints (e.g., payload: OrderPayload = ...)"""
        if self.is_inside_target:
            if isinstance(node.target, ast.Name):
                var_name = node.target.id
                self.local_defs.add(var_name)
                # If they annotated the type, capture it!
                if isinstance(node.annotation, ast.Name):
                    self.local_var_types[var_name] = node.annotation.id
        self.generic_visit(node)

    # --- Capture Local Variables + Type Inference ---
    def visit_Assign(self, node):
        if self.is_inside_target:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.local_defs.add(target.id)
                elif isinstance(target, ast.Tuple):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            self.local_defs.add(elt.id)
                            # Tuple-unpacked names (a, b = func()) can't be inferred
                            # from the RHS, but a scoped cache / harvested / manual
                            # type still applies. Mirror the single-name precedence
                            # below so wizard answers for tuple vars actually stick
                            # (otherwise they blind-spot and re-prompt every run).
                            rel_path = keys.rel_to_root(self.file_path)
                            scoped_key = keys.scoped_key(
                                rel_path, self.target_func_name, elt.id
                            )
                            if scoped_key in DYNAMIC_TYPE_CACHE:
                                self.local_var_types[elt.id] = DYNAMIC_TYPE_CACHE[scoped_key]
                            elif scoped_key in HARVESTED_TYPES:
                                self.local_var_types[elt.id] = HARVESTED_TYPES[scoped_key]
                            elif elt.id in MANUAL_VARIABLE_TYPES:
                                self.local_var_types[elt.id] = MANUAL_VARIABLE_TYPES[elt.id]

            # Infer Type from Constructor/Manager calls
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                var_name = node.targets[0].id

                rel_path = keys.rel_to_root(self.file_path)
                scoped_key = keys.scoped_key(rel_path, self.target_func_name, var_name)

                # Case 0: Scoped Dynamic Cache Override (Highest Priority)
                if scoped_key in DYNAMIC_TYPE_CACHE:
                    self.local_var_types[var_name] = DYNAMIC_TYPE_CACHE[scoped_key]

                # Case 0.25: Runtime-harvested single type (evidence)
                elif scoped_key in HARVESTED_TYPES:
                    self.local_var_types[var_name] = HARVESTED_TYPES[scoped_key]

                # Case 0.5: Global Variable Name Override
                elif var_name in MANUAL_VARIABLE_TYPES:
                    self.local_var_types[var_name] = MANUAL_VARIABLE_TYPES[var_name]

                # Case A: item = Item(...)
                elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                    self.local_var_types[var_name] = node.value.func.id

                    if node.value.func.id in MANUAL_TYPE_OVERRIDES:
                        self.local_var_types[var_name] = MANUAL_TYPE_OVERRIDES[node.value.func.id]

                # Case B: item = Item.objects.get(...)
                elif isinstance(node.value, ast.Call) and isinstance(
                    node.value.func, ast.Attribute
                ):
                    # .objects.get()
                    if (
                        isinstance(node.value.func.value, ast.Attribute)
                        and node.value.func.value.attr == "objects"
                    ):
                        if isinstance(node.value.func.value.value, ast.Name):
                            self.local_var_types[var_name] = node.value.func.value.value.id

                    # .first() chain
                    elif node.value.func.attr in ["first", "last", "get"]:
                        curr = node.value.func.value
                        while isinstance(curr, ast.Call):
                            curr = curr.func
                            if isinstance(curr, ast.Attribute) and curr.attr == "objects":
                                if isinstance(curr.value, ast.Name):
                                    self.local_var_types[var_name] = curr.value.id

        self.generic_visit(node)

    def visit_For(self, node):
        if self.is_inside_target:
            # Handle standard for loops: for var_name in ...
            if isinstance(node.target, ast.Name):
                var_name = node.target.id
                self.local_defs.add(var_name)

                rel_path = keys.rel_to_root(self.file_path)
                scoped_key = keys.scoped_key(rel_path, self.target_func_name, var_name)

                # Check our manual overrides and caches!
                if scoped_key in DYNAMIC_TYPE_CACHE:
                    self.local_var_types[var_name] = DYNAMIC_TYPE_CACHE[scoped_key]
                elif scoped_key in HARVESTED_TYPES:
                    self.local_var_types[var_name] = HARVESTED_TYPES[scoped_key]
                elif var_name in MANUAL_VARIABLE_TYPES:
                    self.local_var_types[var_name] = MANUAL_VARIABLE_TYPES[var_name]

        # Continue traversing the inside of the loop
        self.generic_visit(node)

    # --- Capture Usage (Variables + Class Members + Typed Objects) ---
    def visit_Attribute(self, node):
        if self.is_inside_target:
            # Ignore standard Django & Python methods
            if node.attr in DJANGO_IGNORED_METHODS or node.attr in PYTHON_IGNORED_METHODS:
                self.generic_visit(node)
                return

            # We ONLY do deep type inference if the base is a standard variable name (ast.Name)
            if isinstance(node.value, ast.Name):
                base_name = node.value.id

                # Case 1: self.method
                if base_name == self.instance_alias:
                    self.class_member_uses.add(node.attr)

                # Case 2: Typed Variable Method
                elif base_name in self.local_var_types:
                    class_name = self.local_var_types[base_name]
                    self.class_member_uses.add((class_name, node.attr))

                # Case 2.5: Static/Class Methods
                elif base_name[0].isupper():
                    self.class_member_uses.add((base_name, node.attr))
                    if base_name not in self.local_defs:
                        self.used_names.add(base_name)

                # Case 3 & 4: Global vs Unknown Local
                else:
                    if base_name not in self.local_defs:
                        # Case 3: It's a global import/variable
                        self.used_names.add(base_name)
                        self.used_names.add(node.attr)
                    else:
                        rel_path = keys.rel_to_root(self.file_path)

                        # Blacklisted: a known non-first-party variable, stay quiet
                        blacklist_key = keys.scoped_key(
                            rel_path, self.target_func_name, base_name
                        )
                        if blacklist_key in DYNAMIC_BLACKLIST:
                            pass
                        else:
                            spot_key = f"{base_name}.{node.attr}"
                            location = f"[File: {rel_path} | Func: {self.target_func_name}]"

                            if spot_key not in GLOBAL_BLIND_SPOTS:
                                GLOBAL_BLIND_SPOTS[spot_key] = set()
                            GLOBAL_BLIND_SPOTS[spot_key].add(location)

            elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                class_name = node.value.func.id
                # Duck-type the AST: If it starts with a capital letter, assume it's a class!
                if class_name == "super":
                    self.class_member_uses.add(("__SUPER__", node.attr))

                elif class_name[0].isupper():
                    self.class_member_uses.add((class_name, node.attr))
                    # Add to used_names so it gets resolved/imported if necessary
                    if class_name not in self.local_defs:
                        self.used_names.add(class_name)

            elif (
                isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and getattr(self, "instance_alias", None)
                and node.value.value.id == self.instance_alias
            ):
                component_name = node.value.attr  # e.g., 'payment_gateway'
                method_name = node.attr  # e.g., 'charge'

                # 1. Component type known from config
                if component_name in MANUAL_VARIABLE_TYPES:
                    resolved_type = MANUAL_VARIABLE_TYPES[component_name]
                    self.class_member_uses.add((resolved_type, method_name))
                    self.used_names.add(resolved_type)
                else:
                    # 2. Unknown: report it as a blind spot for the wizard
                    rel_path = keys.rel_to_root(self.file_path)
                    blacklist_key = keys.scoped_key(
                        rel_path, self.target_func_name, component_name
                    )

                    if blacklist_key not in DYNAMIC_BLACKLIST:
                        spot_key = f"self.{component_name}.{method_name}"
                        location = f"[File: {rel_path} | Func: {self.target_func_name}]"
                        if spot_key not in GLOBAL_BLIND_SPOTS:
                            GLOBAL_BLIND_SPOTS[spot_key] = set()
                        GLOBAL_BLIND_SPOTS[spot_key].add(location)

            else:
                # If it's a Subscript (list[0].method) or Call (get_obj().method),
                # we just capture the attribute name as a fallback.
                if isinstance(node.ctx, ast.Load):
                    self.used_names.add(node.attr)

        self.generic_visit(node)

    def visit_Name(self, node):
        if self.is_inside_target and isinstance(node.ctx, ast.Load):
            if node.id not in self.local_defs and node.id != self.instance_alias:
                self.used_names.add(node.id)