# insta-rotate

Rotation d'abonnements Instagram (Windows, Python + Playwright). Chaque jour : lecture de la liste de tes abonnes
(detection des follow-back), desabonnement des comptes dont le delai est atteint et qui ne t'ont pas suivi en
retour, puis abonnement a de nouveaux
comptes tires des abonnes de comptes sources francais. Tache planifiee (09:00 par defaut), fenetre envoyee sur un
bureau virtuel dedie.

## Commandes
- `.\setup.ps1` : venv, dependances, tache planifiee "InstaRotate" (options `-Time 09:00`, `-DailyOnly`, `-NoTask`, `-Remove`),
  declenchee 5 min apres chaque ouverture de session et chaque jour, puis toutes les heures jusqu'a 23:00 (un passage
  rate, hors plage ou plante reprend dans l'heure). La tache contient le chemin complet : relancer setup.ps1 apres
  tout deplacement du dossier.
- `.\.venv\Scripts\python.exe insta_rotate.py login` : connexion manuelle unique, session gardee dans data\profile
  (demande aussi ton pseudo et l'ecrit dans config.json ; leve aussi l'arret "intervention requise")
- `... insta_rotate.py run` : passe quotidienne (`--here` : fenetre visible sur le bureau courant ; `--force` : ignore
  la plage horaire et le jour de repos, pour debugger)
- `... insta_rotate.py check-seeds` : visite chaque source, ecarte celles qui n'existent pas
- `... insta_rotate.py status` : compteurs, abonnements autorises aujourd'hui, follow-back, delai courant

