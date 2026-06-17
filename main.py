#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yggdrasil — Tableau de bord prospectif (Phase 1).

Phase 1 : squelette de l'application, import des exports YAML IRIS-Station,
et persistance dans une base SQLite. La visualisation en arbre, le moteur
bayésien, le mode Vigie et l'export sont implémentés dans les phases ultérieures.

Lancement : python main.py
"""

import os
import re
import sys
import math
import sqlite3
import datetime
from pathlib import Path

import yaml

from PySide6.QtCore import (
    Qt,
    QPointF,
    QRectF,
    QSettings,
    QThread,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Chemins (compatibilité PyInstaller)
# ---------------------------------------------------------------------------

def resource_path(relative_path):
    """Retourne le chemin absolu d'une ressource, compatible PyInstaller.

    En mode "frozen" (PyInstaller), les ressources sont extraites dans
    ``sys._MEIPASS`` ; sinon on se base sur le dossier du script.
    """
    try:
        base_path = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    except AttributeError:
        base_path = Path(__file__).resolve().parent
    return base_path / relative_path


def data_dir():
    """Dossier de données persistant de l'application (créé si absent)."""
    d = Path.home() / ".yggdrasil"
    d.mkdir(parents=True, exist_ok=True)
    return d


DB_PATH = data_dir() / "yggdrasil.db"


# ---------------------------------------------------------------------------
# Schéma de base de données
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created TEXT,
    import_date TEXT DEFAULT CURRENT_TIMESTAMP,
    yaml_source_path TEXT,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS horizons (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    label TEXT NOT NULL,
    years_min INTEGER,
    years_max INTEGER
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    label TEXT NOT NULL,
    description TEXT,
    prior_probability REAL,
    posterior_probability REAL,
    horizon_id INTEGER REFERENCES horizons(id),
    parent_hypothesis_id INTEGER REFERENCES hypotheses(id),
    bifurcation_node_id INTEGER,
    pos_x REAL DEFAULT 0,
    pos_y REAL DEFAULT 0,
    collapsed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bifurcation_nodes (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    label TEXT NOT NULL,
    condition_text TEXT,
    parent_hypothesis_id INTEGER REFERENCES hypotheses(id),
    horizon_id INTEGER REFERENCES horizons(id),
    tension_score REAL,
    pos_x REAL DEFAULT 0,
    pos_y REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bifurcation_links (
    id INTEGER PRIMARY KEY,
    bifurcation_node_id INTEGER REFERENCES bifurcation_nodes(id),
    hypothesis_id INTEGER REFERENCES hypotheses(id)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    content TEXT NOT NULL,
    source TEXT,
    credibility INTEGER DEFAULT 3,
    diagnosticite REAL DEFAULT 0.0,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ach_scores (
    id INTEGER PRIMARY KEY,
    evidence_id INTEGER REFERENCES evidence_items(id),
    hypothesis_id INTEGER REFERENCES hypotheses(id),
    consistency_score TEXT,
    p_e_given_h REAL,
    p_e_given_not_h REAL,
    bayes_factor REAL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    name TEXT NOT NULL,
    type TEXT,
    degree INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    source_entity_id INTEGER REFERENCES entities(id),
    target_entity_id INTEGER REFERENCES entities(id),
    relation_type TEXT,
    weight REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    question TEXT NOT NULL,
    probability REAL,
    deadline TEXT,
    category TEXT,
    outcome INTEGER,
    brier_score REAL,
    resolved_at TEXT,
    resolution_source TEXT
);

CREATE TABLE IF NOT EXISTS probability_history (
    id INTEGER PRIMARY KEY,
    hypothesis_id INTEGER REFERENCES hypotheses(id),
    date TEXT NOT NULL,
    value REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vigie_flows (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id),
    raw_text TEXT,
    parsed_json TEXT,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
    user_validated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# ---------------------------------------------------------------------------
# Couche d'accès aux données
# ---------------------------------------------------------------------------

class Database:
    """Encapsule la connexion SQLite et les opérations de persistance.

    La connexion est ouverte en mode autocommit (isolation_level=None) afin
    que les transactions explicites BEGIN/COMMIT/ROLLBACK du code d'import
    se comportent de façon prévisible.
    """

    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.create_schema()

    def execute(self, sql, params=()):
        """Exécute une requête SQL et retourne le curseur."""
        return self.conn.execute(sql, params)

    def create_schema(self):
        """Crée toutes les tables si elles n'existent pas encore."""
        self.conn.executescript(SCHEMA)

    def close(self):
        """Ferme proprement la connexion."""
        self.conn.close()

    def serialize(self):
        """Retourne l'image binaire complète de la base (pour l'historique d'annulation)."""
        return self.conn.serialize()

    def restore(self, data):
        """Restaure la base depuis une image binaire (annuler / rétablir)."""
        self.conn.close()
        self.path.write_bytes(data)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # -- Lecture ------------------------------------------------------------

    def list_projects(self):
        """Retourne la liste des projets (les plus récents d'abord)."""
        return self.execute(
            "SELECT id, name, created, import_date FROM projects "
            "ORDER BY import_date DESC, id DESC"
        ).fetchall()

    def create_empty_project(self, name):
        """Crée un projet vide (utilisé par la détection Vigie) et retourne son id."""
        cur = self.execute(
            "INSERT INTO projects (name, created) VALUES (?, ?)",
            (name, datetime.date.today().isoformat()),
        )
        return cur.lastrowid

    def delete_project(self, project_id):
        """Supprime un projet et toutes ses données dépendantes (transaction).

        Les FK entre hypothèses et nœuds de bifurcation sont circulaires ;
        on désactive temporairement leur vérification (no-op à l'intérieur
        d'une transaction, donc fait juste avant/après) pour pouvoir effacer
        dans n'importe quel ordre.
        """
        db = self
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN TRANSACTION")
        try:
            db.execute(
                "DELETE FROM ach_scores WHERE evidence_id IN "
                "(SELECT id FROM evidence_items WHERE project_id = ?) "
                "OR hypothesis_id IN "
                "(SELECT id FROM hypotheses WHERE project_id = ?)",
                (project_id, project_id),
            )
            db.execute(
                "DELETE FROM probability_history WHERE hypothesis_id IN "
                "(SELECT id FROM hypotheses WHERE project_id = ?)",
                (project_id,),
            )
            db.execute(
                "DELETE FROM bifurcation_links WHERE bifurcation_node_id IN "
                "(SELECT id FROM bifurcation_nodes WHERE project_id = ?) "
                "OR hypothesis_id IN "
                "(SELECT id FROM hypotheses WHERE project_id = ?)",
                (project_id, project_id),
            )
            for table in (
                "relations", "entities", "evidence_items", "bifurcation_nodes",
                "hypotheses", "horizons", "predictions", "vigie_flows",
            ):
                db.execute(
                    f"DELETE FROM {table} WHERE project_id = ?", (project_id,)
                )
            db.execute(
                "DELETE FROM settings WHERE key = ? OR key = ?",
                (f"ranking_{project_id}", f"ref_vector_{project_id}"),
            )
            db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")

    def project_summary(self, project_id):
        """Retourne un dict résumant un projet (compteurs inclus)."""
        proj = self.execute(
            "SELECT name, created, import_date FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not proj:
            return None

        def count(sql):
            return self.execute(sql, (project_id,)).fetchone()[0]

        return {
            "name": proj["name"],
            "created": proj["created"],
            "import_date": proj["import_date"],
            "hypotheses": count(
                "SELECT COUNT(*) FROM hypotheses WHERE project_id = ?"
            ),
            "evidence": count(
                "SELECT COUNT(*) FROM evidence_items WHERE project_id = ?"
            ),
            "bifurcations": count(
                "SELECT COUNT(*) FROM bifurcation_nodes WHERE project_id = ?"
            ),
            "predictions": count(
                "SELECT COUNT(*) FROM predictions WHERE project_id = ?"
            ),
            "entities": count(
                "SELECT COUNT(*) FROM entities WHERE project_id = ?"
            ),
        }

    # -- Import -------------------------------------------------------------

    def import_project(self, data, source_path):
        """Insère toutes les données d'un export YAML dans la BDD.

        L'ensemble est inséré dans une transaction atomique : en cas
        d'erreur, un ROLLBACK annule l'import partiel. Retourne l'identifiant
        du projet créé.
        """
        db = self
        db.execute("BEGIN TRANSACTION")
        try:
            # 1. Projet --------------------------------------------------
            proj = data.get("project", {}) or {}
            name = proj.get("name") or data.get("name") or "Projet sans nom"
            created = proj.get("created", "") or ""
            cursor = db.execute(
                "INSERT INTO projects (name, created, yaml_source_path) "
                "VALUES (?, ?, ?)",
                (name, created, source_path),
            )
            project_id = cursor.lastrowid

            # 2. Horizons ------------------------------------------------
            # Les horizons peuvent être à la racine ou imbriqués dans project.
            horizons = data.get("horizons") or proj.get("horizons") or []
            horizon_map = {}
            for hz in horizons:
                cur = db.execute(
                    "INSERT INTO horizons (project_id, label, years_min, "
                    "years_max) VALUES (?, ?, ?, ?)",
                    (
                        project_id,
                        hz.get("label", ""),
                        hz.get("years_min"),
                        hz.get("years_max"),
                    ),
                )
                horizon_map[hz.get("id")] = cur.lastrowid

            # 3. Hypothèses (passe 1 : insertion) ------------------------
            hyp_map = {}
            for hyp in data.get("hypotheses", []) or []:
                hz_id = horizon_map.get(hyp.get("horizon"))
                cur = db.execute(
                    "INSERT INTO hypotheses (project_id, label, description, "
                    "prior_probability, posterior_probability, horizon_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        hyp.get("label", ""),
                        hyp.get("description", ""),
                        hyp.get("prior"),
                        hyp.get("posterior"),
                        hz_id,
                    ),
                )
                hyp_map[hyp.get("id")] = cur.lastrowid

                for entry in hyp.get("probability_history", []) or []:
                    db.execute(
                        "INSERT INTO probability_history (hypothesis_id, "
                        "date, value) VALUES (?, ?, ?)",
                        (cur.lastrowid, entry.get("date"), entry.get("value")),
                    )

            # 3bis. Rattacher les parents (passe 2) ----------------------
            for hyp in data.get("hypotheses", []) or []:
                if hyp.get("parent"):
                    hyp_id = hyp_map.get(hyp.get("id"))
                    parent_id = hyp_map.get(hyp.get("parent"))
                    if hyp_id and parent_id:
                        db.execute(
                            "UPDATE hypotheses SET parent_hypothesis_id = ? "
                            "WHERE id = ?",
                            (parent_id, hyp_id),
                        )

            # 4. Preuves + scores ACH + bayésien -------------------------
            for ev in data.get("evidence_items", []) or []:
                cur = db.execute(
                    "INSERT INTO evidence_items (project_id, content, source, "
                    "credibility, diagnosticite) VALUES (?, ?, ?, ?, ?)",
                    (
                        project_id,
                        ev.get("content", ""),
                        ev.get("source", ""),
                        ev.get("credibility", 3),
                        ev.get("diagnosticite", 0.0),
                    ),
                )
                ev_db_id = cur.lastrowid

                for hyp_ref, score in (ev.get("ach_scores", {}) or {}).items():
                    h_db_id = hyp_map.get(hyp_ref)
                    if h_db_id and score:
                        db.execute(
                            "INSERT INTO ach_scores (evidence_id, "
                            "hypothesis_id, consistency_score) "
                            "VALUES (?, ?, ?)",
                            (ev_db_id, h_db_id, score),
                        )

                for hyp_ref, vals in (ev.get("bayesian", {}) or {}).items():
                    h_db_id = hyp_map.get(hyp_ref)
                    if not h_db_id:
                        continue
                    # Met à jour la ligne ACH existante, ou la crée si absente.
                    row = db.execute(
                        "SELECT id FROM ach_scores WHERE evidence_id = ? "
                        "AND hypothesis_id = ?",
                        (ev_db_id, h_db_id),
                    ).fetchone()
                    if row:
                        db.execute(
                            "UPDATE ach_scores SET p_e_given_h = ?, "
                            "p_e_given_not_h = ?, bayes_factor = ? "
                            "WHERE id = ?",
                            (
                                vals.get("p_e_given_h"),
                                vals.get("p_e_given_not_h"),
                                vals.get("bayes_factor"),
                                row["id"],
                            ),
                        )
                    else:
                        db.execute(
                            "INSERT INTO ach_scores (evidence_id, "
                            "hypothesis_id, p_e_given_h, p_e_given_not_h, "
                            "bayes_factor) VALUES (?, ?, ?, ?, ?)",
                            (
                                ev_db_id,
                                h_db_id,
                                vals.get("p_e_given_h"),
                                vals.get("p_e_given_not_h"),
                                vals.get("bayes_factor"),
                            ),
                        )

            # 5. Entités -------------------------------------------------
            entity_map = {}
            for ent in data.get("entities", []) or []:
                cur = db.execute(
                    "INSERT INTO entities (project_id, name, type, degree) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        project_id,
                        ent.get("name", ""),
                        ent.get("type", "MISC"),
                        ent.get("degree", 0),
                    ),
                )
                entity_map[ent.get("name")] = cur.lastrowid

            # 6. Relations -----------------------------------------------
            for rel in data.get("relations", []) or []:
                src_id = entity_map.get(rel.get("source"))
                tgt_id = entity_map.get(rel.get("target"))
                if src_id and tgt_id:
                    db.execute(
                        "INSERT INTO relations (project_id, source_entity_id, "
                        "target_entity_id, relation_type, weight) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            project_id,
                            src_id,
                            tgt_id,
                            rel.get("type", "CO_OCCURS"),
                            rel.get("weight", 1.0),
                        ),
                    )

            # 7. Prédictions ---------------------------------------------
            for pred in data.get("predictions", []) or []:
                db.execute(
                    "INSERT INTO predictions (project_id, question, "
                    "probability, deadline, category, outcome, brier_score) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        pred.get("question", ""),
                        pred.get("probability"),
                        pred.get("deadline"),
                        pred.get("category", ""),
                        pred.get("outcome"),
                        pred.get("brier"),
                    ),
                )

            # 8. Nœuds de bifurcation ------------------------------------
            for bn in data.get("bifurcation_nodes", []) or []:
                hz_id = horizon_map.get(bn.get("horizon"))
                parent_hyp_id = hyp_map.get(bn.get("parent_hypothesis"))
                cur = db.execute(
                    "INSERT INTO bifurcation_nodes (project_id, label, "
                    "condition_text, parent_hypothesis_id, horizon_id, "
                    "tension_score) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        bn.get("label", ""),
                        bn.get("condition", ""),
                        parent_hyp_id,
                        hz_id,
                        bn.get("tension_score"),
                    ),
                )
                bn_db_id = cur.lastrowid

                for hyp_ref in bn.get("leads_to", []) or []:
                    hyp_id = hyp_map.get(hyp_ref)
                    if hyp_id:
                        db.execute(
                            "INSERT INTO bifurcation_links "
                            "(bifurcation_node_id, hypothesis_id) "
                            "VALUES (?, ?)",
                            (bn_db_id, hyp_id),
                        )
                        db.execute(
                            "UPDATE hypotheses SET bifurcation_node_id = ? "
                            "WHERE id = ?",
                            (bn_db_id, hyp_id),
                        )

            db.execute("COMMIT")
            return project_id
        except Exception:
            db.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------------------
# Parsing des exports IRIS-Station
# ---------------------------------------------------------------------------

def parse_iris_export(filepath):
    """Parse un export IRIS-Station (.md) et retourne un dict structuré.

    Le fichier est un Markdown avec un front matter YAML délimité par ``---``.
    Lève ValueError si aucun bloc YAML n'est trouvé, ou yaml.YAMLError si le
    YAML est malformé.
    """
    content = Path(filepath).read_text(encoding="utf-8")

    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        raise ValueError(
            "Format YAML non trouvé — fichier non compatible IRIS-Station"
        )

    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("Le bloc YAML ne contient pas de structure valide")
    return data


# ---------------------------------------------------------------------------
# spaCy (chargé paresseusement — non utilisé en Phase 1)
# ---------------------------------------------------------------------------

_SPACY_MODEL = None


def get_spacy_model():
    """Retourne le modèle spaCy fr_core_news_sm, en le téléchargeant au besoin.

    Chargé paresseusement : il n'est sollicité qu'à partir de la Phase 6
    (détection automatique de projet). L'application démarre donc sans réseau.
    """
    global _SPACY_MODEL
    if _SPACY_MODEL is not None:
        return _SPACY_MODEL
    import spacy
    try:
        _SPACY_MODEL = spacy.load("fr_core_news_sm")
    except OSError:
        from spacy.cli import download as spacy_download
        spacy_download("fr_core_news_sm")
        _SPACY_MODEL = spacy.load("fr_core_news_sm")
    return _SPACY_MODEL


# ---------------------------------------------------------------------------
# Moteur bayésien, tension et bascules de classement (Phase 4)
# ---------------------------------------------------------------------------

