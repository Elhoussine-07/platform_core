# -*- coding: utf-8 -*-
"""
api/review.py
=============
APIs Frontend (avis réciproques Client <-> Agence).

Références CDC v5 :
- 1.4   Collaborations - "Espace d'avis par agence" + "Notation réciproque (agence -> client)"
- 2.2.6 Profil Agence - Avis (avis interne, anti-faux-avis, historique)
- 2.3   Opportunités - "Sur une opportunité Terminée, l'agence peut noter le client"
- 1.1   Score de confiance Entreprise (alimenté par les avis reçus des agences)

Références doctypes_final.pdf :
- AgencyReview, ClientReview, Project, ClientProfile, AgencyProfile, Opportunity

Règle anti-faux-avis (2.2.6) : un avis ne peut être déposé QUE si une
collaboration réellement terminée existe entre les deux parties (Project au
statut "Completed" + Opportunity correspondante au statut "Terminée"/"Gagnée").
Un seul avis par (auteur, projet).

Échelle de notation retenue : entiers de 1 à 5 (à ajuster si une autre
échelle est retenue côté produit).
"""

import frappe
from frappe import _
from frappe.utils import flt


RATING_MIN = 1
RATING_MAX = 5


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _get_active_agency(user=None):
    """
    Retourne le nom (AgencyProfile) de l'agence active pour l'utilisateur
    connecté, selon le contexte de bascule multi-agences (2.1.1).
    Convention : cache Redis `active_agency:<user>` positionné par
    l'API de switch d'agence (cf. api/agency.py).
    """
    user = user or frappe.session.user
    cached = frappe.cache().get_value(f"active_agency:{user}")
    if cached:
        return cached

    memberships = frappe.get_all(
        "AgencyMember", filters={"user": user, "status": "Active"}, pluck="agency",
    )
    if not memberships:
        frappe.throw(_("Aucune agence active trouvée pour cet utilisateur."), frappe.PermissionError)
    if len(memberships) > 1:
        frappe.throw(_("Sélectionnez une agence active via le switch avant de continuer."))
    return memberships[0]


def _get_client_profile(user=None):
    """Retourne le nom du ClientProfile associé à l'utilisateur connecté."""
    user = user or frappe.session.user
    client = frappe.db.get_value("ClientProfile", {"user": user})
    if not client:
        frappe.throw(_("Aucun profil Entreprise trouvé pour cet utilisateur."), frappe.PermissionError)
    return client


def _validate_score(value, field_label):
    if value in (None, ""):
        return None
    value = int(value)
    if value < RATING_MIN or value > RATING_MAX:
        frappe.throw(_("{0} doit être compris entre {1} et {2}.").format(field_label, RATING_MIN, RATING_MAX))
    return value


def _check_completed_collaboration(project_name, client, agency):
    """
    Vérifie qu'une collaboration réellement terminée existe entre ce client
    et cette agence sur ce projet (1.4 - "projets au statut Terminé").
    Retourne le document Project si la vérification est concluante.
    """
    project = frappe.db.get_value(
        "Project", project_name, ["name", "client", "status"], as_dict=True,
    )
    if not project:
        frappe.throw(_("Projet introuvable."), frappe.DoesNotExistError)
    if project.client != client:
        frappe.throw(_("Ce projet n'appartient pas à votre compte."), frappe.PermissionError)
    if project.status != "Completed":
        frappe.throw(_("Ce projet n'est pas encore au statut Terminé."))

    opportunity_exists = frappe.db.exists(
        "Opportunity",
        {"project": project_name, "agency": agency, "status": ["in", ["Terminée", "Gagnée"]]},
    )
    if not opportunity_exists:
        frappe.throw(
            _("Aucune collaboration terminée n'a été trouvée entre ce client et cette agence sur ce projet."),
            frappe.PermissionError,
        )
    return project


