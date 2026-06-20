# Yggdrasil — Tableau de bord prospectif

Yggdrasil importe les exports structurés d'**IRIS-Station** et les fait vivre
sous forme d'un **arbre des possibles** interactif, mis à jour en continu via le
mode Vigie. Le tronc représente les faits, les branches les hypothèses, et les
sous-branches les bifurcations possibles : un futur d'autant plus visible qu'il
est probable.

Application de bureau **PySide6**, **local-first** et **hors-ligne** (hormis le
mode Vigie). Toutes les données restent sur votre machine. Aucune télémétrie,
aucun compte, aucune synchronisation.

---

## Sommaire

1. [Présentation](#présentation)
2. [Installation](#installation)
3. [Premier démarrage](#premier-démarrage)
4. [Fonctionnalités en détail](#fonctionnalités-en-détail)
5. [Raccourcis clavier](#raccourcis-clavier)
6. [Configuration](#configuration)
7. [Stockage des données](#stockage-des-données)
8. [Construction de l'exécutable](#construction-de-lexécutable)
9. [Architecture](#architecture)
10. [Dépannage](#dépannage)
11. [Philosophie](#philosophie)
12. [Feuille de route](#feuille-de-route)

---

## Présentation

Yggdrasil est le compagnon de visualisation d'IRIS-Station. La répartition des
rôles est volontairement stricte :

- **IRIS-Station** fait l'analyse lourde : ACH (Analysis of Competing
  Hypotheses), diagnosticité des preuves, extraction d'entités, mise à jour
  bayésienne initiale, suivi des prédictions. Il produit un export structuré
  (Markdown avec front-matter YAML).
- **Yggdrasil** consomme cet export et le rend vivant : visualisation en arbre,
  exploration interactive, recalcul bayésien local, veille par flux entrants, et
  ré-export sans perte vers IRIS-Station.

Yggdrasil ne refait pas l'ACH ni la diagnosticité : il embarque uniquement la
mise à jour bayésienne, le calcul de tension aux bifurcations, l'extraction de
vraisemblances pour les flux de veille, et le rendu interactif.

---

## Installation

### Option A — Exécutable

1. Récupérez `Yggdrasil.exe` (Windows) ou le binaire correspondant.
2. Lancez-le. Sous Windows, SmartScreen peut afficher un avertissement pour un
   exécutable non signé : c'est attendu.
3. À la première ouverture, importez un fichier `.md` exporté depuis IRIS-Station.

### Option B — Depuis les sources

Prérequis : **Python 3.11 ou supérieur**.

```bash
python -m pip install -r requirements.txt
python -m spacy download fr_core_news_sm   # facultatif (sinon téléchargé au 1er usage de la Vigie)
python main.py
```

> Sous Windows, utilisez toujours `python -m pip` (et non `pip` seul) pour
> garantir que l'installation se fait dans le même interpréteur que celui qui
> lance l'application. Voir la section [Dépannage](#dépannage).

Dépendances : PySide6, spaCy (`fr_core_news_sm`), PyYAML, NumPy, requests,
matplotlib.

---

## Premier démarrage

Au lancement, l'application est vide. Importez un rapport :

1. Menu **Fichier > Importer un export IRIS-Station** (ou `Ctrl+I`, ou le bouton
   📥 de la barre d'outils).
2. Sélectionnez le fichier `.md` produit par IRIS-Station.
3. Le projet apparaît dans la liste de gauche et une page de résumé s'affiche
   (nom, date, nombre d'hypothèses, de preuves, de bifurcations, de prédictions
   et d'entités).
4. Basculez sur la **Vue Arbre** (bouton 🌳) pour explorer l'arbre.

Le projet est persistant : il reste disponible aux lancements suivants.

---

## Fonctionnalités en détail

### Gestion des projets

La liste de gauche regroupe tous les projets importés. Cliquez pour basculer de
l'un à l'autre ; l'arbre, la vue Rivière et le résumé se mettent à jour. Un
clic droit sur un projet permet de le **supprimer** (avec confirmation et
suppression de toutes ses données dépendantes). De nouveaux projets peuvent
aussi être créés automatiquement par le mode Vigie (voir plus bas).

### Vue Arbre — l'arbre des possibles

La vue centrale dessine l'analyse sous forme d'arbre organique qui pousse vers
le haut.

- **Le tronc** porte l'étiquette « Faits (N preuves) » : c'est la base
  factuelle commune.
- **Les nœuds d'hypothèses** sont des fruits dont la **taille est
  proportionnelle à la probabilité a posteriori** (rayon = 30 + 40 × posterior).
  Plus une hypothèse est probable, plus son fruit est gros.
- **La couleur** dépend de l'horizon temporel : court terme (rouge), moyen
  terme (orange), long terme (bleu), ou gris en l'absence d'horizon.
- **L'épaisseur des branches** suit la probabilité de l'hypothèse enfant.
- **Les nœuds de bifurcation** sont les fourches de l'écorce, là où l'histoire
  peut basculer. Quand leur **tension** dépasse 0,7, un **halo** s'allume :
  point de bascule critique. Une tension inconnue est signalée par un nœud grisé
  marqué « ? ».
- L'étiquette sous chaque fruit rappelle le nom court de l'hypothèse.

**Interactions :**

- **Zoom** : `Ctrl + molette` (de 20 % à 300 %) ; le niveau est affiché dans la
  barre de statut.
- **Déplacement de la vue** : cliquer-glisser sur le fond.
- **Déplacer un nœud** : cliquer-glisser le nœud ; les branches se recalculent
  en temps réel.
- **Rattacher par glisser-déposer** : lâcher une hypothèse sur un nœud de
  bifurcation propose de l'y rattacher ; la lâcher sur une autre hypothèse
  propose d'en faire une hypothèse enfant. Une confirmation est demandée, puis
  l'arbre se réorganise.
- **Replier / déplier** : double-clic sur une hypothèse pour masquer ou
  réafficher son sous-arbre.
- **Bascule de classement** : lorsqu'une hypothèse change de rang après un
  recalcul, un badge animé apparaît brièvement et la barre de statut le signale.

### Panneau d'information

Masquable via le menu **Affichage**, ce panneau latéral détaille l'élément
sélectionné au clic.

Pour une **hypothèse** :

- nom et description complète ;
- probabilité a priori → a posteriori ;
- horizon et hypothèse parente ;
- preuves associées (avec crédibilité et score de cohérence ACH) ;
- **vraisemblances éditables** : chaque preuve expose `P(E|H)` et `P(E|¬H)`
  modifiables, avec un bouton de recalcul qui met à jour la probabilité et
  l'arbre en direct ;
- mini-graphe d'historique de la probabilité (matplotlib) ;
- listes cliquables des hypothèses filles et des bifurcations enfants.

Pour un **nœud de bifurcation** :

- libellé et condition textuelle ;
- jauge de tension (0 à 1) ;
- horizon et hypothèse parente ;
- hypothèses filles avec leurs probabilités ;
- badge « ⚠ Point de bascule critique » si la tension est élevée.

### Moteur bayésien

À chaque ajout ou modification de preuve, Yggdrasil recalcule localement la
probabilité a posteriori en log-odds (stable numériquement), met à jour la
taille du nœud et l'épaisseur des branches, recalcule la tension des
bifurcations parentes, et détecte les bascules de classement. Ce moteur est
identique à celui d'IRIS-Station, embarqué en quelques lignes.

### Vue Rivière

Accessible par le bouton 📊, elle trace l'**évolution temporelle des
probabilités** : une courbe par hypothèse (couleur = horizon), des marqueurs aux
dates d'ajout de preuves, et une légende interactive (clic pour masquer/afficher
une hypothèse). On y voit une conviction se construire ou s'effondrer au fil du
temps. Les hypothèses sans historique suffisant sont représentées par un simple
marqueur.

### Mode Vigie — veille par flux entrants

Le mode Vigie (bouton 🔭) transforme un rapport figé en système vivant. Il
nécessite une clé API configurée (voir [Configuration](#configuration)).

1. Sélectionnez le projet actif (ou laissez la détection automatique s'en
   charger).
2. Collez un texte : article, dépêche, notes. Un compteur de tokens estimés
   s'affiche.
3. Cliquez sur **▶ Analyser et mettre à jour l'arbre**.
4. Le modèle estime, pour chaque hypothèse, la vraisemblance du nouveau texte,
   et propose une preuve résumée, une crédibilité, des entités et des
   prédictions.
5. Si l'option **Révision avant application** est cochée (par défaut), un
   dialogue permet de vérifier et d'éditer le tout, avec un aperçu en direct du
   nouveau a posteriori et du delta, avant d'appliquer ou de rejeter.
6. Après application : la preuve est ajoutée, les probabilités recalculées,
   l'historique enrichi, les bascules détectées, l'arbre mis à jour, le flux
   journalisé pour la traçabilité, et un résumé des deltas affiché.

Le LLM est volontairement **périphérique** : jamais appelé automatiquement,
toujours sous validation humaine. Les erreurs réseau et API sont explicitées
(clé invalide, quota dépassé, abonnement expiré, délai dépassé, réponse mal
formée).

### Détection automatique de projet

Quand vous collez un flux, Yggdrasil devine à quel projet il appartient :

- il calcule un **vecteur de référence** par projet (moyenne des vecteurs spaCy
  des preuves, descriptions et nom du projet) et compare le flux par
  **similarité cosinus** ;
- en complément, il mesure le **chevauchement d'entités nommées** : si le
  vocabulaire diffère mais que les acteurs se recoupent fortement, le projet est
  quand même proposé.

Selon la confiance : bascule automatique (≥ 0,60), suggestion avec choix
(0,45–0,60 ou fort chevauchement d'entités), ou proposition de créer un nouveau
projet (en deçà). L'utilisateur garde toujours le dernier mot. Si spaCy est
indisponible, la détection est simplement ignorée et le projet sélectionné est
utilisé.

### Export IRIS-Station (round-tripping)

Menu **Fichier > Exporter pour IRIS-Station** (ou `Ctrl+E`, bouton 📤). Yggdrasil
régénère le même format YAML qu'IRIS-Station, accompagné d'un rapport Markdown
lisible. L'aller-retour **IRIS → Yggdrasil → IRIS → Yggdrasil est sans perte de
données** : les références internes utilisent des identifiants stables, pas les
libellés affichés, pour rester fiables même après réattribution des identifiants
en base. Le round-tripping préserve les données, pas les positions visuelles des
nœuds (recalculées à chaque rendu).

### Sauvegarde automatique

Après chaque mise à jour Vigie, l'état complet du projet est écrit dans un
fichier `yggdrasil_autosave_{id}.yml` à côté de la base, pour la restauration,
le réimport dans IRIS-Station ou le versionnement.

### Annuler / Rétablir

`Ctrl+Z` annule la dernière action, `Ctrl+Maj+Z` la rétablit. L'historique
couvre l'import, la suppression de projet, le recalcul de vraisemblances,
l'application d'un flux Vigie et les réorganisations de l'arbre. Il repose sur
des instantanés binaires de la base (profondeur 20).

### Plein écran et préférences

`F11` bascule en plein écran. À la fermeture, la géométrie de la fenêtre et le
projet actif sont mémorisés et restaurés au lancement suivant.

---

## Raccourcis clavier

| Raccourci | Action |
|---|---|
| `Ctrl+I` | Importer un export IRIS-Station |
| `Ctrl+E` | Exporter pour IRIS-Station |
| `Ctrl+Z` | Annuler |
| `Ctrl+Maj+Z` | Rétablir |
| `F11` | Plein écran |
| `Ctrl+molette` | Zoom dans la Vue Arbre |
| Double-clic | Replier / déplier un sous-arbre |

---

## Configuration

Menu **Configuration > API FantasyAI** :

- **Clé API** (champ masqué) — stockée localement via QSettings, **jamais en
  base de données**.
- **URL de base** — par défaut `https://www.fantasyai.cloud/api/v1` ;
  modifiable si votre fournisseur compatible OpenAI utilise un autre endpoint.
  L'appel se fait sur `{URL de base}/chat/completions`.
- **Modèle** — liste peuplée dynamiquement via le bouton **Tester la
  connexion**, ou valeurs de repli.

Tant qu'aucune clé n'est configurée, le bouton 🔭 Vigie reste désactivé.

---

## Stockage des données

Tout est local, dans le dossier `~/.yggdrasil/` (soit
`C:\Users\<vous>\.yggdrasil\` sous Windows) :

- `yggdrasil.db` — base SQLite contenant tous les projets et leurs données ;
- `yggdrasil_autosave_{id}.yml` — sauvegardes YAML automatiques après Vigie.

Les préférences (géométrie de la fenêtre, projet actif, configuration API) sont
gérées par QSettings (registre Windows / fichier de configuration selon l'OS).

---

## Construction de l'exécutable

Build mono-fichier avec PyInstaller, lancé via le même interpréteur que les
dépendances :

```bash
python -m pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name Yggdrasil ^
  --collect-all spacy --collect-all fr_core_news_sm --collect-all yaml ^
  --hidden-import matplotlib.backends.backend_qtagg ^
  --exclude-module sklearn --exclude-module scipy ^
  --exclude-module tkinter --exclude-module PyQt5 --exclude-module PyQt6 ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtMultimedia ^
  --clean --noconfirm main.py
```

(Sous Linux/macOS, remplacez les `^` de fin de ligne par `\`, ou mettez tout sur
une seule ligne.)

**Alléger le binaire :**

- les exclusions ci-dessus évitent d'embarquer des bibliothèques non utilisées
  (scikit-learn, scipy, modules Qt superflus) ;
- pour compresser davantage, installez **UPX** et ajoutez `--upx-dir "C:\upx"` ;
- pour un binaire plus léger au prix d'un téléchargement au premier usage,
  retirez `--collect-all fr_core_news_sm` (le modèle spaCy sera récupéré à la
  première Vigie).

**Icône (facultative) :** ajoutez `--icon yggdrasil_icon.ico`.

L'exécutable est généré dans `dist/`.

---

## Architecture

- **Un seul fichier `main.py`** contient toute l'application.
- **PySide6** pour l'interface ; la Vue Arbre s'appuie sur
  `QGraphicsView`/`QGraphicsScene`.
- **SQLite** en SQL brut (sans ORM), base unique persistante.
- **spaCy** (`fr_core_news_sm`) pour la détection automatique de projet.
- **PyYAML** pour l'import/export ; **NumPy** pour les vecteurs ; **requests**
  pour la Vigie ; **matplotlib** pour les graphes.
- Fonctions pures pour les algorithmes (bayésien, tension, similarité, layout) ;
  classe `Database` pour l'accès aux données.

Le schéma couvre : projets, horizons, hypothèses, nœuds et liens de bifurcation,
preuves, scores ACH, entités, relations, prédictions, historique de
probabilité, flux de veille, et préférences.

**Comportement hors-ligne :** l'application fonctionne intégralement sans réseau.
Seuls deux usages le requièrent : le mode Vigie (appel à l'API) et le premier
téléchargement du modèle spaCy.

---

## Dépannage

**`ModuleNotFoundError: No module named 'yaml'` (ou autre module).**
Les dépendances ne sont pas installées dans le bon interpréteur (piège fréquent
sous Windows avec plusieurs Python). Vérifiez et corrigez :

```bash
python -c "import sys; print(sys.executable)"
python -m pip install -r requirements.txt
python -c "import yaml, numpy, requests, matplotlib, spacy, PySide6; print('OK')"
```

Si vous compilez, lancez aussi PyInstaller via `python -m PyInstaller ...` pour
forcer le même interpréteur.

**L'exécutable compilé plante au démarrage.**
Recompilez en remplaçant `--noconsole` par `--console` : une fenêtre affichera
la trace d'erreur exacte.

**Le mode Vigie renvoie « Expecting value: line 1 column 1 ».**
Le serveur répond en flux SSE ; l'application le gère désormais (reconstruction
du contenu). Vérifiez sinon l'URL de base et la clé via **Tester la connexion**.

**Build trop volumineux.**
Utilisez la commande de build ci-dessus (exclusions de sklearn/scipy/modules Qt)
et, au besoin, UPX.

**Première Vigie lente ou détection inopérante.**
Au premier usage, spaCy télécharge `fr_core_news_sm` (connexion requise une
fois). Ensuite, tout est local.

---

## Philosophie

- **Local-first** — toutes les données sont stockées localement.
- **Hors-ligne** — l'arbre fonctionne sans connexion ; seuls la Vigie et le
  premier téléchargement du modèle touchent le réseau.
- **IA périphérique** — le LLM assiste l'extraction, il ne décide pas ; le
  raisonnement, les probabilités et la traçabilité restent déterministes et
  locaux.
- **L'humain garde la main** — détection, application d'un flux, réorganisation :
  tout passe par une validation.
- **Aucune télémétrie**, aucun compte, aucune synchronisation.

---

## Feuille de route

- Arbitrage plus fin des cas ambigus en détection de projet.
- Enrichissement des courbes temporelles de la Vue Rivière.
- Intégration plus poussée avec IRIS-Station selon les usages réels.
