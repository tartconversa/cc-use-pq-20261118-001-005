"""区域汇总、显式排除、故事级去重与非互斥议题贡献。

分母约定（所有输出都显式标注）：
- 故事级分母 ``unique_story_count``：区域内去重后的 (region, story_key) 数量，
  一个多议题故事只算一个，不随议题复制而虚增。
- 议题级分母 ``topic_contribution_count``：(region, story_key, topic) 贡献行数量，
  同一故事可向科技、文化各贡献一次，各次贡献带自己的倾向和证据。
- 观察级分母 ``observations_in_window``：窗口内来源采集量，转载各自独立计数。
"""
from collections import Counter, defaultdict
from .contracts import digest, report_query
from .errors import require


# 观察级分类的优先顺序；部分议题有争议时稿件本身仍保留已确认贡献。
def _observation_category(item, state, spec, passing_units):
    if item["status"] == "retracted":
        return "retracted"
    topics = state["topics"]
    if not topics or all(entry["status"] == "awaiting_review" for entry in topics.values()):
        return "uncoded"
    if all(entry["status"] == "disputed" for entry in topics.values()):
        return "disputed"
    if not passing_units:
        confirmed = [entry for entry in topics.values() if "coding" in entry]
        if spec["topics"] and all(entry["topic"] not in spec["topics"] for entry in confirmed):
            return "outside_topic"
        return "outside_audience"
    return "eligible"


