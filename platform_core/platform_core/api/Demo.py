# -*- coding: utf-8 -*-
"""
platform_core/api/demo.py

API Frontend — Module Démonstration & Guide utilisateur (CDC v5, §5).

Couvre :
    get_demo_videos, get_demo_progress, update_demo_progress, skip_demo

Règles métier respectées :
    - §5.2 : contenu adapté au type de compte (Client / Agence / Les deux).
    - §5.2 : reprise de la progression à la dernière vidéo visionnée.
    - §5.1 : possibilité de fermer/reporter le guide à la première
      connexion (skip_demo, distinct d'un guide réellement terminé).
"""

import frappe
from frappe import _
from frappe.utils import cint


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _require_logged_in():
    if frappe.session.user == "Guest":
        frappe.throw(_("Connexion requise."), frappe.PermissionError)
    return frappe.session.user


def _detect_account_type(user):
    """Déduit le type de compte (Client / Agence) à partir des profils liés."""
    if frappe.db.exists("ClientProfile", {"user": user}):
        return "Client"
    if frappe.db.exists("AgencyMember", {"user": user, "status": "Active"}):
        return "Agence"
    return None


def _get_or_create_progress(user=None):
    user = user or _require_logged_in()

    name = frappe.db.get_value("DemoProgress", {"user": user}, "name")
    if name:
        return frappe.get_doc("DemoProgress", name)

    doc = frappe.get_doc({
        "doctype": "DemoProgress",
        "user": user,
        "last_video": None,
        "completed": 0,
        "dismissed_on_first_login": 0,
    })
    doc.insert(ignore_permissions=True)
    return doc


def _get_video_list(account_type):
    filters = {"is_active": 1}
    if account_type:
        filters["account_type"] = ["in", [account_type, "Les deux"]]
    return frappe.get_all(
        "DemoVideo",
        filters=filters,
        fields=["name", "title", "account_type", "sequence_order", "video_url",
                "duration_seconds", "feature_tag"],
        order_by="sequence_order asc",
    )


# ---------------------------------------------------------------------------
# 1. get_demo_videos
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_demo_videos(account_type=None):
    """
    Liste des vidéos du guide interactif (§5.2), filtrées selon le type
    de compte. Si `account_type` n'est pas fourni explicitement (utile
    pour un utilisateur multi-rôles), il est déduit automatiquement des
    profils liés à l'utilisateur (Client / Agence).
    """
    user = _require_logged_in()

    if account_type and account_type not in ("Client", "Agence"):
        frappe.throw(_("Type de compte invalide : {0}").format(account_type))

    account_type = account_type or _detect_account_type(user)

    return _get_video_list(account_type)


# ---------------------------------------------------------------------------
# 2. get_demo_progress
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_demo_progress():
    """
    Progression actuelle de l'utilisateur dans le guide (§5.2), avec
    résolution de la prochaine vidéo à afficher (reprise automatique).
    """
    user = _require_logged_in()
    doc = _get_or_create_progress(user)

    account_type = _detect_account_type(user)
    videos = _get_video_list(account_type)

    next_video = None
    if not doc.completed and videos:
        if not doc.last_video:
            next_video = videos[0]
        else:
            positions = [v.name for v in videos]
            if doc.last_video in positions:
                idx = positions.index(doc.last_video)
                if idx + 1 < len(videos):
                    next_video = videos[idx + 1]
            else:
                # La dernière vidéo vue n'appartient plus à la liste active
                # (ex: désactivée depuis) -> on repart du début.
                next_video = videos[0]

    return {
        "last_video": doc.last_video,
        "completed": doc.completed,
        "dismissed_on_first_login": doc.dismissed_on_first_login,
        "next_video": next_video,
        "total_videos": len(videos),
    }


# ---------------------------------------------------------------------------
# 3. update_demo_progress
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_demo_progress(last_video, completed=None):
    """
    Enregistre la progression après visionnage d'une vidéo (§5.2, reprise
    de la progression). Si `completed` n'est pas fourni explicitement, le
    guide est marqué terminé automatiquement lorsque la vidéo visionnée
    est la dernière de la séquence active pour ce type de compte.
    """
    user = _require_logged_in()

    if not frappe.db.exists("DemoVideo", {"name": last_video, "is_active": 1}):
        frappe.throw(_("Vidéo introuvable ou inactive : {0}").format(last_video))

    doc = _get_or_create_progress(user)
    doc.last_video = last_video

    if completed is not None:
        doc.completed = cint(completed)
    else:
        account_type = _detect_account_type(user)
        videos = _get_video_list(account_type)
        if videos and videos[-1].name == last_video:
            doc.completed = 1

    doc.save(ignore_permissions=True)
    return {"last_video": doc.last_video, "completed": doc.completed}


# ---------------------------------------------------------------------------
# 4. skip_demo
# ---------------------------------------------------------------------------

@frappe.whitelist()
def skip_demo():
    """
    Ferme/reporte le guide (§5.1) — distinct d'un guide réellement
    terminé : `completed` n'est pas modifié, seul `dismissed_on_first_login`
    passe à 1, pour ne plus le proposer automatiquement à la connexion
    suivante. L'utilisateur pourra toujours le rouvrir manuellement (§5.1,
    "Reprise à tout moment").
    """
    user = _require_logged_in()
    doc = _get_or_create_progress(user)

    doc.dismissed_on_first_login = 1
    doc.save(ignore_permissions=True)

    return {"dismissed_on_first_login": True}
