"""Learning system — records feedback, detects patterns, surfaces rules."""
import json
from .vibe import mean


class Learner:
    def __init__(self, db, taste):
        self.db = db
        self.taste = taste

    def record_feedback(self, track, action, reason=None, playlist=None, context="recommend"):
        """Record every approval/rejection/skip with audio features."""
        features = track.get("features") or {}
        self.db.insert_feedback(
            context=context,
            playlist=playlist,
            track_id=track.get("id"),
            track_name=track.get("name"),
            track_artist=track.get("artist"),
            action=action,
            reason=reason,
            audio_features=json.dumps(features),
        )
        self.detect_patterns(playlist)
        if playlist and action == "approved":
            self._evolve_playlist_profile(playlist)

    def record_correction(self, song_name, artist, from_playlist, to_playlist, reason=None):
        self.db.insert_correction(song_name, artist, from_playlist, to_playlist, reason)

    def detect_patterns(self, playlist=None):
        """Analyze recent feedback to learn rules."""
        recent = self.db.get_recent_feedback(80, playlist=playlist)
        rejections = [f for f in recent if f["action"] == "rejected"]
        approvals = [f for f in recent if f["action"] == "approved"]

        if len(rejections) < 3:
            return

        def feat(rows, key):
            vals = []
            for r in rows:
                try:
                    data = json.loads(r["audio_features"] or "{}")
                    if data.get(key) is not None:
                        vals.append(data[key])
                except Exception:
                    pass
            return vals

        scope = f" on {playlist}" if playlist else ""

        # energy drift
        rej_e = feat(rejections, "energy")
        app_e = feat(approvals, "energy")
        if rej_e and app_e:
            if mean(rej_e) < mean(app_e) - 0.12:
                self.db.upsert_rule(
                    f"User rejects low-energy tracks{scope} — prefer energy ≥ {mean(app_e) - 0.1:.2f}",
                    confidence=min(0.9, 0.5 + len(rejections) * 0.04),
                    source="auto-pattern",
                )
            elif mean(rej_e) > mean(app_e) + 0.12:
                self.db.upsert_rule(
                    f"User rejects high-energy tracks{scope} — prefer energy ≤ {mean(app_e) + 0.1:.2f}",
                    confidence=min(0.9, 0.5 + len(rejections) * 0.04),
                    source="auto-pattern",
                )

        # acoustic drift
        rej_a = feat(rejections, "acousticness")
        app_a = feat(approvals, "acousticness")
        if rej_a and app_a and abs(mean(rej_a) - mean(app_a)) > 0.2:
            direction = "less" if mean(rej_a) > mean(app_a) else "more"
            self.db.upsert_rule(
                f"User prefers {direction} acoustic tracks{scope}",
                confidence=0.6 + min(0.3, len(rejections) * 0.03),
                source="auto-pattern",
            )

        # reason-based rules
        reason_counts = {}
        for r in rejections:
            if r.get("reason"):
                reason_counts[r["reason"]] = reason_counts.get(r["reason"], 0) + 1
        for reason, cnt in reason_counts.items():
            if cnt >= 2:
                self.db.upsert_rule(
                    f"Repeated rejection reason{scope}: {reason} ({cnt}x)",
                    confidence=min(0.9, 0.4 + cnt * 0.15),
                    source="user-reason",
                )

    def _evolve_playlist_profile(self, playlist):
        """Update playlist profile from approved tracks."""
        approvals = self.db.get_recent_feedback(200, action="approved", playlist=playlist)
        if len(approvals) < 4:
            return
        feats = {"energy": [], "valence": [], "danceability": [], "acousticness": [], "tempo": []}
        for a in approvals:
            try:
                data = json.loads(a["audio_features"] or "{}")
                for k in feats:
                    if data.get(k) is not None:
                        feats[k].append(data[k])
            except Exception:
                pass
        profile = {}
        for k, vals in feats.items():
            if vals:
                profile[k] = mean(vals)
        if profile:
            self.db.save_playlist_profile(playlist, profile)

    def get_active_rules(self, min_confidence=0.4):
        return self.db.get_rules(min_confidence=min_confidence)

    def get_taste_context(self, playlist=None, max_rules=8, max_recent=8):
        """Build compact context string for the AI."""
        rules = self.get_active_rules()[:max_rules]
        recent_rej = self.db.get_recent_feedback(max_recent, action="rejected", playlist=playlist)
        recent_app = self.db.get_recent_feedback(max_recent, action="approved", playlist=playlist)

        parts = []
        if rules:
            parts.append("LEARNED RULES:")
            for r in rules:
                parts.append(f"  - {r['rule']} (conf {r['confidence']:.0%})")
        if recent_rej:
            parts.append("\nRECENTLY REJECTED (don't repeat):")
            for r in recent_rej:
                line = f"  - {r['track_artist']} — {r['track_name']}"
                if r.get("reason"):
                    line += f" [{r['reason']}]"
                parts.append(line)
        if recent_app:
            parts.append("\nRECENTLY APPROVED (what's working):")
            for r in recent_app:
                parts.append(f"  - {r['track_artist']} — {r['track_name']}")
        return "\n".join(parts) if parts else "No feedback yet."

    def stats(self):
        """Summary stats for UI."""
        all_rules = self.db.get_rules(min_confidence=0.0)
        recent = self.db.get_recent_feedback(500)
        app = sum(1 for r in recent if r["action"] == "approved")
        rej = sum(1 for r in recent if r["action"] == "rejected")
        return {
            "rules_total": len(all_rules),
            "rules_high_conf": sum(1 for r in all_rules if r["confidence"] >= 0.7),
            "feedback_total": len(recent),
            "approvals": app,
            "rejections": rej,
        }
