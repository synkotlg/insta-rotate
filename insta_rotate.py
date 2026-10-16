#!/usr/bin/env python3
"""
insta_rotate.py - Rotation d'abonnements Instagram.

Chaque jour : desabonnement des comptes suivis par ce script depuis N jours,
puis abonnement a de nouveaux comptes pris dans les abonnes de comptes sources.
Le navigateur utilise un profil persistant : connexion manuelle une seule fois.

  python insta_rotate.py login     connexion manuelle (une seule fois)
  python insta_rotate.py run       passe quotidienne (--here : rester sur le bureau courant)
  python insta_rotate.py status    etat de la rotation
  python insta_rotate.py check-seeds   verifie que les comptes sources existent
"""
import argparse
import atexit
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PROFILE = DATA / "profile"
DB_PATH = DATA / "state.db"
LOCK = DATA / "run.lock"
SHOTS = DATA / "debug"

# ---------------------------------------------------------------------------
# UI : tout ce qui depend du texte ou du DOM d'Instagram (FR + EN) est ici.
# ---------------------------------------------------------------------------
IG = "https://www.instagram.com"
FOLLOW_RE = re.compile(r"^(Suivre|Follow)$", re.I)
FOLLOWING_RE = re.compile(
    r"^(Abonné\(e\)|Abonné|Suivi(\(e\)|e)?|Following|Demande envoyée|Demandé|Requested)", re.I)
UNFOLLOW_RE = re.compile(
    r"^(Ne plus suivre|Se désabonner|Unfollow|Annuler la demande|Cancel request)$", re.I)
LATER_RE = re.compile(r"^(Plus tard|Pas maintenant|Not now)$", re.I)
COOKIES_RE = re.compile(r"^(Refuser les cookies optionnels|Decline optional cookies)$", re.I)
# Reactions d'Instagram, de la moins grave a la plus grave :
BLOCK_RE = re.compile(  # rythme trop rapide
    r"Réessayez plus tard|Try Again Later|Action bloquée|Action Blocked|"
    r"We limit how often|nous limitons|Veuillez patienter quelques minutes|"
    r"Please wait a few minutes", re.I)
WARN_RE = re.compile(  # automatisation soupconnee
    r"comportement automatisé|automated behavior|activité suspecte|suspicious activity", re.I)
ATTENTION_RE = re.compile(  # verification d'identite, compte suspendu
    r"Confirmez que c.est vous|Confirm it.s you|Votre compte a été (désactivé|suspendu)|"
    r"Your account has been (disabled|suspended)", re.I)
ATTENTION_URL_RE = re.compile(r"/challenge|/accounts/(suspended|disabled|login)")
NOT_FOUND_RE = re.compile(r"n.est (malheureusement )?pas disponible|isn.t available|Page not found", re.I)
LOGGED_IN_SEL = 'a[href="/direct/inbox/"], a[href="/explore/"]'
LOGIN_FORM_SEL = 'input[name="username"]'
# Ouverture de la liste d'abonnes d'un profil : ancien lien /pseudo/followers/, ou compteur
# "11,3 M followers" (lien sans href depuis octobre 2026)
FOLLOWERS_HREF_SEL = 'a[href="/{}/followers/"]'
FOLLOWERS_COUNT_RE = re.compile(r"^[\d\s.,]+\s*[A-Za-z]{0,3}\s*(followers?|abonné\(e\)s|abonnés?)$", re.I)
DIALOG_SEL = 'div[role="dialog"]'
ROW_LINK_SEL = 'a[href^="/"]'

# Pour chaque bouton "Suivre" : pseudo de sa ligne (premier lien /pseudo/ trouve dans le plus
# petit ancetre qui contient un lien). Si cet ancetre contient plusieurs boutons (ligne sans
# lien, donc on a remonte jusqu'a la liste entiere), on renvoie null plutot que le pseudo d'un autre.
JS_USERS = r"""els => els.map(el => {
  let n = el;
  for (let i = 0; i < 8 && n; i++) {
    n = n.parentElement;
    if (!n) break;
    const a = n.querySelector('a[href^="/"]');
    if (a) {
      if (els.filter(e => n.contains(e)).length !== 1) return null;
      const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9._]+)\/?$/);
      return m ? m[1].toLowerCase() : null;
    }
  }
  return null;
})"""

# Pour chaque lien de la liste : pseudo cible (sert a lire la liste de TES abonnes).
JS_HREF_USERS = r"""els => els.map(a => {
  const m = (a.getAttribute('href') || '').match(/^\/([A-Za-z0-9._]+)\/?$/);
  return m ? m[1].toLowerCase() : null;
})"""

