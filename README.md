# Yggdrasil — Tableau de bord prospectif

Yggdrasil importe les exports structurés d'IRIS-Station et les fait vivre sous
forme d'un **arbre des possibles** interactif, mis à jour en continu via le
mode Vigie. Le tronc représente les faits, les branches les hypothèses, et les
sous-branches les bifurcations possibles.

## Installation

### Exécutable
1. Télécharger `Yggdrasil` (ou `Yggdrasil.exe` sous Windows) depuis les releases.
2. Lancer l'exécutable (Windows peut afficher un avertissement SmartScreen).
3. À la première ouverture : importer un fichier `.md` exporté depuis IRIS-Station.

### Depuis les sources
```bash
pip install -r requirements.txt
python -m spacy download fr_core_news_sm   # facultatif, sinon récupéré au 1er usage
python main.py
```

## Fonctionnalités

- **Vue Arbre** — arbre organique top-down interactif ; nœuds proportionnels aux
  probabilités, couleurs par horizon, halo sur les bifurcations critiques.
  Zoom (Ctrl+molette), déplacement, rattachement par glisser-déposer,
  repli/dépli (double-clic), panneau latéral détaillé.
- **Vue Rivière** — courbes d'évolution temporelle des probabilités.
- **Mode Vigie** — flux entrants analysés par IA (FantasyAI Cloud) pour une
  mise à jour bayésienne ; détection automatique du projet concerné (spaCy).
- **Export IRIS-Station** — round-tripping complet (`.md`).

## Raccourcis

| Raccourci | Action |
|---|---|
| Ctrl+I | Importer un export IRIS-Station |
| Ctrl+E | Exporter pour IRIS-Station |
| Ctrl+Z / Ctrl+Maj+Z | Annuler / Rétablir |
| F11 | Plein écran |
| Ctrl+Q | Quitter |

## Configuration

- Menu **Configuration > API FantasyAI** : renseigner la clé API, le modèle et
  l'URL de base. La clé est stockée localement (QSettings), jamais en base.

## Construction de l'exécutable

```bash
./build.sh        # Linux / macOS
build.bat         # Windows
```
Le binaire est généré dans `dist/`. Une icône par défaut (`yggdrasil_icon.png`
/ `.ico`) est fournie ; vous pouvez la remplacer par la vôtre.

## Philosophie

- **Local-first** — toutes les données sont stockées localement (SQLite dans
  `~/.yggdrasil/`).
- **Hors-ligne** — l'arbre fonctionne sans connexion ; seuls le mode Vigie et le
  premier téléchargement du modèle spaCy nécessitent le réseau.
- **IA périphérique** — le LLM est un assistant, pas le cœur du système.
- **Aucune télémétrie**, aucun compte, aucune synchronisation.
