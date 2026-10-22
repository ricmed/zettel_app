# ADR-061: Stable MOCs — Persisted Bucket Models, Rebalance and Adoption

**Status:** Accepted
**Date:** 2026-10-20
**Issue:** #300 (epic #291)
**Depends on:** [ADR-019: Taxonomy-First MOC Clustering](./ADR-019-taxonomy-first-moc-clustering.md), [ADR-021: Single LLM Call Per Cluster](./ADR-021-single-llm-call-per-cluster-routing.md), [ADR-060: MOCs and Hubs Are Entry Points](./ADR-060-mocs-and-hubs-are-entry-points.md)
**Related to:** [ADR-020: Hub-Anchored MOC Generation](./ADR-020-hub-anchored-moc-pipeline.md), [ADR-030: Manual Notes Adopted at Sync](../MANUAL/ADR-030-manual-notes-adopted-at-sync-without-review-gate.md)

## Context and Problem Statement

Every `zettel garden` clustered the whole vault again. With a fixed seed (#294) the same vectors gave the same partition, but one new note changed the partition and the signatures, so the routing saw "new" clusters on every growth. MOCs only grew: a note that moved to another cluster stayed in its old MOC, and 10 notes of the development vault were in two taxonomy MOCs at once. The `cluster_signature` in the MOC frontmatter went stale on every incremental update (5 of 11 MOCs diverged from SQLite). A generated MOC could not be handed to the user: `sync-manual` skipped it, the next incremental update rewrote it and `--recreate` deleted it. A hub MOC whose hub note was deleted stayed without warning.

## Decision Drivers

* A MOC must stay recognisable between runs; a run with no new note must change nothing and cost no LLM call.
* A note belongs to at most one taxonomy MOC, the one its cluster reaches.
* The user can take any generated MOC and edit it freely.
* No schema migration: the vault and `state.db` are reset at will.

## Decision Outcome

1. **Persisted bucket models.** `gardener_assign.fit_bucket` fits UMAP + HDBSCAN with `prediction_data=True` (KMeans and the whole-bucket `single` fallbacks too) and returns a `BucketModel`: the label of every fitted note, a hash of the vector it was fitted on, and the fitted objects. `BucketModelStore` keeps one model per category under `cache_path/garden/` (joblib), valid for a fingerprint of the embedding model, the taxonomy file, the category representation (#295) and the clustering knobs (`gardener.model_store`). `bucket_clusters` reuses a valid model: notes fitted with the same vector keep their label, the others are placed with `hdbscan.approximate_predict` (or `KMeans.predict`), and no new cluster is created. The bucket is fitted again with `zettel garden --refit`, when the fingerprint changes, or when the unseen notes exceed `gardener.model_refit_growth` (0.5) times the fitted ones. `garden --plan` reads the store and never writes it. The global path (`cluster_within_category: false`) is not persisted.
2. **Rebalance.** The run records which notes reached each MOC (`_GardenStats.routed`); after routing, `gardener._rebalance` removes from each reached pipeline MOC the entries no cluster sent to it, and `sync_moc_backrefs` drops their backrefs. Each cluster reaches one MOC, so a note ends up in at most one taxonomy MOC. A MOC that no cluster reached is kept and listed in the report (`mocs_without_cluster`); the report also counts departures, reused and refitted buckets and the notes a model placed.
3. **Stability of a run with nothing new.** A MOC built from `k` clusters records `clusters: k` in its frontmatter, and an incremental update offers each cluster only `max_entries_per_moc // k` candidates (the share it got at creation). Candidates the LLM considered and left out (omitted at creation or in an update, or cut by the per-subsection cap) go to `moc_discards` like `"ignorar"`, so they are not offered and paid for again while the note is unchanged.
4. **Signature only in SQLite.** `cluster_signature` is no longer written to the MOC frontmatter (`_write_moc` drops it); SQLite holds the routing signature.
5. **Adoption.** `zettel adopt-moc <moc_id>` (and "Adotar MOC" on the web MOC page, run as a job) sets `origin: manual` in the file and in SQLite and clears the signature, keeping the body, the `auto-moc-backrefs` blocks and the `topic_index_terms` rows. From then on `owns_moc` and `purge_mocs_by_origin` keep every garden pipeline away from it, and `sync-manual` treats it as an edited manual MOC. A later run whose clusters match it creates a new pipeline MOC beside it, as #292 already does for a manual MOC with a category name.
6. **Orphan hubs.** `StateDB.list_orphan_hub_mocs` finds hub MOCs whose `hub_note_id` names no note. The hub report and `zettel doctor` flag them, and `delete-source` lists the ones its purge created. They are not removed automatically: `garden --hubs --recreate` removes them, `adopt-moc` keeps them.

## Consequences

* After this change: `zettel init --reset`, then `zettel garden --recreate` (which fits and saves every bucket model).
* The cache directory now holds pickles written by this process; the store refuses a model whose fingerprint or bucket does not match, but it must never be pointed at foreign files.
* Growth is absorbed into existing clusters; a genuinely new theme inside a category waits for `--refit` or for the growth threshold, and then the rebalance moves notes between MOCs.

## Addendum 2026-10-22 — library versions in the fingerprint, unreadable models refit (#331)

A bucket model is a pickle of numpy arrays and UMAP/HDBSCAN (or KMeans) objects, so what reads it back is the installed libraries, not only the configuration. Under NumPy 2.5 joblib 1.5.3 still assigned `array.shape` while loading, deprecated in NumPy 2.5; joblib 1.6.0 reshapes instead, and it is now a direct dependency (`joblib>=1.6.0`, imported by `BucketModelStore`), so the suite no longer ignores that warning. The fingerprint built by `gardener.model_store` now includes `gardener_assign.model_library_versions()`: the installed `numpy`, `joblib`, `scikit-learn`, `umap-learn` and `hdbscan`, read from the distribution metadata (umap and numba are not imported). An upgrade of any of them refits every bucket on the next `garden` instead of unpickling another version's objects, the same reasoning as the Docling versions in `compute_docling_config_hash`; the cost is one refit per upgrade, after which the rebalance may move notes between MOCs. A model file that does not load (corrupt, truncated, or written by an incompatible version) was already logged and treated as absent; `tests/test_gardener_assign.py` now pins that path and the version-driven refit.

## References

* `zettel/gardener_assign.py` — `BucketModel`, `BucketModelStore`, `bucket_clusters`, `fit_bucket`, `clusters_of`, `vector_hash`
* `zettel/gardener.py` — `model_store`, `_rebalance`, `_route`, `_moc_clusters`, `_keep_discards`, `adopt_moc`, `MocAdoptError`
* `zettel/gardener_hub.py` — `_orphan_hub_topics`
* `zettel/state/mocs.py` — `list_orphan_hub_mocs`
* `zettel/cli/manual.py` — `adopt-moc`; `zettel/cli/synthesis.py` — `garden --refit`
* `zettel/web/details.py` — `POST /mocs/{moc_id}/adopt`