# ---------------------------------------------------------------------------
# Configuration (surchargee par config.json)
# ---------------------------------------------------------------------------
DEFAULTS = {
    "username": "",
    # sources : seeds.txt (ou la cle "seeds" de config.json) ; une seule par passage, en rotation ; au plus
    # seeds_per_run si la source du passage est epuisee ou inaccessible
    "seeds_per_run": 6,
    "follows_per_day": 100,
    "unfollow_after_days": 5,
    # prudence : montee en charge, plafonds, plage horaire, repos, reaction aux blocages
    "ramp_start": 20,
    "ramp_step": 10,
    "max_follows_per_hour": 20,
    "max_unfollows_per_hour": 25,
    "max_unfollows_per_day": 100,
    "max_following": 1000,
    "max_wait_minutes": 75,
    "pause_jitter": [0.6, 1.5],
    "long_pause_every": 10,
    "long_pause_seconds": [120, 300],
    "active_hours": [8, 23],
    "daily_minutes": 180,
    "keep_awake": True,
    "rest_day_chance": 0.15,
    "block_cooldown_hours": 48,
    "warning_cooldown_hours": 168,
    # delai adaptatif : mesure du temps de follow-back, puis ajustement du desabonnement
    "adaptive_delay": True,
    "unfollow_days_min": 2,
    "unfollow_days_max": 7,
    "adapt_target_share": 0.9,
    "adapt_min_events": 25,
    "adapt_min_at_risk": 15,
    "adapt_window_days": 60,
    "explore_share": 0.2,
    "followback_scan_rows": 1000,
    "max_scrolls_per_seed": 80,
    "virtual_desktop": 2,
    "browser_channel": "msedge",
    "locale": "fr-FR",
    "timezone": "Europe/Paris",
    "debug_screenshots": True,
}

LEGACY_SEEDS = ["squeezie", "lenasituations", "booba", "k.mbappe"]  # ancienne liste par defaut de config.json


def norm_handles(items):
    return list(dict.fromkeys(h.strip().lstrip("@").lower() for h in items if h and h.strip()))


