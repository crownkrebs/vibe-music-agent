"""Vibe space math: distance, percentiles, signatures."""
from statistics import mean as _mean


FEATURES = ["energy", "valence", "danceability", "acousticness",
            "instrumentalness", "speechiness", "liveness"]
TEMPO_SCALE = 200.0


def mean(xs):
    xs = [x for x in xs if x is not None]
    return _mean(xs) if xs else 0.0


def percentile(xs, pct):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def vibe_signature(features):
    """Compact per-track signature for comparison."""
    if not features:
        return None
    return {
        "energy": features.get("energy", 0.5),
        "valence": features.get("valence", 0.5),
        "danceability": features.get("danceability", 0.5),
        "tempo": features.get("tempo", 110),
        "acousticness": features.get("acousticness", 0.3),
        "key": features.get("key"),
        "mode": features.get("mode"),
    }


def vibe_distance(a, b):
    """Euclidean-ish distance between two vibe signatures (0 = same, ~1 = far)."""
    if not a or not b:
        return 1.0
    d_energy = abs(a.get("energy", 0.5) - b.get("energy", 0.5)) * 0.25
    d_val = abs(a.get("valence", 0.5) - b.get("valence", 0.5)) * 0.20
    d_dance = abs(a.get("danceability", 0.5) - b.get("danceability", 0.5)) * 0.20
    d_ac = abs(a.get("acousticness", 0.3) - b.get("acousticness", 0.3)) * 0.15
    t_a = a.get("tempo", 110) or 110
    t_b = b.get("tempo", 110) or 110
    d_tempo = min(
        abs(t_a - t_b),
        abs(t_a - t_b * 2),
        abs(t_a * 2 - t_b),
    ) / TEMPO_SCALE * 0.20
    return clamp(d_energy + d_val + d_dance + d_ac + d_tempo, 0, 1)


def similarity(a, b):
    return 1.0 - vibe_distance(a, b)


def profile_from_features(features_list):
    """Build a statistical profile (center + range) from a list of audio_features dicts."""
    valid = [f for f in features_list if f]
    if not valid:
        return None
    stats = {}
    for feat in FEATURES:
        vals = [f.get(feat) for f in valid if f.get(feat) is not None]
        if vals:
            stats[feat] = {
                "mean": mean(vals),
                "p10": percentile(vals, 10),
                "p90": percentile(vals, 90),
                "p25": percentile(vals, 25),
                "p75": percentile(vals, 75),
            }
    tempos = [f.get("tempo") for f in valid if f.get("tempo")]
    if tempos:
        stats["tempo"] = {
            "mean": mean(tempos),
            "p10": percentile(tempos, 10),
            "p90": percentile(tempos, 90),
            "p25": percentile(tempos, 25),
            "p75": percentile(tempos, 75),
        }
    return stats


def center_of(profile):
    """Get {feat: mean} from a statistical profile."""
    if not profile:
        return {}
    return {k: (v["mean"] if isinstance(v, dict) else v) for k, v in profile.items()}
