"""Constructed pairs for the MinHash evaluation (#251). Not a vault measurement."""

from __future__ import annotations

import time

from zettel.harvester.minhash import (
    NUM_HASHES,
    containment,
    jaccard,
    minhash_jaccard,
    minhash_signature,
    word_shingles,
)

_SENTENCE = "o argumento sobre a maquina kantiana segue nesta pagina do livro. "


def _book(seed: str, n: int = 80) -> str:
    return seed + (_SENTENCE * n)


def _sign(text: str) -> tuple[int, ...]:
    return minhash_signature(word_shingles(text))


def test_identical_texts_have_jaccard_one():
    text = _book("capitulo unico. ")
    shingles = word_shingles(text)
    assert jaccard(shingles, word_shingles(text)) == 1.0
    assert minhash_jaccard(_sign(text), _sign(text)) == 1.0


def test_small_edit_stays_near_a_copy():
    words = [f"token{i}" for i in range(400)]
    original = " ".join(words)
    edited = words.copy()
    for index in range(0, 400, 50):
        edited[index] = f"trocado{index}"
    revised = " ".join(edited)
    left, right = word_shingles(original), word_shingles(revised)
    exact = jaccard(left, right)
    estimate = minhash_jaccard(_sign(original), _sign(revised))
    assert exact > 0.8
    assert estimate > 0.65


def test_chapter_is_contained_in_the_book():
    chapter = _book("capitulo tres. ", n=20)
    book = _book("capitulo um. ", n=20) + chapter + _book("capitulo cinco. ", n=20)
    part, whole = word_shingles(chapter), word_shingles(book)
    assert containment(part, whole) > 0.95
    assert jaccard(part, whole) < containment(part, whole)


def test_pdf_and_markdown_of_the_same_words_match():
    pdf = "Capítulo 1: A Máquina kantiana.\n\nO argumento segue nesta página do tratado."
    markdown = "capitulo 1 a maquina kantiana o argumento segue nesta pagina do tratado"
    assert jaccard(word_shingles(pdf), word_shingles(markdown)) == 1.0


def test_distinct_works_by_one_author_stay_apart():
    first = " ".join(f"inteiro primo modulo residuo {i}" for i in range(200))
    second = " ".join(f"paisagem oleo nuvem horizonte valle {i}" for i in range(200))
    left, right = word_shingles(first), word_shingles(second)
    assert jaccard(left, right) < 0.05
    assert minhash_jaccard(_sign(first), _sign(second)) < 0.15


def test_signature_of_a_long_text_is_under_a_second():
    text = _SENTENCE * 2000
    started = time.perf_counter()
    signature = minhash_signature(word_shingles(text))
    elapsed = time.perf_counter() - started
    assert len(signature) == NUM_HASHES
    assert elapsed < 1.0