def bayesian_update(prior, likelihoods):
    """Mise à jour bayésienne en log-odds.

    prior : probabilité a priori (0 < prior < 1)
    likelihoods : liste de tuples (p_e_given_h, p_e_given_not_h)
    Retourne la probabilité a posteriori, bornée pour éviter les extrêmes.
    """
    if prior is None or prior <= 0 or prior >= 1:
        return prior
    log_odds = math.log(prior / (1 - prior))
    for p_e_h, p_e_not_h in likelihoods:
        if p_e_h and p_e_not_h and p_e_not_h > 0:
            log_odds += math.log(p_e_h / p_e_not_h)
    posterior = 1 / (1 + math.exp(-log_odds))
    if posterior > 0.9999:
        posterior = 0.9999
    elif posterior < 0.0001:
        posterior = 0.0001
    return posterior


def compute_posterior_for_hypothesis(db, hypothesis_id):
    """Recalcule la probabilité a posteriori d'une hypothèse depuis ses preuves."""
    row = db.execute(
        "SELECT prior_probability FROM hypotheses WHERE id = ?", (hypothesis_id,)
    ).fetchone()
    if not row:
        return None

    prior = row[0]
    likelihoods = db.execute(
        "SELECT p_e_given_h, p_e_given_not_h FROM ach_scores "
        "WHERE hypothesis_id = ? AND p_e_given_h IS NOT NULL "
        "AND p_e_given_not_h IS NOT NULL",
        (hypothesis_id,),
    ).fetchall()

    if not likelihoods:
        return prior

    return bayesian_update(prior, [(l[0], l[1]) for l in likelihoods])


def _binary_entropy(p):
    """Entropie binaire normalisée (0 à 1), maximale à p = 0,5."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def compute_tension(db, bn_id):
    """Calcule la tension d'un nœud de bifurcation depuis ses hypothèses filles.

    Heuristique (la formule n'est pas fixée par IRIS-Station) : entropie
    normalisée des probabilités des branches. Une fille → fork binaire
    {p, 1-p} ; plusieurs filles → entropie normalisée de leurs poids relatifs.
    Retourne None si aucune fille n'a de probabilité connue.
    """
    rows = db.execute(
        "SELECT h.posterior_probability FROM bifurcation_links bl "
        "JOIN hypotheses h ON bl.hypothesis_id = h.id "
        "WHERE bl.bifurcation_node_id = ?",
        (bn_id,),
    ).fetchall()
    posts = [r[0] for r in rows if r[0] is not None]
    if not posts:
        return None
    if len(posts) == 1:
        return round(_binary_entropy(posts[0]), 3)
    total = sum(posts)
    if total <= 0:
        return None
    probs = [p / total for p in posts]
    ent = -sum(p * math.log2(p) for p in probs if p > 0)
    norm = ent / math.log2(len(probs))
    return round(norm, 3)


def recompute_all_tensions(db, project_id):
    """Recalcule et persiste la tension de tous les nœuds de bifurcation d'un projet."""
    for bn in db.execute(
        "SELECT id FROM bifurcation_nodes WHERE project_id = ?", (project_id,)
    ).fetchall():
        t = compute_tension(db, bn["id"])
        db.execute(
            "UPDATE bifurcation_nodes SET tension_score = ? WHERE id = ?",
            (t, bn["id"]),
        )


def detect_ranking_switches(db, project_id):
    """Détecte les hypothèses ayant changé de rang depuis le dernier calcul."""
    current = db.execute(
        "SELECT id, label, posterior_probability FROM hypotheses "
        "WHERE project_id = ? ORDER BY posterior_probability DESC",
        (project_id,),
    ).fetchall()

    old_ranking = db.execute(
        "SELECT value FROM settings WHERE key = ?", (f"ranking_{project_id}",)
    ).fetchone()

    new_labels = [h["label"] for h in current]
    new_ranking_str = ",".join(new_labels)

    switches = []
    if old_ranking:
        old_labels = old_ranking[0].split(",")
        for i, label in enumerate(new_labels):
            if label in old_labels:
                old_pos = old_labels.index(label)
                if old_pos != i:
                    switches.append({
                        "label": label,
                        "old_rank": old_pos + 1,
                        "new_rank": i + 1,
                        "direction": "up" if i < old_pos else "down",
                    })

    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"ranking_{project_id}", new_ranking_str),
    )
    return switches


# ---------------------------------------------------------------------------
# Mode Vigie — appel FantasyAI (Phase 5)
# ---------------------------------------------------------------------------

FANTASYAI_BASE = "https://www.fantasyai.cloud/api/v1"
FANTASYAI_MODELS_FALLBACK = ["openai/gpt-4o-mini", "anthropic/claude-3.5-haiku"]

VIGIE_SYSTEM_PROMPT = """Tu es un analyste structuré. Tu reçois un texte et la liste des hypothèses
d'un projet existant. Pour chaque hypothèse, estime la vraisemblance de ce nouveau texte.
Retourne UNIQUEMENT un objet JSON, pas de texte avant/après.

RÈGLES :
- p_e_given_h : probabilité d'observer ce texte si l'hypothèse est vraie (0.1 à 0.9)
- p_e_given_not_h : probabilité d'observer ce texte si l'hypothèse est fausse (0.1 à 0.9)
- Ne mets pas 0.0 ou 1.0 (risque de division par zéro)
- Si le texte ne concerne pas une hypothèse, mets p_e_given_h = 0.5, p_e_given_not_h = 0.5
- credibility : 1 (peu fiable) à 5 (très fiable)
- entities : extrais les personnes, organisations, lieux mentionnés
- N'invente RIEN qui n'est pas dans le texte.
"""


def clamp_likelihood(v):
    """Borne une vraisemblance dans [0.01, 0.99]."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.5
    return max(0.01, min(0.99, v))


def build_vigie_prompt(db, project_id, text):
    """Construit le prompt utilisateur pour l'analyse d'un flux Vigie."""
    proj = db.execute(
        "SELECT name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    project_name = proj["name"] if proj else "Projet"

    hypotheses = db.execute(
        "SELECT label, description, posterior_probability FROM hypotheses "
        "WHERE project_id = ? ORDER BY id",
        (project_id,),
    ).fetchall()

    hyp_list = "\n".join(
        f"- {h['label']}: {(h['description'] or '')[:200]} "
        f"(probabilité actuelle: "
        f"{(h['posterior_probability'] or 0):.1%})"
        for h in hypotheses
    )

    return f"""Projet : {project_name}

Hypothèses existantes :
{hyp_list}

Nouveau texte :
{text}

Retourne UN OBJET JSON avec cette structure exacte :
{{
  "evidence": {{
    "content": "résumé neutre du fait nouveau apporté par ce texte",
    "source": "source citée ou 'flux anonyme'",
    "credibility": 3
  }},
  "likelihoods": {{
    "H1": {{"p_e_given_h": 0.7, "p_e_given_not_h": 0.3}},
    "H2": {{"p_e_given_h": 0.5, "p_e_given_not_h": 0.5}}
  }},
  "entities": [{{"name": "Nom", "type": "PERSON"}}],
  "relations": [{{"source": "A", "target": "B", "type": "AFFIRMS"}}],
  "predictions": [{{"question": "...", "probability": 0.6, "deadline": "2027-01-01"}}],
  "narrative": "En une phrase, ce que ce texte change à l'analyse."
}}"""


def _parse_sse_content(text):
    """Reconstitue le contenu d'une réponse en flux SSE (chunks OpenAI).

    Chaque ligne « data: {chunk} » porte un delta ; on concatène les
    ``choices[0].delta.content`` (ou ``message.content``) jusqu'à « [DONE] ».
    Retourne None si rien d'exploitable.
    """
    import json as _json

    parts = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            obj = _json.loads(payload)
        except ValueError:
            continue
        try:
            choice = obj["choices"][0]
        except (KeyError, IndexError, TypeError):
            continue
        piece = (choice.get("delta") or {}).get("content")
        if piece is None:
            piece = (choice.get("message") or {}).get("content")
        if isinstance(piece, str):
            parts.append(piece)
    return "".join(parts) if parts else None


def call_fantasyai(prompt_system, prompt_user, api_key, model,
                   base_url=FANTASYAI_BASE, timeout=60):
    """Appelle l'endpoint chat de FantasyAI et retourne le contenu texte.

    Gère les réponses JSON classiques ET les réponses en flux SSE (le serveur
    peut streamer par défaut). Lève requests.exceptions.HTTPError sur erreur
    HTTP, Timeout sur dépassement, ValueError explicite si le corps est
    inexploitable.
    """
    import requests

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": prompt_user},
        ],
        "temperature": 0.3,
        "stream": False,  # on veut une réponse unique
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{base_url}/chat/completions",
        headers=headers, json=body, timeout=timeout,
    )
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError:
        # Le serveur a streamé malgré tout : reconstituer depuis le flux SSE.
        text = response.text or ""
        if "data:" in text[:64]:
            content = _parse_sse_content(text)
            if content:
                return content
        snippet = text.strip()[:300].replace("\n", " ")
        raise ValueError(
            f"Le serveur a répondu sans JSON (HTTP {response.status_code}). "
            f"Vérifiez l'URL de base et la clé API. "
            f"Début de la réponse : {snippet!r}"
        )

    if isinstance(data, dict) and data.get("error"):
        raise ValueError(f"Erreur renvoyée par l'API : {data['error']}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError(
            "Structure de réponse inattendue : "
            f"{str(data)[:300]}"
        )


def fetch_fantasyai_models(api_key, base_url=FANTASYAI_BASE, timeout=15):
    """Récupère la liste des modèles disponibles ; lève en cas d'échec réseau."""
    import requests

    headers = {"Authorization": f"Bearer {api_key}"}
    response = requests.get(f"{base_url}/models", headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    items = data.get("data", data if isinstance(data, list) else [])
    models = []
    for it in items:
        if isinstance(it, dict) and it.get("id"):
            models.append(it["id"])
        elif isinstance(it, str):
            models.append(it)
    return models or list(FANTASYAI_MODELS_FALLBACK)


def parse_vigie_json(raw):
    """Parse la réponse JSON du LLM, avec repli regex si du texte l'entoure."""
    import json as _json

    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if match:
        return _json.loads(match.group(0))
    raise ValueError("Réponse non JSON")


# ---------------------------------------------------------------------------
# Détection automatique de projet (Phase 6)
# ---------------------------------------------------------------------------

SPACY_DIM = 96  # largeur du tok2vec de fr_core_news_sm


def normalize_entity_name(name):
    """Normalise un nom d'entité pour comparaison (minuscules, espaces réduits)."""
    return " ".join((name or "").strip().lower().split())


def compute_project_reference_vector(db, project_id):
    """Vecteur de référence d'un projet : moyenne des vecteurs spaCy de ses textes."""
    import numpy as np

    nlp = get_spacy_model()
    texts = []
    for row in db.execute(
        "SELECT content FROM evidence_items WHERE project_id = ?", (project_id,)
    ):
        if row[0]:
            texts.append(row[0])
    for row in db.execute(
        "SELECT description FROM hypotheses WHERE project_id = ? "
        "AND description IS NOT NULL", (project_id,)
    ):
        if row[0]:
            texts.append(row[0])
    row = db.execute(
        "SELECT name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row and row[0]:
        texts.append(row[0])

    if not texts:
        return np.zeros(SPACY_DIM)
    vectors = [nlp(t).vector for t in texts]
    return np.mean(vectors, axis=0)


def get_or_compute_ref_vector(db, project_id):
    """Retourne le vecteur de référence (depuis settings ou recalculé et stocké)."""
    import numpy as np
    import json as _json

    row = db.execute(
        "SELECT value FROM settings WHERE key = ?",
        (f"ref_vector_{project_id}",),
    ).fetchone()
    if row:
        try:
            return np.array(_json.loads(row[0]))
        except (ValueError, TypeError):
            pass
    vec = compute_project_reference_vector(db, project_id)
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"ref_vector_{project_id}", _json.dumps(vec.tolist())),
    )
    return vec


def invalidate_ref_vector(db, project_id):
    """Invalide le vecteur de référence d'un projet (recalcul à la prochaine détection)."""
    db.execute(
        "DELETE FROM settings WHERE key = ?", (f"ref_vector_{project_id}",)
    )


def _best_project_by_similarity(db, text):
    """Retourne (project_id, score) du projet le plus proche par similarité cosinus."""
    import numpy as np

    nlp = get_spacy_model()
    flow = nlp(text).vector
    nf = np.linalg.norm(flow)
    best_id, best = None, 0.0
    for row in db.execute("SELECT id FROM projects WHERE is_active = 1"):
        ref = get_or_compute_ref_vector(db, row["id"])
        nr = np.linalg.norm(ref)
        if nf == 0 or nr == 0:
            continue
        score = float(np.dot(flow, ref) / (nf * nr))
        if score > best:
            best, best_id = score, row["id"]
    return best_id, best


def detect_project_for_flow(db, text, threshold=0.45):
    """Retourne (project_id, confidence) si au-dessus du seuil, sinon (None, score)."""
    best_id, best = _best_project_by_similarity(db, text)
    if best_id is not None and best >= threshold:
        return best_id, best
    return None, best


def entity_overlap(db, project_id, text):
    """Ratio d'entités du texte déjà connues dans le projet (0 à 1)."""
    nlp = get_spacy_model()
    doc = nlp(text)
    flow_entities = {normalize_entity_name(ent.text) for ent in doc.ents}
    flow_entities.discard("")
    if not flow_entities:
        return 0.0
    project_entities = {
        normalize_entity_name(row["name"])
        for row in db.execute(
            "SELECT name FROM entities WHERE project_id = ?", (project_id,)
        )
    }
    return len(flow_entities & project_entities) / len(flow_entities)


def suggest_project(db, text, auto=0.60, low=0.45, overlap_min=0.30):
    """Décide quel projet proposer pour un flux.

    Retourne un dict {mode, project_id, confidence, by_entities} où mode vaut :
    - "unavailable" : spaCy indisponible (pas de détection)
    - "auto"        : confiance ≥ auto → bascule automatique
    - "suggest"     : confiance intermédiaire OU fort chevauchement d'entités
    - "none_match"  : aucune correspondance fiable
    """
    try:
        get_spacy_model()
    except Exception:
        return {"mode": "unavailable", "project_id": None, "confidence": 0.0}

    best_id, score = _best_project_by_similarity(db, text)
    if best_id is None:
        return {"mode": "none_match", "project_id": None, "confidence": score}
    if score >= auto:
        return {"mode": "auto", "project_id": best_id, "confidence": score}
    if score >= low:
        return {"mode": "suggest", "project_id": best_id, "confidence": score}

    # Repli par chevauchement d'entités : entités communes fortes malgré un
    # vocabulaire différent → proposer quand même le projet.
    best_ov_id, best_ov = None, 0.0
    for row in db.execute("SELECT id FROM projects WHERE is_active = 1"):
        ov = entity_overlap(db, row["id"], text)
        if ov > best_ov:
            best_ov, best_ov_id = ov, row["id"]
    if best_ov_id is not None and best_ov > overlap_min:
        return {
            "mode": "suggest", "project_id": best_ov_id,
            "confidence": score, "by_entities": True,
        }
    return {"mode": "none_match", "project_id": best_id, "confidence": score}


# ---------------------------------------------------------------------------
# Export IRIS-Station — round-tripping (Phase 7)
# ---------------------------------------------------------------------------

def _round_or_none(v, n=3):
    return round(v, n) if v is not None else None


