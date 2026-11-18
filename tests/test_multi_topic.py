"""非互斥多议题编码：故事级/议题级双分母、分议题争议与证据追溯。"""
import unittest
from tests.common import ServiceCase
from meridian.demo import decision, observation, query
from meridian.errors import MeridianError


def topic_decision(external_id, reviewer, topic, stance="positive", audience="general", revision=1):
    payload = decision(external_id, reviewer, stance, topic=topic, audience=audience)
    payload["decision_id"] = f"{external_id}-r{revision}-{reviewer}-{topic}"
    payload["revision"] = revision
    return payload


class MultiTopicCodingTests(ServiceCase):
    def code_topics(self, external_id, topics, source_id="north-a", revision=1):
        """两名审核员对给定议题列表给出一致编码。"""
        for topic in topics:
            self.service.coding.submit(
                topic_decision(external_id, "alice", topic, revision=revision))
            self.service.coding.submit(
                topic_decision(external_id, "bob", topic, revision=revision))

    def test_one_story_two_topics_does_not_inflate_story_denominator(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        report = self.report()
        # 故事级分母仍是 1，议题级贡献为 2，来源采集量为 1。
        self.assertEqual(report["observations_in_window"], 1)
        self.assertEqual(report["eligible_observations"], 1)
        self.assertEqual(report["story_count"], 1)
        self.assertEqual(report["unique_story_count"], 1)
        self.assertEqual(report["topic_contribution_count"], 2)
        self.assertEqual(report["eligible_topic_units"], 2)
        self.assertEqual(report["multi_topic_story_count"], 1)
        self.assertEqual(report["multi_topic_story_share"], 1.0)
        self.assertEqual(report["denominators"]["unique_stories"], 1)
        self.assertEqual(report["denominators"]["topic_contributions"], 2)
        self.assertEqual({row["topic"] for row in report["stories"]},
                         {"technology", "culture"})
        region = report["regions"][0]
        self.assertEqual(region["unique_story_count"], 1)
        self.assertEqual(region["topic_contribution_count"], 2)
        self.assertEqual(region["multi_topic_story_share"], 1.0)
        # 倾向占比以议题贡献为分母：2/2。
        self.assertEqual(region["stance_shares"], {"positive": 1.0})
        self.assertEqual(region["stance_share_denominator"], "topic_contribution")

    def test_reviewer_codes_each_topic_independently(self):
        self.add("a")
        self.service.coding.submit(topic_decision("a", "alice", "technology"))
        # 同一审核员可对同一观察的另一议题编码，不触发 reviewer_conflict。
        self.service.coding.submit(topic_decision("a", "alice", "culture"))
        status = self.service.coding.status("local-fixture", "a", 1)
        self.assertEqual(status["status"], "awaiting_review")
        self.assertEqual(
            {entry["topic"]: entry["status"] for entry in status["topics"].values()},
            {"technology": "awaiting_review", "culture": "awaiting_review"})
        # 同议题重复编码仍被拒绝。
        duplicate = topic_decision("a", "alice", "technology", "negative")
        duplicate["decision_id"] = "a-alice-technology-alt"
        with self.assertRaises(MeridianError) as caught:
            self.service.coding.submit(duplicate)
        self.assertEqual(caught.exception.code, "reviewer_conflict")

    def test_partial_agreement_keeps_confirmed_topic_only(self):
        # 科技部分两人一致；文化部分两人分歧。
        self.add("a")
        self.service.coding.submit(topic_decision("a", "alice", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "alice", "culture", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "culture", "negative"))
        status = self.service.coding.status("local-fixture", "a", 1)
        self.assertEqual(status["status"], "disputed")
        topic_states = {entry["topic"]: entry["status"] for entry in status["topics"].values()}
        self.assertEqual(topic_states, {"technology": "consensus", "culture": "disputed"})
        self.assertEqual(status["confirmed_topics"], ["technology"])
        self.assertEqual(status["pending_topics"], ["culture"])
        report = self.report()
        # 已确认的科技贡献进入趋势，争议的文化贡献不进入；整条稿件不被排除。
        self.assertEqual(report["story_count"], 1)
        self.assertEqual(report["topic_contribution_count"], 1)
        self.assertEqual(report["stories"][0]["topic"], "technology")
        self.assertEqual(report["excluded"], {})
        self.assertEqual(report["pending_topic_count"], 1)
        provenance = report["provenance"][0]
        self.assertEqual(provenance["category"], "eligible")
        self.assertEqual(provenance["confirmed_topics"], ["technology"])
        self.assertEqual(provenance["pending_topics"], ["culture"])
        # 发布前质量门识别未解决的议题争议。
        preview = self.service.releases.preview(query())
        self.assertFalse(preview["quality"]["passed"])
        self.assertIn("unresolved_disputes", preview["quality"]["reasons"])
        self.assertEqual(preview["quality"]["disputed_topic_count"], 1)

    def test_awaiting_topic_does_not_block_confirmed_contribution(self):
        # 科技已共识，文化仅一人编码（等待第二人，无争议）。
        self.add("a")
        self.service.coding.submit(topic_decision("a", "alice", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "alice", "culture", "positive"))
        report = self.report()
        self.assertEqual(report["awaiting_topic_count"], 1)
        self.assertEqual(report["disputed_topic_count"], 0)
        # 已确认的科技贡献可发布；待审议题在就绪检查中以警告暴露。
        self.assertTrue(self.service.releases.preview(query())["quality"]["passed"])
        readiness = self.service.diagnostics.readiness(query())
        self.assertIn("open_topic_reviews", readiness["warnings"])

    def test_topic_specific_adjudication_resolves_only_that_topic(self):
        self.add("a")
        self.service.coding.submit(topic_decision("a", "alice", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "alice", "culture", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "culture", "negative"))
        ruling = {"origin": "local-fixture", "external_id": "a", "revision": 1,
                  "adjudicator": "carol", "topic": "culture", "stance": "mixed",
                  "audience": "general",
                  "reason": "Culture section genuinely split"}
        result = self.service.coding.adjudicate(ruling)
        topic_states = {entry["topic"]: entry["status"] for entry in result["topics"].values()}
        self.assertEqual(topic_states, {"technology": "consensus", "culture": "adjudicated"})
        report = self.report()
        self.assertEqual(report["topic_contribution_count"], 2)
        culture = next(row for row in report["stories"] if row["topic"] == "culture")
        self.assertEqual(culture["stance"], "mixed")
        # 裁定依据可追溯到具体 decision id 和裁定人。
        evidence = culture["evidence"][0]
        self.assertEqual(evidence["topic_status"], "adjudicated")
        self.assertEqual(evidence["reviewers"], ["alice", "bob"])
        self.assertEqual(evidence["decision_ids"], ["a-r1-alice-culture", "a-r1-bob-culture"])
        lineage = self.service.provenance.coding_lineage("local-fixture", "a")
        self.assertEqual(len(lineage["adjudications"]), 1)
        self.assertEqual(lineage["adjudications"][0]["topic"], "culture")
        self.assertEqual(lineage["adjudications"][0]["adjudicator"], "carol")
        # 争议解决后质量门放行。
        self.assertTrue(self.service.releases.preview(query())["quality"]["passed"])

    def test_adjudication_requires_that_topic_to_be_disputed(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        ruling = {"origin": "local-fixture", "external_id": "a", "revision": 1,
                  "adjudicator": "carol", "topic": "technology", "stance": "mixed",
                  "audience": "general", "reason": "nothing to rule on"}
        with self.assertRaises(MeridianError) as caught:
            self.service.coding.adjudicate(ruling)
        self.assertEqual(caught.exception.code, "adjudication_not_needed")

    def test_two_sources_same_story_can_cover_different_topics(self):
        # 同故事：north-a 同时覆盖科技与文化，north-b 转载只覆盖科技，倾向相反。
        self.add("a", "north-a", "shared")
        self.code_topics("a", ["technology", "culture"])
        self.add("b", "north-b", "shared")
        self.service.coding.submit(topic_decision("b", "alice", "technology", "negative"))
        self.service.coding.submit(topic_decision("b", "bob", "technology", "negative"))
        report = self.report()
        # 唯一故事数 1；科技贡献有 2 个来源观察，文化贡献只有 1 个。
        self.assertEqual(report["story_count"], 1)
        rows = {row["topic"]: row for row in report["stories"]}
        self.assertEqual(rows["technology"]["observation_count"], 2)
        self.assertEqual(rows["technology"]["stance"], "mixed")
        self.assertEqual(rows["culture"]["observation_count"], 1)
        self.assertEqual(rows["culture"]["stance"], "positive")
        self.assertEqual(sorted(rows["technology"]["outlets"]), ["Outlet A", "Outlet B"])
        self.assertEqual(rows["culture"]["outlets"], ["Outlet A"])
        # 跨来源倾向冲突被质量门捕获。
        self.assertIn("cross_source_stance_conflict",
                      self.service.releases.preview(query())["quality"]["reasons"])

    def test_retraction_removes_both_contributions_but_keeps_lineage(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        self.service.observations.retract("local-fixture", "a", "withdrawn", expected_revision=1)
        report = self.report()
        self.assertEqual(report["story_count"], 0)
        self.assertEqual(report["topic_contribution_count"], 0)
        self.assertEqual(report["excluded"], {"retracted": 1})
        lineage = self.service.provenance.observation_history("local-fixture", "a")
        self.assertEqual(lineage["revisions"][0]["status"], "retracted")

    def test_correction_resets_per_topic_coding(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        changed = observation("a", "north-a", "updated-story")
        self.service.observations.ingest(changed, expected_revision=1)
        report = self.report()
        self.assertEqual(report["story_count"], 0)
        self.assertEqual(report["excluded"], {"uncoded": 1})
        status = self.service.coding.status("local-fixture", "a", 2)
        self.assertEqual(status["topics"], {})

    def test_topic_filter_counts_topic_level_denominator(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        tech_report = self.service.analytics.report(query() | {"topics": ["technology"]})
        self.assertEqual(tech_report["story_count"], 1)
        self.assertEqual(tech_report["topic_contribution_count"], 1)
        self.assertEqual(tech_report["multi_topic_story_share"], 0.0)
        culture_report = self.service.analytics.report(query() | {"topics": ["culture"]})
        self.assertEqual(culture_report["stories"][0]["topic"], "culture")

    def test_review_queue_shows_open_topic_without_hiding_confirmed_one(self):
        self.add("a")
        self.service.coding.submit(topic_decision("a", "alice", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "technology", "positive"))
        self.service.coding.submit(topic_decision("a", "alice", "culture", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "culture", "negative"))
        queue = self.service.diagnostics.queue(query())
        self.assertEqual(queue["total"], 1)
        item = queue["items"][0]
        self.assertEqual(item["status"], "disputed")
        self.assertEqual(item["confirmed_topics"], ["technology"])
        self.assertEqual(item["open_topics"],
                         [{"topic": "culture", "status": "disputed",
                           "reviewers": ["alice", "bob"]}])
        self.assertEqual(queue["open_topic_count"], 1)
        self.assertEqual(queue["open_topics_by_status"], {"disputed": 1})

    def test_evidence_export_carries_per_topic_provenance(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        bundle = self.service.exports.current(query())
        self.assertTrue(self.service.exports.verify(bundle)["valid"])
        row = bundle["evidence"][0]
        self.assertEqual(row["category"], "eligible")
        self.assertEqual({unit["topic"] for unit in row["topic_units"]},
                         {"technology", "culture"})
        self.assertEqual({tuple(unit["decision_ids"]) for unit in row["topic_units"]},
                         {("a-r1-alice-technology", "a-r1-bob-technology"),
                          ("a-r1-alice-culture", "a-r1-bob-culture")})
        # 多议题时 legacy 单议题 coding 不再出现，避免硬选一个议题。
        self.assertIsNone(row["coding"])

    def test_source_contribution_reports_both_denominators(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        result = self.service.provenance.source_contribution(query(), "north-a")
        self.assertEqual(result["observation_count"], 1)
        self.assertEqual(result["unique_story_count"], 1)
        self.assertEqual(result["topic_contribution_count"], 2)
        self.assertEqual(len(result["stories"]), 2)

    def test_frozen_report_keeps_topic_interpretation_after_later_dispute(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        preview = self.service.releases.preview(query())
        self.service.releases.publish("freeze", query(), preview["token"])
        # 发布后新进来一条对文化议题的争议修正（通过新修订使旧编码失效后重开争议）。
        # 发布后该稿发布修订版，旧编码失效；新修订上文化议题重新陷入争议。
        changed = observation("a", "north-a", "a")
        changed["headline"] = "Synthetic observation a (corrected)"
        self.service.observations.ingest(changed, expected_revision=1)
        self.assertEqual(self.service.observations.describe("local-fixture", "a")["revision"], 2)
        self.service.coding.submit(topic_decision("a", "alice", "culture", "positive", revision=2))
        self.service.coding.submit(topic_decision("a", "bob", "culture", "negative", revision=2))
        frozen = self.service.releases.describe("freeze")["report"]
        self.assertEqual(frozen["topic_contribution_count"], 2)
        self.assertEqual(frozen["multi_topic_story_share"], 1.0)
        bundle = self.service.exports.frozen("freeze")
        self.assertTrue(self.service.exports.verify(bundle)["valid"])
        frozen_topics = {unit["topic"] for row in bundle["evidence"]
                         for unit in row["topic_units"]}
        self.assertEqual(frozen_topics, {"technology", "culture"})
        comparison = self.service.releases.compare("freeze")
        self.assertEqual(comparison["multi_topic_share_before"], 1.0)
        self.assertEqual(comparison["multi_topic_share_after"], 0.0)

    def test_topic_matrix_reconciles_both_denominators(self):
        self.add("a")
        self.code_topics("a", ["technology", "culture"])
        self.add("b")
        self.code_topics("b", ["technology"])
        matrix = self.service.comparisons.topic_matrix(query())
        self.assertEqual(matrix["total_stories"], 2)
        self.assertEqual(matrix["topic_contribution_count"], 3)
        cells = {(row["topic"]): row for row in matrix["rows"]}
        self.assertEqual(cells["technology"]["story_count"], 2)
        self.assertEqual(cells["culture"]["story_count"], 1)

    def test_window_comparison_explains_share_vs_attribution_change(self):
        # 早期窗口只有一条单议题故事；月末新报道一条双议题故事。
        early = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-15T00:00:00+00:00",
                 "regions": ["North"], "topics": [], "audiences": []}
        later = {"start": "2026-11-15T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
                 "regions": ["North"], "topics": [], "audiences": []}
        self.add("a")
        self.code_topics("a", ["technology"])
        self.service.observations.ingest(
            observation("m", "north-a", "multi", "2026-11-20T10:00:00+00:00"))
        self.code_topics("m", ["technology", "culture"])
        comparison = self.service.comparisons.windows(early, later)
        region = comparison["regions"][0]
        # 左窗口：1 个唯一故事、1 项议题贡献、无多议题故事。
        self.assertEqual(region["left_stories"], 1)
        self.assertEqual(region["left_topic_contributions"], 1)
        self.assertEqual(region["left_multi_topic_story_share"], 0.0)
        # 右窗口：1 个新唯一故事，但产生 2 项议题贡献——多议题归属方式显式可见。
        self.assertEqual(region["right_stories"], 1)
        self.assertEqual(region["right_topic_contributions"], 2)
        self.assertEqual(region["right_multi_topic_story_share"], 1.0)


    def test_topic_adjudication_is_idempotent_and_scoped(self):
        self.add("a")
        self.service.coding.submit(topic_decision("a", "alice", "culture", "positive"))
        self.service.coding.submit(topic_decision("a", "bob", "culture", "negative"))
        ruling = {"origin": "local-fixture", "external_id": "a", "revision": 1,
                  "adjudicator": "carol", "topic": "culture", "stance": "mixed",
                  "audience": "general", "reason": "split"}
        first = self.service.coding.adjudicate(ruling)
        head = self.service.store.head()
        second = self.service.coding.adjudicate(ruling)
        self.assertTrue(second["duplicate"])
        self.assertEqual(head, self.service.store.head())
        # 对同一议题给出不同裁定必须冲突。
        conflict = dict(ruling, stance="positive", reason="changed mind")
        with self.assertRaises(MeridianError) as caught:
            self.service.coding.adjudicate(conflict)
        self.assertEqual(caught.exception.code, "adjudication_conflict")
        self.assertFalse(first.get("duplicate", False))

    def test_story_evidence_reconciles_after_partial_retraction(self):
        self.add("a", "north-a", "shared")
        self.code_topics("a", ["technology", "culture"])
        self.add("b", "north-b", "shared")
        self.service.coding.submit(topic_decision("b", "alice", "technology"))
        self.service.coding.submit(topic_decision("b", "bob", "technology"))
        self.service.observations.retract("local-fixture", "a", "bad record", expected_revision=1)
        result = self.service.exports.story_evidence(query(), "North", "shared")
        # 科技贡献仍由来源 b 支撑；文化贡献随撤回消失，证据包仍含撤回行以便追溯。
        self.assertEqual(len(result["story"]), 1)
        self.assertEqual(result["story"][0]["topic"], "technology")
        self.assertEqual(len(result["evidence"]), 2)
        self.assertEqual({row["category"] for row in result["evidence"]},
                         {"eligible", "retracted"})

    def test_observation_history_tracks_revision_specific_topic_coding(self):
        self.add("a")
        self.code_topics("a", ["technology"])
        changed = observation("a", "north-a", "a")
        changed["headline"] = "Synthetic observation a (corrected)"
        self.service.observations.ingest(changed, expected_revision=1)
        self.code_topics("a", ["technology", "culture"], revision=2)
        history = self.service.provenance.observation_history("local-fixture", "a")
        revisions = {item["revision"]: item for item in history["revisions"]}
        self.assertEqual(revisions[1]["coding_status"], "consensus")
        self.assertEqual(len(revisions[1]["decision_ids"]), 2)
        self.assertEqual(revisions[2]["coding_status"], "consensus")
        self.assertEqual(len(revisions[2]["decision_ids"]), 4)
        # 旧修订编码仍可追溯，当前头部贡献包含两个议题。
        report = self.report()
        self.assertEqual(report["topic_contribution_count"], 2)


if __name__ == "__main__":
    unittest.main()
