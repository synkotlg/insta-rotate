#!/usr/bin/env python3
"""
insta_rotate.py - Rotation d'abonnements Instagram.

Chaque jour : desabonnement des comptes suivis par ce script depuis N jours (sauf ceux qui
t'ont suivi en retour), puis abonnement a de nouveaux comptes pris dans les abonnes de comptes sources.
Le navigateur utilise un profil persistant : connexion manuelle une seule fois.

  python insta_rotate.py login     connexion manuelle (une seule fois)
  python insta_rotate.py run       passe quotidienne (--here : rester sur le bureau courant)
  python insta_rotate.py status    etat de la rotation
  python insta_rotate.py check-seeds   verifie que les comptes sources existent

Notifications Telegram (optionnel) : TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env (voir telegram_notify.py).
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
import threading
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import math

_mouse_pos = {}

import psutil
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from telegram_notify import TelegramNotifier, load_env_file

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA = ROOT / "data"
PROFILE = DATA / "profile"
DB_PATH = DATA / "state.db"
LOCK = DATA / "run.lock"
SHOTS = DATA / "debug"
MAX_UNFOLLOW_FAILS = 3  # echecs de desabonnement d'un meme compte (tous passages confondus) avant abandon

# ---------------------------------------------------------------------------
# UI : tout ce qui depend du texte ou du DOM d'Instagram (FR + EN) est ici.
# ---------------------------------------------------------------------------
IG = "https://www.instagram.com"
FOLLOW_RE = re.compile(r"^(Suivre|Follow)$", re.I)
# Profil d'un compte qu'on ne suit pas (ou plus) : "Suivre", ou "Suivre en retour" s'il te suit
NOT_FOLLOWING_RE = re.compile(r"^(Suivre|Follow)( en retour| back)?$", re.I)
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
    # apres un profil ouvert sans clic (deja plus suivi, introuvable, echec) : pas de rafale de pages
    "profile_gap_seconds": [20, 60],
    "active_hours": [8, 23],
    "daily_minutes": 180,
    "keep_awake": True,
    # demarrage (navigateur, bureau virtuel, connexion) plus long : alerte et arret du passage
    "startup_minutes": 8,
    "rest_day_chance": 0.15,
    "rest_days_min": 1,  # jours de repos par semaine au minimum (ajoutes si le tirage n'en donne pas assez)
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
    # horloge du script (plage horaire, jours, plafonds, log) et du navigateur, quel que soit le fuseau du PC
    "timezone": "Europe/Paris",
    "debug_screenshots": True,
    # prefixe des messages Telegram (le meme bot sert a plusieurs projets)
    "notify_name": "insta-rotate",
}

LEGACY_SEEDS = ["squeezie", "lenasituations", "booba", "k.mbappe"]  # ancienne liste par defaut de config.json
TZ = ZoneInfo(DEFAULTS["timezone"])  # remplace par la cle timezone de config.json (load_config)


def norm_handles(items):
    return list(dict.fromkeys(h.strip().lstrip("@").lower() for h in items if h and h.strip()))


def read_seeds_file(path):
    return norm_handles(line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())


def load_config():
    global TZ
    cfg = dict(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    TZ = ZoneInfo(cfg["timezone"])
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
    fmt.converter = lambda secs: datetime.fromtimestamp(secs, TZ).timetuple()  # heure du script, pas du PC
    fh =RotatingFileHandler(DATA / "log.txt", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stdout is not None:  # pythonw : pas de console
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    tg = logging.getLogger("telegram_notify")  # echecs d'envoi Telegram dans le meme log
    for h in logger.handlers:
        tg.addHandler(h)
    return logger


log = setup_log()


def rnd(lo, hi):
    return random.uniform(lo, hi)


# ---------------------------------------------------------------------------
# Etat (SQLite) : qui a ete suivi, quand, et s'il a ete desuivi
# ---------------------------------------------------------------------------
def today():
    return now().date()


def now():
    """Heure de la cle timezone (Paris), pas celle du PC. Sans fuseau attache, comme les horodatages de state.db."""
    return datetime.now(TZ).replace(tzinfo=None)


class State:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS users ("
            " username TEXT PRIMARY KEY, followed_on TEXT NOT NULL, followed_at TEXT NOT NULL,"
            " unfollowed_on TEXT, note TEXT, followed_back_on TEXT, fixed_days INTEGER, unfollowed_at TEXT,"
            " unfollow_fails INTEGER);"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        for col, ddl in (("followed_back_on", "TEXT"), ("fixed_days", "INTEGER"), ("unfollowed_at", "TEXT"),
                         ("unfollow_fails", "INTEGER")):
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

    def mark_unfollowed(self, user, note=None, acted=True):
        """acted=False : rien a faire sur Instagram (deja plus suivi, compte introuvable). Sans unfollowed_at,
        le compte ne compte ni dans les plafonds horaire et journalier, ni dans le resume du passage."""
        self.db.execute("UPDATE users SET unfollowed_on=?, unfollowed_at=?, note=? WHERE username=?",
                        (today().isoformat(), now().isoformat(timespec="seconds") if acted else None, note, user))
        self.db.commit()

    def add_unfollow_fail(self, user):
        """Echec de desabonnement : le compte passe en fin de file (voir due). Retourne le nombre d'echecs."""
        self.db.execute("UPDATE users SET unfollow_fails = COALESCE(unfollow_fails, 0) + 1 WHERE username=?",
                        (user,))
        self.db.commit()
        return self.db.execute("SELECT unfollow_fails FROM users WHERE username=?", (user,)).fetchone()[0]

    def given_up(self):
        """Comptes toujours suivis dont le desabonnement a echoue MAX_UNFOLLOW_FAILS fois : a faire a la main."""
        rows = self.db.execute("SELECT username FROM users WHERE unfollowed_on IS NULL AND followed_back_on IS NULL "
                               "AND unfollow_fails >= ? ORDER BY followed_on, username", (MAX_UNFOLLOW_FAILS,)).fetchall()
        return [r[0] for r in rows]

    def kept(self):
        """Comptes suivis qui t'ont suivi en retour : gardes, jamais desabonnes (voir due)."""
        return self.count("unfollowed_on IS NULL AND followed_back_on IS NOT NULL")

    def mark_followed_back(self, user):
        self.db.execute("UPDATE users SET followed_back_on=? WHERE username=? AND followed_back_on IS NULL",
                        (today().isoformat(), user))
        self.db.commit()

    def followed_today(self):
        return self.count("followed_on=?", (today().isoformat(),))

    def unfollowed_today(self):
        return self.count("unfollowed_on=? AND unfollowed_at IS NOT NULL", (today().isoformat(),))

    def acted_on(self, day):
        """Au moins un abonnement ou desabonnement ce jour-la."""
        d = day.isoformat()
        return self.count("followed_on=? OR unfollowed_on=?", (d, d)) > 0

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

    def users_since(self, kind, since):
        """Pseudos suivis ('follow') ou desabonnes ('unfollow') depuis `since`, dans l'ordre."""
        col = "followed_at" if kind == "follow" else "unfollowed_at"
        rows = self.db.execute(f"SELECT username FROM users WHERE {col} >= ? ORDER BY {col}",
                               (since.isoformat(timespec="seconds"),)).fetchall()
        return [r[0] for r in rows]

    def due(self, days, limit):
        """Comptes a desabonner : age >= delai propre au compte, sinon `days` (delai courant). Ceux qui t'ont
        suivi en retour sont gardes (kept). Les comptes deja en echec passent apres les autres (sinon trois
        echecs en tete de file bloquent chaque passage), ceux qui ont echoue MAX_UNFOLLOW_FAILS fois sont
        abandonnes (given_up)."""
        rows = self.db.execute(
            "SELECT username FROM users WHERE unfollowed_on IS NULL AND followed_back_on IS NULL "
            "AND COALESCE(unfollow_fails, 0) < ? "
            "AND date(followed_on, '+' || COALESCE(fixed_days, ?) || ' days') <= ? "
            "ORDER BY COALESCE(unfollow_fails, 0), followed_on, username LIMIT ?",
            (MAX_UNFOLLOW_FAILS, days, today().isoformat(), limit)).fetchall()
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
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def day_label(day):
    return f"{JOURS[day.weekday()]} {day.day}/{day.month:02d}"


