"""Tests for reference_graph — the first-party import/symbol graph.

Covers the edges and the four derived signals the research harness consumes:
real import edges (absolute + relative), fan-in/fan-out, unreferenced (dead)
exports, orphan modules, and import-cycle detection. All pure / offline.
"""
from app.services.reference_graph import build_reference_graph


# A small corpus with: a<->b cycle, c→a (fan-in to a), an orphan, a relative
# import x→y, and a genuine dead export (b.dead).
CORPUS = {
    "backend/app/a.py": "from app.b import helper\ndef run():\n    return helper()\n",
    "backend/app/b.py": (
        "from app.a import run\n"
        "def helper():\n    return run()\n"
        "def dead():\n    return 2\n"
    ),
    "backend/app/c.py": "from app.a import run\nimport app.b\ndef go():\n    return run()\n",
    "backend/app/orphan.py": "import os\nVALUE = 1\n",
    "backend/app/pkg/x.py": "from .y import thing\ndef use():\n    return thing()\n",
    "backend/app/pkg/y.py": "def thing():\n    return 1\n",
}


class TestEdges:
    def test_absolute_import_edge(self):
        g = build_reference_graph(CORPUS)
        assert "backend/app/b.py" in g.nodes["backend/app/a.py"].imports
        assert "backend/app/a.py" in g.nodes["backend/app/b.py"].imported_by

    def test_relative_import_resolved(self):
        g = build_reference_graph(CORPUS)
        # `from .y import thing` in pkg/x.py resolves to pkg/y.py.
        assert "backend/app/pkg/y.py" in g.nodes["backend/app/pkg/x.py"].imports

    def test_third_party_import_is_not_an_edge(self):
        g = build_reference_graph(CORPUS)
        # orphan imports `os` — not first-party, not in corpus → no edge.
        assert g.nodes["backend/app/orphan.py"].fan_out == 0

    def test_fan_in_counts_real_importers(self):
        g = build_reference_graph(CORPUS)
        # a is imported by b and c.
        assert g.nodes["backend/app/a.py"].fan_in == 2

    def test_from_package_import_module_is_an_edge(self):
        # Regression: `from app.services import foo` imports the MODULE foo, not
        # a symbol of the package __init__. Before the fix this left foo an
        # orphan (the only edge pointed at the package node). Now it's a real
        # module->module edge. This is the bug that produced the false
        # ast_lint/finding_critic "dead code" findings.
        corpus = {
            "backend/app/services/__init__.py": "",
            "backend/app/services/foo.py": "def helper():\n    return 1\n",
            "backend/app/services/bar.py": "from app.services import foo\ndef use():\n    return foo.helper()\n",
        }
        g = build_reference_graph(corpus)
        assert "backend/app/services/foo.py" in g.nodes["backend/app/services/bar.py"].imports
        assert g.nodes["backend/app/services/foo.py"].fan_in == 1
        assert "backend/app/services/foo.py" not in g.orphans()

    def test_function_scoped_import_is_an_edge(self):
        # Regression: a lazy import INSIDE a function body is a real runtime
        # dependency for the graph (ast_lint's module-level-only collector
        # missed these, leaving finding_critic/diff_apply false orphans).
        corpus = {
            "backend/app/services/dep.py": "def thing():\n    return 1\n",
            "backend/app/services/lazy.py": (
                "def run():\n"
                "    from app.services import dep as d\n"
                "    return d.thing()\n"
            ),
        }
        g = build_reference_graph(corpus)
        assert "backend/app/services/dep.py" in g.nodes["backend/app/services/lazy.py"].imports
        assert g.nodes["backend/app/services/dep.py"].fan_in == 1
        assert "backend/app/services/dep.py" not in g.orphans()


class TestSignals:
    def test_dead_export_detected(self):
        g = build_reference_graph(CORPUS)
        ue = g.unreferenced_exports()
        # b.dead is defined and imported nowhere → dead.
        assert "dead" in ue.get("backend/app/b.py", [])

    def test_used_export_not_flagged_dead(self):
        g = build_reference_graph(CORPUS)
        ue = g.unreferenced_exports()
        # b.helper IS used by a → not dead; a.run IS used by b,c → not dead.
        assert "helper" not in ue.get("backend/app/b.py", [])
        assert "backend/app/a.py" not in ue

    def test_reexport_not_counted_as_dead(self):
        # a.py does `from app.b import helper` — that re-export must NOT show up
        # as a dead export of a.py (it's imported, not defined here).
        g = build_reference_graph(CORPUS)
        assert "helper" not in g.unreferenced_exports().get("backend/app/a.py", [])

    def test_orphan_detected(self):
        g = build_reference_graph(CORPUS)
        assert "backend/app/orphan.py" in g.orphans()

    def test_cycle_detected(self):
        g = build_reference_graph(CORPUS)
        assert any(
            set(c) == {"backend/app/a.py", "backend/app/b.py"} for c in g.cycles
        ), g.cycles

    def test_summary_shape(self):
        g = build_reference_graph(CORPUS)
        s = g.to_summary()
        assert s["module_count"] == 6
        assert s["edge_count"] >= 3
        assert isinstance(s["cycles"], list) and s["cycles"]
        assert "backend/app/orphan.py" in s["orphans"]


class TestGodModule:
    def test_high_fanin_module_flagged(self):
        # 8 leaf modules all importing a shared `core`.
        corpus = {
            "backend/app/core.py": "def f():\n    return 1\n",
        }
        for i in range(8):
            corpus[f"backend/app/m{i}.py"] = "from app.core import f\ndef g():\n    return f()\n"
        g = build_reference_graph(corpus)
        assert "backend/app/core.py" in g.god_modules()
        assert g.nodes["backend/app/core.py"].fan_in == 8


class TestFailOpen:
    def test_empty_corpus(self):
        g = build_reference_graph({})
        assert g.nodes == {} and g.cycles == []
        assert g.to_summary()["module_count"] == 0

    def test_unparseable_file_contributes_no_edges(self):
        corpus = {
            "backend/app/broken.py": "def (((:\n",   # syntax error
            "backend/app/ok.py": "def f():\n    return 1\n",
        }
        g = build_reference_graph(corpus)
        # broken still becomes a node (orphan) but has no edges + a parse_error.
        assert g.nodes["backend/app/broken.py"].parse_error is not None
        assert g.nodes["backend/app/broken.py"].fan_out == 0

    def test_non_python_files_ignored(self):
        corpus = {
            "frontend/src/App.tsx": "export const x = 1;",
            "backend/app/a.py": "def f():\n    return 1\n",
        }
        g = build_reference_graph(corpus)
        assert "frontend/src/App.tsx" not in g.nodes
        assert "backend/app/a.py" in g.nodes
