# Potential ADR: Graph-Based Note Discovery with Weighted BFS Expansion

**Module**: RETRIEVAL  
**Category**: Retrieval Architecture / Knowledge Graph  
**Priority**: Must Document (Score: 120)  
**Date Identified**: 2026-08-30

---

## Existing ADR Context

ℹ️ **RELATED DECISIONS**

This decision closely relates to:
- **hybrid-dense-bm25-retrieval.md**: Graph expansion is mentioned as optional component (enabled: true, max_hops: 1). This ADR details the graph traversal mechanism specifically.
- Related to **note_connections architecture** built by connector phase.

**Relationship**: Graph expansion is the *how* (algorithm, weighting, traversal); RRF hybrid retrieval is the *what* (data fusion). Both work together in `Retriever.search_notes()`.

---

## What Was Identified

The retrieval system includes a graph expansion layer (`zettel/graph.py`, `expand_notes()` function) that performs 1..N hop weighted breadth-first search (BFS) over the `note_connections` table. After hybrid RRF retrieval ranks the top seeds, this layer optionally enriches results by walking the typed note-connection graph, treating edges as **undirected**, weighting each relation type differently, and applying exponential hop decay.

This is a lightweight GraphRAG pattern that surfaces conceptually linked notes that dense embeddings miss—e.g., a note marked `contradicts` sits far apart in vector space precisely because it argues the opposite, but the graph signal captures this conceptual link.

**Introduced**: Same commit as hybrid RRF (`2d6ff27`, "Add hybrid retrieval (BM25+vector) and lightweight GraphRAG") — introduced July 18, 2026. Graph traversal evolved alongside RRF, suggesting both were part of a single "retrieval enrichment" initiative.

**Stable**: Traversal algorithm unchanged since introduction. Thresholds frozen (max_hops: 1, decay: 0.5, max_neighbors: 10). Relation weights configured in `DEFAULT_RELATION_WEIGHTS` with `contradicts` weighted highest (1.0).

---

## Why This Might Deserve an ADR

- **Impact**: Optional but active (enabled by default). Affects ask, article, connector, sync with richer context when expanded.
- **Architectural Trade-off**: Undirected edges vs. directed edges in the note graph.
  - *Undirected*: A connection is relevant to both notes it touches; reverse direction reuses same relation weight. Vault presentation shows inverse labels (e.g., `contradicts` ↔ `is-contradicted-by`), but retrieval treats them symmetrically.
  - *Cost*: Must consider both (src→tgt, tgt→src) per edge, doubling frontier size.
  - *Benefit*: Simpler mental model; avoids asymmetry bugs.
- **Weighting Strategy**: Per-relation type (contradicts > extends ≈ depends_on > supports > exemplifies > related).
  - Design rationale: Embeddings are weak at detecting contradictions; relation type encodes signal that dense search misses.
  - Empirically ordered (not learned), suggesting ad-hoc calibration.
- **Hop Depth**: Limited to 1 hop (configurable, default).
  - Why 1? Deeper exploration (2+ hops) surfaces more notes but adds noise. Trade-off not visible in code comments.
  - Memory cost scales linearly per hop.
- **Cost to Change**: Changing traversal strategy (e.g., to BFS with different weights, depth-first, random walk) requires re-validating ask/article/connect behavior.
- **Team Knowledge**: Needed by anyone tuning retrieval or understanding why unrelated notes appear in search results.

---

## Evidence Found in Codebase

### Key Files
- [`zettel/graph.py`](../../../zettel/graph.py) - Complete (114 lines), `expand_notes()` function implements BFS traversal
- [`zettel/retrieval.py`](../../../zettel/retrieval.py) - Lines 299-331, `_expand_with_graph()` orchestrates expansion
- [`zettel/config.py`](../../../zettel/config.py) - Lines 164-173, `GraphExpansionConfig` (enabled, max_hops, decay, max_neighbors, relation_weights)

### Code Evidence

```python
# From zettel/graph.py (expand_notes):
def expand_notes(
    db: "StateDB",
    seed_ids: list[str],
    max_hops: int = 1,
    decay: float = 0.5,
    relation_weights: Optional[dict[str, float]] = None,
    max_neighbors: int = 10,
    seed_weights: Optional[dict[str, float]] = None,
) -> dict[str, GraphNeighbor]:
    """BFS over note_connections from ``seed_ids``.
    
    Edges are treated as **undirected**: a connection is relevant to both notes
    it touches, and the reverse direction reuses the same relation weight (the
    inverse label in the vault is presentation-only).
    """
    # ... BFS loop with frontier expansion
    for hop in range(1, max_hops + 1):
        if not frontier:
            break
        edges = db.get_connections_for_notes(list(frontier.keys()))
        next_frontier: dict[str, tuple[float, list[dict]]] = {}
        for edge in edges:
            src = edge["source_note_id"]
            tgt = edge["target_note_id"]
            rel = edge["relation_type"]
            rel_weight = weights.get(rel, weights.get("related", 0.5))
            
            # Undirected: consider edge from both endpoints
            for anchor, other in ((src, tgt), (tgt, src)):
                if anchor not in frontier or not other:
                    continue
                anchor_weight, anchor_via = frontier[anchor]
                # Weight = seed_score * relation_weight * decay^(hop-1)
                cand_weight = anchor_weight * rel_weight * (decay ** (hop - 1))
                # ...

# From zettel/config.py (GraphExpansionConfig):
class GraphExpansionConfig(BaseModel):
    """Expansao 1-N saltos sobre note_connections apos a fusao hibrida."""
    
    enabled: bool = True
    max_hops: int = 1                 # 1 salto ja traz o valor do GraphRAG leve
    decay: float = 0.5                # atenuacao do score por salto adicional
    max_neighbors: int = 10           # teto de vizinhos trazidos para o contexto
    relation_weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_RELATION_WEIGHTS)
    )

# From zettel/config.py (DEFAULT_RELATION_WEIGHTS):
DEFAULT_RELATION_WEIGHTS: dict[str, float] = {
    "contradicts": 1.0,      # Highest: embedding misses contradictions
    "extends": 0.9,
    "depends_on": 0.9,
    "supports": 0.8,
    "exemplifies": 0.7,
    "related": 0.5,
}

# From zettel/retrieval.py (_expand_with_graph):
def _expand_with_graph(
    self, seeds: list[RetrievedNote], exclude_id: Optional[str]
) -> list[RetrievedNote]:
    gcfg = self.cfg.retrieval.graph_expansion
    by_id = {s.note_id: s for s in seeds}
    neighbors = graph.expand_notes(
        self.db,
        seed_ids=list(by_id.keys()),
        max_hops=gcfg.max_hops,
        decay=gcfg.decay,
        relation_weights=gcfg.relation_weights,
        max_neighbors=gcfg.max_neighbors,
        seed_weights={s.note_id: s.score for s in seeds},
    )
    
    for nid, neigh in neighbors.items():
        # ...graph neighbors are additive to seeds, never displace them
```