def week_start(day):
    return day - timedelta(days=day.weekday())


def rest_days(cfg, st, day):
    """Jours de repos de la semaine (lundi a dimanche) de `day`. Tires une fois, a la premiere demande de la
    semaine, puis gardes dans meta rest_week:<lundi> : annonces a l'avance (announce_week), ils ne changent plus.
    Chaque jour a la chance rest_day_chance, au moins rest_days_min par semaine, jamais deux jours de suite (ni
    un lundi apres un dimanche de repos). Les jours deja passes au tirage, et aujourd'hui s'il a deja eu des
    actions, restent actifs."""
    monday = week_start(day)
    key = f"rest_week:{monday.isoformat()}"
    stored = st.get_meta(key)
    if stored is not None:
        return [date.fromisoformat(d) for d in stored.split(",") if d]
    days = [monday + timedelta(days=i) for i in range(7)]
    open_days = [d for d in days if d > today() or (d == today() and not st.acted_on(d))]
    sunday = monday - timedelta(days=1)
    rest = [sunday] if sunday.isoformat() in (st.get_meta(f"rest_week:{week_start(sunday).isoformat()}") or "") else []
    rng = random.Random("repos-" + monday.isoformat())  # meme tirage si la base est recreee
    for d in days:
        if rng.random() < cfg["rest_day_chance"] and d in open_days and d - timedelta(days=1) not in rest:
            rest.append(d)
    # au moins rest_days_min jours, sauf si un jour deja passe sans action (PC eteint) en a tenu lieu
    idle = any(d < today() and not st.acted_on(d) for d in days)
    while not idle and sum(d >= monday for d in rest) < cfg["rest_days_min"]:
        free = [d for d in open_days if not {d - timedelta(days=1), d, d + timedelta(days=1)} & set(rest)]
        if not free:
            break
        rest = sorted(rest + [rng.choice(free)])
    rest = [d for d in rest if d >= monday]
    st.set_meta(key, ",".join(d.isoformat() for d in rest))
    log.info("Jours de repos tires pour la semaine du %s : %s", day_label(monday),
             ", ".join(map(day_label, rest)) or "aucun")
    return rest