def export_to_iris_format(db, project_id):
    """Génère un dict au format YGGDRASIL_IMPORT v1, réimportable sans perte.

    Les références (parent, leads_to, ach_scores, relations…) utilisent les
    identifiants/ noms internes du fichier (« H{id} », « HZ{id} », noms
    d'entités) afin que IRIS → Yggdrasil → IRIS soit lossless, indépendamment
    des id SQLite réattribués à la réimportation.
    """
    proj = db.execute(
        "SELECT name, created FROM projects WHERE id = ?", (project_id,)
    ).fetchone()

    # Horizons
    horizons = []
    hz_ref = {}
    for row in db.execute(
        "SELECT id, label, years_min, years_max FROM horizons "
        "WHERE project_id = ? ORDER BY id", (project_id,)
    ):
        ref = f"HZ{row['id']}"
        hz_ref[row["id"]] = ref
        horizons.append({
            "id": ref, "label": row["label"],
            "years_min": row["years_min"], "years_max": row["years_max"],
        })

    # Référence d'identifiant pour chaque hypothèse
    hyp_ref = {
        row["id"]: f"H{row['id']}"
        for row in db.execute(
            "SELECT id FROM hypotheses WHERE project_id = ?", (project_id,)
        )
    }

    # Hypothèses
    hypotheses = []
    for row in db.execute(
        "SELECT * FROM hypotheses WHERE project_id = ? ORDER BY id", (project_id,)
    ):
        history = [
            {"date": ph["date"], "value": _round_or_none(ph["value"])}
            for ph in db.execute(
                "SELECT date, value FROM probability_history "
                "WHERE hypothesis_id = ? ORDER BY date", (row["id"],)
            )
        ]
        post = row["posterior_probability"]
        if post is None:
            post = compute_posterior_for_hypothesis(db, row["id"])
        hypotheses.append({
            "id": hyp_ref[row["id"]],
            "label": row["label"],
            "description": row["description"] or "",
            "prior": _round_or_none(row["prior_probability"]),
            "posterior": _round_or_none(post),
            "horizon": hz_ref.get(row["horizon_id"]),
            "parent": hyp_ref.get(row["parent_hypothesis_id"]),
            "probability_history": history,
        })

    # Preuves (+ ACH + bayésien)
    evidence_items = []
    for row in db.execute(
        "SELECT * FROM evidence_items WHERE project_id = ? ORDER BY id",
        (project_id,)
    ):
        ach_scores = {}
        bayesian = {}
        for sc in db.execute(
            "SELECT * FROM ach_scores WHERE evidence_id = ?", (row["id"],)
        ):
            ref = hyp_ref.get(sc["hypothesis_id"])
            if not ref:
                continue
            if sc["consistency_score"]:
                ach_scores[ref] = sc["consistency_score"]
            if sc["p_e_given_h"] is not None:
                bayesian[ref] = {
                    "p_e_given_h": sc["p_e_given_h"],
                    "p_e_given_not_h": sc["p_e_given_not_h"],
                    "bayes_factor": sc["bayes_factor"],
                }
        evidence_items.append({
            "id": f"E{row['id']}",
            "content": row["content"],
            "source": row["source"] or "",
            "credibility": row["credibility"],
            "diagnosticite": row["diagnosticite"] or 0.0,
            "ach_scores": ach_scores,
            "bayesian": bayesian,
        })

    # Nœuds de bifurcation
    bifurcation_nodes = []
    for row in db.execute(
        "SELECT * FROM bifurcation_nodes WHERE project_id = ? ORDER BY id",
        (project_id,)
    ):
        leads = [
            hyp_ref[l["hypothesis_id"]]
            for l in db.execute(
                "SELECT hypothesis_id FROM bifurcation_links "
                "WHERE bifurcation_node_id = ?", (row["id"],)
            )
            if l["hypothesis_id"] in hyp_ref
        ]
        bifurcation_nodes.append({
            "id": f"BN{row['id']}",
            "label": row["label"],
            "condition": row["condition_text"] or "",
            "leads_to": leads,
            "parent_hypothesis": hyp_ref.get(row["parent_hypothesis_id"]),
            "horizon": hz_ref.get(row["horizon_id"]),
            "tension_score": row["tension_score"],
        })

    # Entités
    entities = [
        {"name": r["name"], "type": r["type"], "degree": r["degree"]}
        for r in db.execute(
            "SELECT name, type, degree FROM entities WHERE project_id = ? "
            "ORDER BY id", (project_id,)
        )
    ]

    # Relations (références par nom d'entité)
    relations = []
    for r in db.execute(
        "SELECT source_entity_id, target_entity_id, relation_type, weight "
        "FROM relations WHERE project_id = ?", (project_id,)
    ):
        s = db.execute(
            "SELECT name FROM entities WHERE id = ?", (r["source_entity_id"],)
        ).fetchone()
        t = db.execute(
            "SELECT name FROM entities WHERE id = ?", (r["target_entity_id"],)
        ).fetchone()
        if s and t:
            relations.append({
                "source": s["name"], "target": t["name"],
                "type": r["relation_type"], "weight": r["weight"],
            })

    # Prédictions
    predictions = []
    for r in db.execute(
        "SELECT question, probability, deadline, category, outcome, brier_score "
        "FROM predictions WHERE project_id = ? ORDER BY id", (project_id,)
    ):
        predictions.append({
            "question": r["question"],
            "probability": r["probability"],
            "deadline": r["deadline"],
            "category": r["category"] or "",
            "outcome": r["outcome"],
            "brier": r["brier_score"],
        })

    return {
        "YGGDRASIL_IMPORT": "v1",
        "project": {
            "name": proj["name"], "created": proj["created"],
            "horizons": horizons,
        },
        "hypotheses": hypotheses,
        "evidence_items": evidence_items,
        "bifurcation_nodes": bifurcation_nodes,
        "entities": entities,
        "relations": relations,
        "predictions": predictions,
    }


def build_markdown_report(data):
    """Construit un rapport Markdown lisible à partir du dict d'export."""
    def pct(v):
        return f"{v:.0%}" if isinstance(v, (int, float)) else "—"

    p = data["project"]
    lines = [f"# Rapport — {p['name']}", "*Exporté depuis Yggdrasil*", ""]

    lines.append("## Hypothèses")
    for h in data["hypotheses"]:
        lines.append(
            f"- **{h['label']}** — {h['description']} "
            f"(a priori {pct(h['prior'])} → a posteriori {pct(h['posterior'])})"
        )
    lines.append("")

    lines.append("## Preuves")
    for e in data["evidence_items"]:
        lines.append(
            f"- **{e['id']}** — {e['content']} "
            f"(source : {e['source']}, crédibilité {e['credibility']})"
        )
    lines.append("")

    if data["bifurcation_nodes"]:
        lines.append("## Nœuds de bifurcation")
        for b in data["bifurcation_nodes"]:
            t = b["tension_score"]
            ts = f"{t:.2f}" if isinstance(t, (int, float)) else "—"
            lines.append(f"- **{b['label']}** — {b['condition']} (tension {ts})")
        lines.append("")

    if data["predictions"]:
        lines.append("## Prédictions")
        for pr in data["predictions"]:
            lines.append(f"- {pr['question']} — {pct(pr['probability'])}")
        lines.append("")

    return "\n".join(lines)


def export_iris_markdown(data):
    """Assemble le front matter YAML + le rapport Markdown en un seul document."""
    yaml_block = yaml.dump(
        data, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    report = build_markdown_report(data)
    return f"---\n{yaml_block}---\n\n{report}\n"


# ---------------------------------------------------------------------------
# Vue Arbre — constantes et nœuds graphiques (Phase 2)
# ---------------------------------------------------------------------------

# Couleurs d'horizon, attribuées par rang temporel (du plus court au plus long).
HORIZON_COLORS = ["#E74C3C", "#F39C12", "#3498DB"]  # HZ1 rouge, HZ2 orange, HZ3 bleu
HORIZON_DEFAULT_COLOR = "#95A5A6"  # gris : sans horizon
BIFURCATION_COLOR = "#8E44AD"      # violet
BIFURCATION_NULL_COLOR = "#BDC3C7"  # gris : tension inconnue
EDGE_COLOR = "#7F8C8D"
SCENE_BG = "#FAFAFA"

# Palette « arbre des possibles » (Phase 2, rendu organique)
TRUNK_COLOR = "#5C3A1E"     # écorce du tronc (foncé)
BRANCH_COLOR = "#7C5436"    # écorce des branches
BRANCH_LIGHT = "#9C7248"    # branches fines
FOLIAGE_COLOR = "#7FB069"   # feuillage (vert)
SKY_TOP = "#EAF4FB"         # haut du ciel
SKY_BOTTOM = "#F6F1E7"      # bas (terre claire)
KNOT_COLOR = "#6E4B2A"      # nœud de bifurcation (fourche)


def horizon_color_map(db, project_id):
    """Associe chaque horizon_id à une couleur selon son rang temporel."""
    rows = db.execute(
        "SELECT id FROM horizons WHERE project_id = ? "
        "ORDER BY years_min IS NULL, years_min, years_max, id",
        (project_id,),
    ).fetchall()
    cmap = {}
    for rank, row in enumerate(rows):
        cmap[row["id"]] = HORIZON_COLORS[min(rank, len(HORIZON_COLORS) - 1)]
    return cmap


def hypothesis_radius(posterior):
    """Rayon d'un nœud d'hypothèse en fonction de sa probabilité a posteriori."""
    p = posterior if posterior is not None else 0.5
    return 30 + 40 * p


def branch_thickness(posterior):
    """Épaisseur d'une arête en fonction de la probabilité de l'enfant."""
    p = posterior if posterior is not None else 0.5
    return 1 + 6 * p


def branch_width(posterior):
    """Épaisseur (px) d'une branche selon la probabilité de l'hypothèse fille."""
    p = posterior if posterior is not None else 0.5
    return 5 + 18 * p


class BranchItem(QGraphicsPathItem):
    """Limbe d'arbre : forme effilée et courbe entre un parent et une fille.

    Le parent est l'extrémité large, la fille l'extrémité fine. Le chemin est
    reconstruit à chaque déplacement via ``set_endpoints``.
    """

    def __init__(self, w_parent, w_child, color):
        super().__init__()
        self.w_parent = w_parent
        self.w_child = w_child
        self.setBrush(QBrush(QColor(color)))
        self.setPen(QPen(QColor(color).darker(115), 1))
        self.setZValue(-1)

    def set_endpoints(self, p0, p1):
        """Construit un limbe effilé courbe de p0 (large) vers p1 (fin)."""
        dx = p1.x() - p0.x()
        dy = p1.y() - p0.y()
        length = math.hypot(dx, dy) or 1.0
        nx = -dy / length  # perpendiculaire unitaire
        ny = dx / length
        wp = self.w_parent / 2
        wc = self.w_child / 2
        # Points de contrôle biaisés verticalement : la branche part droite
        # puis s'incurve vers la fille (allure organique).
        c1x, c1y = p0.x() + dx * 0.15, p0.y() + dy * 0.5
        c2x, c2y = p1.x() - dx * 0.15, p0.y() + dy * 0.5
        w1 = wp * 0.7 + wc * 0.3
        w2 = wp * 0.3 + wc * 0.7

        path = QPainterPath()
        path.moveTo(p0.x() + nx * wp, p0.y() + ny * wp)
        path.cubicTo(
            c1x + nx * w1, c1y + ny * w1,
            c2x + nx * w2, c2y + ny * w2,
            p1.x() + nx * wc, p1.y() + ny * wc,
        )
        path.lineTo(p1.x() - nx * wc, p1.y() - ny * wc)
        path.cubicTo(
            c2x - nx * w2, c2y - ny * w2,
            c1x - nx * w1, c1y - ny * w1,
            p0.x() - nx * wp, p0.y() - ny * wp,
        )
        path.closeSubpath()
        self.setPath(path)


class _InteractiveNode:
    """Mixin ajoutant déplacement, sélection, clic et double-clic aux nœuds.

    Placé avant la classe Qt dans l'ordre d'héritage, ses surcharges délèguent
    au comportement Qt via ``super()``. L'interaction est armée par
    ``init_interaction`` une fois le nœud rattaché à sa vue.
    """

    def init_interaction(self, view, key):
        """Active le déplacement et enregistre la vue + la clé du nœud."""
        self.view = view
        self.key = key
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.OpenHandCursor)

    def itemChange(self, change, value):
        if (
            change == QGraphicsItem.ItemPositionHasChanged
            and getattr(self, "view", None) is not None
        ):
            self.view.on_node_moved(self)
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        if getattr(self, "view", None) is not None:
            self.view.on_node_clicked(self)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if getattr(self, "view", None) is not None:
            start = getattr(self, "_press_pos", self.pos())
            self.view.on_node_dropped(self, start)

    def mouseDoubleClickEvent(self, event):
        if getattr(self, "view", None) is not None:
            self.view.on_node_double_clicked(self)
        super().mouseDoubleClickEvent(event)


class HypothesisNode(_InteractiveNode, QGraphicsEllipseItem):
    """Hypothèse : « fruit » lustré dimensionné par le posterior, coloré par horizon.

    Les feuilles (hypothèses terminales) reçoivent un halo de feuillage.
    """

    def __init__(self, node_id, x, y, posterior, color, label, sublabel,
                 tooltip, collapsed=False, leaf=False):
        r = hypothesis_radius(posterior)
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.node_id = node_id
        self.radius = r
        self.setPos(x, y)

        # Feuillage derrière les hypothèses terminales (plusieurs taches vertes)
        if leaf:
            import random
            rng = random.Random(node_id)  # déterministe par nœud
            for _ in range(5):
                rad = r * rng.uniform(0.5, 0.85)
                ox = rng.uniform(-r, r)
                oy = rng.uniform(-r, r)
                leafblob = QGraphicsEllipseItem(
                    ox - rad, oy - rad, 2 * rad, 2 * rad, self
                )
                g = QColor(FOLIAGE_COLOR)
                g.setAlpha(120)
                leafblob.setBrush(QBrush(g))
                leafblob.setPen(QPen(Qt.NoPen))
                leafblob.setFlag(QGraphicsItem.ItemStacksBehindParent, True)

        # Corps « fruit » : dégradé radial lustré dans la couleur d'horizon
        grad = QRadialGradient(-r * 0.3, -r * 0.3, r * 1.6)
        base = QColor(color)
        grad.setColorAt(0.0, base.lighter(135))
        grad.setColorAt(1.0, base.darker(115))
        self.setBrush(QBrush(grad))
        pen = QPen(base.darker(160), 2)
        if collapsed:
            pen.setStyle(Qt.DashLine)  # bordure pointillée = sous-arbre replié
        self.setPen(pen)
        self.setToolTip(tooltip)
        self.setZValue(2)

        # Label centré dans le fruit
        text = QGraphicsSimpleTextItem(label, self)
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)
        text.setFont(font)
        text.setBrush(QBrush(QColor("white")))
        br = text.boundingRect()
        text.setPos(-br.width() / 2, -br.height() / 2)

        # Étiquette sous le fruit : nom court de l'hypothèse
        if sublabel:
            cap = QGraphicsSimpleTextItem(sublabel, self)
            cap_font = QFont()
            cap_font.setPointSize(9)
            cap.setFont(cap_font)
            cap.setBrush(QBrush(QColor("#2C3E50")))
            cbr = cap.boundingRect()
            cap.setPos(-cbr.width() / 2, r + 6)


class BifurcationNode(_InteractiveNode, QGraphicsEllipseItem):
    """Nœud de bifurcation : « nœud » d'écorce où la branche fourche.

    Halo coloré si la tension est critique ; aspect grisé + « ? » si inconnue.
    """

    R = 11  # rayon du nœud

    def __init__(self, node_id, x, y, tension, label, tooltip):
        r = self.R
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.node_id = node_id
        self.setPos(x, y)
        self.setToolTip(tooltip)
        self.setZValue(2)

        if tension is None:
            self.setBrush(QBrush(QColor(BIFURCATION_NULL_COLOR)))
            self.setPen(QPen(QColor("#7F8C8D"), 2))
            shown = "?"
        else:
            grad = QRadialGradient(-r * 0.3, -r * 0.3, r * 1.6)
            base = QColor(KNOT_COLOR)
            grad.setColorAt(0.0, base.lighter(130))
            grad.setColorAt(1.0, base.darker(120))
            self.setBrush(QBrush(grad))
            self.setPen(QPen(base.darker(150), 2))
            shown = ""

        if shown:
            text = QGraphicsSimpleTextItem(shown, self)
            font = QFont()
            font.setBold(True)
            font.setPointSize(9)
            text.setFont(font)
            text.setBrush(QBrush(QColor("white")))
            br = text.boundingRect()
            text.setPos(-br.width() / 2, -br.height() / 2)