### Impact Analysis

- **Introduced**: 2026-07-18 15:22:31 (commit 2d6ff27, alongside RRF)
- **Last modified**: 2026-07-18 (initial), stable since then
- **Files affected**: graph.py (core), retrieval.py (orchestration), config.py (config)
- **Consumers**: ask, article, connector, sync (all use Retriever.search_notes with expand_graph=true by default)
- **Graph scope**: Every note with connections (built by connector phase; optional but typically non-empty)
- **Query cost**: One batched DB query per BFS frontier (O(hops) round-trips to StateDB)

### Design Decisions Embedded

1. **Undirected edges**: Lines 86-87 in graph.py consider both (src, tgt) and (tgt, src) per edge
   - Rationale: Vault shows inverse labels for human readability; retrieval treats symmetrically
   - Alternative considered (implied by design): directed edges would preserve upstream/downstream semantics

2. **Exponential hop decay**: Line 90 in graph.py: `decay ** (hop - 1)`
   - Decay 0.5 means: hop 1 = 1x weight, hop 2 = 0.5x, hop 3 = 0.25x
   - Rationale: Closer notes are more relevant; distant notes noise
   - Alternative: linear decay, no decay, learned decay

3. **Per-relation weighting**: Lines 84, 155-161 in config.py
   - Contradicts = 1.0 (highest): embeddings weak at detecting opposites
   - Related = 0.5 (lowest): weakest signal
   - Rationale: Encodes domain knowledge about which relations signal relevance
   - Not learned; empirically ordered

4. **Max 1 hop by default**: GraphExpansionConfig.max_hops = 1
   - Comment says "1 salto ja traz o valor do GraphRAG leve" (1 hop already brings lightweight GraphRAG value)
   - Implies: deeper hops have diminishing returns / risk of noise
   - Not tuned per query type

### Alternatives Not Chosen (Implied)

- **Learned fusion**: No ML model for relation weighting; using static config
- **Directed traversal**: Would require treating upstream/downstream separately; not chosen
- **Multiple strategies per query**: Same expansion config for ask, article, connector; not per-consumer tuning
- **Dynamic depth**: max_hops is static; no per-query depth adaptation

---

## Questions to Address in ADR (if created)

1. Why BFS instead of DFS or random walk?
   - Answer likely: BFS exposes most relevant (closest) neighbors first; fairer for max_neighbors cutoff
   
2. Why exponential decay instead of linear or step decay?
   - Answer likely: exponential models "diminishing relevance" found in real networks
   
3. Why is `contradicts` weighted 1.0 (equal to seed relevance)?
   - Answer likely: contradictions are rare/valuable in embeddings; high weight reflects rarity
   
4. Why undirected edges, not directed?
   - Answer likely: simpler mental model; avoids asymmetry bugs; vault shows inverse labels anyway
   
5. Should different consumers (ask vs. article vs. connector) have different graph thresholds?
   - Answer likely: not currently; assumes one-size-fits-all expansion works for all
   
6. How was max_hops=1 chosen? Any evidence deeper expansion is worse?
   - Answer likely: empirical / pragmatic choice; deeper = more noise, higher query cost
   
7. How are relation weights maintained? Any process to retune them?
   - Answer likely: hardcoded in config.py; would require manual tuning + revalidation

---

## Related Potential ADRs

- **hybrid-dense-bm25-retrieval.md**: Graph expansion is the optional enrichment layer on top of RRF fusion
- **note-connection-typed-graph** (if created): The data structure graph.expand_notes consumes
- **ask-command-architecture** (if created): Ask command uses graph expansion; could document call site

---

## Additional Notes

- Graph traversal is O(hops) DB round-trips; with max_hops=1, that's 1 query for frontier neighbors
- `via` field in GraphNeighbor carries path provenance (which notes the edge passed through); useful for explaining why a note appeared
- `seed_weights` parameter allows ask/article to pass their own relevance scores; weights downstream discovery
- Relation weights are configurable in config.yaml (retrieval.graph_expansion.relation_weights); not hardcoded runtime
- Vault shows inverse labels for human readability (e.g., "contradicts" ↔ "is-contradicted-by"); graph treats them the same weight
- No A/B testing framework visible; changes to weights/decay require manual revalidation