def is_rest_day(cfg, st, day):
    """Jour de repos du planning de la semaine (rest_days) : aucune action ce jour-la."""
    return day in rest_days(cfg, st, day)


def announce_week(cfg, st, notify):
    """Planning de la semaine sur Telegram, une fois par semaine (meta rest_week_sent) : du jour de l'envoi a
    dimanche, chaque jour en repos ou actif. Envoi rate ou Telegram absent : nouvel essai au passage suivant."""
    monday = week_start(today())
    if st.get_meta("rest_week_sent") == monday.isoformat():
        return
    rest = rest_days(cfg, st, today())
    days = [today() + timedelta(days=i) for i in range(7 - today().weekday())]
    n = sum(d in rest for d in days)
    lines = [f"Planning de la semaine : {n} jour{'s' if n > 1 else ''} de repos (aucune action ces jours-là)"]
    lines += [f"{day_label(d)} : {'repos' if d in rest else 'actif'}" for d in days]
    if notify("\n".join(lines)):
        st.set_meta("rest_week_sent", monday.isoformat())


def in_active_hours(cfg, moment):
    lo, hi = cfg["active_hours"]
    return lo <= moment.hour < hi


def follow_budget(st, cfg):
    """Abonnements autorises aujourd'hui : montee en charge progressive, reduite apres un blocage."""
    ramp = cfg["ramp_start"] + cfg["ramp_step"] * st.active_follow_days()
    factor = float(st.get_meta("quota_factor") or 1.0)
    return max(0, int(min(cfg["follows_per_day"], ramp) * factor))


def register_block(st, cfg, kind):
    """Reaction d'Instagram : quotas reduits, pause qui double a chaque blocage rapproche (14 jours max).
    'attention' compte comme 'warning'."""
    severe = kind in ("warning", "attention")
    streak = int(st.get_meta("block_streak") or 0) + 1
    factor = max(0.25, float(st.get_meta("quota_factor") or 1.0) * (0.25 if severe else 0.5))
    base = cfg["warning_cooldown_hours"] if severe else cfg["block_cooldown_hours"]
    hours = min(base * 2 ** (streak - 1), 14 * 24)
    st.set_meta("block_streak", str(streak))
    st.set_meta("quota_factor", f"{factor:.2f}")
    st.set_meta("blocked_until", (now() + timedelta(hours=hours)).isoformat(timespec="seconds"))
    st.set_meta("last_clean_on", today().isoformat())  # pas de remontee le jour meme
    return hours, factor


def register_attention(st, cfg):
    """Verification d'identite, suspension ou deconnexion : arret total jusqu'a 'login', puis pause et quotas
    reduits comme pour un avertissement. La pause court des maintenant : elle continue apres le 'login'."""
    st.set_meta("needs_attention", f"{now():%Y-%m-%d %H:%M} verification, suspension ou deconnexion")
    hours, factor = register_block(st, cfg, "attention")
    log.error("Instagram demande une intervention (verification d'identite, suspension ou deconnexion) : "
              "arret total. Regle-le a la main, puis lance 'insta_rotate.py login' ; ensuite pause de %d h "
              "(comptee d'ici), quotas ramenes a %d %%", hours, factor * 100)
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
    """Une seule instance. Un verrou laisse par un passage tue (PC eteint, plantage) est ignore : Windows
    reutilise les numeros de processus, on verifie donc que celui du verrou est bien insta_rotate.py."""
    DATA.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
            if pid != os.getpid() and "insta_rotate" in " ".join(psutil.Process(pid).cmdline()).lower():
                return False
        except (ValueError, psutil.Error):
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
        # repli seulement si le navigateur manque : un demarrage trop lent (PC sature) ne va pas mieux avec un
        # autre navigateur sur le meme profil
        if not channel or "Timeout" in str(e):
            raise
        log.warning("Navigateur '%s' indisponible (%s), essai avec le Chromium de Playwright",
                    channel, str(e).splitlines()[0][:120])
        return p.chromium.launch_persistent_context(**kw)


def profile_pids():
    """Processus du navigateur lances sur data\\profile."""
    target = str(PROFILE.resolve()).lower()
    pids = set()
    for pr in psutil.process_iter(["pid", "cmdline"]):
        try:
            if target in " ".join(pr.info["cmdline"] or []).lower():
                pids.add(pr.info["pid"])
        except psutil.Error:
            continue
    return pids


def place_on_bot_desktop(desktop_no):
    """Windows : envoie la fenetre du navigateur sur le bureau virtuel dedie (cree si besoin).
    Renvoie les identifiants des bureaux crees par cet appel, a passer a remove_desktops en fin de passage."""
    created = []
    if os.name != "nt":
        return created
    try:
        import ctypes
        from ctypes import wintypes
        from pyvda import AppView, VirtualDesktop, get_virtual_desktops
    except Exception as e:
        log.warning("Bureau virtuel indisponible (%s) : la fenetre reste hors ecran", e)
        return created

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def find_hwnd():
        pids = profile_pids()
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
        return created
    try:
        total = len(get_virtual_desktops())
        while total < desktop_no:
            desktop = VirtualDesktop.create()
            created.append(desktop.id)
            total += 1
            try:
                desktop.rename("Insta")
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
        return created
    log.info("Fenetre placee sur le bureau virtuel %d", desktop_no)
    return created