class EvidenceAnalytics:
    def __init__(self, store, observations, sources, coding):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding

    def _rows(self, spec):
        rows = []
        for item in self.observations.current():
            data = item["data"]
            if not spec["start"] <= data["published_at"] < spec["end"]:
                continue
            source = self.sources.describe(data["source_id"])
            if spec["regions"] and source["region"] not in spec["regions"]:
                continue
            state = self.coding.status(data["origin"], data["external_id"], item["revision"])
            rows.append({"item": item, "source": source, "state": state})
        return rows

    def _classify(self, row, spec):
        passing = self._topic_units(row, spec)
        return _observation_category(row["item"], row["state"], spec, passing)

    def _topic_units(self, row, spec):
        """该观察通过质量门槛、且命中查询筛选的分议题贡献单元。"""
        units = []
        for entry in row["state"]["topics"].values():
            if "coding" not in entry:
                continue
            coding = entry["coding"]
            if spec["topics"] and coding["topic"] not in spec["topics"]:
                continue
            if spec["audiences"] and coding["audience"] not in spec["audiences"]:
                continue
            units.append(coding | {"topic_status": entry["status"],
                                   "decision_ids": entry["decision_ids"],
                                   "reviewers": entry["reviewers"]})
        return units

    def report(self, payload):
        spec = report_query(payload)
        observed = self._rows(spec)
        exclusions = Counter()
        awaiting_topic_total = 0
        disputed_topic_total = 0
        groups = defaultdict(list)          # (region, story, topic, audience) -> 观察成员
        observed_keys = set()               # 通过筛选的观察
        eligible_keys = set()               # 至少有一个议题贡献的观察
        contribution_evidence = {}          # (origin, external_id) -> 贡献单元列表
        provenance = []
        for row in observed:
            data = row["item"]["data"]
            obs_key = (data["origin"], data["external_id"])
            observed_keys.add(obs_key)
            units = self._topic_units(row, spec)
            category = _observation_category(row["item"], row["state"], spec, units)
            pending = row["state"]["pending_topics"]
            open_states = [entry["status"] for entry in row["state"]["topics"].values()
                           if entry["status"] in {"awaiting_review", "disputed"}]
            awaiting_topic_total += open_states.count("awaiting_review")
            disputed_topic_total += open_states.count("disputed")
            provenance.append({"origin": data["origin"], "external_id": data["external_id"],
                               "revision": row["item"]["revision"],
                               "source_id": data["source_id"], "category": category,
                               "confirmed_topics": row["state"]["confirmed_topics"],
                               "pending_topics": pending,
                               "disputed_topics": [topic for topic, entry
                                                   in row["state"]["topics"].items()
                                                   if entry["status"] == "disputed"]})
            if category != "eligible":
                exclusions[category] += 1
                continue
            eligible_keys.add(obs_key)
            contribution_evidence[obs_key] = units
            for unit in units:
                key = (row["source"]["region"], data["story_key"], unit["topic"], unit["audience"])
                groups[key].append(row | {"unit": unit})
        story_rows = []
        for (region, story, topic, audience), members in sorted(groups.items()):
            ordered = sorted(members, key=lambda row: (row["item"]["data"]["published_at"],
                                                        row["item"]["data"]["source_id"],
                                                        row["item"]["data"]["origin"],
                                                        row["item"]["data"]["external_id"]))
            stances = [row["unit"]["stance"] for row in members]
            stance = stances[0] if len(set(stances)) == 1 else "mixed"
            evidence = []
            for row in members:
                data = row["item"]["data"]
                unit = row["unit"]
                evidence.append({"origin": data["origin"], "external_id": data["external_id"],
                                 "revision": row["item"]["revision"], "source_id": data["source_id"],
                                 "outlet": row["source"]["outlet"], "topic": unit["topic"],
                                 "stance": unit["stance"],
                                 "audience": unit["audience"], "topic_status": unit["topic_status"],
                                 "decision_ids": unit["decision_ids"], "reviewers": unit["reviewers"]})
            story_rows.append({"region": region, "story_key": story, "topic": topic,
                               "audience": audience, "stance": stance,
                               "observation_count": len(members),
                               "outlets": sorted({row["source"]["outlet"] for row in members}),
                               "evidence": evidence,
                               "representative": {"origin": ordered[0]["item"]["data"]["origin"],
                                                  "external_id": ordered[0]["item"]["data"]["external_id"]}})
        # 故事级分母：区域内 (region, story_key) 去重，与议题数量无关。
        unique_stories_by_region = defaultdict(set)
        topic_sets_by_story = defaultdict(set)
        for story in story_rows:
            unique_stories_by_region[story["region"]].add(story["story_key"])
            topic_sets_by_story[(story["region"], story["story_key"])].add(story["topic"])
        multi_topic_stories = sum(1 for topics in topic_sets_by_story.values() if len(topics) > 1)
        unique_story_count = sum(len(stories) for stories in unique_stories_by_region.values())
        by_region = defaultdict(lambda: {"stories": set(), "topic_contributions": 0,
                                         "stances": Counter(), "topics": Counter(),
                                         "audiences": Counter(), "observations": 0})
        for story in story_rows:
            bucket = by_region[story["region"]]
            bucket["stories"].add(story["story_key"])
            bucket["topic_contributions"] += 1
            bucket["stances"][story["stance"]] += 1
            bucket["topics"][story["topic"]] += 1
            bucket["audiences"][story["audience"]] += 1
            bucket["observations"] += story["observation_count"]
        regions = []
        for region in sorted(unique_stories_by_region):
            data = by_region.get(region)
            if data is None:
                regions.append({"region": region, "story_count": 0, "unique_story_count": 0,
                                "topic_contribution_count": 0, "multi_topic_story_count": 0,
                                "multi_topic_story_share": 0.0, "observation_count": 0,
                                "stance_counts": {}, "stance_shares": {},
                                "stance_share_denominator": "topic_contribution",
                                "topic_counts": {}, "audience_counts": {}})
                continue
            story_denominator = len(data["stories"])
            topic_denominator = data["topic_contributions"]
            multi_in_region = sum(1 for (reg, _), topics in topic_sets_by_story.items()
                                  if reg == region and len(topics) > 1)
            regions.append({
                "region": region,
                "story_count": story_denominator,
                "unique_story_count": story_denominator,
                "topic_contribution_count": topic_denominator,
                "multi_topic_story_count": multi_in_region,
                "multi_topic_story_share": round(multi_in_region / story_denominator, 6)
                if story_denominator else 0.0,
                "observation_count": data["observations"],
                "stance_counts": dict(sorted(data["stances"].items())),
                # 倾向占比以议题贡献为分母，并同时给出故事级分母以便对照。
                "stance_shares": {key: round(value / topic_denominator, 6)
                                  for key, value in sorted(data["stances"].items())},
                "stance_share_denominator": "topic_contribution",
                "topic_counts": dict(sorted(data["topics"].items())),
                "audience_counts": dict(sorted(data["audiences"].items()))})
        return {
            "query": spec, "query_hash": digest(spec), "event_head": self.store.head(),
            "observations_in_window": len(observed_keys),
            "eligible_observations": len(eligible_keys),
            "eligible_topic_units": sum(len(units) for units in contribution_evidence.values()),
            "excluded": dict(sorted(exclusions.items())),
            "awaiting_topic_count": awaiting_topic_total,
            "disputed_topic_count": disputed_topic_total,
            # 向后兼容字段：pending 统指尚未定论的议题（待审或争议）。
            "pending_topic_count": awaiting_topic_total + disputed_topic_total,
            "denominators": {
                "observations": len(observed_keys),
                "unique_stories": unique_story_count,
                "topic_contributions": len(story_rows),
                "multi_topic_stories": multi_topic_stories,
                "multi_topic_story_share": round(multi_topic_stories / unique_story_count, 6)
                if unique_story_count else 0.0,
                "note": "unique_stories counts each story once; topic_contributions counts "
                        "each (story, topic) pair, so a multi-topic story contributes once per topic."},
            "story_count": unique_story_count,
            "unique_story_count": unique_story_count,
            "topic_contribution_count": len(story_rows),
            "multi_topic_story_count": multi_topic_stories,
            "multi_topic_story_share": round(multi_topic_stories / unique_story_count, 6)
            if unique_story_count else 0.0,
            "stories": story_rows, "regions": regions,
            "provenance": sorted(provenance, key=lambda item: (item["origin"], item["external_id"]))}

    def trace_story(self, spec, region, story_key):
        report = self.report(spec)
        rows = [row for row in report["stories"] if row["region"] == region and row["story_key"] == story_key]
        require(rows, "story_missing", "story not found in query")
        observations = []
        for row in self._rows(report["query"]):
            data = row["item"]["data"]
            if row["source"]["region"] == region and data["story_key"] == story_key:
                observations.append({"origin": data["origin"], "external_id": data["external_id"],
                                     "revision": row["item"]["revision"],
                                     "classification": self._classify(row, report["query"]),
                                     "coding_status": row["state"]["status"],
                                     "confirmed_topics": row["state"]["confirmed_topics"],
                                     "pending_topics": row["state"]["pending_topics"],
                                     "topic_units": self._topic_units(row, report["query"]),
                                     "source_id": data["source_id"]})
        return {"stories": rows, "observations": sorted(observations, key=lambda x: (x["origin"], x["external_id"]))}

    def quality(self, report, *, minimum_stories=1, maximum_exclusion_share=0.4):
        require(type(minimum_stories) is int and minimum_stories >= 1, "invalid_gate", "minimum_stories invalid")
        require(isinstance(maximum_exclusion_share, (int, float)) and 0 <= maximum_exclusion_share <= 1,
                "invalid_gate", "maximum_exclusion_share invalid")
        total = report["observations_in_window"]
        reasons = []
        if report["story_count"] < minimum_stories:
            reasons.append("insufficient_stories")
        excluded = sum(report["excluded"].values())
        if total == 0 or excluded / total > maximum_exclusion_share:
            reasons.append("excess_exclusions")
        # 发布前质量门：任何议题仍处于争议，都不能把该议题纳入确定性趋势。
        # 仅等待第二人编码的议题不拦截发布，但通过 awaiting_topic_count 显式暴露。
        if report["excluded"].get("disputed", 0) or report.get("disputed_topic_count"):
            reasons.append("unresolved_disputes")
        if any(story["stance"] == "mixed" and story["observation_count"] > 1
               for story in report["stories"]):
            reasons.append("cross_source_stance_conflict")
        return {"passed": not reasons, "reasons": reasons, "total": total,
                "excluded": excluded, "minimum_stories": minimum_stories,
                "maximum_exclusion_share": maximum_exclusion_share,
                "awaiting_topic_count": report.get("awaiting_topic_count", 0),
                "disputed_topic_count": report.get("disputed_topic_count", 0),
                "pending_topic_count": report.get("pending_topic_count", 0)}
