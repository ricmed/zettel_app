"""Reparo das cercas cortadas pelo export por página do Docling."""

import re

from zettel.code_fence_repair import repair_docling_code_fences
from zettel.markdown_fences import iter_fenced_spans


def _bodies(text: str) -> list[str]:
    return [text[start:end] for start, end in iter_fenced_spans(text)]


def _markers(text: str) -> int:
    return len(re.findall(r"<!-- zettel:page \d+ -->", text))


_WATCH_OUTSIDE = (
    "As you watch this train, you can see the colors getting closer "
    "to each other, the adjectives getting closer to each other."
)

TRAINING = f"""\
```
for epoch in range(100):
    predicted = model.forward(input)
-----------------                            Word Vectors    |    293
```

|

<!-- zettel:page 314 -->

```
epoch_loss += loss.item()
As you watch this train, you can see the colors getting closer to each other the adic-
```

{_WATCH_OUTSIDE}
"""

_APPEAR = (
    "(Obviously bed and cat are not really similar, but in our training "
    "sentences they appear to be, and that's what the model is capturing.)"
)

CHART = f"""\
<!-- zettel:page 315 -->

```
(Obviously bed and cat are not really similar, but in our training sentences the
```

{_APPEAR}

We can also extract the first two principal components and plot them:

```
appear to be, and that's what the model is capturing.)

We can also extract the first two principal components and plot them:

plt.show()
```
"""

PAGE_RANK = """\
<!-- zettel:page 333 -->

### import tqdm

```
import tqdm

def page_rank(users, endorsements):
    for iter in tqdm.trange(num_iters):
        pass
```

|

<!-- zettel:page 334 -->

```
next_pr = {user.id: base_pr for user in users}
return pr
PageRank (Figure 22-6) identifies user 4 as the highest ranked data scientist.
```

PageRank (Figure 22-6) identifies user 4 as the highest ranked data scientist.
"""

RETURN_INPUT = """\
<!-- zettel:page 260 -->

```
def forward(self, input):
    for layer in self.layers:
        input = layer.forward(input)
```

|

<!-- zettel:page 261 -->

### return input

```
return input

def backward(self, gradient):
    return gradient
```
"""

PAID_TABLE = """\
```
0.7 paid
1.9 unpaid
2.5 paid
```

|

<!-- zettel:page 31 -->

```
4.2 unpaid
6.0 unpaid
```
"""

SEPARATE = """\
```
left_program()
```

The model learns a separate idea between these two listings.

<!-- zettel:page 40 -->

```
right_program()
```
"""

COMMENT = """\
<!-- zettel:page 12 -->

A concept is the smallest claim this chapter defends.

```
# A concept is the smallest claim this chapter defends.
value = 1
```
"""

NATIVE = """\
```
This sentence is English prose trapped inside a hand-written fence.
x = 1
```
"""

BOOK_SENTENCE = """\
<!-- zettel:page 3 -->

```
x = 1
And now we can create training data:
y = 2
```
"""

CODE_PIPE = """\
<!-- zettel:page 9 -->

```
print("P(both | older):", both_girls / older_girl)
$ cat the_bible.txt | python most_common_words.py 10
```
"""


def test_training_loop_cut_at_the_page_becomes_one_fence():
    repaired = repair_docling_code_fences(TRAINING)
    bodies = _bodies(repaired)
    assert len(bodies) == 1
    assert "model.forward" in bodies[0]
    assert "epoch_loss +=" in bodies[0]
    assert "<!-- zettel:page 314 -->" in bodies[0]
    assert "Word Vectors" not in repaired
    assert repaired.count("As you watch") == 1
    assert "As you watch" not in bodies[0]
    assert _markers(repaired) == _markers(TRAINING)


def test_chart_fence_does_not_open_with_the_echoed_sentence():
    repaired = repair_docling_code_fences(CHART)
    chart = next(body for body in _bodies(repaired) if "plt.show()" in body)
    assert "appear to be" not in chart
    assert repaired.count("appear to be") == 1
    assert "plt.show()" in chart