def remove_desktops(ids):
    """Supprime, une fois le navigateur ferme, les bureaux crees par place_on_bot_desktop pendant ce passage.
    Un bureau ou il reste une fenetre (deplacee la par toi entre-temps) est garde."""
    if not ids:
        return
    try:
        from pyvda import VirtualDesktop
    except Exception:
        return
    for desktop_id in ids:
        try:
            desktop = VirtualDesktop(desktop_id=desktop_id)
        except Exception:
            continue  # deja ferme entre-temps
        try:
            for _ in range(10):  # ~5 s pour que la fenetre du navigateur disparaisse
                if not desktop.apps_by_z_order(include_pinned=False):
                    break
                time.sleep(0.5)
            else:
                log.info("Bureau virtuel garde : une fenetre y est encore ouverte")
                continue
            desktop.remove()
            log.info("Bureau virtuel supprime")
        except Exception as e:
            log.warning("Suppression du bureau virtuel impossible (%s)", e)


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
        page.screenshot(path=str(SHOTS / f"{now():%Y%m%d-%H%M%S}-{name}.png"))
        for old in sorted(SHOTS.glob("*.png"))[:-30]:
            old.unlink()
    except Exception:
        pass


def logged_in(page):
    for attempt in range(2):  # Instagram lent juste apres le demarrage : un second essai
        try:
            page.goto(IG + "/", wait_until="domcontentloaded")
            break
        except PWTimeout:
            if attempt:
                raise
            page.wait_for_timeout(5000)
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
    capped_wait(page, cfg, secs)


def profile_gap(page, cfg):
    """Pause apres un profil ouvert sans clic (deja plus suivi, introuvable, echec, page non chargee) ou une
    source inaccessible : sans elle, plusieurs profils s'ouvrent en quelques secondes."""
    capped_wait(page, cfg, rnd(*cfg["profile_gap_seconds"]))


def capped_wait(page, cfg, secs):
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

def _ease_in_out(t):
    return 0.5 - math.cos(math.pi * t) / 2

def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0],u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1],)

def _curve(page, start, end):
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)
    if dist < 2:
        return
    nx, ny = -(ey - sy) / dist, (ex - sx) / dist
    bend = random.uniform(0.08, 0.3) * dist * random.choice((-1, 1))
    a, b = random.uniform(0.15, 0.4), random.uniform(0.6, 0.85)
    c1 = (sx + (ex - sx) * a + nx * bend * random.uniform(0.6, 1.2),
          sy + (ey - sy) * a + ny * bend * random.uniform(0.6, 1.2))
    c2 = (sx + (ex - sx) * b + nx * bend * random.uniform(0.2, 0.9),
          sy + (ey - sy) * b + ny * bend * random.uniform(0.2, 0.9))
    steps = max(10, min(60, int(dist / random.uniform(7, 14))))
    for i in range(1, steps + 1):
        x, y = _bezier((sx, sy), c1, c2, (ex, ey), _ease_in_out(i / steps))
        shake = 0.9 if i < steps else 0
        page.mouse.move(max(0, x + random.gauss(0, shake)), max(0, y + random.gauss(0, shake)))
        page.wait_for_timeout(random.uniform(6, 18))

def human_move(page, x, y):
    start = _mouse_pos.get(id(page), (0, 0))
    dist = math.hypot(x - start[0], y - start[1])
    if dist > 150 and random.random() < 0.15:
        over = random.uniform(6, 22)
        ox = x + (x - start[0]) / dist * over + random.uniform(-5, 5)
        oy = y + (y - start[1]) / dist * over + random.uniform(-5, 5)
        _curve(page, start, (ox, oy))
        page.wait_for_timeout(random.uniform(40, 140))
        _curve(page, (ox, oy), (x, y))
    else:
        _curve(page, start, (x, y))
    _mouse_pos[id(page)] = (x, y)

def _wheel_gesture(page, distance):
    sign = 1 if distance > 0 else -1
    distance = abs(distance)
    ticks = max(3, int(distance / random.uniform(45, 90)))
    p, q = random.uniform(0.3, 0.7), random.uniform(1.4, 2.6)
    weights = [((i + 0.5) / ticks) ** p * (1 - (i + 0.5) / ticks) ** q for i in range(ticks)]
    total = sum(weights)
    for i, w in enumerate(weights):
        delta = round(distance * w / total * random.uniform(0.85, 1.15))
        if delta:
            page.mouse.wheel(0, sign * delta)
        page.wait_for_timeout(random.uniform(10, 22) + 30 * i / ticks)

def _inside(pos, box, margin=10):
    return (pos is not None
            and box["x"] + margin < pos[0] < box["x"] + box["width"] - margin
            and box["y"] + margin < pos[1] < box["y"] + box["height"] - margin)