class TreeView(QGraphicsView):
    """Vue graphique interactive de l'arbre prospectif (top-down).

    Phase 3 : zoom molette (Ctrl), pan (hand-drag / molette), déplacement des
    nœuds avec arêtes temps réel, rattachement par drop, collapse/expand.
    """

    ZOOM_MIN = 0.2
    ZOOM_MAX = 3.0
    ZOOM_STEP = 1.15
    GRID = 100  # pas de la grille de snap

    def __init__(self, db):
        self.scene_obj = QGraphicsScene()
        super().__init__(self.scene_obj)
        self.db = db
        self.current_project_id = None

        # Callbacks branchés par la fenêtre principale
        self.on_select = None       # (key) -> None : nœud cliqué
        self.status_cb = None       # (texte) -> None : message de statut
        self.before_change = None   # () -> None : avant une mutation (undo)

        # Suivi des éléments pour la mise à jour temps réel
        self.node_items = {}        # key -> QGraphicsItem
        self.edges = []             # [{"item", "a", "b"}]
        self.halos = {}             # key bifurcation -> halo QGraphicsEllipseItem
        self._anims = []            # animations de badge de bascule (anti-GC)

        sky = QLinearGradient(0, 0, 0, 1)
        sky.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
        sky.setColorAt(0.0, QColor(SKY_TOP))
        sky.setColorAt(1.0, QColor(SKY_BOTTOM))
        self.scene_obj.setBackgroundBrush(QBrush(sky))
        self.setRenderHint(QPainter.Antialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Le hand-drag remplace le scroll classique ; les nœuds déplaçables
        # restent prioritaires sur le pan.
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)

    # -- Zoom / pan --------------------------------------------------------

    def wheelEvent(self, event):
        """Ctrl+molette = zoom ; molette seule = défilement de la vue."""
        if event.modifiers() & Qt.ControlModifier:
            current = self.transform().m11()
            factor = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1 / self.ZOOM_STEP
            target = max(self.ZOOM_MIN, min(self.ZOOM_MAX, current * factor))
            if abs(target - current) > 1e-6:
                self.scale(target / current, target / current)
                self._emit_zoom()
            event.accept()
        else:
            delta = event.angleDelta()
            hbar = self.horizontalScrollBar()
            vbar = self.verticalScrollBar()
            if event.modifiers() & Qt.ShiftModifier:
                hbar.setValue(hbar.value() - delta.y())
            else:
                vbar.setValue(vbar.value() - delta.y())
                hbar.setValue(hbar.value() - delta.x())
            event.accept()

    def _emit_zoom(self):
        if self.status_cb:
            pct = int(round(self.transform().m11() * 100))
            self.status_cb(f"Zoom : {pct}%")

    # -- Données -----------------------------------------------------------

    def _get_node_data(self, node_id):
        """Retourne les champs utiles d'une hypothèse pour le layout."""
        return self.db.execute(
            "SELECT id, label, description, posterior_probability, "
            "horizon_id, collapsed FROM hypotheses WHERE id = ?",
            (node_id,),
        ).fetchone()

    def _count_leaves(self, node_id, visited=None):
        """Nombre de feuilles d'un sous-arbre (utilitaire de dimensionnement)."""
        if visited is None:
            visited = set()
        if node_id in visited:
            return 0
        visited.add(node_id)
        children = self.db.execute(
            "SELECT id FROM hypotheses WHERE parent_hypothesis_id = ? "
            "AND bifurcation_node_id IS NULL",
            (node_id,),
        ).fetchall()
        bifurcations = self.db.execute(
            "SELECT id FROM bifurcation_nodes WHERE parent_hypothesis_id = ?",
            (node_id,),
        ).fetchall()
        leaves = 0
        for c in children:
            leaves += self._count_leaves(c["id"], visited)
        for bn in bifurcations:
            links = self.db.execute(
                "SELECT hypothesis_id FROM bifurcation_links "
                "WHERE bifurcation_node_id = ?",
                (bn["id"],),
            ).fetchall()
            for ln in links:
                leaves += self._count_leaves(ln["hypothesis_id"], visited)
        return max(1, leaves)

    def _horizon_color_map(self, project_id):
        """Associe chaque horizon_id à une couleur selon son rang temporel."""
        return horizon_color_map(self.db, project_id)

    # -- Layout top-down ---------------------------------------------------

    def layout_tree(self, project_id):
        """Positionne récursivement les nœuds de l'arbre en top-down."""
        roots = self.db.execute(
            "SELECT id, label, posterior_probability, horizon_id, collapsed "
            "FROM hypotheses WHERE project_id = ? "
            "AND parent_hypothesis_id IS NULL "
            "ORDER BY posterior_probability DESC",
            (project_id,),
        ).fetchall()

        if not roots:
            return []

        spacing_x = 200
        spacing_y = 150
        current_x = 0
        layout_items = []
        placed = set()  # garde anti-cycle : chaque hypothèse placée une seule fois

        for root in roots:
            if root["id"] in placed:
                continue
            subtree_width = self._subtree_width(root["id"], spacing_x)
            # Racine centrée au-dessus de sa propre largeur de sous-arbre
            # (les enfants étant centrés sous le parent dans _position_node).
            root_x = current_x + subtree_width / 2
            self._position_node(
                root["id"], root_x, 0, spacing_x, spacing_y,
                layout_items, placed,
            )
            current_x += subtree_width + spacing_x

        return layout_items

    def _position_node(self, node_id, x, y, spacing_x, spacing_y,
                       layout_items, visited):
        """Positionne récursivement un nœud et ses enfants (centrés sous lui).

        ``visited`` est l'ensemble partagé des hypothèses déjà placées : il
        évite à la fois le double placement et la récursion infinie quand une
        bifurcation renvoie vers un ancêtre (cycle).
        """
        if node_id in visited:
            return
        visited.add(node_id)
        node = self._get_node_data(node_id)

        layout_items.append({
            "id": node["id"],
            "type": "hypothesis",
            "x": x,
            "y": y,
            "label": node["label"],
            "posterior": node["posterior_probability"],
            "horizon": node["horizon_id"],
            "collapsed": node["collapsed"],
        })
        self.db.execute(
            "UPDATE hypotheses SET pos_x = ?, pos_y = ? WHERE id = ?",
            (x, y, node_id),
        )

        if node["collapsed"]:
            return

        bifurcations = self.db.execute(
            "SELECT id, label, tension_score FROM bifurcation_nodes "
            "WHERE parent_hypothesis_id = ?",
            (node_id,),
        ).fetchall()
        direct_children = self.db.execute(
            "SELECT id FROM hypotheses WHERE parent_hypothesis_id = ? "
            "AND bifurcation_node_id IS NULL",
            (node_id,),
        ).fetchall()

        # Unités à placer sous le nœud : bifurcations (avec leurs filles) et
        # enfants directs, côte à côte. Un nœud peut avoir les deux.
        units = []
        for bn in bifurcations:
            units.append(("bn", bn, self._bifurcation_width(bn["id"], spacing_x)))
        for c in direct_children:
            units.append(("hyp", c, self._subtree_width(c["id"], spacing_x)))

        if not units:
            return

        total = sum(u[2] for u in units)
        cursor = x - total / 2
        for kind, obj, width in units:
            center = cursor + width / 2
            if kind == "hyp":
                self._position_node(
                    obj["id"], center, y + spacing_y,
                    spacing_x, spacing_y, layout_items, visited,
                )
            else:
                bn_y = y + spacing_y * 0.7
                layout_items.append({
                    "id": obj["id"],
                    "type": "bifurcation",
                    "x": center,
                    "y": bn_y,
                    "label": obj["label"],
                    "tension": obj["tension_score"],
                })
                self.db.execute(
                    "UPDATE bifurcation_nodes SET pos_x = ?, pos_y = ? "
                    "WHERE id = ?",
                    (center, bn_y, obj["id"]),
                )
                links = self.db.execute(
                    "SELECT h.id FROM bifurcation_links bl "
                    "JOIN hypotheses h ON bl.hypothesis_id = h.id "
                    "WHERE bl.bifurcation_node_id = ?",
                    (obj["id"],),
                ).fetchall()
                if links:
                    total_w = sum(
                        self._subtree_width(ln["id"], spacing_x) for ln in links
                    )
                    start_lx = center - total_w / 2
                    child_y2 = bn_y + spacing_y * 0.7
                    current_lx = start_lx
                    for link in links:
                        lw = self._subtree_width(link["id"], spacing_x)
                        lx = current_lx + lw / 2
                        self._position_node(
                            link["id"], lx, child_y2,
                            spacing_x, spacing_y, layout_items, visited,
                        )
                        current_lx += lw
            cursor += width

    def _bifurcation_width(self, bn_id, spacing_x, visited=None):
        """Largeur réservée pour un nœud de bifurcation et ses filles."""
        if visited is None:
            visited = set()
        links = self.db.execute(
            "SELECT h.id FROM bifurcation_links bl "
            "JOIN hypotheses h ON bl.hypothesis_id = h.id "
            "WHERE bl.bifurcation_node_id = ?",
            (bn_id,),
        ).fetchall()
        w = sum(self._subtree_width(ln["id"], spacing_x, visited) for ln in links)
        return max(200, w)

    def _subtree_width(self, node_id, spacing_x, visited=None):
        """Largeur (px) nécessaire pour un sous-arbre.

        Les hypothèses rattachées via un nœud de bifurcation sont exclues du
        comptage des enfants directs (elles sont comptées via les liens de
        bifurcation), pour éviter tout double comptage. ``visited`` garantit la
        terminaison même si une bifurcation renvoie vers un ancêtre (cycle).
        """
        if visited is None:
            visited = set()
        if node_id in visited:
            return 0
        visited.add(node_id)

        node = self._get_node_data(node_id)
        if node is None or node["collapsed"]:
            return 200

        children = self.db.execute(
            "SELECT id FROM hypotheses WHERE parent_hypothesis_id = ? "
            "AND bifurcation_node_id IS NULL",
            (node_id,),
        ).fetchall()

        bifurcations = self.db.execute(
            "SELECT id FROM bifurcation_nodes WHERE parent_hypothesis_id = ?",
            (node_id,),
        ).fetchall()

        child_width = sum(
            self._subtree_width(c["id"], spacing_x, visited) for c in children
        )

        bn_width = sum(
            self._bifurcation_width(bn["id"], spacing_x, visited)
            for bn in bifurcations
        )

        return max(200, child_width + bn_width)

    # -- Rendu -------------------------------------------------------------

    def render_project(self, project_id):
        """Rend le projet ; en cas d'erreur, affiche un message plutôt qu'un vide."""
        try:
            self._render_impl(project_id)
        except Exception as exc:
            self.scene_obj.clear()
            self.node_items = {}
            self.edges = []
            self.halos = {}
            msg = self.scene_obj.addText(
                "Impossible d'afficher l'arbre pour ce projet.\n"
                f"Détail : {exc}"
            )
            msg.setDefaultTextColor(QColor("#C0392B"))
            self.scene_obj.setSceneRect(msg.boundingRect().adjusted(-40, -40, 40, 40))
            self.resetTransform()
            self.centerOn(msg)
            if self.status_cb:
                self.status_cb("Erreur d'affichage de l'arbre.")

    def _render_impl(self, project_id):
        """Efface la scène, calcule le layout et dessine nœuds + arêtes."""
        self.scene_obj.clear()
        self.node_items = {}
        self.edges = []
        self.halos = {}
        self.current_project_id = project_id
        if project_id is None:
            return

        layout_items = self.layout_tree(project_id)
        if not layout_items:
            placeholder = self.scene_obj.addText(
                "Aucune hypothèse à afficher pour ce projet."
            )
            placeholder.setDefaultTextColor(QColor("#7F8C8D"))
            return

        color_map = self._horizon_color_map(project_id)

        ROOT_LIFT = 70      # surélève les racines au-dessus du tronc
        TRUNK_LEN = 170     # longueur du tronc sous les racines
        TRUNK_W_BASE = 58
        TRUNK_W_TOP = 26

        # Positions de rendu : l'arbre pousse vers le HAUT (y écran = -y).
        render_pos = {}
        for it in layout_items:
            key = ("h" if it["type"] == "hypothesis" else "b", it["id"])
            render_pos[key] = (it["x"], -it["y"] - ROOT_LIFT)

        roots = [it for it in layout_items
                 if it["type"] == "hypothesis" and it["y"] == 0]
        root_xs = [it["x"] for it in roots] or [0]
        cx = (min(root_xs) + max(root_xs)) / 2
        trunk_top = (cx, 0.0)
        trunk_base = (cx, float(TRUNK_LEN))

        post_by_id = {}
        for h in self.db.execute(
            "SELECT id, posterior_probability FROM hypotheses WHERE project_id = ?",
            (project_id,),
        ).fetchall():
            post_by_id[h["id"]] = h["posterior_probability"]

        # 1. Branches (sous les nœuds), enregistrées pour mise à jour live
        self._build_branches(project_id, render_pos, post_by_id, roots, trunk_top)

        # 2. Tronc (statique) + étiquette « Faits »
        trunk = BranchItem(TRUNK_W_BASE, TRUNK_W_TOP, TRUNK_COLOR)
        trunk.set_endpoints(QPointF(*trunk_base), QPointF(*trunk_top))
        trunk.setZValue(-2)
        self.scene_obj.addItem(trunk)
        n_ev = self.db.execute(
            "SELECT COUNT(*) c FROM evidence_items WHERE project_id = ?",
            (project_id,),
        ).fetchone()["c"]
        faits = QGraphicsSimpleTextItem(f"Faits ({n_ev} preuves)")
        ff = QFont()
        ff.setBold(True)
        ff.setPointSize(11)
        faits.setFont(ff)
        faits.setBrush(QBrush(QColor(TRUNK_COLOR)))
        fbr = faits.boundingRect()
        faits.setPos(cx - fbr.width() / 2, TRUNK_LEN + 8)
        faits.setZValue(1)
        self.scene_obj.addItem(faits)

        # Hypothèses « internes » (parentes d'au moins une branche) ; les autres
        # sont des feuilles, ornées de feuillage.
        internal = set()
        for e in self.edges:
            if e["a"] is not None and e["a"][0] == "h":
                internal.add(e["a"])

        # 3. Nœuds
        for it in layout_items:
            key = ("h" if it["type"] == "hypothesis" else "b", it["id"])
            rx, ry = render_pos[key]
            if it["type"] == "hypothesis":
                node = self._get_node_data(it["id"])
                color = color_map.get(it["horizon"], HORIZON_DEFAULT_COLOR)
                posterior = it["posterior"]
                desc = node["description"] or ""
                pct = f"{posterior:.1%}" if posterior is not None else "—"
                tooltip = f"{node['label']}\n{desc}\nProbabilité : {pct}"
                short = (
                    node["label"] if len(node["label"]) <= 24
                    else node["label"][:23] + "…"
                )
                gnode = HypothesisNode(
                    it["id"], rx, ry, posterior, color,
                    f"H{it['id']}", short, tooltip,
                    collapsed=bool(it["collapsed"]),
                    leaf=key not in internal,
                )
                self.scene_obj.addItem(gnode)
                self.node_items[key] = gnode
                gnode.init_interaction(self, key)
            else:
                tension = it["tension"]
                bn = self.db.execute(
                    "SELECT label, condition_text FROM bifurcation_nodes "
                    "WHERE id = ?",
                    (it["id"],),
                ).fetchone()
                cond = bn["condition_text"] or ""
                t_str = f"{tension:.2f}" if tension is not None else "inconnue"
                tooltip = f"{bn['label']}\n{cond}\nTension : {t_str}"
                if tension is not None and tension > 0.7:
                    halo = QGraphicsEllipseItem(-26, -26, 52, 52)
                    halo.setPos(rx, ry)
                    glow = QColor("#E67E22")
                    glow.setAlpha(70)
                    halo.setBrush(QBrush(glow))
                    halo.setPen(QPen(Qt.NoPen))
                    halo.setZValue(0)
                    self.scene_obj.addItem(halo)
                    self.halos[key] = halo
                gnode = BifurcationNode(
                    it["id"], rx, ry, tension, bn["label"], tooltip,
                )
                self.scene_obj.addItem(gnode)
                self.node_items[key] = gnode
                gnode.init_interaction(self, key)

        # 4. Vue : tout l'arbre visible
        rect = self.scene_obj.itemsBoundingRect().adjusted(-80, -80, 80, 80)
        self.scene_obj.setSceneRect(rect)
        self.resetTransform()
        self.fitInView(rect, Qt.KeepAspectRatio)
        self._emit_zoom()

    def _build_branches(self, project_id, render_pos, post_by_id, roots, trunk_top):
        """Crée les branches organiques (clés des extrémités enregistrées)."""
        # Tronc -> racines
        for it in roots:
            ckey = ("h", it["id"])
            self._branch(None, ckey, trunk_top, render_pos[ckey],
                         26, branch_width(it["posterior"]), TRUNK_COLOR)

        for h in self.db.execute(
            "SELECT id FROM hypotheses WHERE project_id = ?",
            (project_id,),
        ).fetchall():
            hkey = ("h", h["id"])
            if hkey not in render_pos:
                continue
            hpt = render_pos[hkey]
            hpost = post_by_id.get(h["id"])

            for bn in self.db.execute(
                "SELECT id FROM bifurcation_nodes WHERE parent_hypothesis_id = ?",
                (h["id"],),
            ).fetchall():
                bkey = ("b", bn["id"])
                if bkey in render_pos:
                    bpt = render_pos[bkey]
                    self._branch(hkey, bkey, hpt, bpt,
                                 branch_width(hpost), 10, BRANCH_COLOR)
                    for ln in self.db.execute(
                        "SELECT h.id FROM bifurcation_links bl "
                        "JOIN hypotheses h ON bl.hypothesis_id = h.id "
                        "WHERE bl.bifurcation_node_id = ?",
                        (bn["id"],),
                    ).fetchall():
                        ckey = ("h", ln["id"])
                        if ckey in render_pos and ckey != hkey:
                            wch = branch_width(post_by_id.get(ln["id"]))
                            self._branch(bkey, ckey, bpt, render_pos[ckey],
                                         12, wch, BRANCH_LIGHT)

            for c in self.db.execute(
                "SELECT id FROM hypotheses WHERE parent_hypothesis_id = ? "
                "AND bifurcation_node_id IS NULL",
                (h["id"],),
            ).fetchall():
                ckey = ("h", c["id"])
                if ckey in render_pos:
                    wch = branch_width(post_by_id.get(c["id"]))
                    self._branch(hkey, ckey, hpt, render_pos[ckey],
                                 max(branch_width(hpost), wch + 4), wch,
                                 BRANCH_COLOR)

    def _branch(self, a_key, b_key, a_pt, b_pt, w0, w1, color):
        """Ajoute une branche effilée à la scène et l'enregistre."""
        item = BranchItem(w0, w1, color)
        item.set_endpoints(QPointF(a_pt[0], a_pt[1]), QPointF(b_pt[0], b_pt[1]))
        self.scene_obj.addItem(item)
        self.edges.append({
            "item": item,
            "a": a_key,
            "b": b_key,
            "a_fixed": a_pt if a_key is None else None,
            "b_fixed": b_pt if b_key is None else None,
        })

    # -- Interaction : déplacement, drop, clic, collapse -------------------

    def _endpoint(self, key, fixed):
        """Position d'une extrémité de branche (nœud mobile ou point fixe du tronc)."""
        if key is not None:
            item = self.node_items.get(key)
            if item is not None:
                return item.pos()
        if fixed is not None:
            return QPointF(fixed[0], fixed[1])
        return None

    def on_node_moved(self, node):
        """Appelé pendant le drag : déplace le halo et reconstruit les branches."""
        key = getattr(node, "key", None)
        if key in self.halos:
            self.halos[key].setPos(node.pos())
        self._update_edges_for(key)

    def _update_edges_for(self, key):
        """Reconstruit les branches touchant un nœud."""
        for e in self.edges:
            if e["a"] == key or e["b"] == key:
                a = self._endpoint(e["a"], e.get("a_fixed"))
                b = self._endpoint(e["b"], e.get("b_fixed"))
                if a is not None and b is not None:
                    e["item"].set_endpoints(a, b)

    def _refresh_all_edges(self):
        """Recale toutes les branches depuis les positions courantes."""
        for e in self.edges:
            a = self._endpoint(e["a"], e.get("a_fixed"))
            b = self._endpoint(e["b"], e.get("b_fixed"))
            if a is not None and b is not None:
                e["item"].set_endpoints(a, b)

    def on_node_clicked(self, node):
        """Notifie la fenêtre principale pour alimenter le panneau latéral."""
        if self.on_select:
            self.on_select(node.key)

    def on_node_double_clicked(self, node):
        """Double-clic sur une hypothèse : replie/déplie son sous-arbre."""
        if not isinstance(node, HypothesisNode):
            return
        row = self.db.execute(
            "SELECT collapsed FROM hypotheses WHERE id = ?", (node.node_id,)
        ).fetchone()
        if row is None:
            return
        new_val = 0 if row["collapsed"] else 1
        if self.before_change:
            self.before_change()
        self.db.execute(
            "UPDATE hypotheses SET collapsed = ? WHERE id = ?",
            (new_val, node.node_id),
        )
        self.render_project(self.current_project_id)

    def on_node_dropped(self, node, start_pos):
        """Relâchement : tente un rattachement, sinon snappe et sauvegarde."""
        target = self._drop_target(node)

        if isinstance(node, HypothesisNode) and target is not None:
            if isinstance(target, BifurcationNode):
                if self._confirm(
                    "Rattacher cette hypothèse à ce nœud de bifurcation ?"
                ):
                    if self.before_change:
                        self.before_change()
                    self._attach_to_bifurcation(node.node_id, target.node_id)
                self.render_project(self.current_project_id)
                return
            if isinstance(target, HypothesisNode):
                if (
                    target.node_id != node.node_id
                    and not self._is_descendant(node.node_id, target.node_id)
                ):
                    tlabel = self.db.execute(
                        "SELECT label FROM hypotheses WHERE id = ?",
                        (target.node_id,),
                    ).fetchone()["label"]
                    if self._confirm(
                        f"Faire de cette hypothèse un enfant de « {tlabel} » ?"
                    ):
                        if self.before_change:
                            self.before_change()
                        self._make_child(node.node_id, target.node_id)
                self.render_project(self.current_project_id)
                return

        # Pas de rattachement : snap + sauvegarde + déplacement du sous-arbre
        new_pos = self._snap(node.pos())
        dx = new_pos.x() - start_pos.x()
        dy = new_pos.y() - start_pos.y()
        node.setPos(new_pos)
        self._save_node_pos(node)
        self._reposition_children(node, dx, dy)
        self._refresh_all_edges()

    def _drop_target(self, node):
        """Retourne le nœud (hypothèse/bifurcation) sous le centre du nœud déplacé."""
        for it in self.scene_obj.items(node.scenePos()):
            if it is node:
                continue
            if isinstance(it, (HypothesisNode, BifurcationNode)):
                return it
        return None

    def _snap(self, p):
        """Aligne une position sur la grille invisible (pas GRID)."""
        g = self.GRID
        return QPointF(round(p.x() / g) * g, round(p.y() / g) * g)

    def _save_node_pos(self, node):
        """Persiste la position d'un nœud (hypothèse ou bifurcation)."""
        p = node.pos()
        if isinstance(node, HypothesisNode):
            self.db.execute(
                "UPDATE hypotheses SET pos_x = ?, pos_y = ? WHERE id = ?",
                (p.x(), p.y(), node.node_id),
            )
        else:
            self.db.execute(
                "UPDATE bifurcation_nodes SET pos_x = ?, pos_y = ? WHERE id = ?",
                (p.x(), p.y(), node.node_id),
            )

    def _reposition_children(self, node, dx, dy):
        """Décale le sous-arbre d'un nœud du même delta, pour rester cohérent."""
        if dx == 0 and dy == 0:
            return
        for key in self._collect_descendants(node):
            item = self.node_items.get(key)
            if item is None:
                continue
            item.moveBy(dx, dy)
            if key in self.halos:
                self.halos[key].moveBy(dx, dy)
            self._save_node_pos(item)

    def _collect_descendants(self, node):
        """Clés de tous les descendants d'un nœud (hypothèses + bifurcations).

        Protégé contre les cycles (bifurcation renvoyant vers un ancêtre) :
        chaque nœud n'est visité qu'une fois et le nœud de départ est exclu.
        """
        keys = []
        seen_h = {node.node_id} if isinstance(node, HypothesisNode) else set()
        seen_b = {node.node_id} if isinstance(node, BifurcationNode) else set()

        def walk_hyp(hid):
            for c in self.db.execute(
                "SELECT id FROM hypotheses WHERE parent_hypothesis_id = ? "
                "AND bifurcation_node_id IS NULL",
                (hid,),
            ).fetchall():
                if c["id"] in seen_h:
                    continue
                seen_h.add(c["id"])
                keys.append(("h", c["id"]))
                walk_hyp(c["id"])
            for bn in self.db.execute(
                "SELECT id FROM bifurcation_nodes WHERE parent_hypothesis_id = ?",
                (hid,),
            ).fetchall():
                if bn["id"] in seen_b:
                    continue
                seen_b.add(bn["id"])
                keys.append(("b", bn["id"]))
                walk_bn(bn["id"])

        def walk_bn(bnid):
            for ln in self.db.execute(
                "SELECT hypothesis_id FROM bifurcation_links "
                "WHERE bifurcation_node_id = ?",
                (bnid,),
            ).fetchall():
                if ln["hypothesis_id"] in seen_h:
                    continue
                seen_h.add(ln["hypothesis_id"])
                keys.append(("h", ln["hypothesis_id"]))
                walk_hyp(ln["hypothesis_id"])

        if isinstance(node, BifurcationNode):
            walk_bn(node.node_id)
        else:
            walk_hyp(node.node_id)
        return keys

    def _is_descendant(self, ancestor_id, candidate_id):
        """True si candidate_id est dans le sous-arbre de l'hypothèse ancestor_id."""
        seed = self.node_items.get(("h", ancestor_id))
        if seed is None:
            return False
        for key in self._collect_descendants(seed):
            if key == ("h", candidate_id):
                return True
        return False

    def _attach_to_bifurcation(self, hyp_id, bn_id):
        """Rattache une hypothèse à un nœud de bifurcation."""
        parent_row = self.db.execute(
            "SELECT parent_hypothesis_id FROM bifurcation_nodes WHERE id = ?",
            (bn_id,),
        ).fetchone()
        parent_hyp = parent_row["parent_hypothesis_id"] if parent_row else None
        self.db.execute(
            "DELETE FROM bifurcation_links WHERE hypothesis_id = ?", (hyp_id,)
        )
        self.db.execute(
            "INSERT INTO bifurcation_links (bifurcation_node_id, hypothesis_id) "
            "VALUES (?, ?)",
            (bn_id, hyp_id),
        )
        self.db.execute(
            "UPDATE hypotheses SET bifurcation_node_id = ?, "
            "parent_hypothesis_id = ? WHERE id = ?",
            (bn_id, parent_hyp, hyp_id),
        )

    def _make_child(self, child_id, parent_id):
        """Fait d'une hypothèse l'enfant direct d'une autre."""
        self.db.execute(
            "DELETE FROM bifurcation_links WHERE hypothesis_id = ?", (child_id,)
        )
        self.db.execute(
            "UPDATE hypotheses SET parent_hypothesis_id = ?, "
            "bifurcation_node_id = NULL WHERE id = ?",
            (parent_id, child_id),
        )

    def _confirm(self, text):
        """Boîte de dialogue Oui/Annuler ; retourne True si Oui."""
        reply = QMessageBox.question(
            self, "Yggdrasil", text,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes,
        )
        return reply == QMessageBox.Yes

    # -- Bascules de classement (Phase 4) ----------------------------------

    def flash_ranking_switches(self, switches):
        """Affiche un badge animé (fondu 3 s) sur les nœuds ayant changé de rang."""
        if not switches:
            if self.status_cb:
                self.status_cb("⚠ Bascule de classement : aucune.")
            return

        for sw in switches:
            row = self.db.execute(
                "SELECT id FROM hypotheses WHERE project_id = ? AND label = ?",
                (self.current_project_id, sw["label"]),
            ).fetchone()
            if not row:
                continue
            item = self.node_items.get(("h", row["id"]))
            if item is None:
                continue
            up = sw["direction"] == "up"
            arrow = "↑" if up else "↓"
            color = "#27AE60" if up else "#C0392B"
            badge = QGraphicsSimpleTextItem(
                f"{arrow} R{sw['old_rank']}→R{sw['new_rank']}"
            )
            f = QFont()
            f.setBold(True)
            f.setPointSize(10)
            badge.setFont(f)
            badge.setBrush(QBrush(QColor(color)))
            br = badge.boundingRect()
            p = item.pos()
            r = getattr(item, "radius", 30)
            badge.setPos(p.x() - br.width() / 2, p.y() - r - 22)
            badge.setZValue(3)
            self.scene_obj.addItem(badge)

            anim = QVariantAnimation()
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.setDuration(3000)
            anim.valueChanged.connect(badge.setOpacity)

            def _cleanup(b=badge, a=anim):
                if b.scene() is not None:
                    self.scene_obj.removeItem(b)
                if a in self._anims:
                    self._anims.remove(a)

            anim.finished.connect(_cleanup)
            self._anims.append(anim)
            anim.start()

        if self.status_cb:
            first = switches[0]
            self.status_cb(
                f"⚠ Bascule de classement : {first['label']} passe de "
                f"#{first['old_rank']} à #{first['new_rank']}"
            )