def test_page_rank_is_one_fence_and_the_import_heading_is_gone():
    repaired = repair_docling_code_fences(PAGE_RANK)
    bodies = _bodies(repaired)
    assert len(bodies) == 1
    assert "def page_rank" in bodies[0]
    assert "next_pr" in bodies[0]
    assert "<!-- zettel:page 334 -->" in bodies[0]
    assert "### import tqdm" not in repaired
    assert "import tqdm" in bodies[0]
    assert repaired.count("PageRank (Figure 22-6)") == 1
    assert "PageRank (Figure 22-6)" not in bodies[0]
    assert _markers(repaired) == _markers(PAGE_RANK)


def test_return_input_heading_enters_the_method_fence():
    repaired = repair_docling_code_fences(RETURN_INPUT)
    assert "### return input" not in repaired
    assert not re.search(r"^#{1,6}\s+return input\s*$", repaired, re.MULTILINE)
    method = next(body for body in _bodies(repaired) if "return input" in body)
    assert "def forward" in method
    assert "<!-- zettel:page 261 -->" in method


def test_paid_unpaid_listing_cut_at_the_page_becomes_one_fence():
    repaired = repair_docling_code_fences(PAID_TABLE)
    bodies = _bodies(repaired)
    assert len(bodies) == 1
    assert "0.7 paid" in bodies[0]
    assert "4.2 unpaid" in bodies[0]
    assert "<!-- zettel:page 31 -->" in bodies[0]


def test_a_real_sentence_between_fences_blocks_the_join():
    repaired = repair_docling_code_fences(SEPARATE)
    bodies = _bodies(repaired)
    assert len(bodies) == 2
    assert "left_program" in bodies[0]
    assert "right_program" in bodies[1]
    assert repaired.count("separate idea") == 1
    assert all("separate idea" not in body for body in bodies)
    assert "<!-- zettel:page 40 -->" in repaired


def test_concept_comment_stays_inside_the_fence():
    repaired = repair_docling_code_fences(COMMENT)
    bodies = _bodies(repaired)
    assert len(bodies) == 1
    assert "# A concept is the smallest claim this chapter defends." in bodies[0]
    assert "value = 1" in bodies[0]


def test_book_sentence_that_exists_only_inside_the_fence_moves_out():
    repaired = repair_docling_code_fences(BOOK_SENTENCE)
    bodies = _bodies(repaired)
    assert len(bodies) == 2
    assert "x = 1" in bodies[0]
    assert "y = 2" in bodies[1]
    assert repaired.count("And now we can create training data:") == 1
    assert all("And now we can create training data:" not in body for body in bodies)


def test_shell_and_print_pipes_stay_inside_the_fence():
    assert repair_docling_code_fences(CODE_PIPE) == CODE_PIPE


def test_native_markdown_without_a_page_marker_is_unchanged():
    assert repair_docling_code_fences(NATIVE) == NATIVE
    assert repair_docling_code_fences("") == ""
    assert repair_docling_code_fences("<!-- zettel:page-break -->\n\n" + NATIVE) == (
        "<!-- zettel:page-break -->\n\n" + NATIVE
    )


def test_second_pass_matches_the_first():
    for source in (TRAINING, CHART, PAGE_RANK, RETURN_INPUT, PAID_TABLE, SEPARATE, COMMENT):
        once = repair_docling_code_fences(source)
        assert repair_docling_code_fences(once) == once


def test_two_programs_touching_at_a_page_boundary_stay_one_fence():
    """Resíduo aceito: sem prosa no vão, dois programas viram uma cerca só."""
    text = (
        "```\ndef left():\n    return 1\n```\n\n"
        "<!-- zettel:page 8 -->\n\n"
        "```\ndef right():\n    return 2\n```\n"
    )
    bodies = _bodies(repair_docling_code_fences(text))
    assert len(bodies) == 1
    assert "def left" in bodies[0]
    assert "def right" in bodies[0]
    assert "<!-- zettel:page 8 -->" in bodies[0]
