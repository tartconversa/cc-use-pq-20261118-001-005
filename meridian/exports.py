"""导出可独立检查的完整报告证据包。"""
from collections import Counter, defaultdict
from .contracts import digest, report_query, text
from .errors import require


class EvidenceExporter:
    def __init__(self, store, observations, sources, coding, analytics, releases):
        self.store = store
        self.observations = observations
        self.sources = sources
        self.coding = coding
        self.analytics = analytics
        self.releases = releases

    def _evidence_rows(self, report):
        rows = []
        query = report["query"]
        for row in self.analytics._rows(query):
            item = row["item"]
            data = item["data"]
            state = row["state"]
            category = self.analytics._classify(row, query)
            units = self.analytics._topic_units(row, query)
            # 单议题时保留 legacy coding 字段；多议题时以 topic_units 为准。
            coding = units[0] if len(units) == 1 else state.get("coding")
            rows.append({"origin": data["origin"], "external_id": data["external_id"],
                         "revision": item["revision"], "observation_hash": item["hash"],
                         "source_id": data["source_id"], "region": row["source"]["region"],
                         "story_key": data["story_key"], "published_at": data["published_at"],
                         "category": category, "coding": coding,
                         "topic_units": units,
                         "confirmed_topics": state["confirmed_topics"],
                         "pending_topics": state["pending_topics"],
                         "reviewer_ids": [d["decision_id"] for d in state["decisions"]]})
        rows.sort(key=lambda item: (item["origin"], item["external_id"]))
        return rows

    def current(self, query):
        report = self.analytics.report(query)
        evidence = self._evidence_rows(report)
        bundle = {"kind": "current", "report": report, "evidence": evidence,
                  "event_head": report["event_head"]}
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    def frozen(self, release_id):
        release_id = text(release_id, "release_id")
        release = self.releases.describe(release_id)
        report = release["report"]
        # 冻结证据取自发布时已封存的报告：provenance 记录观察级分类，
        # stories 记录每项议题贡献及其人工意见依据，两者均不可变。
        provenance_index = {(row["origin"], row["external_id"]): row
                            for row in report["provenance"]}
        topic_units = defaultdict(list)
        decision_lookup = defaultdict(list)
        for story in report["stories"]:
            embedded = story.get("evidence")
            if embedded is None:
                # 兼容更早格式的冻结报告：证据仅保留代表观察。
                embedded = [{"origin": story["representative"]["origin"],
                             "external_id": story["representative"]["external_id"],
                             "topic": story["topic"], "stance": story["stance"],
                             "audience": story["audience"], "topic_status": "consensus",
                             "decision_ids": [], "reviewers": []}]
            for evidence in embedded:
                key = (evidence["origin"], evidence["external_id"])
                unit = {"topic": evidence.get("topic", story["topic"]),
                        "stance": evidence["stance"], "audience": evidence["audience"],
                        "topic_status": evidence.get("topic_status", "consensus"),
                        "decision_ids": evidence.get("decision_ids", []),
                        "reviewers": evidence.get("reviewers", [])}
                topic_units[key].append(unit)
                decision_lookup[key].extend(evidence.get("decision_ids", []))
        evidence = []
        for key, row in sorted(provenance_index.items()):
            evidence.append({**row,
                             "topic_units": topic_units.get(key, []),
                             "reviewer_ids": sorted(set(decision_lookup.get(key, [])))})
        bundle = {"kind": "frozen", "release_id": release_id, "report": report,
                  "evidence": evidence, "event_head": release["event_head"],
                  "preview_token": release["preview_token"]}
        bundle["bundle_hash"] = digest(bundle)
        return bundle

    @staticmethod
    def verify(bundle):
        require(isinstance(bundle, dict), "invalid_bundle", "bundle must be an object")
        required = {"kind", "report", "evidence", "event_head", "bundle_hash"}
        require(required <= set(bundle), "invalid_bundle", "bundle fields missing")
        actual = digest({key: value for key, value in bundle.items() if key != "bundle_hash"})
        require(actual == bundle["bundle_hash"], "bundle_hash_mismatch", "bundle content changed")
        report = bundle["report"]
        require(report["event_head"] == bundle["event_head"], "bundle_head_mismatch",
                "report and evidence head differ")
        require(report["story_count"] == len({(row["region"], row["story_key"])
                                              for row in report["stories"]}),
                "bundle_count_mismatch", "unique story count differs from rows")
        categories = Counter(row["category"] for row in bundle["evidence"])
        require(sum(categories.values()) == report["observations_in_window"], "bundle_count_mismatch",
                "observation count differs from evidence")
        require(categories["eligible"] == report["eligible_observations"], "bundle_count_mismatch",
                "eligible count differs from evidence")
        topic_units = sum(len(row.get("topic_units", [])) for row in bundle["evidence"])
        require(topic_units == report.get("eligible_topic_units",
                                          report["eligible_observations"]),
                "bundle_count_mismatch", "topic contribution count differs from evidence")
        for category, count in report["excluded"].items():
            require(categories[category] == count, "bundle_count_mismatch",
                    "exclusion differs from evidence", category=category)
        return {"valid": True, "bundle_hash": bundle["bundle_hash"],
                "stories": report["story_count"],
                "topic_contributions": report.get("topic_contribution_count",
                                                   len(report["stories"])),
                "observations": sum(categories.values())}

    def source_breakdown(self, query):
        bundle = self.current(query)
        by_source = defaultdict(lambda: Counter())
        for row in bundle["evidence"]:
            by_source[row["source_id"]][row["category"]] += 1
        result = []
        for source_id, counts in sorted(by_source.items()):
            source = self.sources.describe(source_id)
            result.append({"source_id": source_id, "outlet": source["outlet"],
                           "region": source["region"], "categories": dict(sorted(counts.items())),
                           "observations": sum(counts.values())})
        require(sum(row["observations"] for row in result) == bundle["report"]["observations_in_window"],
                "breakdown_mismatch", "source counts do not reconcile")
        return {"query_hash": bundle["report"]["query_hash"],
                "event_head": bundle["event_head"], "sources": result,
                "total_observations": bundle["report"]["observations_in_window"]}

    def story_evidence(self, query, region, story_key):
        spec = report_query(query)
        report = self.analytics.report(spec)
        story = [row for row in report["stories"] if row["region"] == region and row["story_key"] == story_key]
        require(story, "story_missing", "story not found")
        rows = [row for row in self._evidence_rows(report) if row["region"] == region
                and row["story_key"] == story_key]
        eligible_rows = [row for row in rows if row["category"] == "eligible"]
        # 故事级分母（去重观察数）与议题级分母（贡献单元数）分别对账。
        eligible_observations = {(row["origin"], row["external_id"]) for row in eligible_rows}
        require(len(eligible_observations) ==
                len({(evidence["origin"], evidence["external_id"])
                     for contribution in story for evidence in contribution["evidence"]}),
                "story_evidence_mismatch", "story observation count differs from evidence")
        require(sum(len(row["topic_units"]) for row in eligible_rows)
                == sum(row["observation_count"] for row in story),
                "story_evidence_mismatch", "topic contribution count differs from evidence")
        return {"query_hash": report["query_hash"], "event_head": report["event_head"],
                "story": story, "evidence": rows}

