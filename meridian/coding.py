"""独立人工编码及分议题争议裁定，不自动推断倾向。

一条观察可同时覆盖多个非互斥议题。每名审核员按「观察 × 议题」各提交一条
判断，议题之间独立流转：可以在科技议题上达成共识，而文化议题仍在争议。
"""
from .contracts import adjudication, decision, digest, utc_now
from .errors import MeridianError, require
from .storage import decode, encode


class CodingDesk:
    def __init__(self, store, observations, sources):
        self.store = store
        self.observations = observations
        self.sources = sources

    def _eligible(self, origin, external_id, revision):
        item = self.observations.describe(origin, external_id, revision)
        require(item["is_head"] and item["status"] == "active", "stale_observation",
                "coding requires active head revision")
        return item

    def submit(self, payload):
        with self.store.transaction():
            return self._submit_locked(payload)

    def _submit_locked(self, payload):
        data = decision(payload)
        existing = self.store.one("SELECT * FROM decisions WHERE decision_id=?", (data["decision_id"],))
        if existing:
            saved = self.describe(data["decision_id"])
            if all(saved[key] == data[key] for key in data):
                return saved | {"duplicate": True}
            raise MeridianError("decision_conflict", "decision id was reused with different content")
        item = self._eligible(data["origin"], data["external_id"], data["revision"])
        self.sources.validate_coding(item["data"]["source_id"], data["topic"], data["audience"])
        key = (data["origin"], data["external_id"], data["revision"], data["reviewer"], data["topic"])
        previous = self.store.one(
            "SELECT decision_id FROM decisions WHERE origin=? AND external_id=? "
            "AND revision=? AND reviewer=? AND topic=?", key)
        require(previous is None, "reviewer_conflict", "reviewer already coded this topic on this revision")
        now = utc_now()
        with self.store.transaction():
            event_no = self.store.event("coding_submitted", data["decision_id"], digest(data), now)
            self.store.db.execute(
                "INSERT INTO decisions(decision_id,origin,external_id,revision,reviewer,topic,stance,"
                "audience,reason,created_at,event_no) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (data["decision_id"], data["origin"], data["external_id"], data["revision"],
                 data["reviewer"], data["topic"], data["stance"],
                 data["audience"], data["reason"], now, event_no),
            )
        return self.describe(data["decision_id"]) | {"duplicate": False}

    def describe(self, decision_id):
        row = self.store.one("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        require(row is not None, "decision_missing", "decision not found")
        return {key: row[key] for key in ("decision_id", "origin", "external_id", "revision",
                                           "reviewer", "topic", "stance", "audience", "reason",
                                           "event_no", "created_at")}

    def decisions_for(self, origin, external_id, revision):
        rows = self.store.all("SELECT decision_id FROM decisions WHERE origin=? AND external_id=? "
                              "AND revision=? ORDER BY reviewer,topic", (origin, external_id, revision))
        return [self.describe(row["decision_id"]) for row in rows]

    @staticmethod
    def case_key(origin, external_id, revision, topic):
        return f"{origin}/{external_id}/{revision}/{topic}"

    def _topic_states(self, origin, external_id, revision, decisions):
        """每个议题独立计算待审、共识、争议或裁定状态。"""
        by_topic = {}
        for row in decisions:
            by_topic.setdefault(row["topic"], []).append(row)
        states = {}
        for topic, rows in sorted(by_topic.items()):
            ordered = sorted(rows, key=lambda row: row["reviewer"])
            values = {(row["stance"], row["audience"]) for row in rows}
            entry = {"topic": topic, "reviewers": [row["reviewer"] for row in ordered],
                     "decision_ids": [row["decision_id"] for row in ordered],
                     "decision_count": len(ordered)}
            if len(rows) < 2:
                entry["status"] = "awaiting_review"
            elif len(values) == 1:
                entry["status"] = "consensus"
                entry["coding"] = {"topic": topic, "stance": ordered[0]["stance"],
                                   "audience": ordered[0]["audience"]}
            else:
                key = self.case_key(origin, external_id, revision, topic)
                ruling = self.store.one("SELECT * FROM adjudications WHERE case_key=?", (key,))
                heads = [row["decision_id"] for row in ordered]
                if ruling and decode(ruling["decision_heads"]) == heads:
                    entry["status"] = "adjudicated"
                    entry["coding"] = {"topic": topic, "stance": ruling["stance"],
                                       "audience": ruling["audience"]}
                    entry["adjudicator"] = ruling["adjudicator"]
                    entry["case_key"] = key
                else:
                    entry["status"] = "disputed"
            states[topic] = entry
        return states

    def topic_status(self, origin, external_id, revision, topic):
        states = self.status(origin, external_id, revision)["topics"]
        require(topic in states, "topic_not_coded", "no coding for topic on this revision", topic=topic)
        return states[topic]

    @staticmethod
    def _aggregate_status(states):
        statuses = {entry["status"] for entry in states.values()}
        if not statuses:
            return "awaiting_review"
        if statuses == {"consensus"} or statuses == {"adjudicated"} or \
                statuses <= {"consensus", "adjudicated"}:
            return "consensus" if "adjudicated" not in statuses else "adjudicated"
        if "disputed" in statuses:
            return "disputed"
        return "awaiting_review"

    def status(self, origin, external_id, revision):
        decisions = self.decisions_for(origin, external_id, revision)
        topics = self._topic_states(origin, external_id, revision, decisions)
        aggregate = self._aggregate_status(topics)
        result = {"status": aggregate, "decisions": decisions, "topics": topics}
        confirmed = [entry for entry in topics.values() if "coding" in entry]
        result["confirmed_topics"] = [entry["topic"] for entry in confirmed]
        result["pending_topics"] = [topic for topic, entry in topics.items()
                                    if entry["status"] in {"awaiting_review", "disputed"}]
        if len(topics) == 1:
            only = next(iter(topics.values()))
            if "coding" in only:
                result["coding"] = only["coding"]
            if "adjudicator" in only:
                result["adjudicator"] = only["adjudicator"]
        return result

    def confirmed_units(self, origin, external_id, revision):
        """该观察当前可计入确定性趋势的分议题贡献。"""
        state = self.status(origin, external_id, revision)
        return [entry["coding"] | {"topic_status": entry["status"],
                                   "reviewers": entry["reviewers"],
                                   "decision_ids": entry["decision_ids"]}
                for entry in state["topics"].values() if "coding" in entry]

    def adjudicate(self, payload):
        with self.store.transaction():
            return self._adjudicate_locked(payload)

    def _adjudicate_locked(self, payload):
        data = adjudication(payload)
        item = self._eligible(data["origin"], data["external_id"], data["revision"])
        self.sources.validate_coding(item["data"]["source_id"], data["topic"], data["audience"])
        topic_state = self.topic_status(data["origin"], data["external_id"],
                                        data["revision"], data["topic"])
        require(topic_state["status"] in {"disputed", "adjudicated"}, "adjudication_not_needed",
                "only a disputed topic can be adjudicated")
        require(data["adjudicator"] not in set(topic_state["reviewers"]),
                "self_adjudication", "reviewer cannot adjudicate a topic they coded")
        key = self.case_key(data["origin"], data["external_id"], data["revision"], data["topic"])
        heads = topic_state["decision_ids"]
        existing = self.store.one("SELECT * FROM adjudications WHERE case_key=?", (key,))
        if existing:
            same = (all(existing[field] == data[field] for field in ("adjudicator", "topic",
                                                                  "stance", "audience", "reason"))
                    and decode(existing["decision_heads"]) == heads)
            require(same, "adjudication_conflict", "topic already has another ruling")
            return self.status(data["origin"], data["external_id"], data["revision"]) | {"duplicate": True}
        now = utc_now()
        with self.store.transaction():
            event_no = self.store.event("case_adjudicated", key, digest(data), now)
            self.store.db.execute(
                "INSERT INTO adjudications(case_key,origin,external_id,revision,adjudicator,topic,"
                "stance,audience,reason,decision_heads,created_at,event_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, data["origin"], data["external_id"], data["revision"], data["adjudicator"],
                 data["topic"], data["stance"], data["audience"], data["reason"],
                 encode(heads), now, event_no),
            )
        return self.status(data["origin"], data["external_id"], data["revision"]) | {"duplicate": False}