def scroll_dialog(page, dlg):
    box = dlg.bounding_box()
    if box:
        pos = _mouse_pos.get(id(page))
        if not _inside(pos, box):
            human_move(page,
                       box["x"] + box["width"] * random.uniform(0.3, 0.7),
                       box["y"] + box["height"] * random.uniform(0.35, 0.75))
        elif random.random() < 0.3:
            human_move(page,
                       min(max(pos[0] + random.uniform(-40, 40), box["x"] + 20), box["x"] + box["width"] - 20),
                       min(max(pos[1] + random.uniform(-30, 30), box["y"] + 20), box["y"] + box["height"] - 20))
    back = random.randint(60, 180) if random.random() < 0.12 else 0
    target = (random.randint(450, 950) if random.random() < 0.8 else random.randint(950, 1400)) + back
    n = random.choices((1, 2, 3), weights=(45, 40, 15))[0]
    shares = [random.uniform(0.6, 1.4) for _ in range(n)]
    for k, share in enumerate(shares):
        _wheel_gesture(page, target * share / sum(shares))
        if k < n - 1:
            page.wait_for_timeout(random.uniform(120, 700))
    if back:
        page.wait_for_timeout(random.uniform(250, 650))
        _wheel_gesture(page, -back)
    r = random.random()
    if r < 0.08:
        pause = random.uniform(4500, 9000)
    elif r < 0.25:
        pause = random.uniform(700, 1400)
    else:
        pause = random.lognormvariate(math.log(2300), 0.25)
    if box and random.random() < 0.3:
        split = random.uniform(0.3, 0.7)
        page.wait_for_timeout(pause * split)
        x, y = _mouse_pos.get(id(page), (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2))
        human_move(page, x + random.uniform(-25, 25), y + random.uniform(-20, 20))
        page.wait_for_timeout(pause * (1 - split))
    else:
        page.wait_for_timeout(pause)


def follow_from_seed(page, seed, quota, st, cfg, counter, fixed_days=lambda: None):
    """Suit jusqu'a `quota` abonnes de `seed`. Retourne (nombre, 'ok' | 'skip' | 'stop' | 'blocked')."""
    try:
        dlg = open_followers(page, seed)
    except PWTimeout:  # profil de la source trop lent a charger
        dlg = None
    if dlg is None:
        log.info("[%s] liste d'abonnes inaccessible, source ignoree", seed)
        shot(page, cfg, f"followers-{seed}")
        return 0, "skip"
    done = fails = scrolls = stalls = 0
    skip = set()
    try:
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
    except PWTimeout as e:  # liste disparue ou page figee : on passe a la source suivante
        log.warning("[%s] liste d'abonnes interrompue (%s)", seed, str(e).splitlines()[0][:120])
        shot(page, cfg, f"followers-{seed}-error")
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
    cap = cfg["followback_scan_rows"]
    seen, stalls, scrolls = set(), 0, 0
    try:
        dlg = open_followers(page, me)
        if dlg is None:
            log.warning("Liste de tes abonnes inaccessible (pseudo '%s' correct ?) : mesure du jour ignoree", me)
            shot(page, cfg, "scan-followers")
            return "skip", 0, 0
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
    except PWError as e:  # la mesure ne doit pas empecher les desabonnements ; navigateur ferme : on arrete
        if page.is_closed():
            raise
        log.warning("Lecture de tes abonnes interrompue apres %d lignes (%s) : mesure du jour ignoree",
                    len(seen), str(e).splitlines()[0][:120])
        shot(page, cfg, "scan-error")
        return "skip", len(seen), 0
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
    """Retourne 'ok' | 'already' (deja plus suivi) | 'gone' | 'noload' (page vide) | 'failed' | 'blocked'.
    Leve PWTimeout si la page ne charge pas (traite comme 'noload' par unfollow_pass)."""
    page.goto(f"{IG}/{user}/", wait_until="domcontentloaded")
    page.wait_for_timeout(rnd(2500, 4000))
    dismiss_popups(page)
    if blocked(page):
        return "blocked"
    if page.get_by_text(NOT_FOUND_RE).first.is_visible():
        return "gone"
    header = page.locator("header").first
    if header.count() == 0:  # page blanche ou incomplete : connexion coupee ou Instagram lent, pas l'interface
        shot(page, cfg, f"unfollow-{user}-noload")
        return "noload"
    if header.get_by_role("button", name=NOT_FOLLOWING_RE).count() > 0:
        return "already"
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
        header.get_by_role("button", name=NOT_FOLLOWING_RE).first.wait_for(state="visible", timeout=5000)
        return "ok"
    except PWTimeout:
        shot(page, cfg, f"unfollow-{user}")
        return "failed"