def read_seeds_file(path):
    return norm_handles(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


def load_config():
    cfg = dict(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    own = norm_handles(cfg.get("seeds") or [])
    seeds_file = ROOT / "seeds.txt"
    if own and own != LEGACY_SEEDS:
        cfg["seeds"], cfg["seeds_source"] = own, "config.json"
    elif seeds_file.exists():
        cfg["seeds"], cfg["seeds_source"] = read_seeds_file(seeds_file), "seeds.txt"
    else:
        cfg["seeds"], cfg["seeds_source"] = own, "config.json"
    cfg["username"] = str(cfg["username"]).strip().lstrip("@").lower()
    return cfg


def setup_log():
    DATA.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("rotate")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(DATA / "log.txt", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout is not None:  # pythonw : pas de console
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger


log = setup_log()


def rnd(lo, hi):
    return random.uniform(lo, hi)


# ---------------------------------------------------------------------------
# Etat (SQLite) : qui a ete suivi, quand, et s'il a ete desuivi
# ---------------------------------------------------------------------------
def today():
    return date.today()


def now():
    return datetime.now()


class State:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS users ("
            " username TEXT PRIMARY KEY, followed_on TEXT NOT NULL, followed_at TEXT NOT NULL,"
            " unfollowed_on TEXT, note TEXT, followed_back_on TEXT, fixed_days INTEGER, unfollowed_at TEXT);"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        for col, ddl in (("followed_back_on", "TEXT"), ("fixed_days", "INTEGER"), ("unfollowed_at", "TEXT")):
            if col not in cols:  # base creee par la premiere version du script
                self.db.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        self.db.commit()

    def known(self, user):
        return self.db.execute("SELECT 1 FROM users WHERE username=?", (user,)).fetchone() is not None

    def add_follow(self, user, fixed_days=None):
        """fixed_days : delai propre a ce compte (groupe temoin) ; None = delai courant."""
        self.db.execute(
            "INSERT OR IGNORE INTO users (username, followed_on, followed_at, fixed_days) VALUES (?,?,?,?)",
            (user, today().isoformat(), now().isoformat(timespec="seconds"), fixed_days))
        self.db.commit()

    def mark_unfollowed(self, user, note=None):
        self.db.execute("UPDATE users SET unfollowed_on=?, unfollowed_at=?, note=? WHERE username=?",
                        (today().isoformat(), now().isoformat(timespec="seconds"), note, user))
        self.db.commit()

    def mark_followed_back(self, user):
        self.db.execute("UPDATE users SET followed_back_on=? WHERE username=? AND followed_back_on IS NULL",
                        (today().isoformat(), user))
        self.db.commit()

    def followed_today(self):
        return self.count("followed_on=?", (today().isoformat(),))

    def unfollowed_today(self):
        return self.count("unfollowed_on=?", (today().isoformat(),))

    def active_follow_days(self):
        """Jours (avant aujourd'hui) ou des abonnements ont ete faits : sert a la montee en charge."""
        return self.db.execute("SELECT COUNT(DISTINCT followed_on) FROM users WHERE followed_on < ?",
                               (today().isoformat(),)).fetchone()[0]

    def actions_since(self, kind, since):
        """Horodatages (ISO) des abonnements ('follow') ou desabonnements ('unfollow') depuis `since`."""
        col = "followed_at" if kind == "follow" else "unfollowed_at"
        rows = self.db.execute(f"SELECT {col} FROM users WHERE {col} >= ?",
                               (since.isoformat(timespec="seconds"),)).fetchall()
        return [r[0] for r in rows]

    def due(self, days, limit):
        """Comptes a desabonner : age >= delai propre au compte, sinon `days` (delai courant)."""
        rows = self.db.execute(
            "SELECT username FROM users WHERE unfollowed_on IS NULL "
            "AND date(followed_on, '+' || COALESCE(fixed_days, ?) || ' days') <= ? "
            "ORDER BY followed_on, username LIMIT ?", (days, today().isoformat(), limit)).fetchall()
        return [r[0] for r in rows]

    def pending(self):
        """Comptes suivis depuis hier ou avant, toujours suivis, sans follow-back constate."""
        rows = self.db.execute(
            "SELECT username FROM users WHERE unfollowed_on IS NULL AND followed_back_on IS NULL "
            "AND followed_on < ?", (today().isoformat(),)).fetchall()
        return {r[0] for r in rows}

    def samples(self, window_days):
        """[(jours observes, a_suivi_en_retour)] pour les comptes suivis dans la fenetre."""
        since = (today() - timedelta(days=window_days)).isoformat()
        out = []
        for f, fb, uf in self.db.execute(
                "SELECT followed_on, followed_back_on, unfollowed_on FROM users WHERE followed_on>=?", (since,)):
            start = date.fromisoformat(f)
            if fb:
                out.append(((date.fromisoformat(fb) - start).days, True))
            else:  # pas (encore) de follow-back : observe jusqu'au desabonnement, ou jusqu'a aujourd'hui
                out.append((((date.fromisoformat(uf) if uf else today()) - start).days, False))
        return out

    def count(self, where="1=1", args=()):
        return self.db.execute(f"SELECT COUNT(*) FROM users WHERE {where}", args).fetchone()[0]

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
        self.db.commit()


def km_cdf(samples, horizon):
    """Kaplan-Meier. samples : [(jours, a_suivi_en_retour)]. Retourne {t: (F(t), nb_a_risque)}, t = 1..horizon.
    Un compte desabonne sans follow-back est "censure" : on sait seulement qu'il n'avait pas suivi en
    retour avant son desabonnement. Les ignorer, ou les compter comme des echecs, fausserait le delai."""
    curve, surv = {}, 1.0
    for t in range(1, horizon + 1):
        at_risk = sum(1 for dur, _ in samples if dur >= t)
        events = sum(1 for dur, ev in samples if ev and dur == t)
        if at_risk:
            surv *= 1 - events / at_risk
        curve[t] = (1 - surv, at_risk)
    return curve


def followback_stats(samples, cfg, base):
    """Mesure le follow-back et propose le delai de desabonnement.

    delay = plus petit nombre de jours qui capte `adapt_target_share` des follow-back obtenus sur la
    fenetre la plus longue (unfollow_days_max), borne entre unfollow_days_min et unfollow_days_max.
    Tant que cette fenetre longue n'est pas observee sur assez de comptes, delay reste `base`."""
    lo = cfg["unfollow_days_min"]
    hi = max(cfg["unfollow_days_max"], base)
    out = {"delay": base, "events": sum(1 for _, ev in samples if ev), "rate": None,
           "mean": None, "median": None, "horizon": None, "adapted": False}
    curve = km_cdf(samples, hi)
    usable = [t for t, (_, n) in curve.items() if n >= cfg["adapt_min_at_risk"]]
    if not usable:
        return out
    horizon = max(usable)
    total = curve[horizon][0]
    if total <= 0:
        return out
    prev = mean = 0.0
    for t in range(1, horizon + 1):
        mean += t * (curve[t][0] - prev)
        prev = curve[t][0]
    out.update(rate=total, mean=mean / total, horizon=horizon,
               median=next(t for t in range(1, horizon + 1) if curve[t][0] >= 0.5 * total))
    if horizon == hi and out["events"] >= cfg["adapt_min_events"]:
        d = next(t for t in range(1, hi + 1) if curve[t][0] >= cfg["adapt_target_share"] * total)
        out["delay"] = max(lo, min(hi, d))
        out["adapted"] = True
    return out


def describe(stats):
    if stats["rate"] is None:
        return f"pas encore assez de donnees ({stats['events']} follow-back mesure(s))"
    return (f"{stats['rate'] * 100:.1f} % des comptes suivent en retour sous {stats['horizon']} jours, "
            f"delai moyen {stats['mean']:.1f} j, median {stats['median']} j ({stats['events']} mesures)")


# ---------------------------------------------------------------------------
# Prudence : montee en charge, plafonds horaires, plage horaire, repos, blocages
# ---------------------------------------------------------------------------
def is_rest_day(cfg, day):
    """Jour de repos : tire une fois par date (stable d'un passage a l'autre), jamais deux jours de suite."""
    def drawn(d):
        return random.Random("repos-" + d.isoformat()).random() < cfg["rest_day_chance"]
    return drawn(day) and not drawn(day - timedelta(days=1))


def in_active_hours(cfg, moment):
    lo, hi = cfg["active_hours"]
    return lo <= moment.hour < hi


def follow_budget(st, cfg):
    """Abonnements autorises aujourd'hui : montee en charge progressive, reduite apres un blocage."""
    ramp = cfg["ramp_start"] + cfg["ramp_step"] * st.active_follow_days()
    factor = float(st.get_meta("quota_factor") or 1.0)
    return max(0, int(min(cfg["follows_per_day"], ramp) * factor))


def register_block(st, cfg, kind):
    """Reaction d'Instagram : quotas reduits, pause qui double a chaque blocage rapproche (14 jours max)."""
    streak = int(st.get_meta("block_streak") or 0) + 1
    factor = max(0.25, float(st.get_meta("quota_factor") or 1.0) * (0.25 if kind == "warning" else 0.5))
    base = cfg["warning_cooldown_hours"] if kind == "warning" else cfg["block_cooldown_hours"]
    hours = min(base * 2 ** (streak - 1), 14 * 24)
    st.set_meta("block_streak", str(streak))
    st.set_meta("quota_factor", f"{factor:.2f}")
    st.set_meta("blocked_until", (now() + timedelta(hours=hours)).isoformat(timespec="seconds"))
    st.set_meta("last_clean_on", today().isoformat())  # pas de remontee le jour meme
    return hours, factor


def register_clean_day(st):
    """Un jour sans reaction d'Instagram : les quotas remontent de 10 points, jusqu'a 100 %."""
    if st.get_meta("last_clean_on") == today().isoformat():
        return
    st.set_meta("last_clean_on", today().isoformat())
    factor = min(1.0, float(st.get_meta("quota_factor") or 1.0) + 0.1)
    st.set_meta("quota_factor", f"{factor:.2f}")
    if factor >= 1.0:
        st.set_meta("block_streak", "0")


def minutes_left(st, cfg):
    """Temps de passage restant aujourd'hui, cumule sur tous les passages (None = illimite)."""
    if not cfg["daily_minutes"]:
        return None
    used = float(st.get_meta(f"run_seconds:{today().isoformat()}") or 0)
    return max(0.0, cfg["daily_minutes"] - used / 60)


def add_run_time(st, started):
    key = f"run_seconds:{today().isoformat()}"
    st.set_meta(key, str(float(st.get_meta(key) or 0) + (now() - started).total_seconds()))


def keep_awake(on):
    """Windows : empeche la mise en veille pendant le passage (l'ecran peut s'eteindre)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes, fn.restype = [ctypes.c_uint], ctypes.c_uint
        fn(0x80000000 | (0x00000001 if on else 0))  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    except Exception:
        pass


def throttle(page, st, cfg, kind):
    """Respecte le plafond horaire (cumule sur tous les passages), la plage horaire et le temps de
    passage du jour. Retourne False s'il faut s'arreter pour aujourd'hui."""
    limit = cfg[f"max_{kind}s_per_hour"]
    deadline = cfg.get("_deadline")
    while True:
        moment = now()
        if not in_active_hours(cfg, moment):
            log.info("Hors de la plage horaire %dh-%dh : arret, reprise au prochain passage", *cfg["active_hours"])
            return False
        if deadline and moment >= deadline:
            log.info("Temps de passage du jour ecoule (%d min) : arret, reprise au prochain passage", cfg["daily_minutes"])
            return False
        recent = st.actions_since(kind, moment - timedelta(hours=1))
        if len(recent) < limit:
            return True
        wait = (datetime.fromisoformat(min(recent)) + timedelta(hours=1) - moment).total_seconds() + rnd(5, 90)
        if wait > cfg["max_wait_minutes"] * 60 or (deadline and moment + timedelta(seconds=wait) >= deadline):
            log.info("Plafond horaire atteint et attente trop longue : arret pour aujourd'hui")
            return False
        log.info("Plafond horaire atteint (%d %s par heure) : attente de %d min", limit, kind, wait // 60)
        page.wait_for_timeout(wait * 1000)


def acquire_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            if pid != os.getpid() and psutil.pid_exists(pid):
                return False
        except ValueError:
            pass
    LOCK.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK.unlink(missing_ok=True))
    return True


# ---------------------------------------------------------------------------
# Navigateur
# ---------------------------------------------------------------------------
def launch(p, cfg, hidden):
    args = [
        "--window-size=1280,900",
        # une fenetre cachee / sur un autre bureau ne doit pas etre ralentie
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    if hidden:
        args.append("--window-position=-32000,-32000")  # hors ecran jusqu'au placement
    kw = dict(user_data_dir=str(PROFILE), headless=False, args=args, no_viewport=True,
              locale=cfg["locale"], timezone_id=cfg["timezone"])
    channel = cfg.get("browser_channel")
    try:
        return p.chromium.launch_persistent_context(channel=channel, **kw)
    except Exception as e:
        if not channel:
            raise
        log.warning("Navigateur '%s' indisponible (%s), essai avec le Chromium de Playwright",
                    channel, str(e).splitlines()[0][:120])
        return p.chromium.launch_persistent_context(**kw)


def place_on_bot_desktop(desktop_no):
    """Windows : envoie la fenetre du navigateur sur le bureau virtuel dedie (cree si besoin)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        from pyvda import AppView, VirtualDesktop, get_virtual_desktops
    except Exception as e:
        log.warning("Bureau virtuel indisponible (%s) : la fenetre reste hors ecran", e)
        return False

    user32 = ctypes.windll.user32
    target = str(PROFILE.resolve()).lower()
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def find_hwnd():
        pids = set()
        for pr in psutil.process_iter(["pid", "cmdline"]):
            try:
                if target in " ".join(pr.info["cmdline"] or []).lower():
                    pids.add(pr.info["pid"])
            except psutil.Error:
                continue
        best = [None, 0]

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pids:
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    area = (r.right - r.left) * (r.bottom - r.top)
                    if area > best[1]:
                        best[0], best[1] = hwnd, area
            return True

        user32.EnumWindows(enum_proc(cb), 0)
        return best[0]

    hwnd = None
    for _ in range(40):  # ~20 s
        hwnd = find_hwnd()
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        log.warning("Fenetre du navigateur introuvable : elle reste hors ecran")
        return False
    try:
        total = len(get_virtual_desktops())
        while total < desktop_no:
            VirtualDesktop.create()
            total += 1
            try:
                VirtualDesktop(total).rename("Insta")
            except Exception:
                pass
        for _ in range(5):
            try:
                AppView(hwnd=hwnd).move(VirtualDesktop(desktop_no))
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("deplacement refuse par Windows")
        # une fois sur l'autre bureau, on la remet a une position visible (invisible pour toi)
        user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(0), 60, 40, 0, 0,
                            0x0001 | 0x0004 | 0x0010)  # NOSIZE | NOZORDER | NOACTIVATE
    except Exception as e:
        log.warning("Placement sur le bureau %d impossible (%s) : fenetre hors ecran", desktop_no, e)
        return False
    log.info("Fenetre placee sur le bureau virtuel %d", desktop_no)
    return True


def dismiss_popups(page):
    for rx in (COOKIES_RE, LATER_RE):
        try:
            btn = page.get_by_role("button", name=rx).first
            if btn.is_visible():
                btn.click(timeout=3000)
                page.wait_for_timeout(800)
        except Exception:
            pass


def block_kind(page):
    """None, 'limit' (rythme trop rapide), 'warning' (automatisation soupconnee) ou
    'attention' (verification d'identite, compte suspendu, deconnexion)."""
    try:
        if ATTENTION_URL_RE.search(page.url):
            return "attention"
        for kind, rx in (("attention", ATTENTION_RE), ("warning", WARN_RE), ("limit", BLOCK_RE)):
            if page.get_by_text(rx).first.is_visible():
                return kind
    except Exception:
        pass
    return None


def blocked(page):
    return block_kind(page) is not None


def shot(page, cfg, name):
    if not cfg.get("debug_screenshots"):
        return
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"))
        for old in sorted(SHOTS.glob("*.png"))[:-30]:
            old.unlink()
    except Exception:
        pass


def logged_in(page):
    page.goto(IG + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(f"{LOGGED_IN_SEL}, {LOGIN_FORM_SEL}", timeout=20000)
    except PWTimeout:
        pass
    dismiss_popups(page)
    page.wait_for_timeout(1500)
    if "/challenge" in page.url or "/accounts/login" in page.url:
        return False
    return page.locator(LOGGED_IN_SEL).count() > 0


def human_pause(page, cfg, n, kind):
    """Pause entre deux actions : etalee sur l'heure (3600 s / plafond horaire), avec variation."""
    secs = 3600 / cfg[f"max_{kind}s_per_hour"] * rnd(*cfg["pause_jitter"])
    if n % cfg["long_pause_every"] == 0:
        secs += rnd(*cfg["long_pause_seconds"])
    deadline = cfg.get("_deadline")
    if deadline:  # inutile de dormir au-dela du temps de passage
        secs = min(secs, max(0.0, (deadline - now()).total_seconds()))
    page.wait_for_timeout(secs * 1000)


# ---------------------------------------------------------------------------
# Abonnements
# ---------------------------------------------------------------------------
def open_followers(page, seed):
    page.goto(f"{IG}/{seed}/", wait_until="domcontentloaded")
    page.wait_for_timeout(rnd(2500, 4000))
    dismiss_popups(page)
    try:
        page.locator(FOLLOWERS_HREF_SEL.format(seed)).or_(
            page.get_by_role("link", name=FOLLOWERS_COUNT_RE)).first.click(timeout=8000)
    except PWTimeout:
        page.goto(f"{IG}/{seed}/followers/", wait_until="domcontentloaded")
    dlg = page.locator(DIALOG_SEL).last
    try:
        dlg.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        return None
    page.wait_for_timeout(rnd(1500, 2500))
    return dlg


def scroll_dialog(page, dlg):
    box = dlg.bounding_box()
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, random.randint(500, 900))
    page.wait_for_timeout(rnd(1800, 3200))


def follow_from_seed(page, seed, quota, st, cfg, counter, fixed_days=lambda: None):
    """Suit jusqu'a `quota` abonnes de `seed`. Retourne (nombre, 'ok' | 'skip' | 'stop' | 'blocked')."""
    dlg = open_followers(page, seed)
    if dlg is None:
        log.info("[%s] liste d'abonnes inaccessible, source ignoree", seed)
        shot(page, cfg, f"followers-{seed}")
        return 0, "skip"
    done = fails = scrolls = stalls = 0
    skip = set()
    while done < quota and scrolls <= cfg["max_scrolls_per_seed"]:
        btns = dlg.get_by_role("button", name=FOLLOW_RE)
        users = btns.evaluate_all(JS_USERS)
        idx = next((i for i, u in enumerate(users) if u and u not in skip and not st.known(u)), None)
        if idx is None:  # rien d'eligible a l'ecran : on descend dans la liste
            before = dlg.locator(ROW_LINK_SEL).count()
            scroll_dialog(page, dlg)
            scrolls += 1
            stalls = stalls + 1 if dlg.locator(ROW_LINK_SEL).count() == before else 0
            if stalls >= 3:  # fin de liste
                break
            continue
        user = users[idx]
        if not throttle(page, st, cfg, "follow"):
            return done, "stop"
        try:
            btns.nth(idx).click(timeout=8000)
        except PWTimeout:
            skip.add(user)
            continue
        page.wait_for_timeout(rnd(1500, 2500))
        if blocked(page):
            shot(page, cfg, "blocked-follow")
            return done, "blocked"
        if not follow_registered(page, dlg, user):
            # le bouton est reste ou revenu sur "Suivre" : Instagram a ignore le clic (limitation silencieuse)
            fails += 1
            skip.add(user)
            log.info("[%s] abonnement a @%s non pris en compte (%d de suite)", seed, user, fails)
            shot(page, cfg, f"follow-{user}")
            if fails >= 3:
                return done, "blocked"
            continue
        fails = 0
        st.add_follow(user, fixed_days())
        done += 1
        counter[0] += 1
        log.info("[%s] abonne a @%s (%d/%d)", seed, user, done, quota)
        human_pause(page, cfg, counter[0], "follow")
    return done, "ok"


def follow_registered(page, dlg, user):
    """True quand le bouton de la ligne est passe a Suivi(e) / Demande ; Instagram met parfois plusieurs
    secondes (bouton en chargement). False seulement s'il est encore sur Suivre apres ~10 s."""
    for _ in range(5):
        if user in dlg.get_by_role("button", name=FOLLOWING_RE).evaluate_all(JS_USERS):
            return True
        page.wait_for_timeout(2000)
    return user not in dlg.get_by_role("button", name=FOLLOW_RE).evaluate_all(JS_USERS)


def seed_rotation(seeds, last):
    """Sources dans l'ordre de seeds.txt, en commencant par celle qui suit la derniere utilisee :
    chaque passage part d'une autre source."""
    start = seeds.index(last) + 1 if last in seeds else 0
    return seeds[start:] + seeds[:start]


# ---------------------------------------------------------------------------
# Follow-back : detection dans la liste de tes abonnes
# ---------------------------------------------------------------------------
def scan_my_followers(page, me, st, cfg):
    """Parcourt la liste de TES abonnes et marque les comptes suivis qui t'ont suivi en retour.
    L'ordre de cette liste n'est pas garanti chronologique : on la lit en entier (plafonnee).
    Retourne ('ok' | 'skip' | 'blocked', lignes lues, follow-back trouves)."""
    pending = st.pending()
    if not pending:
        return "ok", 0, 0
    dlg = open_followers(page, me)
    if dlg is None:
        log.warning("Liste de tes abonnes inaccessible (pseudo '%s' correct ?) : mesure du jour ignoree", me)
        shot(page, cfg, "scan-followers")
        return "skip", 0, 0
    cap = cfg["followback_scan_rows"]
    seen, stalls, scrolls = set(), 0, 0
    while True:
        before = len(seen)
        seen.update(u for u in dlg.locator(ROW_LINK_SEL).evaluate_all(JS_HREF_USERS) if u)
        if blocked(page):
            shot(page, cfg, "blocked-scan")
            return "blocked", len(seen), 0
        if pending <= seen or len(seen) >= cap or scrolls >= 600:
            break
        stalls = stalls + 1 if scrolls and len(seen) == before else 0
        if stalls >= 3:  # fin de liste
            break
        scroll_dialog(page, dlg)
        scrolls += 1
    found = pending & seen
    for user in found:
        st.mark_followed_back(user)
    if len(seen) >= cap and not pending <= seen:
        log.warning("Liste de tes abonnes tronquee a %d lignes : des follow-back ont pu etre manques", len(seen))
    log.info("Liste de tes abonnes : %d lignes lues, %d nouveau(x) follow-back sur %d en attente",
             len(seen), len(found), len(pending))
    return "ok", len(seen), len(found)


# ---------------------------------------------------------------------------
# Desabonnements
# ---------------------------------------------------------------------------
def unfollow_one(page, user, cfg):
    """Retourne 'ok' | 'gone' | 'failed' | 'blocked'."""
    page.goto(f"{IG}/{user}/", wait_until="domcontentloaded")
    page.wait_for_timeout(rnd(2500, 4000))
    dismiss_popups(page)
    if blocked(page):
        return "blocked"
    if page.get_by_text(NOT_FOUND_RE).first.is_visible():
        return "gone"
    header = page.locator("header").first
    if header.get_by_role("button", name=FOLLOW_RE).count() > 0:
        return "ok"  # deja plus abonne
    btn = header.get_by_role("button", name=FOLLOWING_RE).first
    if btn.count() == 0:
        shot(page, cfg, f"unfollow-{user}-nobutton")
        return "failed"
    btn.click(timeout=8000)
    page.wait_for_timeout(rnd(1000, 1800))
    for _ in range(2):  # menu, puis eventuelle confirmation
        item = page.get_by_text(UNFOLLOW_RE).last
        if not item.is_visible():
            break
        try:
            item.click(timeout=5000)
        except PWTimeout:
            break
        page.wait_for_timeout(rnd(1200, 2000))
    if blocked(page):
        return "blocked"
    try:
        header.get_by_role("button", name=FOLLOW_RE).first.wait_for(state="visible", timeout=5000)
        return "ok"
    except PWTimeout:
        shot(page, cfg, f"unfollow-{user}")
        return "failed"


def unfollow_pass(page, st, cfg, counter, days):
    """Retourne (nombre, 'ok' | 'stop' | 'blocked' | 'broken')."""
    due = st.due(days, max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today()))
    log.info("%d compte(s) a desabonner", len(due))
    done = fails = 0
    for user in due:
        if not throttle(page, st, cfg, "unfollow"):
            return done, "stop"
        res = unfollow_one(page, user, cfg)
        if res == "blocked":
            return done, "blocked"
        if res in ("ok", "gone"):
            st.mark_unfollowed(user, None if res == "ok" else "introuvable")
            done += 1
            fails = 0
            counter[0] += 1
            log.info("desabonne de @%s%s", user, "" if res == "ok" else " (compte introuvable)")
            human_pause(page, cfg, counter[0], "unfollow")
        else:
            fails += 1
            log.warning("echec du desabonnement de @%s (%d de suite)", user, fails)
            if fails >= 3:
                log.warning("3 echecs de suite : interface modifiee ? voir data/debug et la skill maj-selecteurs")
                return done, "broken"
    return done, "ok"


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
def save_config_key(key, value):
    path = ROOT / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_login(cfg):
    DATA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = launch(p, cfg, hidden=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{IG}/accounts/login/")
        input("Connecte-toi dans la fenetre (verification en 2 etapes comprise).\n"
              "Quand ton fil d'actualite s'affiche, reviens ici et appuie sur Entree... ")
        ok = logged_in(page)
        log.info("Session %s", "enregistree" if ok else "NON detectee : relance la commande login")
        if ok:
            State(DB_PATH).set_meta("needs_attention", "")
            if not cfg["username"]:
                name = input("Ton pseudo Instagram (sert a mesurer les follow-back) : ").strip().lstrip("@").lower()
                if name:
                    save_config_key("username", name)
                    log.info("Pseudo '%s' enregistre dans config.json", name)
        ctx.close()
    return 0 if ok else 1


def cmd_status(cfg):
    st = State(DB_PATH)
    base = cfg["unfollow_after_days"]
    days = int(st.get_meta("current_days") or base) if cfg["adaptive_delay"] else base
    stats = followback_stats(st.samples(cfg["adapt_window_days"]), cfg, base)
    factor = float(st.get_meta("quota_factor") or 1.0)
    print(f"Abonnements en cours (faits par le script) : {st.count('unfollowed_on IS NULL')}")
    print(f"A desabonner maintenant                    : {len(st.due(days, 10**6))}")
    print(f"Abonnements aujourd'hui                    : {st.followed_today()} / {follow_budget(st, cfg)} autorises"
          f" (plafond {cfg['follows_per_day']}, coefficient {factor:.2f})")
    print(f"Total historique                           : {st.count()}")
    used = float(st.get_meta(f"run_seconds:{today().isoformat()}") or 0) / 60
    print(f"Temps de passage aujourd'hui               : {used:.0f} / {cfg['daily_minutes'] or 'illimite'} min"
          f" (rythme : {cfg['max_follows_per_hour']} abonnements ou {cfg['max_unfollows_per_hour']} desabonnements par heure)")
    print(f"Delai de desabonnement actuel              : {days} jours (base {base})")
    print(f"Follow-back                                : {describe(stats)}")
    print(f"Sources                                    : {len(cfg['seeds'])} comptes ({cfg['seeds_source']})")
    print(f"Pause anti-blocage jusqu'au                : {st.get_meta('blocked_until') or '-'}")
    if is_rest_day(cfg, today()):
        print("Aujourd'hui                                : jour de repos")
    if st.get_meta("needs_attention"):
        print(f"Intervention requise                       : {st.get_meta('needs_attention')}")
    return 0


def cmd_check_seeds(cfg, here):
    """Visite chaque source une fois et ecarte celles qui n'existent pas (elles ne seront plus utilisees)."""
    st = State(DB_PATH)
    if not PROFILE.exists():
        log.error("Aucun profil navigateur : lance d'abord 'insta_rotate.py login'")
        return 2
    ok, ko = [], []
    with sync_playwright() as p:
        ctx = launch(p, cfg, hidden=not here)
        try:
            if not here:
                place_on_bot_desktop(cfg["virtual_desktop"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(15000)
            if not logged_in(page):
                log.error("Session Instagram absente ou expiree : relance 'insta_rotate.py login'")
                return 3
            for seed in cfg["seeds"]:
                page.goto(f"{IG}/{seed}/", wait_until="domcontentloaded")
                page.wait_for_timeout(rnd(4000, 8000))
                dismiss_popups(page)
                if blocked(page):
                    log.warning("Reaction d'Instagram pendant la verification : arret")
                    break
                missing = page.get_by_text(NOT_FOUND_RE).first.is_visible()
                (ko if missing else ok).append(seed)
                st.set_meta(f"seed_fail:{seed}", "3" if missing else "0")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    log.info("Sources verifiees : %d valides, %d introuvables%s", len(ok), len(ko),
             " (" + ", ".join(ko) + ")" if ko else "")
    return 0


def cmd_run(cfg, here, force=False):
    st = State(DB_PATH)
    attention = st.get_meta("needs_attention")
    if attention:
        log.error("Intervention requise (%s) : regle le probleme a la main dans Instagram, "
                  "puis lance 'insta_rotate.py login'", attention)
        return 5
    until = st.get_meta("blocked_until")
    if until and now() < datetime.fromisoformat(until):
        log.info("Pause anti-blocage jusqu'au %s : rien a faire", until)
        return 0
    if not PROFILE.exists():
        log.error("Aucun profil navigateur : lance d'abord 'insta_rotate.py login'")
        return 2
    if not force:
        if not in_active_hours(cfg, now()):
            log.info("Hors de la plage horaire %dh-%dh : rien a faire (--force pour passer outre)", *cfg["active_hours"])
            return 0
        if is_rest_day(cfg, today()):
            log.info("Jour de repos : rien a faire")
            return 0
    base = cfg["unfollow_after_days"]
    analysis = bool(cfg["adaptive_delay"] and cfg["username"])
    if cfg["adaptive_delay"] and not cfg["username"]:
        log.warning("Cle 'username' absente de config.json : pas de mesure des follow-back, delai fixe de %d jours", base)
    days = int(st.get_meta("current_days") or base) if analysis else base
    budget = follow_budget(st, cfg)
    quota = max(0, budget - st.followed_today())
    if st.count("unfollowed_on IS NULL") >= cfg["max_following"]:
        log.warning("%d comptes suivis par le script : pas de nouvel abonnement tant que les desabonnements "
                    "n'ont pas rattrape leur retard", cfg["max_following"])
        quota = 0
    room = max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today())
    if not quota and not (room and st.due(days, 1)):
        log.info("Rien a faire aujourd'hui (abonnements autorises : %d)", budget)
        return 0
    alive = [seed for seed in cfg["seeds"] if int(st.get_meta(f"seed_fail:{seed}") or 0) < 3]
    if quota and not alive:
        log.error("Aucune source valide : verifie seeds.txt ou lance 'insta_rotate.py check-seeds'")
        return 1
    left = minutes_left(st, cfg)
    if left is not None and left < 5:
        log.info("Temps de passage du jour deja utilise (%d min) : rien a faire", cfg["daily_minutes"])
        return 0
    cfg["_deadline"] = now() + timedelta(minutes=left) if left is not None else None
    started = now()
    longest = max(cfg["unfollow_days_max"], base)

    def fixed_days():  # groupe temoin : une part des comptes reste suivie le delai le plus long
        return longest if analysis and random.random() < cfg["explore_share"] else None

    counter = [0]
    unfollows = follows = 0
    status, kind = "ok", None
    if cfg["keep_awake"]:
        keep_awake(True)
    try:
        with sync_playwright() as p:
            ctx = launch(p, cfg, hidden=not here)
            try:
                if not here:
                    place_on_bot_desktop(cfg["virtual_desktop"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(15000)
                if not logged_in(page):
                    log.error("Session Instagram absente ou expiree : relance 'insta_rotate.py login'")
                    return 3
                if analysis:
                    if st.get_meta("last_scan_on") != today().isoformat():  # une lecture par jour
                        scan = scan_my_followers(page, cfg["username"], st, cfg)[0]
                        if scan == "ok":
                            st.set_meta("last_scan_on", today().isoformat())
                        elif scan == "blocked":
                            status = "blocked"
                    stats = followback_stats(st.samples(cfg["adapt_window_days"]), cfg, base)
                    log.info("Follow-back : %s", describe(stats))
                    if stats["delay"] != days:
                        log.info("Delai de desabonnement ajuste : %d -> %d jours", days, stats["delay"])
                    days = stats["delay"]
                    st.set_meta("current_days", str(days))
                if status == "ok" and room and st.due(days, 1):
                    unfollows, status = unfollow_pass(page, st, cfg, counter, days)
                if status == "ok" and quota:
                    remaining = quota
                    # une seule source par passage, utilisee jusqu'au quota ; la suivante n'est ouverte que
                    # si celle-ci est epuisee ou inaccessible (seeds_per_run sources au plus)
                    pool = [s for s in seed_rotation(cfg["seeds"], st.get_meta("last_seed")) if s in alive]
                    for seed in pool[:cfg["seeds_per_run"]]:
                        n, s = follow_from_seed(page, seed, remaining, st, cfg, counter, fixed_days)
                        if s == "skip":  # source inaccessible : ecartee apres 3 echecs
                            st.set_meta(f"seed_fail:{seed}", str(int(st.get_meta(f"seed_fail:{seed}") or 0) + 1))
                            continue
                        st.set_meta(f"seed_fail:{seed}", "0")
                        st.set_meta("last_seed", seed)
                        follows += n
                        remaining -= n
                        if s in ("blocked", "stop"):
                            status = s
                            break
                        if remaining <= 0:
                            break
                        log.info("[%s] plus de compte a suivre : source suivante", seed)
                    if status == "ok" and remaining > 0:
                        log.info("Quota non atteint : il manque %d abonnement(s)", remaining)
                if status == "blocked":
                    kind = block_kind(page) or "limit"
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception:
        log.exception("Erreur inattendue")
        return 1
    finally:
        add_run_time(st, started)
        keep_awake(False)

    if status == "blocked" and kind == "attention":
        st.set_meta("needs_attention", f"{now():%Y-%m-%d %H:%M} verification, suspension ou deconnexion")
        log.error("Instagram demande une intervention (verification d'identite, suspension ou deconnexion) : "
                  "arret total. Regle-le a la main, puis lance 'insta_rotate.py login'")
    elif status == "blocked":
        hours, factor = register_block(st, cfg, kind)
        log.warning("Reaction d'Instagram (%s) : pause de %d h, quotas ramenes a %d %%", kind, hours, factor * 100)
    else:
        register_clean_day(st)
    log.info("Termine : %d abonnement(s), %d desabonnement(s)", follows, unfollows)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["login", "run", "status", "check-seeds"])
    ap.add_argument("--here", action="store_true",
                    help="run : garder la fenetre visible sur le bureau courant (debug)")
    ap.add_argument("--force", action="store_true",
                    help="run : ignorer la plage horaire et le jour de repos (debug)")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "status":
        return cmd_status(cfg)
    if not acquire_lock():
        log.error("Une autre instance est deja en cours")
        return 4
    if args.cmd == "login":
        return cmd_login(cfg)
    if args.cmd == "check-seeds":
        return cmd_check_seeds(cfg, args.here)
    return cmd_run(cfg, args.here, args.force)


if __name__ == "__main__":
    sys.exit(main())