## Fichiers
- insta_rotate.py : tout le code (config, etat SQLite, statistiques, prudence, navigation Playwright, bureau virtuel)
- seeds.txt : comptes sources, un pseudo par ligne, commentaires avec #. La cle "seeds" de config.json la remplace
  (sauf l'ancienne liste de 4 comptes, ignoree).
- config.json (ignore par git, contient ton pseudo) : surcharge des valeurs par defaut du dictionnaire DEFAULTS ;
  copie de config.example.json creee par setup.ps1, pseudo ajoute par `login`
- data\ (ignore par git) : profil navigateur, state.db, log.txt, debug\*.png
- telegram_notify.py : copie a l'identique de celui du projet github-follow-bot (dossier giithub), stdlib seule.
  Ne pas le modifier ici : le corriger la-bas puis le recopier.
- .env (ignore par git) : TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID, meme bot que github-follow-bot. Lu au demarrage
  (`load_env_file`, les vraies variables d'environnement priment). Sans ces deux cles : aucune notification.

## Notifications Telegram
- Seulement pour `run`, et seulement pour un passage qui ouvre le navigateur (pas pour les relances horaires sans
  travail : hors plage, repos, pause anti-blocage, rien a faire). Prefixe `[insta-rotate] ` (cle `notify_name`).
- "Lancement" avant l'ouverture du navigateur, puis resume (`format_summary`) dans le `finally` de `cmd_run`, y compris
  en cas d'erreur ou d'interruption : statut, abonnements / desabonnements du passage et du jour, temps de passage,
  delai courant, follow-back, pseudos.
- Alertes (`send_alert`, problemes a regler a la main : session expiree, intervention requise, desabonnements en
  echec, erreur, demarrage bloque, profil ou sources absents) : une par jour au plus pour un meme probleme (meta last_alert), et pas
  de "Lancement" tant qu'une alerte du jour est en cours ; un passage termine sans alerte, ou `login`, la leve.
- Test : `.\.venv\Scripts\python.exe telegram_notify.py "test" --prefix "[insta-rotate] "` (lit .env du dossier
  courant). Un envoi rate n'interrompt jamais le passage (avertissement dans log.txt) ; le token n'est jamais logge.

## Prudence (anti-limitation)
- Montee en charge : `follow_budget()` = min(follows_per_day, ramp_start + ramp_step x jours d'abonnements precedents)
  x quota_factor.
- Plafonds glissants cumules sur tous les passages : max_follows_per_hour, max_unfollows_per_hour ; plafonds du jour :
  max_unfollows_per_day ; max_following (plus d'abonnement au-dela, follow-back gardes compris). Les pauses sont
  calees sur 3600 / plafond (`human_pause`) ; `throttle()` attend ou arrete le passage.
- Profil ouvert sans clic (deja plus suivi, introuvable, echec, page non chargee) ou source inaccessible :
  `profile_gap()`, pause de `profile_gap_seconds` (20 a 60 s) avant la page suivante, pour ne pas ouvrir plusieurs
  profils en quelques secondes.
- Plage horaire `active_hours`, jour de repos (`rest_day_chance`, tire par date, seulement si la
  veille a eu des abonnements ou desabonnements : une veille sans action, PC eteint compris, vaut deja repos).
- Une seule source par passage, utilisee jusqu'au quota, en rotation dans l'ordre de seeds.txt (meta last_seed :
  le passage suivant part de la source d'apres). La source suivante n'est ouverte dans le meme passage que si
  celle-ci est epuisee ou inaccessible, `seeds_per_run` sources au plus.
- Reactions d'Instagram (`block_kind`) : "limit" -> pause block_cooldown_hours, quota_factor x 0,5 ; "warning" ->
  warning_cooldown_hours, quota_factor x 0,25 ; "attention" (verification d'identite, suspension, deconnexion, en
  cours de passage ou des l'ouverture) -> `register_attention` : meta needs_attention, arret total jusqu'a `login`,
  plus la pause et les quotas de "warning", comptes des la detection (le `login` ne les leve pas). La pause double
  a chaque blocage rapproche (14 jours max) ;
  quota_factor remonte de 0,1 par jour sans reaction. Trois clics ignores de suite = limitation silencieuse ("limit").

## Temps de passage (PC non allume toute la journee)
- `daily_minutes` (180 par defaut, 0 = illimite) : temps de passage maximal par jour, cumule sur tous les passages
  (meta run_seconds:<date>), compte a partir de la connexion a Instagram (le demarrage n'est pas decompte). Le
  passage s'arrete proprement a l'echeance (`cfg["_deadline"]`, lue par `throttle` et
  `human_pause`) ; les desabonnements passent avant les abonnements. Le travail non fait n'est pas rattrape (pas de rafale).
- A rythme prudent : environ max_follows_per_hour abonnements ou max_unfollows_per_hour desabonnements par heure de PC
  allume. Le volume du jour est donc fixe par le temps disponible, pas par follows_per_day.
- `keep_awake` : empeche la mise en veille Windows pendant le passage (SetThreadExecutionState).

## Delai de desabonnement adaptatif
- Chaque jour (une lecture), scan de la liste de TES abonnes (cle `username` de config.json, obligatoire pour la
  mesure) : un compte suivi qui y apparait recoit `followed_back_on` et n'est plus jamais desabonne (`due`,
  `kept`). Sans `username`, pas de mesure : tous les comptes sont desabonnes apres le delai. Un follow-back qui se
  desabonne ensuite de toi n'est pas detecte (le compte reste suivi).
- `followback_stats()` : estimation Kaplan-Meier. Un compte desabonne sans follow-back est "censure" (observe
  jusqu'a ce jour), pas un echec. Delai retenu = plus petit nombre de jours qui capte `adapt_target_share` (90 %) des
  follow-back obtenus sur `unfollow_days_max`, borne par `unfollow_days_min` / `unfollow_days_max`.
- Valeur courante dans meta.current_days. `unfollow_after_days` ne sert qu'au depart et tant que les donnees manquent.
- Groupe temoin : `explore_share` (20 %) des nouveaux comptes ont `fixed_days = unfollow_days_max`. Ne pas le
  retirer : sans lui, une fois le delai raccourci, les follow-back tardifs ne sont plus observes.
- L'ordre de la liste d'abonnes n'est pas fiable (pas chronologique) : elle est lue en entier, plafonnee a
  `followback_scan_rows`.
- Une erreur pendant cette lecture ou son ouverture (liste disparue, delai depasse) ne fait pas echouer le passage : capture
  `scan-error`, mesure du jour ignoree (relue au passage suivant), puis desabonnements et abonnements. Seul un
  navigateur ferme arrete le passage.

## Regles
- Pas d'emoji (code, logs, docs).
- Tout ce qui depend de l'interface Instagram (regex FR + EN, selecteurs, JS_USERS, JS_HREF_USERS) reste dans le bloc
  "UI" en haut du script.
- Aucun mot de passe nulle part : la session vient du profil persistant. Le token Telegram reste dans .env.
- Le script ne se desabonne que des comptes enregistres dans state.db, jamais des abonnements faits a la main.
- Un compte desabonne n'est jamais re-suivi (l'historique est conserve).
- Ne pas augmenter quotas ni plafonds, ni raccourcir les pauses, sans demande explicite. Ne jamais desactiver l'arret
  sur reaction d'Instagram.

## Points d'attention
- Les pseudos de seeds.txt ont ete ecrits de memoire : `check-seeds` avant la premiere vraie passe.
- Le placement sur bureau virtuel passe par pyvda (API COM interne de Windows) : peut casser apres une mise a jour
  de Windows. Repli automatique : fenetre hors ecran sur le bureau courant.
- Un bureau cree par un passage (run, check-seeds) est supprime a la fin de ce passage, navigateur ferme
  (`remove_desktops`), sauf s'il y reste une fenetre. Un bureau deja present n'est jamais supprime : si tu as
  deja `virtual_desktop` bureaux, la fenetre va sur le tien.
- L'interface web d'Instagram change souvent : voir la skill maj-selecteurs.

## Robustesse (le passage ne doit pas rester bloque)
- Desabonnement en echec : le compte passe en fin de file (colonne unfollow_fails) et est abandonne apres
  MAX_UNFOLLOW_FAILS (3) echecs, tous passages confondus ; `status` et le resume Telegram le listent (a desabonner a
  la main). Dans un passage : 3 echecs d'interface de suite -> arret "broken" (alerte) ; 3 profils non charges de
  suite (page blanche, delai depasse) -> arret "slow" (sans alerte, reprise au passage suivant).
- Deja plus suivi ("Suivre" / "Suivre en retour", NOT_FOLLOWING_RE) ou compte introuvable : enregistre sans
  unfollowed_at (note "deja plus suivi" / "introuvable"), hors plafonds horaire et journalier, sans pause longue
  (seulement `profile_gap`).
- Une page lente ne fait pas echouer le passage : source suivante, compte suivant (en fin de file) ou mesure ignoree.
  Un navigateur ferme ou une connexion coupee l'arretent ("Erreur inattendue").
- Demarrage (navigateur, bureau virtuel, connexion) limite a `startup_minutes` (8) par une minuterie
  (`startup_stuck`) : au-dela, alerte, navigateur du profil tue, verrou libere, processus arrete. Sans elle, un
  appel bloque occupe la tache planifiee (relances horaires ignorees) et le bureau virtuel cree reste en place.
- Verrou data\run.lock : ignore si le processus note n'est plus insta_rotate.py (PC eteint pendant un passage,
  numero de processus repris par Windows).
- Repli sur le Chromium de Playwright seulement si le navigateur de `browser_channel` manque, pas sur un demarrage
  trop lent. Ce Chromium n'est pas installe par setup.ps1 (`.\.venv\Scripts\playwright.exe install chromium`).