def unfollow_pass(page, st, cfg, counter, days):
    """Retourne (nombre, 'ok' | 'stop' | 'slow' | 'blocked' | 'broken').
    Un compte en echec passe en fin de file (abandonne apres MAX_UNFOLLOW_FAILS echecs). Trois echecs
    d'interface de suite arretent le passage ('broken'), trois pages non chargees de suite aussi ('slow')."""
    due = st.due(days, max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today()))
    log.info("%d compte(s) a desabonner", len(due))
    done = fails = noload = 0
    notes = {"ok": None, "already": "deja plus suivi", "gone": "introuvable"}
    for user in due:
        if not throttle(page, st, cfg, "unfollow"):
            return done, "stop"
        try:
            res = unfollow_one(page, user, cfg)
        except PWTimeout:  # page trop lente : echec pour ce compte, pas pour tout le passage
            shot(page, cfg, f"unfollow-{user}-timeout")
            res = "noload"
        if res == "blocked":
            return done, "blocked"
        if res in notes:
            st.mark_unfollowed(user, notes[res], acted=res == "ok")
            fails = noload = 0
            log.info("desabonne de @%s%s", user, f" ({notes[res]})" if notes[res] else "")
            if res == "ok":
                done += 1
                counter[0] += 1
                human_pause(page, cfg, counter[0], "unfollow")
            else:  # aucun clic : pas de pause longue, mais pas de rafale de profils non plus
                profile_gap(page, cfg)
            continue
        if st.add_unfollow_fail(user) >= MAX_UNFOLLOW_FAILS:
            log.warning("desabonnement de @%s abandonne apres %d echecs : a faire a la main", user, MAX_UNFOLLOW_FAILS)
        if res == "noload":
            noload += 1
            log.warning("profil de @%s non charge (%d de suite)", user, noload)
            if noload >= 3:
                log.warning("3 profils non charges de suite : connexion ou Instagram lent, reprise au prochain passage")
                return done, "slow"
            profile_gap(page, cfg)
            continue
        fails += 1
        log.warning("echec du desabonnement de @%s (%d de suite)", user, fails)
        if fails >= 3:
            log.warning("3 echecs de suite : interface modifiee ? voir data/debug et la skill maj-selecteurs")
            return done, "broken"
        profile_gap(page, cfg)
    return done, "ok"


# ---------------------------------------------------------------------------
# Notifications Telegram : "Lancement" puis resume de fin pour chaque passage qui ouvre le navigateur
# ---------------------------------------------------------------------------
def make_notify(cfg):
    """Fonction d'envoi (texte -> bool), qui ne fait rien si Telegram n'est pas configure (.env)."""
    notifier = TelegramNotifier.from_env(prefix=f"[{cfg['notify_name']}] ")
    return notifier.send if notifier else (lambda text: False)


def send_alert(st, notify, code, text):
    """Probleme a regler a la main : un message par jour au plus pour un meme probleme (la tache relance le
    passage toutes les heures). meta last_alert = "<date> <code>", vide quand un passage se termine sans alerte."""
    key = f"{today().isoformat()} {code}"
    if st.get_meta("last_alert") != key and notify(text):
        st.set_meta("last_alert", key)


def format_duration(seconds):
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


def run_outcome(status, kind, pause, error):
    """(statut lisible, code d'alerte ou None) d'un passage termine."""
    if error:
        return f"échec, {error}", "error"
    if status == "session":
        return "session Instagram absente ou expirée, relance 'insta_rotate.py login'", "session"
    if status == "broken":
        return ("3 désabonnements ratés de suite, interface d'Instagram modifiée ? "
                "(data/debug, skill maj-selecteurs)"), "broken"
    if status == "blocked" and kind == "attention":
        hours, factor = pause
        return ("ARRÊT TOTAL : Instagram demande une intervention (vérification d'identité, suspension ou "
                "déconnexion). Règle-le à la main, puis lance 'insta_rotate.py login'. Ensuite pause de "
                f"{hours} h comptée d'ici, quotas ramenés à {factor * 100:.0f} %"), "attention"
    if status == "blocked":
        hours, factor = pause
        return f"réaction d'Instagram ({kind}), pause de {hours} h, quotas ramenés à {factor * 100:.0f} %", None
    if status == "stop":
        return "arrêté avant la fin (plafond horaire, plage horaire ou temps du jour), reprise au prochain passage", None
    if status == "slow":
        return "pages Instagram qui ne chargent pas (connexion ou Instagram lent), reprise au prochain passage", None
    return "OK", None


def format_summary(st, cfg, started, outcome, days, budget):
    """Message de fin de passage (francais, lu sur le telephone)."""
    followed = st.users_since("follow", started)
    unfollowed = st.users_since("unfollow", started)
    used = float(st.get_meta(f"run_seconds:{today().isoformat()}") or 0) / 60
    lines = [
        f"Fin du passage en {format_duration((now() - started).total_seconds())} : {outcome}",
        f"Ce passage : {len(followed)} abonnements, {len(unfollowed)} désabonnements",
        f"Aujourd'hui : {st.followed_today()}/{budget} abonnements, {st.unfollowed_today()} désabonnements, "
        f"{used:.0f} min de passage" + (f" sur {cfg['daily_minutes']}" if cfg["daily_minutes"] else ""),
        f"Comptes suivis par le script : {st.count('unfollowed_on IS NULL')} dont {st.kept()} follow-back gardés, "
        f"désabonnement des autres après {days} j",
    ]
    if cfg["adaptive_delay"] and cfg["username"]:
        stats = followback_stats(st.samples(cfg["adapt_window_days"]), cfg, cfg["unfollow_after_days"])
        if stats["rate"] is None:
            lines.append(f"Follow-back : {stats['events']} mesuré(s), pas encore assez de données")
        else:
            lines.append(f"Follow-back : {stats['rate'] * 100:.1f} % sous {stats['horizon']} j, "
                         f"médian {stats['median']} j ({stats['events']} mesures)")
    if followed:
        lines.append("Nouveaux abonnements : " + ", ".join(followed))
    if unfollowed:
        lines.append("Désabonnements : " + ", ".join(unfollowed))
    given_up = st.given_up()
    if given_up:
        lines.append("Désabonnements abandonnés, à faire à la main : " + ", ".join(given_up))
    return "\n".join(lines)


