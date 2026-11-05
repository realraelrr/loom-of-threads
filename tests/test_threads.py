import unittest

from knowledge_archive.adapters.x.threads import ThreadGraph


def record(id, author="10", parent=None, parent_author=None):
    return {
        "id": id,
        "author": {"id": author},
        "reply_to_id": parent,
        "reply_to_author_id": parent_author,
    }


class ThreadTests(unittest.TestCase):
    def test_middle_entry_recovers_ancestors_and_descendants(self):
        graph = ThreadGraph(
            [record("100"), record("101", parent="100"), record("102", parent="101")]
        )
        thread = graph.resolve("101")
        self.assertEqual(thread.root_id, "100")
        self.assertEqual(thread.node_ids, ("100", "101", "102"))
        self.assertEqual(thread.selected_id, "101")
        self.assertEqual(thread.coverage, "observed_only")

    def test_other_readers_and_authors_answers_to_them_are_not_continuations(self):
        graph = ThreadGraph(
            [record("100"), record("101", "20", "100"), record("102", "10", "101")]
        )
        self.assertEqual(graph.resolve("100").node_ids, ("100",))
        selected_reply = graph.resolve("102")
        self.assertEqual(selected_reply.node_ids, ("102",))
        self.assertEqual(selected_reply.context_ids, ("101",))

    def test_branches_are_included_without_rewriting_their_edges(self):
        graph = ThreadGraph(
            [
                record("100"),
                record("102", parent="100"),
                record("101", parent="100"),
                record("103", parent="101"),
            ]
        )
        self.assertEqual(graph.resolve("102").node_ids, ("100", "101", "103", "102"))
        self.assertEqual(graph.nodes["102"].parent_id, "100")

    def test_missing_parent_is_reported_and_not_fabricated(self):
        thread = ThreadGraph([record("101", parent="100")]).resolve("101")
        self.assertEqual(thread.missing_parent_ids, ("100",))
        self.assertEqual(thread.node_ids, ("101",))

    def test_missing_identity_is_not_replaced_by_handle_similarity(self):
        thread = ThreadGraph([record("100"), record("101", None, "100")]).resolve("100")
        self.assertEqual(thread.node_ids, ("100",))
        self.assertEqual(thread.unresolved_author_ids, ("101",))

    def test_repeated_capture_is_idempotent(self):
        self.assertEqual(
            ThreadGraph([record("100"), record("100")]).resolve("100").node_ids,
            ("100",),
        )

    def test_conflicting_observations_fail(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            ThreadGraph([record("100"), record("100", "20")])

    def test_parent_author_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "disagrees"):
            ThreadGraph(
                [record("100"), record("101", parent="100", parent_author="20")]
            )

    def test_cycles_fail(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            ThreadGraph(
                [record("100", parent="101"), record("101", parent="100")]
            ).resolve("100")

    def test_missing_author_observation_can_be_enriched_without_identity_inference(
        self,
    ):
        graph = ThreadGraph(
            [record("100", None), record("100", "10"), record("101", "10", "100")]
        )
        self.assertEqual(graph.resolve("101").node_ids, ("100", "101"))

    def test_numeric_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            ThreadGraph([record(2117000000000000000)])


if __name__ == "__main__":
    unittest.main()