def _recompute_agency_rating(agency):
    """Recalcule AgencyProfile.rating et reviews_count à partir des avis approuvés (2.2.6)."""
    stats = frappe.db.sql(
        """
        SELECT COUNT(*) AS cnt, COALESCE(AVG(rating), 0) AS avg_rating
        FROM `tabAgencyReview`
        WHERE agency = %s AND status = 'Approved'
        """,
        (agency,), as_dict=True,
    )[0]

    frappe.db.set_value("AgencyProfile", agency, {
        "rating": flt(stats.avg_rating, 2),
        "reviews_count": stats.cnt,
    })


def _recompute_client_trust_score(client):
    """
    Recalcule (partiellement) le score de confiance Entreprise (1.1),
    en tenant compte des avis reçus des agences (ClientReview approuvés).
    NB : formule simplifiée - à affiner avec la pondération métier réelle
    (identifiant légal vérifié, historique de collaborations terminées,
    avis reçus). Ici on ne recalcule que la composante "avis".
    """
    stats = frappe.db.sql(
        """
        SELECT COUNT(*) AS cnt, COALESCE(AVG(rating), 0) AS avg_rating
        FROM `tabClientReview`
        WHERE client = %s
        """,
        (client,), as_dict=True,
    )[0]

    client_doc = frappe.db.get_value(
        "ClientProfile", client,
        ["legal_id_verified", "projects_published_count"], as_dict=True,
    )

    reviews_component = flt(stats.avg_rating) / RATING_MAX * 100 if stats.cnt else 0
    legal_component = 20 if client_doc.legal_id_verified else 0

    # Pondération simplifiée : 70% avis reçus, 20% identité vérifiée, 10% base.
    trust_score = flt(reviews_component * 0.7 + legal_component + 10, 2)
    trust_score = min(trust_score, 100)

    frappe.db.set_value("ClientProfile", client, "trust_score", trust_score)


# ---------------------------------------------------------------------------
# 1. submit_agency_review (Client)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def submit_agency_review(project_name, rating, comment=None, quality_score=None,
                          deadline_score=None, communication_score=None,
                          value_score=None, understanding_score=None, agency=None):
    """
    Dépose un avis noté et commenté sur une agence, à l'issue d'une
    collaboration terminée (1.4 - Espace d'avis par agence ; 2.2.6 - Avis).

    Un seul avis par (client, projet). L'agence est déduite de l'Opportunity
    "Gagnée"/"Terminée" liée au projet si non fournie explicitement.
    """
    client = _get_client_profile()
    rating = _validate_score(rating, _("La note globale"))
    if rating is None:
        frappe.throw(_("La note globale est obligatoire."))

    if not agency:
        agency = frappe.db.get_value(
            "Opportunity", {"project": project_name, "status": ["in", ["Terminée", "Gagnée"]]}, "agency",
        )
        if not agency:
            frappe.throw(_("Impossible de déterminer l'agence associée à ce projet terminé."))

    _check_completed_collaboration(project_name, client, agency)

    if frappe.db.exists("AgencyReview", {"project": project_name, "client": client, "agency": agency}):
        frappe.throw(_("Vous avez déjà déposé un avis pour cette collaboration."))

    review = frappe.get_doc({
        "doctype": "AgencyReview",
        "client": client,
        "agency": agency,
        "project": project_name,
        "rating": rating,
        "quality_score": _validate_score(quality_score, _("Qualité du travail")),
        "deadline_score": _validate_score(deadline_score, _("Respect des délais")),
        "communication_score": _validate_score(communication_score, _("Communication")),
        "value_score": _validate_score(value_score, _("Rapport qualité-prix")),
        "understanding_score": _validate_score(understanding_score, _("Compréhension du besoin")),
        "comment": comment,
        "status": "Approved",
        "is_verified": 1,
    }).insert(ignore_permissions=True)

    _recompute_agency_rating(agency)

    return {"review": review.name, "status": review.status}


