"""Flow engine — orders tracks for smooth transitions.
Like a DJ sequencing a set, not a shuffle."""


CAMELOT_MAJOR = {0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
                 6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"}
CAMELOT_MINOR = {0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
                 6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"}


class FlowEngine:
    STYLES = ("smooth", "build", "steady", "journey", "rollercoaster")

    def order(self, tracks, style="smooth"):
        """Order tracks for optimal flow.
        Each track dict must have: id, energy, valence, danceability, tempo; optionally key, mode.
        """
        if len(tracks) <= 2:
            return list(tracks)
        method = {
            "smooth": self._smooth,
            "build": self._build_arc,
            "steady": self._steady,
            "journey": self._journey,
            "rollercoaster": self._rollercoaster,
        }.get(style, self._smooth)
        return method(list(tracks))

    def transition_score(self, a, b):
        """How well does track A → track B flow? 0-1, higher = smoother."""
        if not a or not b:
            return 0.0
        e_delta = abs((a.get("energy") or 0.5) - (b.get("energy") or 0.5))
        ta = (a.get("tempo") or 110)
        tb = (b.get("tempo") or 110)
        bpm_diff = min(abs(ta - tb), abs(ta - tb * 2), abs(ta * 2 - tb)) / 200.0
        key_ok = self._keys_compatible(a.get("key"), a.get("mode"), b.get("key"), b.get("mode"))
        key_penalty = 0.0 if key_ok else 0.2
        v_delta = abs((a.get("valence") or 0.5) - (b.get("valence") or 0.5))
        d_delta = abs((a.get("danceability") or 0.5) - (b.get("danceability") or 0.5))

        cost = (
            e_delta * 0.35
            + bpm_diff * 0.25
            + key_penalty * 0.10
            + v_delta * 0.18
            + d_delta * 0.12
        )
        return max(0.0, 1.0 - cost)

    def flow_score(self, ordered_tracks):
        """Overall flow quality 0-1 for an already-ordered list."""
        if len(ordered_tracks) < 2:
            return 1.0
        scores = [
            self.transition_score(ordered_tracks[i], ordered_tracks[i + 1])
            for i in range(len(ordered_tracks) - 1)
        ]
        return sum(scores) / len(scores)

    # ---------- ordering strategies ----------
    def _smooth(self, tracks):
        """Nearest-neighbor: start at median-energy track, always pick smoothest next."""
        energies = [t.get("energy") or 0.5 for t in tracks]
        median_e = sorted(energies)[len(energies) // 2]
        start = min(tracks, key=lambda t: abs((t.get("energy") or 0.5) - median_e))
        ordered = [start]
        remaining = [t for t in tracks if t is not start]
        while remaining:
            last = ordered[-1]
            best = max(remaining, key=lambda t: self.transition_score(last, t))
            ordered.append(best)
            remaining.remove(best)
        return ordered

    def _build_arc(self, tracks):
        """Start chill, peak ~65%, cool down."""
        sorted_e = sorted(tracks, key=lambda t: t.get("energy") or 0.5)
        peak_at = max(1, int(len(sorted_e) * 0.65))
        buildup = self._smooth(sorted_e[:peak_at])
        cooldown = self._smooth(sorted_e[peak_at:])
        return buildup + list(reversed(cooldown))

    def _steady(self, tracks):
        """Group by similar energy — edges at ends, core in middle."""
        avg_e = sum((t.get("energy") or 0.5) for t in tracks) / len(tracks)
        core = [t for t in tracks if abs((t.get("energy") or 0.5) - avg_e) < 0.2]
        edges = [t for t in tracks if t not in core]
        if not core:
            return self._smooth(tracks)
        mid = len(edges) // 2
        return self._smooth(edges[:mid] + core + edges[mid:])

    def _journey(self, tracks):
        """Chapters: intro, build, peak, breakdown, outro."""
        n = len(tracks)
        if n < 5:
            return self._smooth(tracks)
        sorted_e = sorted(tracks, key=lambda t: t.get("energy") or 0.5)
        ch1 = sorted_e[:n // 5]
        ch2 = sorted_e[n // 5:2 * n // 5]
        ch3 = sorted_e[3 * n // 5:]
        ch4 = sorted_e[2 * n // 5:3 * n // 5]
        return (self._smooth(ch1) + self._smooth(ch2) + self._smooth(ch3)
                + list(reversed(self._smooth(ch4))))

    def _rollercoaster(self, tracks):
        """Alternate high and low for dynamic listening."""
        sorted_e = sorted(tracks, key=lambda t: t.get("energy") or 0.5)
        out = []
        low, high = sorted_e[:len(sorted_e) // 2], sorted_e[len(sorted_e) // 2:]
        while low or high:
            if high:
                out.append(high.pop())
            if low:
                out.append(low.pop(0))
        return out

    # ---------- key compatibility ----------
    def _keys_compatible(self, key_a, mode_a, key_b, mode_b):
        if key_a is None or key_b is None:
            return True
        cam_a = self._to_camelot(key_a, mode_a)
        cam_b = self._to_camelot(key_b, mode_b)
        if cam_a == cam_b:
            return True
        if cam_a[-1] != cam_b[-1] and cam_a[:-1] == cam_b[:-1]:
            return True
        try:
            na, nb = int(cam_a[:-1]), int(cam_b[:-1])
            if cam_a[-1] == cam_b[-1] and (abs(na - nb) <= 1 or abs(na - nb) == 11):
                return True
        except (ValueError, IndexError):
            pass
        return False

    def _to_camelot(self, key, mode):
        if mode == 1:
            return CAMELOT_MAJOR.get(key, "1B")
        return CAMELOT_MINOR.get(key, "1A")