def startup_stuck(cfg, notify):
    """Minuterie du demarrage : un appel bloque (Chrome sature, bureau virtuel) ne doit pas occuper la tache
    planifiee pendant des heures (les relances horaires sont ignorees tant qu'elle tourne, et le temps
    du jour fond). Alerte, ferme le navigateur du profil, libere le verrou et termine le processus."""
    minutes = cfg["startup_minutes"]
    log.error("Demarrage bloque depuis %d min (navigateur, bureau virtuel ou connexion) : arret du passage", minutes)
    try:  # autre thread : sa propre connexion SQLite
        send_alert(State(DB_PATH), notify, "startup", f"Échec : démarrage bloqué depuis {minutes} min (navigateur, "
                   "bureau virtuel ou connexion), nouvel essai au prochain passage")
    except Exception as e:
        log.warning("Alerte Telegram non envoyee (%s)", e)
    for pid in profile_pids():
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    LOCK.unlink(missing_ok=True)
    logging.shutdown()
    os._exit(1)


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
            st = State(DB_PATH)
            st.set_meta("needs_attention", "")
            st.set_meta("last_alert", "")  # le prochain passage annonce de nouveau son lancement
            until = st.get_meta("blocked_until")
            if until and now() < datetime.fromisoformat(until):
                log.info("Pause anti-blocage jusqu'au %s : aucune action du script d'ici la", until)
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
    print(f"Heure du script                            : {now():%Y-%m-%d %H:%M} ({cfg['timezone']}, "
          f"plage {cfg['active_hours'][0]}h-{cfg['active_hours'][1]}h)")
    print(f"Abonnements en cours (faits par le script) : {st.count('unfollowed_on IS NULL')}")
    print(f"Dont follow-back gardes                    : {st.kept()}")
    print(f"A desabonner maintenant                    : {len(st.due(days, 10**6))}")
    given_up = st.given_up()
    if given_up:
        print(f"Desabonnements abandonnes (a la main)      : {len(given_up)} ({', '.join(given_up)})")
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
    telegram = os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
    print(f"Notifications Telegram                     : {'actives' if telegram else 'non configurees (.env)'}")
    rest = rest_days(cfg, st, today())
    print(f"Jours de repos cette semaine               : {', '.join(map(day_label, rest)) or 'aucun'}")
    if today() in rest:
        print("Aujourd'hui                                : jour de repos (aucune action, reprise demain)")
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
        created = []
        try:
            if not here:
                created = place_on_bot_desktop(cfg["virtual_desktop"])
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
            remove_desktops(created)
    log.info("Sources verifiees : %d valides, %d introuvables%s", len(ok), len(ko),
             " (" + ", ".join(ko) + ")" if ko else "")
    return 0


