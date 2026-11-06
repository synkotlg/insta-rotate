# insta-rotate

Rotation d'abonnements Instagram (Windows, Python + Playwright). Chaque jour : lecture de la liste de tes abonnes
(detection des follow-back), desabonnement des comptes dont le delai est atteint et qui ne t'ont pas suivi en
retour, puis abonnement a de nouveaux
comptes tires des abonnes de comptes sources francais. Tache planifiee (09:00 par defaut), fenetre envoyee sur un
bureau virtuel dedie.

## Fuseau horaire
- Toutes les heures du script sont celles de Paris (cle `timezone`, "Europe/Paris"), pas celles du PC : `now()` /
  `today()` (plage horaire, jours, plafonds, horodatages de state.db sans fuseau attache), log.txt, captures,
  navigateur (`timezone_id`). Sous Windows, zoneinfo a besoin du paquet tzdata (requirements.txt).
- Le PC lui-meme est regle sur l'heure de l'Inde (India Standard Time, constate le 2026-11-05) : le navigateur du bot
  annonce Paris, mais tout autre navigateur de ce PC annonce l'Inde a Instagram. A regler dans Windows, pas ici.
- setup.ps1 : `-Time` et 23:00 sont des heures de Paris. La tache garde un instant UTC, donc un changement de fuseau
  du PC ne la decale pas (pas besoin de relancer setup.ps1). Depart a `-Time` heure d'ete, fin a 23:00 heure
  d'hiver : en hiver le premier passage a lieu une heure plus tot, le script ignore ceux hors plage.

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
  travail : hors plage, repos, pause anti-blocage, rien a faire), sauf le planning de la semaine. Prefixe `[insta-rotate] ` (cle `notify_name`).
- Planning de la semaine (`announce_week`) : une fois par semaine (meta rest_week_sent), au premier `run` dans la
  plage horaire (avant le test du jour de repos, meme sans travail), chaque jour jusqu'a dimanche en "repos" ou
  "actif". Envoi rate ou Telegram absent : nouvel essai au passage suivant. `status` affiche aussi les repos.
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
  x quota_factor x volume du jour.
- Volume du jour (`day_volume`) : tire une fois par jour dans `daily_volume` ([0,6 ; 1]), garde dans meta
  day_volume:<date>, applique aux abonnements (`follow_budget`) et aux desabonnements autorises (`unfollow_room`) :
  des jours plus calmes, jamais le meme maximum chaque jour. Ne fait que baisser les quotas.
