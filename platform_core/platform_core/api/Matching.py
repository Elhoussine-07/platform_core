# -*- coding: utf-8 -*-
"""
platform_core/api/matching.py

API Frontend (Client) + API interne consommée par le microservice
Spring Boot matching-service (CDC v5, §1.3.4 "Shortlist IA" et §3.2).

Couvre :
    Frontend :
        get_matching_results(project) : Client, résultats de matching
    Interne (Spring Boot, matching-service — Token API) :
        trigger_matching(project) : fournit les données du projet à matcher
        update_matching_results(project, results) : pousse les scores calculés
        create_opportunities_from_matching(project, agencies) : matérialise
            les Opportunity pour les agences retenues (source="Shortlist IA")

INTERPRÉTATION DE LA CHORÉGRAPHIE (les 3 endpoints Spring Boot sont tous
marqués "Token API" dans la spec fournie -> je les traite comme 3 appels
ENTRANTS, initiés par matching-service, pas des appels sortants de Frappe) :
    1. matching-service appelle trigger_matching(project) pour récupérer
       les champs du brief (catégorie, budget, localisation, délai...).
    2. matching-service calcule ses scores en interne (algorithme propre
       au microservice, hors périmètre Frappe).
    3. matching-service appelle update_matching_results(project, results)
       pour pousser le classement (shortlist_ia + mise à jour des
       matching_score/success_prediction des Opportunity déjà existantes).
    4. matching-service appelle create_opportunities_from_matching(project,
       agencies) pour matérialiser de nouvelles Opportunity pour les
       agences retenues (source="Shortlist IA"), séparément de l'étape 3 —
       ceci permet de garder le scoring d'affichage (shortlist visible par
       le client) distinct de la décision métier "qui reçoit une
       opportunité réelle dans son onglet Offres".

Si cette chorégraphie ne correspond pas à ce que fait réellement
matching-service côté Spring Boot, dites-le : c'est la partie la plus
sujette à interprétation de ce fichier.

AUTH INTERNE : même mécanisme que prospection.py — token partagé via
header `X-Service-Token`, comparé à `frappe.conf.get("matching_service_token")`
(clé distincte de prospection pour ne pas partager le même secret entre
microservices).

NOTE : `get_matching_results()` duplique la logique de project.py::
get_shortlist_ia() (même lecture de Project.shortlist_ia + enrichissement
AgencyProfile). 4e occurrence de ce type de duplication dans le projet —
sérieusement recommandé de créer utils.py maintenant pour centraliser au
moins : get_active_agency, notify, et les helpers de lecture shortlist.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

PROJECT_STATUS_POSTED = "Posted"
OPPORTUNITY_STATUS_RECEIVED = "Reçue"
OPPORTUNITY_SOURCE_SHORTLIST = "Shortlist IA"


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _get_client_profile(user=None):
    user = user or frappe.session.user
    client = frappe.db.get_value("ClientProfile", {"user": user}, "name")
    if not client:
        frappe.throw(_("Aucun profil Entreprise associé à cet utilisateur."),
                      frappe.PermissionError)
    return client


def _verify_service_token():
    """Vérifie le header `X-Service-Token` envoyé par matching-service."""
    expected = frappe.conf.get("matching_service_token")
    if not expected:
        frappe.throw(_("Token de service non configuré côté serveur."), frappe.PermissionError)

    received = frappe.get_request_header("X-Service-Token")
    if not received or received != expected:
        frappe.throw(_("Token de service invalide."), frappe.PermissionError)


def _notify(user, ntype, title, message, reference_doctype=None,
            reference_name=None, action_required=False, agency_context=None):
    if not user:
        return
    from frappe.utils import now_datetime
    frappe.get_doc({
        "doctype": "Notification", "user": user, "type": ntype, "title": title,
        "message": message, "reference_doctype": reference_doctype,
        "reference_name": reference_name, "agency_context": agency_context,
        "action_required": cint(action_required), "is_read": 0,
        "created_date": now_datetime(),
    }).insert(ignore_permissions=True)


def _notify_agency_new_opportunity(agency, project_doc):
    owners = frappe.get_all(
        "AgencyMember", filters={"agency": agency, "status": "Active"}, pluck="user"
    )
    for owner in owners:
        _notify(
            user=owner, ntype="Project",
            title=_("Nouvelle opportunité (Shortlist IA)"),
            message=_("Votre agence a été présélectionnée pour le projet « {0} ».")
                    .format(project_doc.title),
            reference_doctype="Project", reference_name=project_doc.name,
            action_required=True, agency_context=agency,
        )


# =============================================================================
# FRONTEND
# =============================================================================

@frappe.whitelist()
def get_matching_results(project):
    """
    Résultats du matching pour un projet donné (§1.3.4 "Shortlist IA"),
    à destination du client propriétaire — lecture de Project.shortlist_ia
    (alimenté par update_matching_results ci-dessous), enrichi avec le
    profil public de chaque agence pour l'affichage.
    """
    client = _get_client_profile()

    doc = frappe.get_doc("Project", project)
    if doc.client != client:
        frappe.throw(_("Vous n'êtes pas propriétaire de ce projet."), frappe.PermissionError)

    raw = json.loads(doc.shortlist_ia) if doc.shortlist_ia else []
    if not raw:
        return []

    agency_names = [row.get("agency") for row in raw if row.get("agency")]
    profiles = {
        p.name: p for p in frappe.get_all(
            "AgencyProfile",
            filters={"name": ["in", agency_names]},
            fields=["name", "agency_name", "logo", "rating", "pqi_score", "location"],
        )
    }

    result = []
    for row in raw:
        profile = profiles.get(row.get("agency"))
        if not profile:
            continue
        result.append({
            "agency": profile.name,
            "agency_name": profile.agency_name,
            "logo": profile.logo,
            "rating": profile.rating,
            "pqi_score": profile.pqi_score,
            "location": profile.location,
            "matching_score": row.get("matching_score"),
            "success_prediction": row.get("success_prediction"),
        })

    result.sort(key=lambda r: r.get("matching_score") or 0, reverse=True)
    return result


# =============================================================================
# INTERNE — consommé par le microservice Spring Boot matching-service
# =============================================================================

@frappe.whitelist(allow_guest=True)
def trigger_matching(project):
    """
    Appelé par matching-service pour récupérer les données d'un projet à
    matcher. Ne déclenche aucun calcul côté Frappe — fournit simplement
    le brief structuré nécessaire à l'algorithme de matching.
    """
    _verify_service_token()

    doc = frappe.get_doc("Project", project)

    if doc.status != PROJECT_STATUS_POSTED:
        frappe.throw(_("Ce projet n'est pas au statut Postulé (statut actuel : {0}).")
                     .format(doc.status))

    return {
        "project": doc.name,
        "need_type": doc.need_type,
        "category": doc.category,
        "sub_category": doc.sub_category,
        "budget_min": doc.budget_min,
        "budget_max": doc.budget_max,
        "delivery_delay_days": doc.delivery_delay_days,
        "location": doc.location,
        "description": doc.description,
    }


@frappe.whitelist(allow_guest=True)
def update_matching_results(project, results):
    """
    Appelé par matching-service pour pousser le classement calculé.

    `results` : liste (ou JSON) de dicts {agency, matching_score,
    success_prediction}, triée ou non — sera re-triée ici par pertinence
    décroissante avant stockage dans Project.shortlist_ia.

    Met également à jour les Opportunity déjà existantes pour ce projet
    (créées via Unicast/Multicast ou une précédente Shortlist IA) afin de
    garder leurs matching_score/success_prediction à jour.
    """
    _verify_service_token()

    if isinstance(results, str):
        results = frappe.parse_json(results)

    if not isinstance(results, list):
        frappe.throw(_("`results` doit être une liste."))

    doc = frappe.get_doc("Project", project)

    cleaned = []
    for row in results:
        agency = row.get("agency")
        if not agency or not frappe.db.exists("AgencyProfile", agency):
            continue
        cleaned.append({
            "agency": agency,
            "matching_score": flt_score(row.get("matching_score")),
            "success_prediction": flt_score(row.get("success_prediction")),
        })

    cleaned.sort(key=lambda r: r["matching_score"], reverse=True)

    doc.db_set("shortlist_ia", json.dumps(cleaned))

    for row in cleaned:
        frappe.db.set_value(
            "Opportunity",
            {"project": project, "agency": row["agency"]},
            {"matching_score": row["matching_score"], "success_prediction": row["success_prediction"]},
        )

    return {"project": project, "count": len(cleaned)}


def flt_score(value):
    """Clamp un score reçu entre 0 et 100 (Percent field)."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0.0, min(100.0, value))


