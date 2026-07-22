"""
KVBL metrics engine.

Computes, for every rostered player:
  sBPM / sOBPM / sDBPM  — Box Plus/Minus 2.0 (Daniel Myers / basketball-reference
                          methodology) from actual season stats, per 100 possessions,
                          team-adjusted so each roster sums to the team's adjusted
                          net rating.  League average = 0.
  sPER                  — Hollinger PER from actual stats, league-scaled to 15.0.
  pBPM / pOBPM / pDBPM  — the same BPM formula applied to per-36 stats PROJECTED
                          from the 1-99 ratings.  The rating→stat regressions are
                          fit fresh on the current league every build, so the
                          projections self-calibrate as the sim engine evolves.
  pPER                  — Hollinger PER on the projected stat line.
  KV                    — headline 0-100 rating: 70% BPM + 30% PER, where each
                          blends stats-based and ratings-projected by minutes
                          played (low-minute players lean on projections).

Free agents / rookies (ratings only, no team) get the p-metrics and KV.
"""

import json
import math
import os
import re

from scrape import norm_name

POS_NUM = {"PG": 1.0, "SG": 2.0, "G": 1.5, "SF": 3.0, "F": 3.5, "PF": 4.0, "C": 5.0}

# ── BPM 2.0 regression coefficients (basketball-reference about/bpm2) ──
# value at position 1.0 (PG) and position 5.0 (C); single value = constant
BPM_COEF = {
    "pts": (0.860, 0.860), "3pm": (0.389, 0.389), "ast": (0.580, 1.034),
    "to": (-0.964, -0.964), "orb": (0.613, 0.181), "drb": (0.116, 0.181),
    "stl": (1.369, 1.008), "blk": (1.327, 0.703), "pf": (-0.367, -0.367),
}
BPM_SHOT = {"fga": (-0.560, -0.780), "fta": (-0.246, -0.343)}  # by offensive role
BPM_POS_CONST, BPM_ROLE_CONST = -0.818, -2.774

OBPM_COEF = {
    "pts": (0.605, 0.605), "3pm": (0.477, 0.477), "ast": (0.476, 0.476),
    "to": (-0.579, -0.882), "orb": (0.606, 0.422), "drb": (-0.112, 0.103),
    "stl": (0.177, 0.294), "blk": (0.725, 0.097), "pf": (-0.439, -0.439),
}
OBPM_SHOT = {"fga": (-0.330, -0.472), "fta": (-0.145, -0.208)}
OBPM_POS_CONST, OBPM_ROLE_CONST = -1.698, -0.860

GAME_MIN = 48.0        # KVBL games are 48 minutes (team minutes sum to 240)


def lerp(pair, pos):
    a, b = pair
    return a + (b - a) * (pos - 1.0) / 4.0


