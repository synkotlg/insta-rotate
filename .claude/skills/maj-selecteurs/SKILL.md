---
name: maj-selecteurs
description: Diagnostiquer et corriger insta_rotate.py quand l'interface web d'Instagram change (bouton Suivre/Abonne introuvable, liste d'abonnes vide, desabonnement qui echoue, mesure des follow-back a 0, reactions mal detectees, 0 action dans le log). A utiliser des qu'un run logue des echecs repetes.
---

# Mise a jour des selecteurs Instagram

1. Lire la fin de `data/log.txt` et reperer l'etape qui echoue : ouverture d'une liste d'abonnes (celle d'une source
   ou la tienne pour la mesure des follow-back), clic sur Suivre, desabonnement, detection d'une reaction d'Instagram,
   verification de connexion, ou ouverture d'un profil par la recherche ("profil non ouvert par la recherche" pour
   presque chaque compte, y compris ceux qui existent : selecteurs SEARCH_* ou PROFILE_LINK_SEL). Beaucoup de
   "Clic de repli" : un element recouvert par un autre (comme le champ de recherche, recouvert par son bouton).
   Desabonnements depuis ta liste de suivi(e)s : "pas desabonne depuis ta liste de suivi(e)s (failed)" ou "2 echecs
   depuis ta liste" -> FOLLOWING_COUNT_RE (compteur "N suivi(e)s"), NO_RESULT_RE, DIALOG_CLOSE_SEL (bouton Fermer).
   "(absent)" seul est normal (demande en attente, compte supprime). Fil d'actualite non ouvert -> HOME_NAV_SEL.
2. Regarder les captures les plus recentes dans `data/debug/` (nom = date + etape).
3. Relancer en visible pour observer : `.\.venv\Scripts\python.exe insta_rotate.py run --here --force`
   (baisser temporairement `follows_per_day` a 2 dans config.json pour un test court). Si une seule source echoue,
   `insta_rotate.py check-seeds` dit si le compte existe encore.
4. Comparer avec le bloc "UI" en haut de `insta_rotate.py` (regex de textes dont BLOCK_RE / WARN_RE / ATTENTION_RE,
   selecteurs, JS_USERS, JS_HREF_USERS) et corriger uniquement ce bloc. Les textes doivent rester valables en FR et en EN.
5. Verifier un cycle complet : un abonnement, puis un desabonnement (forcer en mettant `unfollow_after_days` a 0 et
   `adaptive_delay` a false).
6. Remettre la configuration d'origine. Ne jamais toucher aux plafonds, pauses ni quotas dans cette operation.