@frappe.whitelist(allow_guest=True)
def create_opportunities_from_matching(project, agencies, top_n=None):
    """
    Appelé par matching-service pour matérialiser des Opportunity
    (source="Shortlist IA") pour les agences retenues, distinctement de
    update_matching_results() qui ne fait que pousser les scores d'affichage.

    `agencies` : liste (ou JSON) de noms d'AgencyProfile à convertir en
    Opportunity. Les agences ayant déjà une Opportunity sur ce projet sont
    ignorées (pas de doublon).
    """
    _verify_service_token()

    doc = frappe.get_doc("Project", project)

    if isinstance(agencies, str):
        agencies = frappe.parse_json(agencies)

    if not agencies or not isinstance(agencies, list):
        frappe.throw(_("`agencies` doit être une liste non vide."))

    if top_n:
        agencies = agencies[:cint(top_n)]

    created, skipped = [], []

    for agency in agencies:
        if not frappe.db.exists("AgencyProfile", agency):
            skipped.append({"agency": agency, "reason": "agency_not_found"})
            continue

        if frappe.db.exists("Opportunity", {"project": project, "agency": agency}):
            skipped.append({"agency": agency, "reason": "already_exists"})
            continue

        if cint(frappe.db.get_value("AgencyProfile", agency, "offers_suspended")):
            skipped.append({"agency": agency, "reason": "offers_suspended"})
            continue

        opportunity = frappe.get_doc({
            "doctype": "Opportunity",
            "project": project,
            "agency": agency,
            "status": OPPORTUNITY_STATUS_RECEIVED,
            "source": OPPORTUNITY_SOURCE_SHORTLIST,
        })
        opportunity.insert(ignore_permissions=True)
        _notify_agency_new_opportunity(agency, doc)
        created.append({"agency": agency, "opportunity": opportunity.name})

    return {"project": project, "created": created, "skipped": skipped}