# ────────────────────────────────────────────────────────────
# Small OLS helper (normal equations + gaussian elimination)
# ────────────────────────────────────────────────────────────
def ols(X, y):
    n, k = len(X), len(X[0])
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    Xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    # gaussian elimination with partial pivoting
    M = [row[:] + [Xty[a]] for a, row in enumerate(XtX)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            return [0.0] * k
        M[col], M[piv] = M[piv], M[col]
        for r in range(k):
            if r != col:
                f = M[r][col] / M[col][col]
                for c in range(col, k + 1):
                    M[r][c] -= f * M[col][c]
    return [M[a][k] / M[a][a] for a in range(k)]


# ────────────────────────────────────────────────────────────
# Team context: possessions, ratings
# ────────────────────────────────────────────────────────────
def team_context(tdata):
    off, dfn = tdata.get("team_off"), tdata.get("team_def")
    if not off or not dfn or off.get("g", 0) <= 0:
        return None
    def poss(t):
        return t["fga"] - t.get("orb", 0) + t.get("to", 0) + 0.44 * t["fta"]
    pace = (poss(off) + poss(dfn)) / 2.0
    if pace <= 0:
        return None
    ortg = off["ppg"] / pace * 100.0
    drtg = dfn["ppg"] / pace * 100.0
    tsa = off["fga"] + 0.44 * off["fta"]
    return {"pace": pace, "ortg": ortg, "drtg": drtg,
            "net": ortg - drtg, "pts_tsa": off["ppg"] / tsa if tsa > 0 else 1.0,
            "off": off, "dfn": dfn}


# ────────────────────────────────────────────────────────────
# sBPM: real-stats Box Plus/Minus 2.0
# ────────────────────────────────────────────────────────────
def compute_sbpm(teams, log=print):
    """teams: dict team -> scraped team data.  Annotates each player dict."""
    ctxs = {}
    for t, td in teams.items():
        ctx = team_context(td)
        if ctx:
            ctxs[t] = ctx
    if not ctxs:
        return
    league_ortg = sum(c["ortg"] for c in ctxs.values()) / len(ctxs)
    league_ptstsa = sum(c["pts_tsa"] for c in ctxs.values()) / len(ctxs)

    for t, td in teams.items():
        ctx = ctxs.get(t)
        if not ctx:
            continue
        off = ctx["off"]
        active = [p for p in td["players"]
                  if p.get("s") and p["s"].get("min", 0) > 0 and p["s"].get("g", 0) > 0]
        if not active:
            continue

        # position & offensive-role estimates (bbref appendix regressions,
        # blended with 50 minutes of listed position / role 4.0, team-centered to 3.0)
        team_trb = off.get("reb", 1) or 1
        team_ast = off.get("ast", 1) or 1
        team_stl = off.get("stl", 1) or 1
        team_blk = off.get("blk", 1) or 1
        team_pf = off.get("pf", 1) or 1
        thresh = ctx["pts_tsa"] - 0.33
        team_thr = off["ppg"] - thresh * (off["fga"] + 0.44 * off["fta"])

        for p in active:
            s = p["s"]
            floor = s["min"] / GAME_MIN            # fraction of game on floor
            mtot = s["min"] * s["g"]
            def share(x, tx):
                return (x / tx) / floor if tx > 0 and floor > 0 else 0.2
            pos_reg = (2.130 + 8.668 * share(s.get("reb", 0), team_trb)
                       - 2.486 * share(s.get("stl", 0), team_stl)
                       + 0.992 * share(s.get("pf", 0), team_pf)
                       - 3.536 * share(s.get("ast", 0), team_ast)
                       + 1.667 * share(s.get("blk", 0), team_blk))
            listed = POS_NUM.get(p.get("pos", "SF"), 3.0)
            p["_pos"] = (pos_reg * mtot + listed * 50) / (mtot + 50)

            thr_pts = s.get("ppg", 0) - thresh * (s.get("fga", 0) + 0.44 * s.get("fta", 0))
            role_reg = (6.00 - 6.642 * share(s.get("ast", 0), team_ast)
                        - 8.544 * share(thr_pts, team_thr if team_thr > 0 else 1))
            p["_role"] = (role_reg * mtot + 4.0 * 50) / (mtot + 50)

        # team-center both to minutes-weighted 3.0, clamp 1..5
        wsum = sum(p["s"]["min"] * p["s"]["g"] for p in active)
        for key in ("_pos", "_role"):
            avg = sum(p[key] * p["s"]["min"] * p["s"]["g"] for p in active) / wsum
            for p in active:
                p[key] = max(1.0, min(5.0, p[key] + (3.0 - avg)))

        # raw BPM / OBPM per player
        shoot_adj = league_ptstsa - ctx["pts_tsa"]     # team shooting context
        for p in active:
            s = p["s"]
            floor = s["min"] / GAME_MIN
            pposs = ctx["pace"] * floor                # possessions played per game
            per100 = lambda x: x / pposs * 100.0 if pposs > 0 else 0.0
            tsa100 = per100(s.get("fga", 0) + 0.44 * s.get("fta", 0))
            v = {
                "pts": per100(s.get("ppg", 0)) + shoot_adj * tsa100,
                "3pm": per100(s.get("3gm", 0)), "ast": per100(s.get("ast", 0)),
                "to": per100(s.get("to", 0)), "orb": per100(s.get("orb", 0)),
                "drb": per100(s.get("reb", 0) - s.get("orb", 0)),
                "stl": per100(s.get("stl", 0)), "blk": per100(s.get("blk", 0)),
                "pf": per100(s.get("pf", 0)),
                "fga": per100(s.get("fga", 0)), "fta": per100(s.get("fta", 0)),
            }
            p["_raw_bpm"] = raw_bpm(v, p["_pos"], p["_role"], BPM_COEF, BPM_SHOT,
                                    BPM_POS_CONST, BPM_ROLE_CONST)
            p["_raw_obpm"] = raw_bpm(v, p["_pos"], p["_role"], OBPM_COEF, OBPM_SHOT,
                                     OBPM_POS_CONST, OBPM_ROLE_CONST)

        # team adjustment: minutes-weighted sum (x5 on-court slots) anchors to
        # the team's lead-adjusted net rating
        lead = (off["ppg"] - ctx["dfn"]["ppg"]) / 2.0          # avg lead estimate
        target = ctx["net"] + (0.35 / 2.0) * lead
        target_off = (ctx["ortg"] - league_ortg) + (0.35 / 4.0) * lead
        minsum = sum(p["s"]["min"] for p in active)
        if minsum <= 0:
            continue
        def tadj(raw_key, tgt):
            tsum = sum(p[raw_key] * (p["s"]["min"] / minsum) * 5.0 for p in active)
            return (tgt - tsum) / 5.0
        c_bpm = tadj("_raw_bpm", target)
        c_obpm = tadj("_raw_obpm", target_off)
        for p in active:
            p["sBPM"] = round(p["_raw_bpm"] + c_bpm, 1)
            p["sOBPM"] = round(p["_raw_obpm"] + c_obpm, 1)
            p["sDBPM"] = round(p["sBPM"] - p["sOBPM"], 1)


def raw_bpm(v, pos, role, coef, shot, pos_const, role_const):
    total = sum(lerp(coef[k], pos) * v[k] for k in coef)
    total += sum(lerp(shot[k], role) * v[k] for k in shot)
    total += max(0.0, 3.0 - pos) * (pos_const / 2.0)     # position constant (< SF only)
    total += (3.0 - role) * (role_const / 2.0)           # offensive role constant
    return total


# ────────────────────────────────────────────────────────────
# PER (Hollinger uPER, minutes-weighted league mean scaled to 15)
# ────────────────────────────────────────────────────────────
def uper(pg, minutes):
    if minutes <= 0:
        return 0.0
    return (1.0 / minutes) * (
        pg["3gm"] * 1.5 + pg["fgm"] - pg["fga"] * 0.316
        + pg["ftm"] * 0.44 - pg["fta"] * 0.44
        + pg["ast"] + pg["orb"] * 1.667 + pg["drb"] + pg["stl"]
        + pg["blk"] * 1.09 - pg["pf"] * 0.75 - pg["to"])


def compute_sper(all_players):
    active = []
    for p in all_players:
        s = p.get("s")
        if not s or s.get("min", 0) <= 0 or s.get("g", 0) <= 0:
            continue
        pg = {k: s.get(k, 0) for k in ("3gm", "fgm", "fga", "ftm", "fta",
                                       "ast", "orb", "stl", "blk", "pf", "to")}
        pg["drb"] = s.get("reb", 0) - s.get("orb", 0)
        p["_uper"] = uper(pg, s["min"])
        active.append(p)
    if not active:
        return
    w = sum(p["s"]["min"] * p["s"]["g"] for p in active)
    mean = sum(p["_uper"] * p["s"]["min"] * p["s"]["g"] for p in active) / w
    scale = 15.0 / mean if mean else 1.0
    for p in active:
        p["sPER"] = round(p["_uper"] * scale, 1)


# ────────────────────────────────────────────────────────────
# Ratings → per-36 stat projections (fit live on current league)
# ────────────────────────────────────────────────────────────
def fit_projections(all_players, log=print):
    """Fit rating→per-36 regressions on rostered players with real minutes."""
    sample = []
    for p in all_players:
        s, r = p.get("s"), p.get("r")
        if not s or not r or s.get("min", 0) < 12 or s.get("g", 0) < 8:
            continue
        m36 = 36.0 / s["min"]
        row = {"r": r}
        row["fga2"] = (s["fga"] - s["3ga"]) * m36
        row["fgm2"] = (s["fgm"] - s["3gm"]) * m36
        row["fta"] = s["fta"] * m36
        row["ftm"] = s["ftm"] * m36
        row["3ga"] = s["3ga"] * m36
        row["3gm"] = s["3gm"] * m36
        for k in ("orb", "ast", "stl", "blk", "to", "pf"):
            row[k] = s.get(k, 0) * m36
        row["drb"] = (s.get("reb", 0) - s.get("orb", 0)) * m36
        sample.append(row)
    # A fresh season wipes every stat line, so there is nothing to regress on.
    # Fall back to the last good fit (model_cache.json, committed) — the sim's
    # rating→stat relationships carry over between seasons.
    if len(sample) < 30:
        cached = load_model_cache()
        if cached:
            log(f"  only {len(sample)} qualifying players — using cached model fit")
            return cached
        if not sample:
            log("  no stats and no cached fit — using built-in prior coefficients")
            return dict(PRIOR_MODELS)
        log(f"  [WARN] only {len(sample)} players to fit on; projections may be noisy")

    models = {}
    def fit(target, rating_keys):
        X = [[row["r"].get(k, 0) for k in rating_keys] + [1.0] for row in sample]
        y = [row[target] for row in sample]
        models[target] = (rating_keys, ols(X, y))
    fit("fga2", ["2ga"])
    fit("fgm2", ["2ga", "2g%"])
    fit("fta",  ["fta"])
    fit("ftm",  ["fta", "ft%"])
    fit("3ga",  ["3ga"])
    fit("3gm",  ["3ga", "3g%"])
    fit("orb",  ["orb"])
    fit("drb",  ["drb"])
    fit("ast",  ["ast"])
    fit("stl",  ["stl"])
    fit("blk",  ["blk"])
    fit("to",   ["to", "2ga", "ast"])    # high to-rating = fewer TOs; usage adds TOs
    fit("pf",   ["blk", "orb"])          # bigs foul more; weak but stable
    save_model_cache(models)
    return models


MODEL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_cache.json")

# Last-resort rating→per-36 coefficients, fitted on the 2011 season (279
# players).  Used only when there are no stats to fit on AND no cached fit,
# so projections still work on a clean checkout mid-offseason.
PRIOR_MODELS = {
    "fga2": (["2ga"], [0.18742, 1.16338]),
    "fgm2": (["2ga", "2g%"], [0.0661, 0.18403, -7.67091]),
    "fta": (["fta"], [0.12761, 0.58386]),
    "ftm": (["fta", "ft%"], [0.09473, 0.05155, -3.26253]),
    "3ga": (["3ga"], [0.10378, 0.17492]),
    "3gm": (["3ga", "3g%"], [0.03445, 0.00297, -0.08397]),
    "orb": (["orb"], [0.05479, 0.13747]),
    "drb": (["drb"], [0.10689, 0.29052]),
    "ast": (["ast"], [0.09704, 0.0157]),
    "stl": (["stl"], [0.0363, -0.1526]),
    "blk": (["blk"], [0.04816, 0.01443]),
    "to": (["to", "2ga", "ast"], [-0.04249, -0.00318, 0.00301, 4.34121]),
    "pf": (["blk", "orb"], [0.00145, -0.00171, 3.70204]),
}


def load_model_cache():
    try:
        with open(MODEL_CACHE, encoding="utf-8") as f:
            return {t: (m["keys"], m["beta"]) for t, m in json.load(f).items()}
    except Exception:
        return None


def save_model_cache(models):
    try:
        with open(MODEL_CACHE, "w", encoding="utf-8") as f:
            json.dump({t: {"keys": ks, "beta": [round(b, 5) for b in beta]}
                       for t, (ks, beta) in models.items()}, f, separators=(",", ":"))
    except Exception:
        pass


def project36(r, models):
    """Ratings dict → projected per-36 stat line."""
    out = {}
    for target, (keys, beta) in models.items():
        v = sum(b * r.get(k, 0) for b, k in zip(beta, keys)) + beta[-1]
        out[target] = max(0.0, v)
    out["fga"] = out["fga2"] + out["3ga"]
    out["fgm"] = min(out["fgm2"], out["fga2"]) + min(out["3gm"], out["3ga"])
    out["3gm"] = min(out["3gm"], out["3ga"])
    out["ftm"] = min(out["ftm"], out["fta"])
    out["pts"] = 2 * min(out["fgm2"], out["fga2"]) + 3 * out["3gm"] + out["ftm"]
    return out


def compute_projected(all_players, models, league_pace, league_ptstsa,
                      avg_tadj_bpm, avg_tadj_obpm):
    """pBPM/pOBPM/pDBPM + pPER for anyone with ratings (incl. free agents)."""
    poss36 = league_pace * 36.0 / GAME_MIN
    raws = []
    for p in all_players:
        r = p.get("r")
        if not r:
            continue
        s36 = project36(r, models)
        # pPER: uPER on per-36 line (minutes = 36)
        pg = {k: s36[k] for k in ("3gm", "fgm", "fga", "ftm", "fta",
                                  "ast", "orb", "drb", "stl", "blk", "pf", "to")}
        p["_puper"] = uper(pg, 36.0)
        # pBPM: per-100 conversion, listed position, neutral role 3
        per100 = lambda x: x / poss36 * 100.0
        tsa100 = per100(s36["fga"] + 0.44 * s36["fta"])
        v = {"pts": per100(s36["pts"]),
             "3pm": per100(s36["3gm"]), "ast": per100(s36["ast"]),
             "to": per100(s36["to"]), "orb": per100(s36["orb"]),
             "drb": per100(s36["drb"]), "stl": per100(s36["stl"]),
             "blk": per100(s36["blk"]), "pf": per100(s36["pf"]),
             "fga": per100(s36["fga"]), "fta": per100(s36["fta"])}
        pos = POS_NUM.get(p.get("pos", "SF"), 3.0)
        role = p.get("_role", 3.0)
        p["_praw_bpm"] = raw_bpm(v, pos, role, BPM_COEF, BPM_SHOT,
                                 BPM_POS_CONST, BPM_ROLE_CONST)
        p["_praw_obpm"] = raw_bpm(v, pos, role, OBPM_COEF, OBPM_SHOT,
                                  OBPM_POS_CONST, OBPM_ROLE_CONST)
        raws.append(p)

    # calibrate: rostered players' minutes-weighted pBPM mean must be 0 (like sBPM).
    # Between seasons there are no minutes, so fall back to weighting every
    # rostered player equally — the scale stays anchored to the league.
    ros = [p for p in raws if p.get("s") and p["s"].get("min", 0) > 0]
    if ros:
        wt = {id(p): p["s"]["min"] * p["s"]["g"] for p in ros}
    else:
        ros = [p for p in raws if p.get("team")]
        wt = {id(p): 1.0 for p in ros}
    if ros:
        w = sum(wt.values())
        shift_b = -sum(p["_praw_bpm"] * wt[id(p)] for p in ros) / w
        shift_o = -sum(p["_praw_obpm"] * wt[id(p)] for p in ros) / w
        mean = sum(p["_puper"] * wt[id(p)] for p in ros) / w
        pscale = 15.0 / mean if mean else 1.0
    else:
        shift_b, shift_o = avg_tadj_bpm, avg_tadj_obpm
        pscale = 1.0
    for p in raws:
        p["pBPM"] = round(p["_praw_bpm"] + shift_b, 1)
        p["pOBPM"] = round(p["_praw_obpm"] + shift_o, 1)
        p["pDBPM"] = round(p["pBPM"] - p["pOBPM"], 1)
        p["pPER"] = round(p["_puper"] * pscale, 1)


# ────────────────────────────────────────────────────────────
# KV headline rating (0-100): 70% BPM + 30% PER,
# stats-vs-projection blended by minutes played
# ────────────────────────────────────────────────────────────
def compute_kv(all_players):
    scored = []
    for p in all_players:
        if "pBPM" not in p:
            continue
        s = p.get("s") or {}
        mtot = s.get("min", 0) * s.get("g", 0)
        wt = min(1.0, mtot / 800.0)          # full trust in stats at ~800 minutes
        bpm = wt * p.get("sBPM", p["pBPM"]) + (1 - wt) * p["pBPM"]
        per = wt * p.get("sPER", p["pPER"]) + (1 - wt) * p["pPER"]
        p["_blend_bpm"] = round(bpm, 1)
        p["_blend_per"] = round(per, 1)
        scored.append(p)
    if not scored:
        return
    def zstats(vals):
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1.0
        return m, sd
    mb, sb = zstats([p["_blend_bpm"] for p in scored])
    mp, sp = zstats([p["_blend_per"] for p in scored])
    for p in scored:
        z = 0.7 * (p["_blend_bpm"] - mb) / sb + 0.3 * (p["_blend_per"] - mp) / sp
        p["_kvz"] = z
    zs = sorted(p["_kvz"] for p in scored)
    n = len(zs)
    for p in scored:
        # percentile rank → 0-100
        import bisect
        p["KV"] = round(bisect.bisect_left(zs, p["_kvz"]) / max(1, n - 1) * 100, 1)


# ────────────────────────────────────────────────────────────
# Draft pick valuation
# ────────────────────────────────────────────────────────────
def pick_curve(slot, rnd, years_out=0):
    """Pick value in Trade-Value units.  A #1 overall ≈ 40: the expected TV of
    a young high-KV starter on a rookie deal, discounted for draft risk."""
    base = 40 * math.exp(-(slot - 1) / 9.0) if rnd == 1 else 10 * math.exp(-(slot - 1) / 12.0)
    return round(base * 0.93 ** max(0, years_out), 1)


# ────────────────────────────────────────────────────────────
# Trade Value: what a player is actually worth in a deal
# ────────────────────────────────────────────────────────────
AGE_MULT = {"rising": 1.2, "improving": 1.1, "peak": 1.0, "declining": 0.75, "falling": 0.5}


def compute_tv(all_players):
    """TV = (KV/100)³ × 100, then age/contract adjusted.

    The cube is the point: talent value is convex.  KV 90 → 73 base while
    KV 50 → 12.5 base, so one star ALWAYS beats a pile of role players
    (2×50 = 25 ≪ 73; you can't paper over a star with quantity).

      × age:      rising 1.2 · improving 1.1 · peak 1.0 · declining 0.75 · falling 0.5
      × contract: +10% per extra year of control (max +30%)
      + surplus:  fair salary for the KV level ((KV/100)² × $20M) minus actual
                  salary, at 40¢ per $1M — overpaid non-contributors go negative.
    Rostered players only (needs a contract to be tradeable)."""
    for p in all_players:
        if not p.get("team") or p.get("KV") is None:
            continue
        kv = p["KV"] / 100.0
        contract = p.get("contract") or []
        sal = contract[0] if contract else 1.5
        years = max(1, len(contract))
        base = kv ** 3 * 100.0
        mult = AGE_MULT.get(age_trend(p.get("age", 27)), 1.0)
        horizon = 1 + 0.10 * (min(years, 4) - 1)
        fair = kv ** 2 * 20.0
        surplus = (fair - sal) * 0.4
        p["TV"] = round(max(-12.0, base * mult * horizon + surplus), 1)


STATS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "stats_cache.json")


