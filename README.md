# Bot Discord : jeux gratuits et actus

Le bot publie **tout seul** (aucune commande à taper), toutes les 40 minutes :

- 🎁 les **jeux gratuits** à garder : Epic Games, Steam, GOG, itch.io, Ubisoft, EA, Battle.net, IndieGala…
- 🔜 les **prochains jeux gratuits Epic Games**, annoncés à l'avance
- 📰 les **actus jeux vidéo** (jeuxvideo.com, ActuGaming ; tu peux ajouter d'autres flux RSS)

Chaque offre n'est publiée **qu'une seule fois**, même si tu redémarres le bot : il garde la liste dans `posted.json`.

Sources : API officielle d'Epic Games, [GamerPower](https://www.gamerpower.com/api-read), et les communautés
[Dealabs](https://www.dealabs.com), [GG.deals](https://gg.deals) et [r/FreeGameFindings](https://www.reddit.com/r/FreeGameFindings).
Quand plusieurs sites parlent du même jeu, il n'est publié qu'une seule fois.

---

## Installation (≈ 5 minutes)

### 1. Créer le bot sur Discord
1. Va sur <https://discord.com/developers/applications> puis clique sur **New Application** et donne-lui un nom.
2. Dans l'onglet **Bot**, clique sur **Reset Token** et **copie le token** (garde-le secret !).
   Au même endroit, clique sur l'icône du bot pour lui mettre la photo de profil `avatar.png`.
3. Dans l'onglet **OAuth2 → URL Generator** :
   - coche **bot** dans *Scopes* ;
   - dans *Bot Permissions*, coche **View Channels**, **Send Messages** et **Embed Links**
     (et **Mention Everyone** si tu veux mentionner un rôle qui n'est pas mentionnable).
4. Ouvre l'URL générée en bas de la page et ajoute le bot à ton serveur.

> Aucun « Privileged Gateway Intent » n'est nécessaire.

### 2. Récupérer les identifiants des salons
Dans Discord, va dans **Paramètres → Avancés** et active le **Mode développeur**.
Ensuite, fais un clic droit sur un salon puis **Copier l'identifiant du salon**.

### 3. Configurer
Double-clique sur **`lancer.bat`**. La première fois, il crée le fichier **`.env`** et l'ouvre dans le Bloc-notes.
Remplis-le, enregistre avec **Ctrl+S**, puis ferme le Bloc-notes :

```
DISCORD_TOKEN=ton_token
GAMES_CHANNEL_ID=123456789012345678
NEWS_CHANNEL_ID=123456789012345679
```

Laisse `NEWS_CHANNEL_ID` vide si tu ne veux pas les actus. Tu peux aussi y mettre le même salon que pour les jeux.
Les autres réglages sont expliqués dans le fichier : rôle à mentionner, fréquence, plateformes, flux RSS.
Pour modifier les réglages plus tard : clic droit sur `.env`, puis **Ouvrir avec → Bloc-notes**.

### 4. Lancer
Double-clique à nouveau sur **`lancer.bat`**. La première fois, il installe tout seul ce qu'il faut (patiente un peu).
Au démarrage, le bot publie les offres gratuites en cours, puis seulement les nouvelles.

Pour voir ce que le bot publierait **sans rien envoyer sur Discord** :

```
.venv\Scripts\python.exe bot.py --test
```

---

## 24h/24 gratuitement, PC éteint (GitHub Actions)

Avec `lancer.bat`, le bot ne tourne que si ton PC est allumé. Avec **GitHub**, c'est gratuit et **sans carte bancaire** :
toutes les 40 minutes, GitHub lance le bot sur ses serveurs. Le bot publie les nouveautés, puis se déconnecte.

### 1. Créer le dépôt
1. Crée un compte gratuit sur <https://github.com/signup>.
2. En haut à droite, clique sur **+** puis **New repository**.
3. Nom : `bot-jeux-gratuits`. Choisis **Public** : les minutes de calcul sont illimitées et ton token ne sera pas visible, il sera caché dans les « secrets ».
4. Clique sur **Create repository**.

### 2. Envoyer les fichiers
1. Sur la page du dépôt, clique sur le lien **uploading an existing file**.
2. Ouvre le dossier extrait du zip et **glisse tout son contenu** dans la page, **y compris le dossier `.github`**.
3. ⚠️ N'envoie **JAMAIS** le fichier `.env` : ton token deviendrait public.
4. Clique sur **Commit changes**.
5. Vérifie que `.github/workflows/bot.yml` apparaît bien dans le dépôt. Sinon, clique sur **Add file → Create new file**,
   tape `.github/workflows/bot.yml` comme nom, colle le contenu du fichier `bot.yml`, puis clique sur **Commit changes**.

### 3. Ajouter les secrets
Dans le dépôt, va dans **Settings → Secrets and variables → Actions**. Clique sur **New repository secret** pour chaque ligne :

| Name               | Secret                                        |
| ------------------ | --------------------------------------------- |
| `DISCORD_TOKEN`    | le token du bot                               |
| `GAMES_CHANNEL_ID` | l'identifiant du salon des jeux gratuits disponibles |
| `UPCOMING_CHANNEL_ID` | l'identifiant du salon des jeux bientôt gratuits (optionnel) |
| `NEWS_CHANNEL_ID`  | l'identifiant du salon des actus (optionnel)  |
| `PING_ROLE_ID`     | l'identifiant du rôle à mentionner (optionnel)|

### 4. Lancer
1. Va dans l'onglet **Actions**. Si GitHub le demande, clique sur le bouton vert pour activer les workflows.
2. Clique sur **Jeux gratuits** à gauche, puis sur **Run workflow** et encore sur **Run workflow**.
3. Au bout d'environ 1 minute :
   - ✅ **coche verte** : les messages arrivent sur Discord ;
   - ❌ **croix rouge** : clique dessus pour lire l'erreur (souvent un secret mal collé).

Ensuite, c'est **automatique toutes les 40 minutes**, pour toujours. Tu n'as plus besoin de `lancer.bat`.

> ⚠️ N'utilise pas `lancer.bat` **et** GitHub en même temps, sinon les jeux seront publiés en double.

---

## À savoir
- **Avec `lancer.bat`**, le bot ne tourne que pendant que la fenêtre est ouverte et que ton PC est allumé.
- **Avec GitHub**, le bot apparaît « hors ligne » dans la liste des membres, car il ne se connecte que quelques secondes toutes les 40 minutes. C'est normal.
  GitHub peut aussi avoir quelques minutes de retard.
  Si un jour GitHub met la tâche en pause, va dans **Actions → Jeux gratuits** et clique sur **Enable workflow**.
- Pour **tout republier** depuis zéro, supprime `posted.json` : dans le dossier du bot, ou dans le dépôt GitHub.
- Ne partage **jamais** ton fichier `.env` ni ton token.