# ---------------------------------------------------------------------------
# Panneau latéral d'information (Phase 3)
# ---------------------------------------------------------------------------

class InfoPanel(QDockWidget):
    """Panneau latéral détaillant le nœud sélectionné dans l'arbre."""

    def __init__(self, db, navigate_cb, apply_cb=None):
        super().__init__("Panneau d'information")
        self.db = db
        self.navigate_cb = navigate_cb  # (key) -> None : naviguer vers un nœud
        self.apply_cb = apply_cb        # (hyp_id, edits) -> None : recalcul
        self._edit_rows = []            # [(ach_id, spin_h, spin_nh)]
        self.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable
        )

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.container)
        self.setWidget(self.scroll)
        self.setMinimumWidth(320)
        self.show_empty()

    # -- Utilitaires -------------------------------------------------------

    def _clear(self):
        while self.layout.count():
            item = self.layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _add(self, widget):
        self.layout.addWidget(widget)

    def _heading(self, text):
        lbl = QLabel(text)
        f = QFont()
        f.setBold(True)
        f.setPointSize(13)
        lbl.setFont(f)
        lbl.setWordWrap(True)
        return lbl

    def _field(self, label, value):
        lbl = QLabel(f"<b>{label}</b> {value}")
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        return lbl

    def _separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    # -- États -------------------------------------------------------------

    def show_empty(self):
        self._clear()
        lbl = QLabel("Cliquez sur un nœud de l'arbre pour voir ses détails.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: #7F8C8D;")
        self._add(lbl)

    def show_key(self, key):
        kind, node_id = key
        if kind == "h":
            self.show_hypothesis(node_id)
        else:
            self.show_bifurcation(node_id)

    # -- Hypothèse ---------------------------------------------------------

    def show_hypothesis(self, hyp_id):
        row = self.db.execute(
            "SELECT * FROM hypotheses WHERE id = ?", (hyp_id,)
        ).fetchone()
        if row is None:
            return
        self._clear()
        self._add(self._heading(f"🧩 {row['label']}"))
        if row["description"]:
            desc = QLabel(row["description"])
            desc.setWordWrap(True)
            self._add(desc)

        self._add(self._separator())
        prior = row["prior_probability"]
        post = row["posterior_probability"]
        prior_s = f"{prior:.1%}" if prior is not None else "—"
        post_s = f"{post:.1%}" if post is not None else "—"
        self._add(self._field("Probabilité :", f"{prior_s} → {post_s}"))

        hz = self.db.execute(
            "SELECT label FROM horizons WHERE id = ?", (row["horizon_id"],)
        ).fetchone()
        self._add(self._field("Horizon :", hz["label"] if hz else "—"))

        if row["parent_hypothesis_id"]:
            par = self.db.execute(
                "SELECT label FROM hypotheses WHERE id = ?",
                (row["parent_hypothesis_id"],),
            ).fetchone()
            self._add(self._field(
                "Parente :", par["label"] if par else "—"
            ))

        # Preuves associées (vraisemblances éditables)
        evid = self.db.execute(
            "SELECT s.id AS ach_id, e.content, e.credibility, "
            "s.consistency_score, s.p_e_given_h, s.p_e_given_not_h "
            "FROM ach_scores s JOIN evidence_items e ON s.evidence_id = e.id "
            "WHERE s.hypothesis_id = ? ORDER BY s.id",
            (hyp_id,),
        ).fetchall()
        self._edit_rows = []
        if evid:
            self._add(self._separator())
            self._add(self._heading("📄 Preuves & vraisemblances"))

            grid_widget = QWidget()
            grid = QGridLayout(grid_widget)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.addWidget(QLabel("<b>Preuve</b>"), 0, 0)
            grid.addWidget(QLabel("<b>P(E|H)</b>"), 0, 1)
            grid.addWidget(QLabel("<b>P(E|¬H)</b>"), 0, 2)
            for r, e in enumerate(evid, start=1):
                cs = e["consistency_score"] or "—"
                txt = e["content"]
                short = txt if len(txt) <= 40 else txt[:39] + "…"
                lab = QLabel(f"[{cs}] {short}")
                lab.setToolTip(f"{txt}\n(crédibilité {e['credibility']})")
                lab.setWordWrap(True)
                grid.addWidget(lab, r, 0)

                spin_h = QDoubleSpinBox()
                spin_h.setRange(0.01, 0.99)
                spin_h.setSingleStep(0.05)
                spin_h.setDecimals(2)
                spin_h.setValue(
                    e["p_e_given_h"] if e["p_e_given_h"] is not None else 0.5
                )
                grid.addWidget(spin_h, r, 1)

                spin_nh = QDoubleSpinBox()
                spin_nh.setRange(0.01, 0.99)
                spin_nh.setSingleStep(0.05)
                spin_nh.setDecimals(2)
                spin_nh.setValue(
                    e["p_e_given_not_h"] if e["p_e_given_not_h"] is not None else 0.5
                )
                grid.addWidget(spin_nh, r, 2)

                self._edit_rows.append((e["ach_id"], spin_h, spin_nh))

            self._add(grid_widget)

            btn = QPushButton("🔄 Recalculer le posterior")
            btn.clicked.connect(lambda: self._apply_likelihoods(hyp_id))
            self._add(btn)

        # Historique
        self._add(self._separator())
        self._add(self._heading("📈 Historique"))
        self._add(self._history_widget(hyp_id))

        # Hypothèses filles + bifurcations (cliquables)
        children = self.db.execute(
            "SELECT id, label FROM hypotheses WHERE parent_hypothesis_id = ? "
            "AND bifurcation_node_id IS NULL ORDER BY id",
            (hyp_id,),
        ).fetchall()
        bifs = self.db.execute(
            "SELECT id, label FROM bifurcation_nodes "
            "WHERE parent_hypothesis_id = ? ORDER BY id",
            (hyp_id,),
        ).fetchall()
        if children or bifs:
            self._add(self._separator())
            self._add(self._heading("🌿 Descendance"))
            nav = QListWidget()
            for c in children:
                it = QListWidgetItem(f"🧩 {c['label']}")
                it.setData(Qt.UserRole, ("h", c["id"]))
                nav.addItem(it)
            for b in bifs:
                it = QListWidgetItem(f"🔀 {b['label']}")
                it.setData(Qt.UserRole, ("b", b["id"]))
                nav.addItem(it)
            nav.itemClicked.connect(self._on_nav_clicked)
            nav.setMaximumHeight(160)
            self._add(nav)

    def _history_widget(self, hyp_id):
        """Mini-graphe matplotlib de l'historique de probabilité (ou repli texte)."""
        history = self.db.execute(
            "SELECT date, value FROM probability_history "
            "WHERE hypothesis_id = ? ORDER BY date",
            (hyp_id,),
        ).fetchall()

        if len(history) < 2:
            lbl = QLabel(
                "Un seul point d'historique." if history
                else "Aucun historique disponible."
            )
            lbl.setStyleSheet("color: #7F8C8D;")
            return lbl

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
        except Exception:
            lbl = QLabel(
                "Historique : "
                + " → ".join(f"{h['value']:.0%}" for h in history)
            )
            lbl.setWordWrap(True)
            return lbl

        fig = Figure(figsize=(3.0, 1.8), tight_layout=True)
        ax = fig.add_subplot(111)
        values = [h["value"] for h in history]
        ax.plot(range(len(values)), values, marker="o", color="#2980B9")
        ax.set_ylim(0, 1)
        ax.set_xticks(range(len(history)))
        ax.set_xticklabels([h["date"][-5:] for h in history], fontsize=6, rotation=45)
        ax.set_ylabel("p", fontsize=8)
        ax.tick_params(axis="y", labelsize=6)
        ax.grid(True, alpha=0.3)
        canvas = FigureCanvasQTAgg(fig)
        canvas.setMinimumHeight(170)
        return canvas

    # -- Bifurcation -------------------------------------------------------

    def show_bifurcation(self, bn_id):
        row = self.db.execute(
            "SELECT * FROM bifurcation_nodes WHERE id = ?", (bn_id,)
        ).fetchone()
        if row is None:
            return
        self._clear()
        self._add(self._heading(f"🔀 {row['label']}"))
        if row["condition_text"]:
            cond = QLabel(row["condition_text"])
            cond.setWordWrap(True)
            self._add(cond)

        self._add(self._separator())
        tension = row["tension_score"]
        if tension is None:
            self._add(self._field("Tension :", "inconnue"))
        else:
            self._add(self._field("Tension :", f"{tension:.2f}"))
            gauge = QProgressBar()
            gauge.setRange(0, 100)
            gauge.setValue(int(round(tension * 100)))
            gauge.setFormat("%p%")
            self._add(gauge)
            if tension > 0.7:
                badge = QLabel("⚠ Point de bascule critique")
                badge.setStyleSheet(
                    "color: white; background: #C0392B; padding: 4px; "
                    "border-radius: 4px; font-weight: bold;"
                )
                badge.setAlignment(Qt.AlignCenter)
                self._add(badge)

        hz = self.db.execute(
            "SELECT label FROM horizons WHERE id = ?", (row["horizon_id"],)
        ).fetchone()
        self._add(self._field("Horizon :", hz["label"] if hz else "—"))

        if row["parent_hypothesis_id"]:
            par = self.db.execute(
                "SELECT label FROM hypotheses WHERE id = ?",
                (row["parent_hypothesis_id"],),
            ).fetchone()
            self._add(self._field("Parente :", par["label"] if par else "—"))

        # Hypothèses filles
        links = self.db.execute(
            "SELECT h.id, h.label, h.posterior_probability "
            "FROM bifurcation_links bl "
            "JOIN hypotheses h ON bl.hypothesis_id = h.id "
            "WHERE bl.bifurcation_node_id = ? ORDER BY h.id",
            (bn_id,),
        ).fetchall()
        if links:
            self._add(self._separator())
            self._add(self._heading("🌿 Hypothèses filles"))
            nav = QListWidget()
            for ln in links:
                p = ln["posterior_probability"]
                p_s = f"{p:.0%}" if p is not None else "—"
                it = QListWidgetItem(f"🧩 {ln['label']} ({p_s})")
                it.setData(Qt.UserRole, ("h", ln["id"]))
                nav.addItem(it)
            nav.itemClicked.connect(self._on_nav_clicked)
            nav.setMaximumHeight(160)
            self._add(nav)

    def _on_nav_clicked(self, item):
        key = item.data(Qt.UserRole)
        if key and self.navigate_cb:
            self.navigate_cb(key)

    def _apply_likelihoods(self, hyp_id):
        """Collecte les vraisemblances éditées et déclenche le recalcul."""
        if not self.apply_cb:
            return
        edits = []
        for ach_id, spin_h, spin_nh in self._edit_rows:
            edits.append({
                "ach_id": ach_id,
                "p_h": spin_h.value(),
                "p_nh": spin_nh.value(),
            })
        self.apply_cb(hyp_id, edits)


