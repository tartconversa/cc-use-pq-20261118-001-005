"""运行本地合成数据的端到端验收场景。"""
import json
import tempfile
from pathlib import Path
from .service import MeridianService


def source(source_id, region, outlet):
    return {"source_id": source_id, "region": region, "outlet": outlet,
            "topics": ["technology", "culture"], "audiences": ["general", "students"]}


def observation(external_id, source_id, story_key, published_at="2026-11-01T10:00:00+00:00"):
    return {"origin": "local-fixture", "external_id": external_id, "source_id": source_id,
            "story_key": story_key, "published_at": published_at,
            "received_at": "2026-11-02T09:00:00+00:00",
            "headline": f"Synthetic observation {external_id}",
            "url": f"https://example.invalid/{external_id}"}


def decision(external_id, reviewer, stance, topic="technology", audience="general"):
    return {"decision_id": f"{external_id}-{reviewer}", "origin": "local-fixture",
            "external_id": external_id, "revision": 1, "reviewer": reviewer,
            "topic": topic, "stance": stance, "audience": audience,
            "reason": "Synthetic reviewer judgment based on supplied fixture"}


def query():
    return {"start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00",
            "regions": ["North"], "topics": [], "audiences": []}


def scenario():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "scenario.sqlite3")
        service = MeridianService(path)
        service.sources.register(source("s1", "North", "Outlet A"))
        service.sources.register(source("s2", "North", "Outlet B"))
        service.sources.register(source("s3", "South", "Outlet C"))
        rows = [observation("a", "s1", "story-1"), observation("b", "s2", "story-1"),
                observation("c", "s1", "story-2")]
        first = service.batches.step("fixture-batch", rows, limit=1)
        assert first["cursor"] == 1
        batch = service.batches.step("fixture-batch", rows)
        assert batch["accepted"] == 3 and batch["state"] == "closed"
        for external_id in ("a", "b", "c"):
            service.coding.submit(decision(external_id, "reviewer-a", "positive"))
            service.coding.submit(decision(external_id, "reviewer-b", "positive"))
        preview = service.releases.preview(query())
        assert preview["quality"]["passed"]
        assert preview["report"]["story_count"] == 2
        release = service.releases.publish("november", query(), preview["token"])
        assert release["report"]["story_count"] == 2
        service.observations.retract("local-fixture", "c", "bad source record", expected_revision=1)
        comparison = service.releases.compare("november")
        assert comparison["story_count_delta"] == -1
        assert service.releases.describe("november")["report"]["story_count"] == 2
        audit = service.audit.run()
        assert audit["ok"]
        result = {"batch": batch["state"], "released_stories": 2,
                  "current_delta": comparison["story_count_delta"], "audit": audit["ok"]}
        service.close()
        return result


def topic_decision(external_id, reviewer, topic, stance="positive", audience="general", revision=1):
    return {"decision_id": f"{external_id}-r{revision}-{reviewer}-{topic}",
            "origin": "local-fixture", "external_id": external_id, "revision": revision,
            "reviewer": reviewer, "topic": topic, "stance": stance, "audience": audience,
            "reason": f"Synthetic {topic} judgment based on supplied fixture"}


def multi_topic_scenario():
    """同一故事同时贡献科技与文化议题，文化议题分议题争议与裁定。"""
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "multi.sqlite3")
        service = MeridianService(path)
        service.sources.register(source("s1", "North", "Outlet A"))
        service.sources.register(source("s2", "North", "Outlet B"))
        # 同故事两条来源：s1 覆盖科技+文化，s2 转载只覆盖科技。
        service.observations.ingest(observation("a", "s1", "story-1"))
        service.observations.ingest(observation("b", "s2", "story-1"))
        for external_id in ("a", "b"):
            service.coding.submit(topic_decision(external_id, "reviewer-a", "technology"))
            service.coding.submit(topic_decision(external_id, "reviewer-b", "technology"))
        # 文化议题只有 s1 编码，且两名审核员分歧。
        service.coding.submit(topic_decision("a", "reviewer-a", "culture", "positive"))
        service.coding.submit(topic_decision("a", "reviewer-b", "culture", "negative"))
        disputed = service.analytics.report(query())
        assert disputed["unique_story_count"] == 1
        # 科技贡献行只有 1 行但含 2 个来源观察；文化未确认，不产生贡献行。
        assert disputed["topic_contribution_count"] == 1
        assert disputed["stories"][0]["observation_count"] == 2
        assert disputed["pending_topic_count"] == 1
        # 第三人只裁定文化议题；科技共识不受影响。
        service.coding.adjudicate({"origin": "local-fixture", "external_id": "a", "revision": 1,
                                   "adjudicator": "reviewer-c", "topic": "culture",
                                   "stance": "mixed", "audience": "general",
                                   "reason": "Culture coverage genuinely split"})
        report = service.analytics.report(query())
        assert report["unique_story_count"] == 1
        assert report["topic_contribution_count"] == 2
        assert report["multi_topic_story_share"] == 1.0
        preview = service.releases.preview(query())
        assert preview["quality"]["passed"]
        release = service.releases.publish("multi-topic", query(), preview["token"])
        bundle = service.exports.frozen("multi-topic")
        assert service.exports.verify(bundle)["valid"]
        result = {"unique_stories": release["report"]["unique_story_count"],
                  "topic_contributions": release["report"]["topic_contribution_count"],
                  "multi_topic_share": release["report"]["multi_topic_story_share"]}
        service.close()
        return result


if __name__ == "__main__":
    print(json.dumps(scenario(), ensure_ascii=False, sort_keys=True))
    print(json.dumps(multi_topic_scenario(), ensure_ascii=False, sort_keys=True))