def stats_fallback(data, log=print):
    """Between seasons kvbl.net zeroes every stat line.  Rather than let the
    whole app degrade to projections, carry last season's stats forward until
    the first sim of the new season replaces them.

    Players with no cached line (rookies, new signings) simply have no stats —
    every downstream metric already falls back to ratings-only projections for
    them, and KV weights stats by minutes played, so they're excluded naturally.
    """
    teams = data["teams"]
    live = sum(1 for td in teams.values() for p in td["players"]
               if p.get("s") and p["s"].get("min", 0) > 0)

    if live >= 50:                      # real season in progress — refresh cache
        payload = {
            "players": {norm_name(p["name"]): p["s"]
                        for td in teams.values() for p in td["players"]
                        if p.get("s") and p["s"].get("min", 0) > 0},
            "teams": {t: {"off": td.get("team_off"), "def": td.get("team_def")}
                      for t, td in teams.items()},
        }
        try:
            with open(STATS_CACHE, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
        except Exception as e:
            log(f"  [WARN] stats cache write: {e}")
        data["stats_stale"] = False
        return

    try:
        with open(STATS_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        log("  [WARN] no stats on site and no cached stats — ratings only")
        data["stats_stale"] = False
        return

    n = 0
    for t, td in teams.items():
        tc = cache.get("teams", {}).get(t)
        if tc and tc.get("off") and tc.get("def"):
            td["team_off"], td["team_def"] = tc["off"], tc["def"]
        for p in td["players"]:
            s = cache.get("players", {}).get(norm_name(p["name"]))
            if s:
                p["s"] = s
                n += 1
    data["stats_stale"] = True
    log(f"  season reset detected — carried last season's stats for {n} players "
        f"({sum(len(td['players']) for td in teams.values()) - n} on projections only)")


STANDINGS_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "standings_cache.json")


def slot_basis(data):
    """Standings to rank draft slots by.  A freshly reset season has every team
    at 0-0, which would make slots arbitrary — and last season's final table is
    what actually sets the draft order anyway, so use the cached one until real
    games are played."""
    cur = data.get("standings") or []
    played = sum(s["w"] + s["l"] for s in cur)
    if played > 0:
        try:
            with open(STANDINGS_CACHE, "w", encoding="utf-8") as f:
                json.dump([{k: s[k] for k in ("team", "conf", "w", "l", "pct")}
                           for s in cur], f, separators=(",", ":"))
        except Exception:
            pass
        return cur, False
    try:
        with open(STANDINGS_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached:
            return cached, True
    except Exception:
        pass
    return cur, False


def value_picks(data):
    """Attach an estimated slot + 0-100 value to every pick on every team.

    Slot: the ORIGINAL owner's reverse-standings rank today (worst record →
    pick 1).  A future year keeps the same slot estimate — best proxy we have.
    Value curve (0-100, comparable to KV): R1 = 100·e^-(slot-1)/9,
    R2 = 32·e^-(slot-1)/12, then discounted 7%/season of waiting.
    """
    basis, from_cache = slot_basis(data)
    order = sorted(basis, key=lambda s: (s["pct"], -s["l"]))
    slot = {s["team"]: i + 1 for i, s in enumerate(order)}
    n = len(order) or 26
    data["league"]["slots_from"] = "last season" if from_cache else "current standings"

    start_years = []
    for td in data["teams"].values():
        for pk in td["picks"]:
            m = re.match(r"(\d{4})-\d{4}\s+round\s+(\d)", pk["pick"], re.I)
            if m:
                start_years.append(int(m.group(1)))
    season = min(start_years) if start_years else 0
    data["league"]["season"] = season

    for team, td in data["teams"].items():
        for pk in td["picks"]:
            m = re.match(r"(\d{4})-\d{4}\s+round\s+(\d)", pk["pick"], re.I)
            if not m:
                continue
            year, rnd = int(m.group(1)), int(m.group(2))
            sl = slot.get(pk.get("from") or team, (n + 1) // 2)
            pk.update(year=year, round=rnd, slot=sl,
                      value=pick_curve(sl, rnd, year - season))


# ────────────────────────────────────────────────────────────
# Transaction evaluation ("who won the trade")
# ────────────────────────────────────────────────────────────
def evaluate_transactions(data, byname):
    """Parse each trade post ('X receives: a, b, 2014 first round pick'),
    price every asset (players at KV, picks on the pick curve using the
    sending team's projected slot) and declare a winner."""
    basis, _ = slot_basis(data)
    order = sorted(basis, key=lambda s: (s["pct"], -s["l"]))
    slot = {s["team"]: i + 1 for i, s in enumerate(order)}
    nicks = [s["team"] for s in data["standings"]]

    citymap = {}
    for f in data.get("finances", []):
        full = f["team_city"].upper()
        for nk in nicks:
            if nk.upper() in full:
                city = full.replace(nk.upper(), "").strip()
                if city:
                    citymap[norm_name(city)] = nk
                citymap[norm_name(full)] = nk
                break
    for nk in nicks:                      # posts sometimes use nicknames
        citymap.setdefault(norm_name(nk), nk)

    season = data["league"].get("season", 0)

    for tx in data.get("transactions", []):
        body = tx.pop("body", "")
        if tx.get("kind") != "trade" or not body:
            continue
        # the thread title names both parties cleanly: 'Trade: Memphis and Minnesota'
        tm = re.match(r"Trade:\s*(.+?)\s+and\s+(.+?)\s*$", tx.get("text", ""))
        if not tm:
            continue
        cities = [tm.group(1).strip(), tm.group(2).strip()]
        sides = []
        for i, city in enumerate(cities):
            other = cities[1 - i]
            m = re.search(
                rf"{re.escape(city)}\s+receives:\s*(.*?)"
                rf"(?=\s*{re.escape(other)}\s+(?:trades|receives):|\s*Deal is official|$)",
                body, re.I | re.S)
            if not m:
                sides = []
                break
            items = []
            for asset in m.group(1).split(","):
                asset = asset.strip().rstrip(".")
                if not asset:
                    continue
                pm = re.search(r"(\d{4}).*?(first|second|1st|2nd)\s+round", asset, re.I)
                if pm:
                    rnd = 1 if pm.group(2).lower() in ("first", "1st") else 2
                    yr = int(pm.group(1))
                    items.append({"label": f"{yr} R{rnd}", "val": None,
                                  "year": yr, "round": rnd})
                else:
                    p = byname.get(norm_name(asset))
                    if p:
                        items.append({"label": p["name"], "val": p.get("TV", p.get("KV"))})
                    elif len(asset) > 2:
                        items.append({"label": asset, "val": None})
            sides.append({"team": citymap.get(norm_name(city), city), "items": items})
        if len(sides) != 2:
            continue
        # picks: the sender is the other side — price on their projected slot
        for i, side in enumerate(sides):
            sender = sides[1 - i]["team"]
            for it in side["items"]:
                if "round" in it:
                    sl = slot.get(sender, (len(nicks) + 1) // 2)
                    it["val"] = pick_curve(sl, it["round"], max(0, it["year"] - season))
                    del it["year"], it["round"]
            side["total"] = round(sum(it["val"] or 0 for it in side["items"]), 1)
        a, b = sides
        if a["total"] != b["total"]:
            w = a if a["total"] > b["total"] else b
            tx["eval"] = {"sides": sides, "winner": w["team"],
                          "margin": round(abs(a["total"] - b["total"]), 1)}
        else:
            tx["eval"] = {"sides": sides, "winner": None, "margin": 0}


# ────────────────────────────────────────────────────────────
# FA-results resolution: who is already off the board
# ────────────────────────────────────────────────────────────
def mark_fa_resolved(data, fa_list):
    res = data.get("fa_results")
    if not res or not res.get("phases"):
        return
    blob = norm_name(" ".join(res["phases"]))
    for p in fa_list:
        if p.get("fa") in ("UFA", "RFA") and norm_name(p["name"]) in blob:
            p["res"] = 1
    data["fa_results"] = {k: res[k] for k in ("year", "url", "title")}
    data["fa_results"]["n_phases"] = len(res["phases"])


def age_trend(age):
    if age <= 23:
        return "rising"
    if age <= 25:
        return "improving"
    if age <= 28:
        return "peak"
    if age <= 31:
        return "declining"
    return "falling"


# ────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────
def compute_all(data, log=print):
    teams = data["teams"]
    log("stats check...")
    stats_fallback(data, log)
    rostered = [p for td in teams.values() for p in td["players"]]

    log("sBPM...")
    compute_sbpm(teams, log)
    log("sPER...")
    compute_sper(rostered)

    log("fitting rating->stat projections...")
    models = fit_projections(rostered, log)

    ctxs = []
    for t, td in teams.items():
        c = team_context(td)
        if c:
            ctxs.append(c)
            td["ctx"] = {k: round(c[k], 1) for k in ("pace", "ortg", "drtg", "net")}
    league_pace = sum(c["pace"] for c in ctxs) / len(ctxs) if ctxs else 96.0
    league_ptstsa = sum(c["pts_tsa"] for c in ctxs) / len(ctxs) if ctxs else 1.05

    # free-agent pool: UFA/RFA sheets + the currently-signable Available page.
    # A player can be on both (went unsigned in FA) — 'avl' flags signability.
    fas = {}
    for src, tag in (("ufa", "UFA"), ("rfa", "RFA")):
        for p in data.get(src, []):
            q = dict(p)
            q["fa"] = tag
            fas[norm_name(p["name"])] = q
    for p in data.get("available", []):
        key = norm_name(p["name"])
        if key in fas:
            fas[key]["avl"] = 1
        else:
            q = dict(p)
            q["fa"] = "AVL"
            q["avl"] = 1
            fas[key] = q
    fa_list = list(fas.values())

    # enrich FAs with their last rostered season: team pages keep stats rows
    # for departed players, so UFAs get real sBPM/sPER/pf alongside projections
    stat_by = {}
    for p in rostered:
        if p.get("s") and p["s"].get("min", 0) > 0:
            stat_by.setdefault(norm_name(p["name"]), p)
    for f in fa_list:
        src = stat_by.get(norm_name(f["name"]))
        if src:
            for k in ("s", "sBPM", "sOBPM", "sDBPM", "sPER"):
                if k in src and k not in f:
                    f[k] = src[k]

    log("projected metrics (rostered + FAs)...")
    everyone = rostered + fa_list
    compute_projected(everyone, models, league_pace, league_ptstsa, -8.0, -4.0)
    compute_kv(everyone)
    compute_tv(everyone)     # needs _blend_bpm from compute_kv, pre-stripping

    # eligibility: accent-insensitive name match against the eligibility sheet;
    # players not on the sheet fall back to their listed position
    elig_norm = {norm_name(k): v for k, v in (data.get("eligibility") or {}).items()}
    for p in everyone:
        p["trend"] = age_trend(p.get("age", 27))
        p["elig"] = elig_norm.get(norm_name(p["name"])) or ([p["pos"]] if p.get("pos") else [])
        # strip intermediates
        for k in list(p.keys()):
            if k.startswith("_"):
                del p[k]

    data["fa_pool"] = fa_list
    data["league"] = {"pace": round(league_pace, 1),
                      "pts_tsa": round(league_ptstsa, 3)}
    # regression slopes, for the curious
    data["models"] = {t: {"keys": ks, "beta": [round(b, 5) for b in beta]}
                      for t, (ks, beta) in models.items()}
    value_picks(data)
    byname = {}
    for p in everyone:
        key = norm_name(p["name"])
        if key not in byname or p.get("team"):
            byname[key] = p
    evaluate_transactions(data, byname)
    mark_fa_resolved(data, fa_list)
    # raw source lists are folded into fa_pool / transactions — don't ship twice
    for k in ("ufa", "rfa", "available", "fapreview"):
        data.pop(k, None)
    # stats-only ghost rows (players who left the team) served their purpose in
    # the team-level BPM math and now live on enriched FA entries — drop them
    for td in teams.values():
        td["players"] = [p for p in td["players"] if p.get("r")]
    return data