# ---------------------------------------------------------------------------
# 2. submit_client_review (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def submit_client_review(project_name, rating, comment=None, client=None):
    """
    Dépose la notation réciproque de l'agence envers le client, sur une
    collaboration terminée (1.4 - "Notation réciproque (agence -> client)" ;
    2.3 - "Sur une opportunité Terminée, l'agence peut noter le client").
    Cette note alimente le Score de confiance Entreprise (1.1).
    """
    agency = _get_active_agency()
    rating = _validate_score(rating, _("La note"))
    if rating is None:
        frappe.throw(_("La note est obligatoire."))

    if not client:
        client = frappe.db.get_value("Project", project_name, "client")
        if not client:
            frappe.throw(_("Impossible de déterminer le client associé à ce projet."))

    _check_completed_collaboration(project_name, client, agency)

    if frappe.db.exists("ClientReview", {"project": project_name, "agency": agency, "client": client}):
        frappe.throw(_("Vous avez déjà noté ce client pour cette collaboration."))

    review = frappe.get_doc({
        "doctype": "ClientReview",
        "agency": agency,
        "client": client,
        "project": project_name,
        "rating": rating,
        "comment": comment,
    }).insert(ignore_permissions=True)

    _recompute_client_trust_score(client)

    return {"review": review.name}


# ---------------------------------------------------------------------------
# 3. get_agency_reviews (public - allow_guest=True)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_agency_reviews(agency, page=1, page_size=10):
    """
    Liste publique des avis d'une agence (2.2.6 - section Avis du profil
    public). Seuls les avis au statut "Approved" sont visibles par le grand
    public ; un Moderator/System Manager connecté voit l'ensemble des avis
    (y compris Pending/Rejected) à des fins de modération.
    """
    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable."), frappe.DoesNotExistError)

    filters = {"agency": agency}
    is_moderator = frappe.session.user != "Guest" and (
        "Moderator" in frappe.get_roles(frappe.session.user)
        or "System Manager" in frappe.get_roles(frappe.session.user)
    )
    if not is_moderator:
        filters["status"] = "Approved"

    page = int(page)
    page_size = int(page_size)

    reviews = frappe.get_all(
        "AgencyReview",
        filters=filters,
        fields=[
            "name", "client", "rating", "quality_score", "deadline_score",
            "communication_score", "value_score", "understanding_score",
            "comment", "status", "is_verified", "creation",
        ],
        order_by="creation desc",
        start=(page - 1) * page_size,
        page_length=page_size,
    )
    total_count = frappe.db.count("AgencyReview", filters=filters)

    agency_stats = frappe.db.get_value(
        "AgencyProfile", agency, ["rating", "reviews_count"], as_dict=True,
    )

    return {
        "reviews": reviews,
        "average_rating": agency_stats.rating,
        "total_reviews": agency_stats.reviews_count,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# 4. get_client_reviews (Client ou Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_client_reviews(page=1, page_size=10):
    """
    Liste des avis reçus par un client (vu par le client lui-même, sur son
    propre profil - contribue au Score de confiance, cf. 1.1), ou liste des
    avis déposés par l'agence active sur ses clients (vu côté agence).

    - Un utilisateur Client ne voit que les avis le concernant.
    - Un utilisateur Agency ne voit que les avis qu'il a lui-même déposés.
    """
    page = int(page)
    page_size = int(page_size)
    roles = frappe.get_roles(frappe.session.user)

    if "Client" in roles:
        client = _get_client_profile()
        filters = {"client": client}
    elif "Agency" in roles:
        agency = _get_active_agency()
        filters = {"agency": agency}
    else:
        frappe.throw(_("Accès réservé aux comptes Entreprise ou Agence."), frappe.PermissionError)

    reviews = frappe.get_all(
        "ClientReview",
        filters=filters,
        fields=["name", "agency", "client", "project", "rating", "comment", "creation"],
        order_by="creation desc",
        start=(page - 1) * page_size,
        page_length=page_size,
    )
    total_count = frappe.db.count("ClientReview", filters=filters)

    return {
        "reviews": reviews,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }