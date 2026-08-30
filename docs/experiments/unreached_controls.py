"""Find controls that are correct in isolation and never reached in production.

Eight instances so far on this branch, each found by hand and each after it had already shipped:
#75 classification, #74 calendar, #58 dependence, #56 detector, #82 slices 3-4, the regime and
anomaly columns, catalyst enrichment, and the counterfactual evaluator. Every one had green unit
tests and no caller.

The shape is mechanical, so the search should be too. A definition in `src/` that no other module
in `src/` references is either dead, or reachable only from tests -- and a control reachable only
from its tests is exactly the failure mode.

This deliberately over-reports rather than under-reports. Re-exports, dynamic dispatch and
protocol implementations all look unreferenced, so the output is a list to triage, not a verdict.
"""

from __future__ import annotations

import ast
import pathlib
from collections import defaultdict

SRC = pathlib.Path("src/quantbot")


def module_name(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(SRC.parent).with_suffix("").parts)


def main() -> int:
    definitions: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    references: dict[str, set[str]] = defaultdict(set)
    exported: dict[str, set[str]] = defaultdict(set)

    files = sorted(SRC.rglob("*.py"))
    for path in files:
        module = module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in tree.body:
            # Functions only. The eight real instances were all *actions nothing invoked*;
            # types, exceptions and models are referenced in ways this cannot see (annotations,
            # `raise`, template context) and drown the signal at 440 results.
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                if not node.name.startswith("_"):
                    definitions[module].append((node.name, node.lineno, "def"))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "__all__"
                        and isinstance(node.value, ast.List | ast.Tuple)
                    ):
                        exported[module] = {
                            element.value
                            for element in node.value.elts
                            if isinstance(element, ast.Constant)
                            and isinstance(element.value, str)
                        }

        for node in ast.walk(tree):
            # A *call*, not a mention. An import or an annotation is not evidence that anything
            # runs the thing -- which is precisely how every one of the eight instances looked
            # reachable while being unreached.
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    references[module].add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    references[module].add(node.func.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Reached through a string: getattr, a registry key, a CLI command name.
                references[module].add(node.value)

    everywhere: dict[str, set[str]] = defaultdict(set)
    for module, names in references.items():
        for name in names:
            everywhere[name].add(module)

    print(f"scanned {len(files)} modules\n")
    unreached: list[tuple[str, str, int, str]] = []
    for module, defined in sorted(definitions.items()):
        for name, lineno, kind in defined:
            users = everywhere.get(name, set()) - {module}
            # A package __init__ re-exporting a name is not a use of it.
            users = {
                item
                for item in users
                if not (item.endswith("__init__") and name in exported.get(item, set()))
            }
            if not users:
                unreached.append((module, name, lineno, kind))

    if not unreached:
        print("no unreferenced definitions")
        return 0

    print(f"{len(unreached)} definition(s) referenced by no other src module:\n")
    current = ""
    for module, name, lineno, kind in unreached:
        if module != current:
            print(f"  {module}")
            current = module
        print(f"      {kind:5} {name}  (line {lineno})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