# ---------------------------------------------------------------------------
# Vue Rivière — courbes d'évolution des probabilités (Phase 4)
# ---------------------------------------------------------------------------

class RiverView(QWidget):
    """Trace l'évolution temporelle des probabilités des hypothèses.

    Chaque hypothèse est une ligne (couleur = horizon). Des triangles marquent
    les dates d'ajout de preuves. La légende est interactive (clic = afficher /
    masquer une courbe).
    """

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.current_project_id = None
        self._layout = QVBoxLayout(self)
        self.canvas = None
        self._placeholder = QLabel(
            "Sélectionnez un projet pour afficher la vue Rivière."
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #7F8C8D; font-size: 15px;")
        self._layout.addWidget(self._placeholder)

    @staticmethod
    def _parse_date(s):
        """Convertit une date ISO (date ou datetime) en datetime.date."""
        if not s:
            return None
        try:
            return datetime.date.fromisoformat(str(s)[:10])
        except ValueError:
            return None

    def render_project(self, project_id):
        """(Re)construit le graphique pour le projet donné."""
        self.current_project_id = project_id

        if self.canvas is not None:
            self._layout.removeWidget(self.canvas)
            self.canvas.setParent(None)
            self.canvas.deleteLater()
            self.canvas = None

        if project_id is None:
            self._placeholder.setText(
                "Sélectionnez un projet pour afficher la vue Rivière."
            )
            self._placeholder.show()
            return

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
            from matplotlib.figure import Figure
        except Exception:
            self._placeholder.setText(
                "matplotlib est requis pour la vue Rivière."
            )
            self._placeholder.show()
            return

        cmap = horizon_color_map(self.db, project_id)
        hyps = self.db.execute(
            "SELECT id, label, horizon_id, prior_probability, "
            "posterior_probability FROM hypotheses WHERE project_id = ? "
            "ORDER BY id",
            (project_id,),
        ).fetchall()

        fig = Figure(tight_layout=True)
        ax = fig.add_subplot(111)
        plotted = []  # (line_artist, hypothesis_label)
        any_line = False

        for h in hyps:
            color = cmap.get(h["horizon_id"], HORIZON_DEFAULT_COLOR)
            history = self.db.execute(
                "SELECT date, value FROM probability_history "
                "WHERE hypothesis_id = ? ORDER BY date",
                (h["id"],),
            ).fetchall()
            points = [
                (self._parse_date(r["date"]), r["value"])
                for r in history
                if self._parse_date(r["date"]) is not None
            ]
            if len(points) >= 2:
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                line, = ax.plot(
                    xs, ys, marker="o", linewidth=2,
                    color=color, label=h["label"],
                )
                plotted.append((line, h["label"]))
                any_line = True
            else:
                # < 2 points : marqueur simple à la date d'import
                imp = self.db.execute(
                    "SELECT import_date FROM projects WHERE id = ?",
                    (project_id,),
                ).fetchone()
                d = self._parse_date(imp["import_date"]) if imp else None
                val = (
                    points[0][1] if points
                    else (h["posterior_probability"]
                          if h["posterior_probability"] is not None
                          else h["prior_probability"])
                )
                if d is not None and val is not None:
                    pt = ax.scatter(
                        [d], [val], color=color, s=60,
                        label=h["label"], zorder=5,
                    )
                    plotted.append((pt, h["label"]))

        # Marqueurs (triangles) aux dates d'ajout de preuves
        ev_dates = []
        for r in self.db.execute(
            "SELECT added_at FROM evidence_items WHERE project_id = ?",
            (project_id,),
        ).fetchall():
            d = self._parse_date(r["added_at"])
            if d is not None:
                ev_dates.append(d)
        for d in ev_dates:
            ax.scatter([d], [0.02], marker="^", color="#34495E", s=40, zorder=4)

        ax.set_ylim(0, 1)
        ax.set_ylabel("Probabilité")
        ax.set_title("Évolution des probabilités (mode Rivière)")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate(rotation=30)

        if plotted:
            legend = ax.legend(loc="best", fontsize=8)
            lookup = {}
            leg_lines = legend.get_lines()
            for leg_line, (artist, _label) in zip(leg_lines, plotted):
                leg_line.set_picker(5)
                lookup[leg_line] = artist

            def on_pick(event):
                artist = lookup.get(event.artist)
                if artist is None:
                    return
                vis = not artist.get_visible()
                artist.set_visible(vis)
                event.artist.set_alpha(1.0 if vis else 0.25)
                self.canvas.draw_idle()

            self._on_pick = on_pick  # garde une référence
        else:
            ax.text(
                0.5, 0.5, "Aucun historique à tracer.",
                ha="center", va="center", transform=ax.transAxes,
                color="#7F8C8D",
            )

        self.canvas = FigureCanvasQTAgg(fig)
        if plotted:
            self.canvas.mpl_connect("pick_event", self._on_pick)
        self._placeholder.hide()
        self._layout.addWidget(self.canvas)


# ---------------------------------------------------------------------------
# Mode Vigie — UI (Phase 5)
# ---------------------------------------------------------------------------

def get_api_settings():
    """Retourne (api_key, model, base_url) depuis QSettings (clé jamais en BDD)."""
    s = QSettings()
    return (
        s.value("fantasyai/api_key", "", str),
        s.value("fantasyai/model", FANTASYAI_MODELS_FALLBACK[0], str),
        s.value("fantasyai/base_url", FANTASYAI_BASE, str),
    )


class ApiConfigDialog(QDialog):
    """Configuration de la clé API et du modèle FantasyAI."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuration API FantasyAI")
        self.setMinimumWidth(420)
        key, model, base = get_api_settings()

        form = QFormLayout(self)

        self.key_edit = QLineEdit(key)
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("Clé API FantasyAI")
        form.addRow("Clé API :", self.key_edit)

        self.base_edit = QLineEdit(base)
        self.base_edit.setPlaceholderText(FANTASYAI_BASE)
        form.addRow("URL de base :", self.base_edit)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(FANTASYAI_MODELS_FALLBACK)
        if model and model not in FANTASYAI_MODELS_FALLBACK:
            self.model_combo.addItem(model)
        self.model_combo.setCurrentText(model or FANTASYAI_MODELS_FALLBACK[0])
        form.addRow("Modèle :", self.model_combo)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        form.addRow(self.status)

        btn_test = QPushButton("Tester la connexion")
        btn_test.clicked.connect(self._test)
        form.addRow(btn_test)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Save).setText("Enregistrer")
        buttons.button(QDialogButtonBox.Cancel).setText("Annuler")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _test(self):
        key = self.key_edit.text().strip()
        base = self.base_edit.text().strip() or FANTASYAI_BASE
        if not key:
            self.status.setText("Renseignez d'abord une clé API.")
            return
        self.status.setText("Test en cours…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            models = fetch_fantasyai_models(key, base)
            current = self.model_combo.currentText()
            self.model_combo.clear()
            self.model_combo.addItems(models)
            self.model_combo.setCurrentText(
                current if current in models else models[0]
            )
            self.status.setText(
                f"✅ Connexion réussie — {len(models)} modèles disponibles."
            )
        except Exception as exc:
            self.status.setText(f"❌ Échec : {exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def _save(self):
        s = QSettings()
        s.setValue("fantasyai/api_key", self.key_edit.text().strip())
        s.setValue("fantasyai/model", self.model_combo.currentText().strip())
        s.setValue(
            "fantasyai/base_url",
            self.base_edit.text().strip() or FANTASYAI_BASE,
        )
        self.accept()


class VigieWorker(QThread):
    """Appelle FantasyAI dans un thread pour ne pas figer l'interface."""

    succeeded = Signal(str)
    failed = Signal(str, str)  # (kind, message)

    def __init__(self, system, user, key, model, base_url=FANTASYAI_BASE):
        super().__init__()
        self.system = system
        self.user = user
        self.key = key
        self.model = model
        self.base_url = base_url

    def run(self):
        try:
            content = call_fantasyai(
                self.system, self.user, self.key, self.model, self.base_url
            )
            self.succeeded.emit(content)
        except Exception as exc:  # mapping des erreurs réseau / HTTP
            kind, msg = "other", str(exc)
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
            name = type(exc).__name__
            if status == 401:
                kind, msg = "401", "Clé API invalide"
            elif status == 429:
                kind, msg = "429", "Limite de taux atteinte. Réessayez dans quelques secondes."
            elif status == 402:
                kind, msg = "402", "Abonnement FantasyAI Cloud expiré."
            elif "Timeout" in name:
                kind, msg = "timeout", "Le serveur ne répond pas. Vérifiez votre connexion."
            elif "ConnectionError" in name:
                kind, msg = "connexion", "Connexion impossible au serveur FantasyAI (URL ou réseau)."
            self.failed.emit(kind, msg)


class ReviewDialog(QDialog):
    """Révision d'un flux avant application : preuve, vraisemblances, deltas."""

    def __init__(self, db, project_id, parsed, parent=None):
        super().__init__(parent)
        self.db = db
        self.project_id = project_id
        self.parsed = parsed
        self.setWindowTitle("Révision du flux")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        ev = parsed.get("evidence", {}) or {}

        layout.addWidget(QLabel("<b>Fait nouveau extrait :</b>"))
        self.summary_edit = QPlainTextEdit(ev.get("content", ""))
        self.summary_edit.setMaximumHeight(80)
        layout.addWidget(self.summary_edit)

        cred_row = QHBoxLayout()
        cred_row.addWidget(QLabel("Crédibilité :"))
        self.cred_spin = QSpinBox()
        self.cred_spin.setRange(1, 5)
        self.cred_spin.setValue(int(ev.get("credibility", 3) or 3))
        cred_row.addWidget(self.cred_spin)
        cred_row.addStretch(1)
        layout.addLayout(cred_row)

        if parsed.get("narrative"):
            narr = QLabel(f"<i>{parsed['narrative']}</i>")
            narr.setWordWrap(True)
            layout.addWidget(narr)

        layout.addWidget(QLabel("<b>Vraisemblances estimées :</b>"))
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.addWidget(QLabel("<b>Hypothèse</b>"), 0, 0)
        grid.addWidget(QLabel("<b>P(E|H)</b>"), 0, 1)
        grid.addWidget(QLabel("<b>P(E|¬H)</b>"), 0, 2)
        grid.addWidget(QLabel("<b>Posterior</b>"), 0, 3)

        self.rows = []  # (hyp_id, label, spin_h, spin_nh, preview_label)
        likelihoods = parsed.get("likelihoods", {}) or {}
        r = 0
        for label, vals in likelihoods.items():
            hyp = self.db.execute(
                "SELECT id, prior_probability, posterior_probability "
                "FROM hypotheses WHERE project_id = ? AND label = ?",
                (project_id, label),
            ).fetchone()
            if hyp is None:
                continue
            r += 1
            grid.addWidget(QLabel(label), r, 0)
            sh = QDoubleSpinBox()
            sh.setRange(0.01, 0.99)
            sh.setSingleStep(0.05)
            sh.setDecimals(2)
            sh.setValue(clamp_likelihood((vals or {}).get("p_e_given_h", 0.5)))
            grid.addWidget(sh, r, 1)
            snh = QDoubleSpinBox()
            snh.setRange(0.01, 0.99)
            snh.setSingleStep(0.05)
            snh.setDecimals(2)
            snh.setValue(clamp_likelihood((vals or {}).get("p_e_given_not_h", 0.5)))
            grid.addWidget(snh, r, 2)
            prev = QLabel("—")
            grid.addWidget(prev, r, 3)
            self.rows.append((hyp["id"], label, sh, snh, prev))
            sh.valueChanged.connect(self._update_previews)
            snh.valueChanged.connect(self._update_previews)
        layout.addWidget(grid_w)

        # Entités extraites
        ents = parsed.get("entities", []) or []
        if ents:
            names = ", ".join(e.get("name", "?") for e in ents if e.get("name"))
            el = QLabel(f"<b>Entités :</b> {names}")
            el.setWordWrap(True)
            layout.addWidget(el)

        buttons = QDialogButtonBox()
        b_apply = buttons.addButton("✅ Appliquer", QDialogButtonBox.AcceptRole)
        buttons.addButton("❌ Rejeter", QDialogButtonBox.RejectRole)
        b_apply.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_previews()

    def _update_previews(self):
        """Recalcule en direct ancien → nouveau posterior pour chaque hypothèse."""
        for hyp_id, _label, sh, snh, prev in self.rows:
            row = self.db.execute(
                "SELECT prior_probability, posterior_probability "
                "FROM hypotheses WHERE id = ?",
                (hyp_id,),
            ).fetchone()
            prior = row["prior_probability"]
            old_post = row["posterior_probability"]
            existing = self.db.execute(
                "SELECT p_e_given_h, p_e_given_not_h FROM ach_scores "
                "WHERE hypothesis_id = ? AND p_e_given_h IS NOT NULL "
                "AND p_e_given_not_h IS NOT NULL",
                (hyp_id,),
            ).fetchall()
            liks = [(l[0], l[1]) for l in existing]
            liks.append((sh.value(), snh.value()))
            new_post = bayesian_update(prior, liks)
            o = f"{old_post:.0%}" if old_post is not None else "—"
            n = f"{new_post:.0%}" if new_post is not None else "—"
            delta = ""
            if old_post is not None and new_post is not None:
                d = (new_post - old_post) * 100
                delta = f"  ({'+' if d >= 0 else ''}{d:.1f})"
            prev.setText(f"{o} → {n}{delta}")

    def result_data(self):
        """Retourne (résumé, crédibilité, {hyp_id: (p_h, p_nh)})."""
        edits = {}
        for hyp_id, _label, sh, snh, _prev in self.rows:
            edits[hyp_id] = (sh.value(), snh.value())
        return self.summary_edit.toPlainText().strip(), self.cred_spin.value(), edits


class VigieView(QWidget):
    """Onglet Vigie : flux entrant → analyse FantasyAI → mise à jour de l'arbre."""

    def __init__(self, db, apply_cb, create_cb=None):
        super().__init__()
        self.db = db
        self.apply_cb = apply_cb   # (project_id, summary, credibility, edits,
                                   #  parsed, raw) -> str (résumé)
        self.create_cb = create_cb  # (name) -> project_id (création + refresh)
        self._worker = None

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Projet actif :"))
        self.project_combo = QComboBox()
        top.addWidget(self.project_combo, 1)
        layout.addLayout(top)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "Collez votre flux (article, dépêche, notes…)"
        )
        self.text_edit.textChanged.connect(self._update_tokens)
        layout.addWidget(self.text_edit, 1)

        row = QHBoxLayout()
        self.token_label = QLabel("≈ 0 tokens")
        row.addWidget(self.token_label)
        row.addStretch(1)
        self.review_check = QCheckBox("Révision avant application")
        self.review_check.setChecked(True)
        row.addWidget(self.review_check)
        layout.addLayout(row)

        self.analyze_btn = QPushButton("▶ Analyser et mettre à jour l'arbre")
        self.analyze_btn.clicked.connect(self._analyze)
        layout.addWidget(self.analyze_btn)

        self.results = QTextBrowser()
        self.results.setMinimumHeight(140)
        layout.addWidget(self.results, 1)

        self.status_cb = None  # (texte) -> None

    # -- Données -----------------------------------------------------------

    def refresh_projects(self, projects, current_id=None):
        """Met à jour la liste des projets du sélecteur."""
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for p in projects:
            self.project_combo.addItem(p["name"], p["id"])
        if current_id is not None:
            idx = self.project_combo.findData(current_id)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        self.project_combo.blockSignals(False)

    def current_project_id(self):
        return self.project_combo.currentData()

    def _update_tokens(self):
        n = max(1, len(self.text_edit.toPlainText()) // 4)
        self.token_label.setText(f"≈ {n} tokens")

    def _status(self, text):
        if self.status_cb:
            self.status_cb(text)

    # -- Analyse -----------------------------------------------------------

    def _analyze(self):
        text = self.text_edit.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "Vigie", "Collez d'abord un texte à analyser.")
            return
        key, model, base = get_api_settings()
        if not key:
            QMessageBox.warning(
                self, "Vigie", "Configurez la clé API FantasyAI d'abord."
            )
            return

        # Détection automatique de projet (Phase 6) ----------------------
        project_id = self._resolve_target_project(text)
        if project_id is None:
            return  # annulé / aucun projet

        self.analyze_btn.setEnabled(False)
        self._status("Analyse en cours…")
        self.results.setHtml("<i>Analyse en cours…</i>")

        system = VIGIE_SYSTEM_PROMPT
        user = build_vigie_prompt(self.db, project_id, text)
        self._pending = {"project_id": project_id, "text": text}
        self._worker = VigieWorker(system, user, key, model, base)
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _resolve_target_project(self, text):
        """Détermine le projet cible via détection spaCy + arbitrage utilisateur.

        Retourne l'id du projet à utiliser, ou None si l'utilisateur annule.
        """
        selected = self.current_project_id()
        try:
            decision = suggest_project(self.db, text)
        except Exception:
            decision = {"mode": "unavailable"}
        mode = decision.get("mode")

        def project_name(pid):
            row = self.db.execute(
                "SELECT name FROM projects WHERE id = ?", (pid,)
            ).fetchone()
            return row["name"] if row else "?"

        if mode in ("unavailable", None):
            if selected is None:
                return self._prompt_create_project()
            return selected

        if mode == "auto":
            pid = decision["project_id"]
            if pid != selected:
                idx = self.project_combo.findData(pid)
                if idx >= 0:
                    self.project_combo.setCurrentIndex(idx)
                self._status(
                    f"Projet détecté : {project_name(pid)} "
                    f"(confiance {decision['confidence']:.0%})"
                )
            return pid

        if mode == "suggest":
            pid = decision["project_id"]
            conf = decision["confidence"]
            extra = " (entités communes)" if decision.get("by_entities") else ""
            box = QMessageBox(self)
            box.setWindowTitle("Détection de projet")
            box.setText(
                f"Ce flux ressemble au projet « {project_name(pid)} » "
                f"(confiance {conf:.0%}){extra}.\nL'analyser dans ce contexte ?"
            )
            b_yes = box.addButton("Oui", QMessageBox.YesRole)
            b_cur = box.addButton("Non, projet actuel", QMessageBox.NoRole)
            b_new = box.addButton("Créer un projet", QMessageBox.ActionRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is b_yes:
                idx = self.project_combo.findData(pid)
                if idx >= 0:
                    self.project_combo.setCurrentIndex(idx)
                return pid
            if clicked is b_cur:
                return selected if selected is not None else self._prompt_create_project()
            if clicked is b_new:
                return self._prompt_create_project()
            return None

        # none_match
        box = QMessageBox(self)
        box.setWindowTitle("Détection de projet")
        box.setText(
            "Ce flux ne correspond à aucun projet existant.\n"
            "Voulez-vous créer un nouveau projet ?"
        )
        b_new = box.addButton("Créer", QMessageBox.AcceptRole)
        b_cur = box.addButton("Analyser dans le projet actuel", QMessageBox.NoRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_new:
            return self._prompt_create_project()
        if clicked is b_cur:
            return selected if selected is not None else self._prompt_create_project()
        return None

    def _prompt_create_project(self):
        """Demande un nom et crée un projet vide via le callback ; retourne son id."""
        if not self.create_cb:
            return None
        name, ok = QInputDialog.getText(
            self, "Nouveau projet", "Nom du nouveau projet :"
        )
        if not ok or not name.strip():
            return None
        pid = self.create_cb(name.strip())
        idx = self.project_combo.findData(pid)
        if idx >= 0:
            self.project_combo.setCurrentIndex(idx)
        return pid

    def _on_failed(self, kind, message):
        self.analyze_btn.setEnabled(True)
        self._status("Analyse échouée.")
        self.results.setHtml(f"<b>Erreur ({kind}) :</b> {message}")

    def _on_success(self, raw):
        self.analyze_btn.setEnabled(True)
        self._status("Prêt")
        try:
            parsed = parse_vigie_json(raw)
        except Exception:
            self.results.setHtml(
                "<b>L'IA a renvoyé une réponse mal structurée.</b><br>"
                "<pre>" + (raw or "")[:2000].replace("<", "&lt;") + "</pre>"
            )
            return

        project_id = self._pending["project_id"]
        raw_text = self._pending["text"]

        if self.review_check.isChecked():
            dlg = ReviewDialog(self.db, project_id, parsed, self)
            if dlg.exec() != QDialog.Accepted:
                self.results.setHtml("Flux rejeté — aucun changement appliqué.")
                return
            summary, credibility, edits = dlg.result_data()
        else:
            ev = parsed.get("evidence", {}) or {}
            summary = ev.get("content", "")
            credibility = int(ev.get("credibility", 3) or 3)
            edits = {}
            for label, vals in (parsed.get("likelihoods", {}) or {}).items():
                hyp = self.db.execute(
                    "SELECT id FROM hypotheses WHERE project_id = ? AND label = ?",
                    (project_id, label),
                ).fetchone()
                if hyp:
                    edits[hyp["id"]] = (
                        clamp_likelihood((vals or {}).get("p_e_given_h", 0.5)),
                        clamp_likelihood((vals or {}).get("p_e_given_not_h", 0.5)),
                    )

        summary_html = self.apply_cb(
            project_id, summary, credibility, edits, parsed, raw_text
        )
        self.results.setHtml(summary_html)


# ---------------------------------------------------------------------------
# Interface graphique
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """Fenêtre principale de Yggdrasil."""

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.current_project_id = None
        self._undo_stack = []
        self._redo_stack = []
        self._is_fullscreen = False

        self.setWindowTitle("Yggdrasil")
        self.setMinimumSize(1400, 900)

        icon_path = resource_path("yggdrasil_icon.png")
        if Path(icon_path).exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self.statusBar().showMessage("Prêt")

        self.tree_view.before_change = self._push_undo

        self._restore_preferences()
        self.refresh_project_list()
        self._restore_active_project()
        self._update_undo_actions()

    # -- Construction de l'UI ----------------------------------------------

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&Fichier")

        act_import = QAction("Importer un export IRIS-Station…", self)
        act_import.setShortcut(QKeySequence("Ctrl+I"))
        act_import.triggered.connect(self.on_import)
        file_menu.addAction(act_import)

        act_export = QAction("Exporter pour IRIS-Station…", self)
        act_export.setShortcut(QKeySequence("Ctrl+E"))
        act_export.triggered.connect(self.on_export)
        file_menu.addAction(act_export)

        self.recent_menu = file_menu.addMenu("Projets récents")
        self._rebuild_recent_menu()

        file_menu.addSeparator()
        act_delete = QAction("Supprimer le projet actif…", self)
        act_delete.triggered.connect(self.delete_current_project)
        file_menu.addAction(act_delete)

        file_menu.addSeparator()
        act_quit = QAction("Quitter", self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        config_menu = menubar.addMenu("&Configuration")
        act_api = QAction("API FantasyAI…", self)
        act_api.triggered.connect(self.on_config_api)
        config_menu.addAction(act_api)

        view_menu = menubar.addMenu("&Affichage")
        self._view_menu = view_menu  # complété après création du dock

        self.act_undo = QAction("Annuler", self)
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_undo.triggered.connect(self.undo)
        view_menu.addAction(self.act_undo)

        self.act_redo = QAction("Rétablir", self)
        self.act_redo.setShortcut(QKeySequence("Ctrl+Shift+Z"))
        self.act_redo.triggered.connect(self.redo)
        view_menu.addAction(self.act_redo)

        view_menu.addSeparator()

        act_full = QAction("Plein écran", self)
        act_full.setShortcut(QKeySequence("F11"))
        act_full.triggered.connect(self._toggle_fullscreen)
        view_menu.addAction(act_full)

        view_menu.addSeparator()

        help_menu = menubar.addMenu("&Aide")
        act_about = QAction("À propos", self)
        act_about.triggered.connect(self.on_about)
        help_menu.addAction(act_about)

    def _build_toolbar(self):
        toolbar = QToolBar("Principale")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        act_import = QAction("📥 Importer", self)
        act_import.triggered.connect(self.on_import)
        toolbar.addAction(act_import)

        act_export = QAction("📤 Exporter", self)
        act_export.triggered.connect(self.on_export)
        toolbar.addAction(act_export)

        toolbar.addSeparator()

        self.act_tree = QAction("🌳 Vue Arbre", self)
        self.act_tree.triggered.connect(lambda: self.show_page("Arbre"))
        toolbar.addAction(self.act_tree)

        self.act_river = QAction("📊 Vue Rivière", self)
        self.act_river.triggered.connect(self.show_river)
        toolbar.addAction(self.act_river)

        self.act_vigie = QAction("🔭 Vigie", self)
        self.act_vigie.triggered.connect(self.show_vigie)
        toolbar.addAction(self.act_vigie)

    def _build_central(self):
        splitter = QSplitter(Qt.Horizontal)

        # Gauche : liste des projets
        self.project_list = QListWidget()
        self.project_list.setMinimumWidth(260)
        self.project_list.currentItemChanged.connect(self.on_project_selected)
        self.project_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(
            self._project_list_menu
        )
        splitter.addWidget(self.project_list)

        # Droite : pages empilées
        self.stack = QStackedWidget()
        self.pages = {}

        self.pages["Accueil"] = self._build_home_page()
        self.stack.addWidget(self.pages["Accueil"])

        self.tree_view = TreeView(self.db)
        self.tree_view.on_select = self._on_tree_select
        self.tree_view.status_cb = self.statusBar().showMessage
        self.pages["Arbre"] = self.tree_view
        self.stack.addWidget(self.pages["Arbre"])

        self.river_view = RiverView(self.db)
        self.pages["Rivière"] = self.river_view
        self.stack.addWidget(self.pages["Rivière"])

        self.vigie_view = VigieView(
            self.db, self.apply_vigie_flow, self.create_project_from_vigie
        )
        self.vigie_view.status_cb = self.statusBar().showMessage
        self.pages["Vigie"] = self.vigie_view
        self.stack.addWidget(self.pages["Vigie"])

        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 1120])

        self.setCentralWidget(splitter)

        # Panneau latéral d'information (Phase 3 + édition Phase 4)
        self.info_panel = InfoPanel(
            self.db, self._on_tree_select, self.recompute_likelihoods
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.info_panel)
        toggle = self.info_panel.toggleViewAction()
        toggle.setText("Panneau d'information")
        self._view_menu.addAction(toggle)

        self._update_vigie_enabled()

    def _build_home_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)

        self.home_label = QLabel(
            "Importez un rapport IRIS-Station pour commencer"
        )
        self.home_label.setAlignment(Qt.AlignCenter)
        self.home_label.setStyleSheet("font-size: 16px;")
        layout.addWidget(self.home_label)

        btn = QPushButton("📥 Importer")
        btn.setFixedWidth(200)
        btn.clicked.connect(self.on_import)
        layout.addWidget(btn, alignment=Qt.AlignCenter)

        return page

    def _placeholder_page(self, text):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #7F8C8D; font-size: 15px;")
        layout.addWidget(label)
        return page

    def _center_on_screen(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(geo.center())
        self.move(frame.topLeft())

    # -- Navigation --------------------------------------------------------

    def show_page(self, name):
        """Affiche une page du QStackedWidget par son nom."""
        if name in self.pages:
            self.stack.setCurrentWidget(self.pages[name])

    # -- Liste des projets -------------------------------------------------

    def refresh_project_list(self, select_id=None):
        """Recharge la liste des projets et sélectionne éventuellement l'un d'eux."""
        self.project_list.blockSignals(True)
        self.project_list.clear()
        target_row = -1
        for i, proj in enumerate(self.db.list_projects()):
            item = QListWidgetItem(proj["name"])
            item.setData(Qt.UserRole, proj["id"])
            self.project_list.addItem(item)
            if select_id is not None and proj["id"] == select_id:
                target_row = i
        self.project_list.blockSignals(False)

        self._rebuild_recent_menu()
        self.vigie_view.refresh_projects(
            self.db.list_projects(),
            select_id if select_id is not None else self.current_project_id,
        )

        if target_row >= 0:
            self.project_list.setCurrentRow(target_row)
        elif self.project_list.count() > 0 and select_id is None:
            # Ne rien sélectionner automatiquement au démarrage : on laisse
            # la page d'accueil par défaut. (Décommenter pour auto-sélection.)
            pass

    def _rebuild_recent_menu(self):
        self.recent_menu.clear()
        projects = self.db.list_projects()
        if not projects:
            empty = QAction("(aucun)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        for proj in projects[:10]:
            act = QAction(proj["name"], self)
            act.triggered.connect(
                lambda checked=False, pid=proj["id"]: self.select_project(pid)
            )
            self.recent_menu.addAction(act)

    def select_project(self, project_id):
        """Sélectionne un projet par son id dans la liste."""
        for i in range(self.project_list.count()):
            item = self.project_list.item(i)
            if item.data(Qt.UserRole) == project_id:
                self.project_list.setCurrentRow(i)
                return

    def on_project_selected(self, current, _previous):
        if current is None:
            self.current_project_id = None
            return
        project_id = current.data(Qt.UserRole)
        self.current_project_id = project_id
        self.display_summary(project_id)
        self.tree_view.render_project(project_id)
        self.river_view.render_project(project_id)
        self.info_panel.show_empty()
        self.show_page("Accueil")

    def _on_tree_select(self, key):
        """Alimente le panneau latéral quand un nœud est cliqué (arbre ou liste)."""
        self.show_page("Arbre")
        self.info_panel.show_key(key)
        if not self.info_panel.isVisible():
            self.info_panel.show()

    def show_river(self):
        """Affiche la vue Rivière pour le projet courant."""
        if self.current_project_id is None:
            QMessageBox.information(
                self, "Vue Rivière",
                "Sélectionnez d'abord un projet.",
            )
            return
        self.river_view.render_project(self.current_project_id)
        self.show_page("Rivière")

    def recompute_likelihoods(self, hyp_id, edits):
        """Applique les vraisemblances éditées et propage le recalcul bayésien."""
        proj = self.db.execute(
            "SELECT project_id FROM hypotheses WHERE id = ?", (hyp_id,)
        ).fetchone()
        if proj is None:
            return
        project_id = proj["project_id"]

        # 1. Mettre à jour les vraisemblances
        self._push_undo()
        for ed in edits:
            p_h, p_nh = ed["p_h"], ed["p_nh"]
            bf = (p_h / p_nh) if p_nh else None
            self.db.execute(
                "UPDATE ach_scores SET p_e_given_h = ?, p_e_given_not_h = ?, "
                "bayes_factor = ? WHERE id = ?",
                (p_h, p_nh, bf, ed["ach_id"]),
            )

        # 2. Recalculer le posterior
        old_post = self.db.execute(
            "SELECT posterior_probability FROM hypotheses WHERE id = ?",
            (hyp_id,),
        ).fetchone()["posterior_probability"]
        new_post = compute_posterior_for_hypothesis(self.db, hyp_id)
        self.db.execute(
            "UPDATE hypotheses SET posterior_probability = ? WHERE id = ?",
            (new_post, hyp_id),
        )

        # 3. Point d'historique daté du jour
        today = datetime.date.today().isoformat()
        self.db.execute(
            "INSERT INTO probability_history (hypothesis_id, date, value) "
            "VALUES (?, ?, ?)",
            (hyp_id, today, new_post),
        )

        # 4. Tensions des bifurcations + 5. bascules de classement
        recompute_all_tensions(self.db, project_id)
        switches = detect_ranking_switches(self.db, project_id)

        # 6. Rafraîchir les vues
        self.tree_view.render_project(project_id)
        self.river_view.render_project(project_id)
        self.info_panel.show_hypothesis(hyp_id)
        self.tree_view.flash_ranking_switches(switches)

        o = f"{old_post:.1%}" if old_post is not None else "—"
        n = f"{new_post:.1%}" if new_post is not None else "—"
        self.statusBar().showMessage(f"Posterior recalculé : {o} → {n}")

    # -- Suppression de projet ---------------------------------------------

    def _project_list_menu(self, point):
        """Menu contextuel sur la liste des projets (clic droit)."""
        item = self.project_list.itemAt(point)
        if item is None:
            return
        project_id = item.data(Qt.UserRole)
        menu = QMenu(self)
        act = menu.addAction("🗑 Supprimer ce projet…")
        chosen = menu.exec(self.project_list.mapToGlobal(point))
        if chosen == act:
            self._delete_project(project_id, item.text())

    def delete_current_project(self):
        """Supprime le projet actuellement sélectionné."""
        if self.current_project_id is None:
            QMessageBox.information(
                self, "Suppression", "Aucun projet sélectionné."
            )
            return
        name = self.db.execute(
            "SELECT name FROM projects WHERE id = ?", (self.current_project_id,)
        ).fetchone()
        self._delete_project(
            self.current_project_id, name["name"] if name else ""
        )

    def _delete_project(self, project_id, name):
        """Confirme puis supprime un projet et toutes ses données."""
        reply = QMessageBox.question(
            self, "Supprimer le projet",
            f"Supprimer définitivement « {name} » et toutes ses données ?\n"
            "Cette action est irréversible.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self._push_undo()
            self.db.delete_project(project_id)
        except Exception as exc:
            QMessageBox.critical(
                self, "Suppression échouée", f"Erreur : {exc}"
            )
            return

        was_current = project_id == self.current_project_id
        if was_current:
            self.current_project_id = None
            self.tree_view.render_project(None)
            self.river_view.render_project(None)
            self.info_panel.show_empty()
            self.home_label.setText(
                "Importez un rapport IRIS-Station pour commencer"
            )
            self.home_label.setTextFormat(Qt.AutoText)
            self.show_page("Accueil")
        self.refresh_project_list()
        self.statusBar().showMessage(f"Projet « {name} » supprimé")

    def display_summary(self, project_id):
        """Affiche le résumé d'un projet sur la page d'accueil."""
        summary = self.db.project_summary(project_id)
        if not summary:
            return
        created = summary["created"] or "—"
        text = (
            f"<h2>{summary['name']}</h2>"
            f"<p><b>Créé :</b> {created}<br>"
            f"<b>Importé :</b> {summary['import_date']}</p>"
            f"<p>"
            f"🧩 {summary['hypotheses']} hypothèses<br>"
            f"📄 {summary['evidence']} preuves<br>"
            f"🔀 {summary['bifurcations']} nœuds de bifurcation<br>"
            f"🔮 {summary['predictions']} prédictions<br>"
            f"🏷️ {summary['entities']} entités"
            f"</p>"
        )
        self.home_label.setText(text)
        self.home_label.setTextFormat(Qt.RichText)
        self.statusBar().showMessage(
            f"Projet « {summary['name']} » sélectionné"
        )

    # -- Actions -----------------------------------------------------------

    def on_import(self):
        """Ouvre un sélecteur de fichier et importe l'export choisi."""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Importer un export IRIS-Station",
            "",
            "Exports IRIS-Station (*.md);;Tous les fichiers (*)",
        )
        if not filepath:
            return

        try:
            data = parse_iris_export(filepath)
        except yaml.YAMLError:
            QMessageBox.critical(
                self,
                "Import impossible",
                "Format non reconnu. Assurez-vous d'utiliser un export "
                "IRIS-Station (Phase 8+).",
            )
            return
        except ValueError as exc:
            QMessageBox.critical(self, "Import impossible", str(exc))
            return
        except Exception as exc:  # lecture fichier, encodage…
            QMessageBox.critical(
                self, "Import impossible", f"Erreur de lecture : {exc}"
            )
            return

        if data.get("YGGDRASIL_IMPORT") != "v1":
            QMessageBox.warning(
                self,
                "Version d'export",
                "Version d'export non reconnue — import partiel possible.",
            )

        try:
            self._push_undo()
            project_id = self.db.import_project(data, filepath)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Import échoué",
                f"L'insertion en base a échoué : {exc}",
            )
            return

        self.refresh_project_list(select_id=project_id)
        self.statusBar().showMessage("Import réussi")

    def on_export(self):
        """Exporte le projet courant au format IRIS-Station (.md round-trippable)."""
        if self.current_project_id is None:
            QMessageBox.information(
                self, "Export", "Sélectionnez d'abord un projet à exporter."
            )
            return
        name = self.db.execute(
            "SELECT name FROM projects WHERE id = ?", (self.current_project_id,)
        ).fetchone()["name"]
        safe = re.sub(r"[^\w\-]+", "_", name).strip("_") or "projet"
        default = str(Path.home() / f"{safe}_yggdrasil.md")
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Exporter pour IRIS-Station", default,
            "Markdown IRIS-Station (*.md)",
        )
        if not filepath:
            return
        if not filepath.lower().endswith(".md"):
            filepath += ".md"
        try:
            data = export_to_iris_format(self.db, self.current_project_id)
            Path(filepath).write_text(
                export_iris_markdown(data), encoding="utf-8"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export échoué", f"Erreur : {exc}")
            return
        self.statusBar().showMessage(f"Exporté : {filepath}")

    def on_config_api(self):
        """Ouvre la configuration de l'API FantasyAI."""
        dlg = ApiConfigDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self._update_vigie_enabled()
            self.statusBar().showMessage("Configuration FantasyAI enregistrée")

    def _update_vigie_enabled(self):
        """Active le bouton Vigie seulement si une clé API est configurée."""
        key, _model, _base = get_api_settings()
        has_key = bool(key)
        self.act_vigie.setEnabled(has_key)
        if has_key:
            self.act_vigie.setToolTip("Mode Vigie")
        else:
            self.act_vigie.setToolTip("Configurer la clé API FantasyAI d'abord")

    def create_project_from_vigie(self, name):
        """Crée un projet vide depuis la Vigie, rafraîchit les listes, retourne l'id."""
        pid = self.db.create_empty_project(name)
        self.refresh_project_list()
        self.statusBar().showMessage(f"Projet « {name} » créé")
        return pid

    def show_vigie(self):
        """Affiche l'onglet Vigie."""
        self.vigie_view.refresh_projects(
            self.db.list_projects(), self.current_project_id
        )
        self.show_page("Vigie")

    def apply_vigie_flow(self, project_id, summary, credibility, edits,
                         parsed, raw_text):
        """Applique un flux Vigie : preuve + vraisemblances → recalcul bayésien.

        Retourne un résumé HTML des deltas pour l'affichage dans l'onglet.
        """
        import json as _json

        # 1. Nouvelle preuve
        self._push_undo()
        ev = parsed.get("evidence", {}) or {}
        source = ev.get("source", "flux Vigie") or "flux Vigie"
        cur = self.db.execute(
            "INSERT INTO evidence_items (project_id, content, source, "
            "credibility) VALUES (?, ?, ?, ?)",
            (project_id, summary, source, credibility),
        )
        ev_id = cur.lastrowid

        # 2. Vraisemblances → ach_scores ; mémoriser les anciens posteriors
        old_posts = {}
        labels = {}
        for hyp_id, (p_h, p_nh) in edits.items():
            row = self.db.execute(
                "SELECT label, posterior_probability FROM hypotheses WHERE id = ?",
                (hyp_id,),
            ).fetchone()
            if row is None:
                continue
            old_posts[hyp_id] = row["posterior_probability"]
            labels[hyp_id] = row["label"]
            bf = (p_h / p_nh) if p_nh else None
            self.db.execute(
                "INSERT INTO ach_scores (evidence_id, hypothesis_id, "
                "p_e_given_h, p_e_given_not_h, bayes_factor) "
                "VALUES (?, ?, ?, ?, ?)",
                (ev_id, hyp_id, p_h, p_nh, bf),
            )

        # 3. Recalcul des posteriors + historique
        today = datetime.date.today().isoformat()
        deltas = []
        for hyp_id, old in old_posts.items():
            new_post = compute_posterior_for_hypothesis(self.db, hyp_id)
            self.db.execute(
                "UPDATE hypotheses SET posterior_probability = ? WHERE id = ?",
                (new_post, hyp_id),
            )
            self.db.execute(
                "INSERT INTO probability_history (hypothesis_id, date, value) "
                "VALUES (?, ?, ?)",
                (hyp_id, today, new_post),
            )
            deltas.append((labels[hyp_id], old, new_post))

        # 4. Entités extraites (ajoutées si nouvelles)
        for ent in parsed.get("entities", []) or []:
            name = ent.get("name")
            if not name:
                continue
            exists = self.db.execute(
                "SELECT 1 FROM entities WHERE project_id = ? AND name = ?",
                (project_id, name),
            ).fetchone()
            if not exists:
                self.db.execute(
                    "INSERT INTO entities (project_id, name, type, degree) "
                    "VALUES (?, ?, ?, 0)",
                    (project_id, name, ent.get("type", "MISC")),
                )

        # 5. Tensions + bascules
        recompute_all_tensions(self.db, project_id)
        switches = detect_ranking_switches(self.db, project_id)

        # Le projet a de nouvelles preuves : invalider son vecteur de référence
        # (recalculé à la prochaine détection automatique).
        invalidate_ref_vector(self.db, project_id)

        # Autosauvegarde YAML à côté de la BDD (restauration / réimport IRIS)
        try:
            autosave = export_to_iris_format(self.db, project_id)
            path = data_dir() / f"yggdrasil_autosave_{project_id}.yml"
            path.write_text(
                yaml.dump(autosave, allow_unicode=True,
                          default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except Exception:
            pass  # une autosauvegarde ratée ne doit jamais bloquer le flux

        # 6. Traçabilité
        self.db.execute(
            "INSERT INTO vigie_flows (project_id, raw_text, parsed_json, "
            "user_validated) VALUES (?, ?, ?, 1)",
            (project_id, raw_text, _json.dumps(parsed, ensure_ascii=False)),
        )

        # 7. Rafraîchir les vues
        if self.current_project_id == project_id:
            self.tree_view.render_project(project_id)
            self.river_view.render_project(project_id)
            self.tree_view.flash_ranking_switches(switches)
        self.display_summary(project_id)

        # 8. Résumé HTML
        parts = []
        for label, old, new in deltas:
            o = f"{old:.1%}" if old is not None else "—"
            n = f"{new:.1%}" if new is not None else "—"
            d = ""
            if old is not None and new is not None:
                dd = (new - old) * 100
                d = f" ({'+' if dd >= 0 else ''}{dd:.1f})"
            parts.append(f"{label} : {o} → {n}{d}")
        sw_txt = (
            "aucune" if not switches
            else ", ".join(
                f"{s['label']} #{s['old_rank']}→#{s['new_rank']}" for s in switches
            )
        )
        narrative = parsed.get("narrative", "")
        html = "<b>✅ Arbre mis à jour</b><br>"
        if narrative:
            html += f"<i>{narrative}</i><br>"
        html += "<br>".join(parts) if parts else "Aucune hypothèse impactée."
        html += f"<br>⚠ Bascule : {sw_txt}."
        return html

    def on_about(self):
        QMessageBox.about(
            self,
            "À propos de Yggdrasil",
            "<h3>Yggdrasil</h3>"
            "<p>Tableau de bord prospectif — pendant d'IRIS-Station.</p>"
            "<p>Version 1.0</p>"
            "<p>Local-first · Hors-ligne · Aucune télémétrie.</p>",
        )

    # -- Annuler / Rétablir ------------------------------------------------

    UNDO_DEPTH = 20

    def _push_undo(self):
        """Capture l'état de la base avant une mutation (pour l'annulation)."""
        try:
            self._undo_stack.append(self.db.serialize())
        except Exception:
            return
        if len(self._undo_stack) > self.UNDO_DEPTH:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._update_undo_actions()

    def undo(self):
        """Annule la dernière mutation."""
        if not self._undo_stack:
            return
        self._redo_stack.append(self.db.serialize())
        self.db.restore(self._undo_stack.pop())
        self._refresh_after_restore()
        self.statusBar().showMessage("Annulé")

    def redo(self):
        """Rétablit la mutation annulée."""
        if not self._redo_stack:
            return
        self._undo_stack.append(self.db.serialize())
        self.db.restore(self._redo_stack.pop())
        self._refresh_after_restore()
        self.statusBar().showMessage("Rétabli")

    def _update_undo_actions(self):
        if hasattr(self, "act_undo"):
            self.act_undo.setEnabled(bool(self._undo_stack))
            self.act_redo.setEnabled(bool(self._redo_stack))

    def _refresh_after_restore(self):
        """Recharge listes et vues après un restore (le projet courant peut avoir disparu)."""
        ids = {p["id"] for p in self.db.list_projects()}
        if self.current_project_id not in ids:
            self.current_project_id = None
        self.refresh_project_list(select_id=self.current_project_id)
        if self.current_project_id is not None:
            self.display_summary(self.current_project_id)
            self.tree_view.render_project(self.current_project_id)
            self.river_view.render_project(self.current_project_id)
        else:
            self.tree_view.render_project(None)
            self.river_view.render_project(None)
            self.home_label.setText(
                "Importez un rapport IRIS-Station pour commencer"
            )
            self.home_label.setTextFormat(Qt.AutoText)
        self.info_panel.show_empty()
        self._update_undo_actions()

    # -- Plein écran -------------------------------------------------------

    def _toggle_fullscreen(self):
        if self._is_fullscreen:
            self.showNormal()
        else:
            self.showFullScreen()
        self._is_fullscreen = not self._is_fullscreen

    # -- Préférences (QSettings) -------------------------------------------

    def _restore_preferences(self):
        """Restaure la géométrie de la fenêtre depuis QSettings."""
        s = QSettings()
        geo = s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self._center_on_screen()

    def _save_preferences(self):
        s = QSettings()
        s.setValue("window/geometry", self.saveGeometry())
        if self.current_project_id is not None:
            s.setValue("session/active_project", self.current_project_id)
        else:
            s.remove("session/active_project")

    def _restore_active_project(self):
        """Resélectionne le projet actif de la session précédente s'il existe."""
        s = QSettings()
        pid = s.value("session/active_project", None)
        if pid is None:
            return
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return
        if self.db.execute(
            "SELECT 1 FROM projects WHERE id = ?", (pid,)
        ).fetchone():
            self.select_project(pid)

    # -- Fermeture ---------------------------------------------------------

    def closeEvent(self, event):
        """Sauvegarde les préférences et ferme proprement la base."""
        try:
            self._save_preferences()
            self.db.close()
        finally:
            super().closeEvent(event)


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

APP_QSS = """
* { font-size: 13px; }

QMainWindow, QDialog, QWidget {
    background: #FAFAF7;
    color: #2C3E50;
}
QStackedWidget, QStackedWidget > QWidget { background: #FAFAF7; }

QMenuBar { background: #F4F1EA; color: #2C3E50; }
QMenuBar::item { background: transparent; padding: 4px 10px; }
QMenuBar::item:selected { background: #E4DECF; }
QMenu { background: #FCFBF7; color: #2C3E50; border: 1px solid #D8D2C4; }
QMenu::item:selected { background: #7FB069; color: white; }
QMenu::item:disabled { color: #AAB2BD; }

QToolBar { background: #F4F1EA; border: 0; padding: 4px; spacing: 6px; }
QToolButton { color: #2C3E50; padding: 4px 8px; border-radius: 6px; background: transparent; }
QToolButton:hover { background: #E4DECF; }
QToolButton:disabled { color: #AAB2BD; }

QStatusBar { background: #F4F1EA; color: #2C3E50; }
QStatusBar::item { border: 0; }

QLabel { color: #2C3E50; background: transparent; }

QListWidget { background: #FCFBF7; color: #2C3E50; border: 1px solid #E0DCD0; }
QListWidget::item { padding: 6px; }
QListWidget::item:selected { background: #7FB069; color: white; }

QPushButton {
    background: #7FB069; color: white; border: 0; border-radius: 6px;
    padding: 6px 12px;
}
QPushButton:hover { background: #6FA059; }
QPushButton:disabled { background: #BDC3C7; color: #ECF0F1; }

QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QComboBox,
QSpinBox, QDoubleSpinBox {
    background: #FFFFFF; color: #2C3E50;
    border: 1px solid #D8D2C4; border-radius: 4px; padding: 3px;
    selection-background-color: #7FB069; selection-color: white;
}
QComboBox QAbstractItemView {
    background: #FFFFFF; color: #2C3E50; selection-background-color: #7FB069;
}

QGraphicsView { background: #FAFAFA; border: 1px solid #E0DCD0; }

QDockWidget { color: #2C3E50; }
QDockWidget::title { background: #F4F1EA; padding: 6px; }

QScrollBar:vertical, QScrollBar:horizontal { background: #F0ECE2; }
QScrollBar::handle { background: #C9C1AE; border-radius: 4px; }

QToolTip { background: #2C3E50; color: white; border: 0; padding: 4px; }
QCheckBox { color: #2C3E50; }
QMessageBox, QMessageBox QLabel { background: #FAFAF7; color: #2C3E50; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Yggdrasil")
    app.setOrganizationName("Yggdrasil")
    app.setStyleSheet(APP_QSS)

    icon_path = resource_path("yggdrasil_icon.png")
    if Path(icon_path).exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    db = Database()
    window = MainWindow(db)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