- Plafonds glissants cumules sur tous les passages : max_follows_per_hour, max_unfollows_per_hour ; plafonds du jour :
  max_unfollows_per_day ; max_following (plus d'abonnement au-dela, follow-back gardes compris). Les pauses sont
  calees en moyenne sur 3600 / plafond (`human_pause`) ; `throttle()` attend ou arrete le passage.
- Rafales (`human_pause`, demande explicite du 2026-11-06) : apres une pause normale, parfois 1 ou 2 actions
  rapprochees (8 a 40 s), puis une pause plus longue qui rend le temps gagne (`_debt`) : le rythme moyen et les
  plafonds horaires ne changent pas, seules certaines pauses sont plus courtes.
- Souris : tous les clics passent par `human_click` (molette jusqu'a l'element, trajectoire courbe `human_move`
  vers un point au hasard du bouton, courte pause, appui puis relache espaces), le defilement des listes par
  `scroll_dialog`. Ne pas revenir a `locator.click()` (curseur teleporte au centre, clic instantane) ; `_mouse_pos`
  doit toujours suivre le curseur, sinon il saute au mouvement suivant. Repli sur le clic de Playwright ("Clic de
  repli" dans log.txt) si l'element reste hors de vue ou est recouvert ; un element disparu leve PWTimeout.
- Navigation : jamais d'adresse tapee pour un profil (`open_profile`) : ton profil par le lien de la barre de gauche,
  les autres par la Recherche (`_search_profile` : lien Recherche, clic dans le champ, pseudo tape par `human_type`,
  clic sur le resultat), sans recharger la page. `human_type` : rythme irregulier, fautes de frappe (touche voisine
  AZERTY ou lettre doublee) effacees au retour arriere, arret possible des que le compte apparait. Repli sur
  `page.goto` (log "profil non ouvert par la recherche") si le compte n'apparait pas (supprime, renomme) ou si
  l'interface a change. Seul l'accueil (`logged_in`) s'ouvre par son adresse.
- Fenetres (listes d'abonnes, de suivi(e)s) : elles recouvrent toute la page, barre de gauche comprise, et Echap ne
  les ferme pas. `close_dialogs` les ferme par leur bouton Fermer avant toute navigation (`open_profile`,
  `browse_feed`) ; sans ca, la recherche suivante echoue et la page se recharge.
- Desabonnements (`unfollow_pass`) : les abonnements directs (colonne `requested` = 0) depuis ta liste de suivi(e)s
  (`unfollow_in_list` : pseudo tape dans le champ de la liste, Suivi(e), confirmation), les autres par leur profil
  (`unfollow_one`) : demandes en attente (comptes prives, `requested` = 1, absentes de ta liste tant qu'elles ne
  sont pas acceptees) et comptes suivis avant cette mesure (`requested` NULL). Les deux groupes dans un ordre tire
  au hasard. Compte absent de la liste ou echec : passage par le profil ; apres 2 echecs de la liste dans un passage,
  tout par le profil. `requested` est lu sur le bouton apres l'abonnement (`follow_kind`, "Demande" = demande).
- Fil d'actualite (`browse_feed`) : 0, 1 ou 2 passages par passage (15 / 60 / 25 %), souvent au debut, de
  `browse_seconds` (40 a 150 s) : molette et temps de lecture, aucun clic (ni like, ni story). Compte dans le temps
  de passage.
- Pauses jamais figees : `capped_wait` et les attentes de `throttle` passent par `idle` (petits gestes au hasard,
  curseur ou molette, sans clic, avec des absences). `rnd(lo, hi)` : jamais moins que lo, mediane au milieu, une
  fois sur dix plus long (jusqu'a lo + 3 x (hi - lo)) ; pause longue au hasard (1 sur `long_pause_every` en
  moyenne), pas toutes les N actions.
- Abonnements : un compte au hasard parmi les eligibles affiches (pas le premier de la liste), `pass_over_share`
  (25 %) passes sans abonnement. Desabonnements (`due`) : les plus anciens, dans un ordre melange (jamais
  alphabetique), ceux en echec en dernier.
- Ordre du passage tire au hasard (`task_order`) : lecture de tes abonnes toujours avant les desabonnements ; parfois
  2 a 5 abonnements avant (au debut ou apres la lecture), le reste apres ; passages sur le fil inseres au hasard
  (jamais deux de suite). Si ces premiers abonnements butent sur le plafond horaire, les desabonnements continuent
  quand meme (plus d'abonnement dans ce passage).
- Navigateur (`launch`) : sans `--enable-automation` (navigator.webdriver = true, bandeau) ni `--disable-extensions`,
  avec `--disable-blink-features=AutomationControlled`. Ne pas les remettre. Pas d'option `locale` de Playwright
  (elle reduisait navigator.languages et Accept-Language a une seule langue) : `--lang` pour l'interface, les
  langues annoncees sont celles du profil (fr-FR, fr, en-US, en). Fenetre de la taille de l'ecran (zone de
  travail), en haut a gauche ; jamais laissee a -32000 (position lisible depuis la page).
- Depart decale : un passage planifie (sans `--here` ni `--force`) qui a du travail attend `start_delay_minutes`
  (0 a 20 min) avant d'ouvrir le navigateur, pour ne pas commencer a la meme minute chaque heure.
- Restent visibles cote technique, sans correction propre : la connexion de pilotage de Playwright (detectable par
  des techniques avancees) et les scripts evalues dans la page elle-meme. Seul un Playwright modifie par des tiers
  les retirerait : pas fait.
- Profil ouvert sans clic (deja plus suivi, introuvable, echec, page non chargee) ou source inaccessible :
  `profile_gap()`, pause de `profile_gap_seconds` (20 a 60 s) avant la page suivante, pour ne pas ouvrir plusieurs
  profils en quelques secondes.
- Plage horaire `active_hours`, jours de repos planifies par semaine (`rest_days`, lundi a dimanche) : tires une
  fois a la premiere demande de la semaine (`rest_day_chance` par jour, au moins `rest_days_min` (1) par semaine,
  jamais deux de suite, ni un lundi apres un dimanche de repos), gardes dans meta rest_week:<lundi>, puis fixes.
  Les jours deja passes au tirage, et aujourd'hui s'il a deja eu des actions, restent actifs ; un jour deja passe
  sans action (PC eteint) compte comme le repos minimum. Un jour actif PC eteint n'est pas rattrape.
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
  `human_pause`) ; les desabonnements passent avant les abonnements (sauf 2 a 5 abonnements dans certains passages,
  voir `task_order`). Le travail non fait n'est pas rattrape (pas de rafale).
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
  `followback_scan_rows`. Le 2026-11-05 elle faisait 90 lignes (quelques defilements) : a revoir seulement si le
  compte depasse quelques centaines d'abonnes.
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
  de Windows. Repli automatique : fenetre derriere toutes les autres sur le bureau courant
  (visible seulement si rien d'autre n'est ouvert).
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
