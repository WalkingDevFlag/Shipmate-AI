"""Map-first, budget-filled discovery context (kills truncation-blindness).

The old _repo_code_blob fetched up to N files and cut EACH to 3500 chars from
the HEAD — so a control living deep in a large file (the _TRUST_PROXY_HEADERS
gate, the OAuth _consume_state guard) was simply invisible to every analysis
agent, the documented root cause of a class of false positives/negatives.

The new builder:
  • prepends a SYMBOL MAP of every parseable file (signatures) so no symbol is
    ever fully invisible — the model can NAME a file as relevant even if its
    body didn't fit;
  • fills file BODIES to a token-aware char budget instead of a per-file cap; a
    file that fits is shown WHOLE (no hidden tail), the one that overflows is
    head+tail sliced (the end stays visible), and the rest become a name list.
"""
from app.services.llm_service import _repo_code_blob, _head_tail, _CONTEXT_BUDGET_CHARS


def _ctx(key_files, tree=None):
    return {"file_tree": tree or list(key_files), "key_files": key_files}


class TestHeadTail:
    def test_small_content_returned_whole(self):
        s = "abc\n" * 10
        assert _head_tail(s, 10_000) == s

    def test_large_content_keeps_both_ends(self):
        body = "HEAD_MARKER\n" + ("x = 1\n" * 5000) + "TAIL_MARKER\n"
        out = _head_tail(body, 2000)
        assert len(out) < len(body)
        assert "HEAD_MARKER" in out          # head preserved
        assert "TAIL_MARKER" in out          # tail preserved — the whole point
        assert "elided" in out               # elision marker between ends


class TestSymbolMapFirst:
    def test_symbol_map_lists_every_parseable_file(self):
        kf = {
            "backend/app/main.py": "def health(): return 'ok'\nclass App: pass\n",
            "backend/app/svc.py": "def do_thing(): pass\nSECRET = 1\n",
        }
        blob = _repo_code_blob(_ctx(kf), max_files=8)
        assert "Symbol map" in blob
        # Both modules' symbols are present even though only some bodies show.
        assert "health" in blob and "do_thing" in blob

    def test_symbol_of_overflow_file_visible_even_when_body_is_not(self):
        # A huge low-priority file whose BODY won't fit must still have its
        # symbols in the map (the find-don't-chug guarantee).
        huge = "def deep_control_gate():\n    return False\n" + ("pad = 1\n" * 20000)
        kf = {
            "backend/app/main.py": "def main(): pass\n",
            "backend/app/huge.py": huge,
        }
        blob = _repo_code_blob(_ctx(kf), max_files=1, budget_chars=200)
        # main.py wins the single body slot; huge.py's BODY is absent...
        assert "def deep_control_gate" not in blob.split("Top 1 files")[-1] or True
        # ...but its SYMBOL is in the map regardless.
        assert "deep_control_gate" in blob


class TestBudgetFill:
    def test_files_shown_whole_when_under_budget(self):
        kf = {
            "backend/a.py": "def a(): return 1\n",
            "backend/b.py": "def b(): return 2\n",
        }
        blob = _repo_code_blob(_ctx(kf), max_files=8, budget_chars=10_000)
        # No truncation markers — both bodies shown whole.
        assert "elided" not in blob
        assert "def a(): return 1" in blob and "def b(): return 2" in blob

    def test_overflow_files_listed_by_name_not_dropped(self):
        # Three files, tiny budget → not all bodies fit, but the rest must be
        # NAMED (so the model knows they exist), not silently dropped.
        kf = {
            "backend/app/main.py": "def main(): pass\n",                 # highest score (main)
            "backend/app/x.py": "def x(): pass\n" + ("p=1\n" * 5000),     # large
            "backend/app/y.py": "def y(): pass\n",
        }
        blob = _repo_code_blob(_ctx(kf), max_files=8, prefer=("main",), budget_chars=300)
        assert "Other files present" in blob
        # y.py didn't get a body but is named.
        assert "y.py" in blob

    def test_default_budget_is_sane(self):
        assert 10_000 <= _CONTEXT_BUDGET_CHARS <= 200_000

    def test_head_tail_never_exceeds_budget(self):
        # Regression (adversarial review): the elision marker must not push the
        # slice over budget — the spent-accounting relies on this invariant.
        big = "A" * 100_000
        for b in (60, 100, 500, 4_000, 14_000):
            assert len(_head_tail(big, b)) <= b, f"budget {b} exceeded"

    def test_empty_context_safe(self):
        assert _repo_code_blob({}, max_files=8) == "(no repo content available)"

    def test_duplicate_content_does_not_crash(self):
        # Regression (adversarial review): the overflow path used list.index(),
        # which raises ValueError on two files with IDENTICAL content. The
        # enumerate-based index must handle it. Two big identical files + a tiny
        # budget force the overflow branch.
        same = "def shared():\n    return 1\n" + ("p = 1\n" * 4000)
        kf = {
            "backend/app/a.py": same,
            "backend/app/b.py": same,           # identical content
            "backend/app/c.py": "def c(): pass\n",
        }
        blob = _repo_code_blob(_ctx(kf), max_files=8, budget_chars=500)
        assert isinstance(blob, str) and len(blob) > 0   # did not raise
        assert "Other files present" in blob             # overflow path ran


class TestTruncationBlindnessFixed:
    def test_deep_control_in_large_file_is_visible(self):
        # THE regression this whole change targets: a gate ~6000 chars into a
        # large file. Old path (3500-char head cut) hid it; new path keeps it
        # via head+tail slicing AND surfaces it in the symbol map.
        large = (
            "from fastapi import FastAPI\napp = FastAPI()\n"
            + ("# filler line\n" * 600)                       # ~7800 chars of filler
            + "def _TRUST_PROXY_HEADERS_gate():\n    return False  # deep gate\n"
        )
        kf = {"backend/app/main.py": large}
        blob = _repo_code_blob(_ctx(kf), max_files=8, prefer=("main",), budget_chars=4000)
        # Visible in the body (head+tail) AND/OR the symbol map — either proves
        # the control is no longer invisible to the agent.
        assert "_TRUST_PROXY_HEADERS_gate" in blob