def cmd_run(cfg, here, force=False):
    st = State(DB_PATH)
    notify = make_notify(cfg)
    attention = st.get_meta("needs_attention")
    if attention:
        log.error("Intervention requise (%s) : regle le probleme a la main dans Instagram, "
                  "puis lance 'insta_rotate.py login'", attention)
        send_alert(st, notify, "attention", f"Toujours à l'arrêt : intervention requise ({attention}). "
                   "Règle le problème dans Instagram, puis lance 'insta_rotate.py login'")
        return 5
    until = st.get_meta("blocked_until")
    if until and now() < datetime.fromisoformat(until):
        log.info("Pause anti-blocage jusqu'au %s : aucune action d'ici la", until)
        return 0
    if not PROFILE.exists():
        log.error("Aucun profil navigateur : lance d'abord 'insta_rotate.py login'")
        send_alert(st, notify, "profile", "Échec : aucun profil navigateur, lance 'insta_rotate.py login'")
        return 2
    if not force and not in_active_hours(cfg, now()):
        log.info("Hors de la plage horaire %dh-%dh : aucune action avant %dh (--force pour passer outre)",
                 *cfg["active_hours"], cfg["active_hours"][0])
        return 0
    base = cfg["unfollow_after_days"]
    analysis = bool(cfg["adaptive_delay"] and cfg["username"])
    days = int(st.get_meta("current_days") or base) if analysis else base
    announce_week(cfg, st, notify)  # avant le repos : un lundi de repos est annonce lui aussi
    if not force and is_rest_day(cfg, st, today()):
        log.info("Jour de repos (planning de la semaine) : aucune action aujourd'hui, "
                 "%d desabonnement(s) en attente, reprise demain (--force pour passer outre)",
                 len(st.due(days, 10**6)))
        return 0
    if cfg["adaptive_delay"] and not cfg["username"]:
        log.warning("Cle 'username' absente de config.json : pas de mesure des follow-back, delai fixe de %d jours", base)
    budget = follow_budget(st, cfg)
    quota = max(0, budget - st.followed_today())
    following = st.count("unfollowed_on IS NULL")
    if following >= cfg["max_following"]:
        log.warning("max_following atteint : %d comptes suivis par le script, dont %d follow-back gardes ; "
                    "pas de nouvel abonnement", following, st.kept())
        quota = 0
    room = max(0, cfg["max_unfollows_per_day"] - st.unfollowed_today())
    if not quota and not (room and st.due(days, 1)):
        log.info("Rien a faire aujourd'hui (abonnements autorises : %d)", budget)
        return 0
    alive = [seed for seed in cfg["seeds"] if int(st.get_meta(f"seed_fail:{seed}") or 0) < 3]
    if quota and not alive:
        log.error("Aucune source valide : verifie seeds.txt ou lance 'insta_rotate.py check-seeds'")
        send_alert(st, notify, "seeds",
                   "Échec : aucune source valide, vérifie seeds.txt ou lance 'insta_rotate.py check-seeds'")
        return 1
    left = minutes_left(st, cfg)
    if left is not None and left < 5:
        log.info("Temps de passage du jour deja utilise (%d min) : aucune action avant demain", cfg["daily_minutes"])
        return 0
    started = now()
    ready = None  # fin du demarrage : le temps de passage du jour ne compte qu'a partir de la
    longest = max(cfg["unfollow_days_max"], base)

    def fixed_days():  # groupe temoin : une part des comptes reste suivie le delai le plus long
        return longest if analysis and random.random() < cfg["explore_share"] else None

    counter = [0]
    unfollows = follows = 0
    status, kind, pause = "ok", None, None
    error = "interrompu"  # remplace quand le passage va au bout ou leve une erreur
    # deja une alerte aujourd'hui : pas de "Lancement" a chaque relance horaire tant que le probleme dure
    if not (st.get_meta("last_alert") or "").startswith(today().isoformat()):
        avail = f"{left:.0f} min disponibles" if left is not None else "sans limite de temps"
        notify(f"Lancement : {quota} abonnements possibles, {len(st.due(days, room))} désabonnements "
               f"en attente, {avail}")
    if cfg["keep_awake"]:
        keep_awake(True)
    watchdog = threading.Timer(cfg["startup_minutes"] * 60, startup_stuck, (cfg, notify))
    watchdog.daemon = True
    watchdog.start()
    try:
        with sync_playwright() as p:
            log.info("Ouverture du navigateur")
            ctx = launch(p, cfg, hidden=not here)
            created = []
            try:
                if not here:
                    created = place_on_bot_desktop(cfg["virtual_desktop"])
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_default_timeout(15000)
                if not logged_in(page):
                    if "/accounts/login" not in page.url and block_kind(page) == "attention":
                        # verification d'identite des l'ouverture : meme arret et meme pause qu'en cours de passage
                        status, kind, error = "blocked", "attention", None
                        pause = register_attention(st, cfg)
                        return 5
                    log.error("Session Instagram absente ou expiree : relance 'insta_rotate.py login'")
                    status, error = "session", None
                    return 3
                watchdog.cancel()
                ready = now()
                left = minutes_left(st, cfg)
                cfg["_deadline"] = ready + timedelta(minutes=left) if left is not None else None
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
                            profile_gap(page, cfg)
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
                remove_desktops(created)

        if status == "blocked" and kind == "attention":
            pause = register_attention(st, cfg)
        elif status == "blocked":
            pause = register_block(st, cfg, kind)
            log.warning("Reaction d'Instagram (%s) : pause de %d h, quotas ramenes a %d %%",
                        kind, pause[0], pause[1] * 100)
        else:
            register_clean_day(st)
        log.info("Termine : %d abonnement(s), %d desabonnement(s)", follows, unfollows)
        error = None
        return 0
    except Exception as e:
        log.exception("Erreur inattendue")
        error = f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}"
        return 1
    finally:
        watchdog.cancel()
        if ready:
            add_run_time(st, ready)
        keep_awake(False)
        try:  # fin du passage annoncee dans tous les cas, sans jamais faire echouer le passage
            outcome, code = run_outcome(status, kind, pause, error)
            text = format_summary(st, cfg, started, outcome, days, budget)
            if code:
                send_alert(st, notify, code, text)
            else:
                st.set_meta("last_alert", "")
                notify(text)
        except Exception as e:
            log.warning("Resume Telegram non envoye (%s)", e)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["login", "run", "status", "check-seeds"])
    ap.add_argument("--here", action="store_true",
                    help="run : garder la fenetre visible sur le bureau courant (debug)")
    ap.add_argument("--force", action="store_true",
                    help="run : ignorer la plage horaire et le jour de repos (debug)")
    args = ap.parse_args()
    load_env_file(ENV_FILE)  # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ; les vraies variables d'environnement priment
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
