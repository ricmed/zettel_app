"""The test doubles stay faithful to what they replace (#327).

About twenty hand-written index fakes lived in as many test files, each with the
methods its file needed on the day it was written. Two still offered
``upsert_moc``, a method ``VectorIndex`` lost with the Chroma MOC collection
(#301), and nothing noticed: a fake describes an interface no test compares to
the real one. These tests make that comparison.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from zettel.index import VectorIndex
from zettel.state import StateDB

from tests.fakes import FakeVectorIndex

TESTS = Path(__file__).resolve().parent
PUBLIC = {
    name
    for name, fn in inspect.getmembers(VectorIndex, inspect.isfunction)
    if not name.startswith("_")
}
# Names StateDB also has (upsert_chunk, close...) do not make a class an index fake.
INDEX_ONLY = PUBLIC - {name for name in dir(StateDB) if not name.startswith("_")}


def _params(fn) -> list[tuple[str, object, object]]:
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(fn).parameters.values()
        if p.name != "self"
    ]


@pytest.mark.parametrize("name", sorted(PUBLIC))
def test_the_fake_has_every_public_method_with_the_real_signature(name):
    assert _params(getattr(FakeVectorIndex, name)) == _params(getattr(VectorIndex, name))


def test_the_fake_offers_nothing_the_real_index_lacks():
    own = {
        n
        for n in vars(FakeVectorIndex)
        if not n.startswith("_") and callable(getattr(FakeVectorIndex, n))
    }
    assert own <= PUBLIC


def _shape(entry: dict) -> dict[str, type]:
    return {key: type(value) for key, value in entry.items()}


def test_search_results_have_the_real_index_shape(tmp_path, token_hash_embedding):
    """Same calls on the real index (offline embedding) and on the fake: same keys and types."""
    real = VectorIndex(tmp_path / "chroma", token_hash_embedding, "m")
    fake = FakeVectorIndex(notes={"n1": 0.2}, chunks={"c1": 0.1}, chapters={"ch1": 0.3})
    for index in (real, fake):
        index.upsert_permanent_note("n1", "grafos de conhecimento", {"source_id": "@S"})
        index.upsert_chunk("c1", "texto do chunk", {"source_id": "@S"})
        index.upsert_chapter_summary("ch1", "resumo do capitulo", {"source_id": "@S"})

    pairs = [
        (real.query_similar_notes("grafos", 3), fake.query_similar_notes("grafos", 3)),
        (real.query_notes_by_ids("grafos", ["n1"]), fake.query_notes_by_ids("grafos", ["n1"])),
        (real.query_chapter_summaries("resumo", 3), fake.query_chapter_summaries("resumo", 3)),
        (real.find_similar_chunks(["texto"], 3), fake.find_similar_chunks(["texto"], 3)),
    ]
    for real_hits, fake_hits in pairs:
        assert real_hits and fake_hits
        assert _shape(fake_hits[0]) == _shape(real_hits[0])
    assert real.chunk_ids_for_source("@S") == fake.chunk_ids_for_source("@S") == {"c1"}
    assert real.existing_ids("chunks", ["c1", "x"]) == fake.existing_ids("chunks", ["c1", "x"])
    assert len(fake.embed_texts(["a b"])[0]) == len(real.embed_texts(["a b"])[0])


# -- every index fake in the suite derives from the canonical one -----------


def _test_files() -> list[Path]:
    return sorted(TESTS.rglob("test_*.py"))


def _classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Every class in the file, including those defined inside a test function."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def _index_fakes(tree: ast.Module) -> list[ast.ClassDef]:
    """Classes that stand in for the index: named so, or defining an index-only method."""
    found = []
    for node in _classes(tree):
        methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
        if any(tag in node.name for tag in ("Idx", "Index")) or methods & INDEX_ONLY:
            found.append(node)
    return found


def _derives_from_fake(node: ast.ClassDef, local: dict[str, ast.ClassDef]) -> bool:
    for base in node.bases:
        name = ast.unparse(base)
        if name == "FakeVectorIndex":
            return True
        if name in local and _derives_from_fake(local[name], local):
            return True
    return False


def test_every_index_fake_derives_from_the_canonical_one():
    offenders = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local = {n.name: n for n in _classes(tree)}
        for node in _index_fakes(tree):
            if not _derives_from_fake(node, local):
                offenders.append(f"{path.relative_to(TESTS)}:{node.lineno} {node.name}")
    assert not offenders, "index fakes outside tests/fakes.FakeVectorIndex: " + ", ".join(offenders)


def test_an_index_fake_overrides_only_real_methods_with_the_real_parameters():
    problems = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _index_fakes(tree):
            for fn in (n for n in node.body if isinstance(n, ast.FunctionDef)):
                if fn.name.startswith("_"):
                    continue
                where = f"{path.relative_to(TESTS)}:{fn.lineno} {node.name}.{fn.name}"
                if fn.name not in PUBLIC:
                    problems.append(f"{where} (VectorIndex has no such method)")
                    continue
                args = fn.args
                names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs][1:]
                real = [name for name, _kind, _default in _params(getattr(VectorIndex, fn.name))]
                if names != real:
                    problems.append(f"{where} {names} != {real}")
    assert not problems, "\n".join(problems)


def test_no_test_file_imports_another_test_file():
    """Shared doubles live in tests/fakes.py; a test file is not a library (#327)."""
    offenders = []
    for path in _test_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            if "tests.test_" in module or module.startswith("test_"):
                offenders.append(f"{path.relative_to(TESTS)}:{node.lineno} {module}")
    assert not offenders, ", ".join(offenders)
