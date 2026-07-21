# -*- coding: utf-8 -*-
"""
platform_core/api/opportunity.py

API Frontend — Module Agence / Opportunités (CDC v5, §1.3.3 et §2.3).

Couvre :
    get_opportunities, get_opportunity_detail, accept_opportunity,
    send_proposal, cancel_acceptance, download_cdc, get_matching_score,
    get_success_prediction

Règles métier clés respectées (cf. CDC v5) :
    - §1.3.3 : workflow en DEUX étapes distinctes.
        Étape 1 (accept_opportunity) : l'agence signale son intérêt.
            Opportunity.status: "Reçue" -> "Acceptée".
            Le projet reste "Postulé" côté client (aucun changement Project).
        Étape 2/3 (send_proposal) : devis chiffré = acceptation définitive.
            Opportunity.status: "Acceptée" -> "Devis envoyé".
            Project.status: "Posted" -> "Awaiting" côté client, avec un
            délai de réponse de PlatformSettings.quote_response_hours (48h).
    - Point d'attention §1.3.3 : une agence qui a Accepté mais n'a pas
      encore envoyé de devis peut annuler librement (cancel_acceptance).
      Après envoi du devis, toute annulation devient une exception
      encadrée par la modération — hors périmètre de cette fonction
      (nécessite un circuit de validation humaine, probablement dans
      proposal.py, pas ici).
    - §2.3 : onglets Offres / Gagnées / Terminées / Archivées. L'onglet
      "Disponibles" (vue exhaustive de tous les projets publics) N'EST
      PAS une liste d'Opportunity — c'est une recherche/découverte de
      projets, qui relève logiquement de search.py, pas de ce fichier.

NOTE : `_get_active_agency()` est dupliquée ici (cache Redis + fallback
DB), identique à la version d'agency.py. Cette logique existe maintenant
en double dans le projet (une version simplifiée dans project.py, la
version réelle dans agency.py, et ici) — fortement recommandé de la
centraliser dans utils.py dès que ce fichier sera créé.

Absent de ce fichier (volontairement, non demandé dans la liste des 8
API) : refuse_opportunity() — le refus à réception (§2.3, "Refuser (à
réception)"). Si c'est un oubli côté spec, dites-le et je l'ajoute.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date, cint, get_url


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

OPP_STATUS_RECEIVED = "Reçue"
OPP_STATUS_ACCEPTED = "Acceptée"
OPP_STATUS_QUOTE_SENT = "Devis envoyé"
OPP_STATUS_WON = "Gagnée"
OPP_STATUS_DONE = "Terminée"
OPP_STATUS_ARCHIVED = "Archivée"

TAB_OFFRES = "offres"
TAB_GAGNEES = "gagnees"
TAB_TERMINEES = "terminees"
TAB_ARCHIVEES = "archivees"

TAB_STATUS_MAP = {
    TAB_OFFRES: [OPP_STATUS_RECEIVED, OPP_STATUS_ACCEPTED, OPP_STATUS_QUOTE_SENT],
    TAB_GAGNEES: [OPP_STATUS_WON],
    TAB_TERMINEES: [OPP_STATUS_DONE],
    TAB_ARCHIVEES: [OPP_STATUS_ARCHIVED],
}

ACTIVE_AGENCY_CACHE_PREFIX = "active_agency"


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _get_active_agency(user=None):
    """
    cf. agency.py::_get_active_agency — À CENTRALISER dans utils.py.
    Duplication volontaire pour garder ce fichier autonome pour l'instant.
    """
    user = user or frappe.session.user
    cache_key = f"{ACTIVE_AGENCY_CACHE_PREFIX}:{user}"

    agency = frappe.cache().get_value(cache_key)
    if agency:
        still_active = frappe.db.exists(
            "AgencyMember", {"user": user, "agency": agency, "status": "Active"}
        )
        if still_active:
            return agency
        frappe.cache().delete_value(cache_key)

    fallback = frappe.db.get_value(
        "AgencyMember",
        {"user": user, "status": "Active"},
        "agency",
        order_by="member_role asc, joined_on asc",
    )
    if not fallback:
        frappe.throw(_("Aucune agence active pour cet utilisateur."), frappe.PermissionError)

    frappe.cache().set_value(cache_key, fallback)
    return fallback


def _get_opportunity(opportunity, agency=None):
    """Charge une Opportunity et vérifie qu'elle appartient à l'agence active."""
    agency = agency or _get_active_agency()
    doc = frappe.get_doc("Opportunity", opportunity)
    if doc.agency != agency:
        frappe.throw(_("Cette opportunité n'appartient pas à votre agence."),
                      frappe.PermissionError)
    return doc


def _notify(user, ntype, title, message, reference_doctype=None,
            reference_name=None, action_required=False, agency_context=None):
    if not user:
        return
    frappe.get_doc({
        "doctype": "Notification",
        "user": user,
        "type": ntype,
        "title": title,
        "message": message,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "agency_context": agency_context,
        "action_required": cint(action_required),
        "is_read": 0,
        "created_date": now_datetime(),
    }).insert(ignore_permissions=True)


def _get_client_user(project_name):
    client = frappe.db.get_value("Project", project_name, "client")
    return frappe.db.get_value("ClientProfile", client, "user")


# ---------------------------------------------------------------------------
# 1. get_opportunities
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_opportunities(tab=None, need_type=None, category=None, sub_category=None,
                       location=None, limit_start=0, limit_page_length=20,
                       order_by="modified desc"):
    """
    Liste des opportunités de l'agence active, organisée par onglet (§2.3) :
    "offres" | "gagnees" | "terminees" | "archivees". Sans `tab`, retourne
    tout sauf rien de filtré par statut (toutes les opportunités de l'agence).

    NB : l'onglet "Disponibles" (projets publics non encore liés à cette
    agence) n'est pas couvert ici — voir search.py.
    """
    agency = _get_active_agency()

    filters = {"agency": agency}
    if tab:
        if tab not in TAB_STATUS_MAP:
            frappe.throw(_("Onglet invalide : {0}").format(tab))
        filters["status"] = ["in", TAB_STATUS_MAP[tab]]

    opportunities = frappe.get_all(
        "Opportunity",
        filters=filters,
        fields=["name", "project", "status", "matching_score", "success_prediction",
                "source", "accepted_on", "archived_on", "archive_reason", "modified"],
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        order_by=order_by,
    )
    if not opportunities:
        return []

    project_names = [o.project for o in opportunities]
    project_filters = {"name": ["in", project_names]}
    if need_type:
        project_filters["need_type"] = need_type
    if category:
        project_filters["category"] = category
    if sub_category:
        project_filters["sub_category"] = sub_category
    if location:
        project_filters["location"] = ["like", f"%{location}%"]

    projects = {
        p.name: p for p in frappe.get_all(
            "Project",
            filters=project_filters,
            fields=["name", "title", "need_type", "category", "sub_category",
                    "budget_min", "budget_max", "location", "delivery_delay_days"],
        )
    }

    result = []
    for o in opportunities:
        project = projects.get(o.project)
        if not project:
            continue  # filtré par les critères projet ci-dessus
        result.append({**o, "project_detail": project})

    return result


# ---------------------------------------------------------------------------
# 2. get_opportunity_detail
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_opportunity_detail(opportunity):
    """Détail complet d'une opportunité, incluant le résumé du projet et du devis éventuel."""
    doc = _get_opportunity(opportunity)
    project = frappe.get_doc("Project", doc.project)

    proposal = frappe.get_all(
        "Proposal",
        filters={"opportunity": doc.name},
        fields=["name", "amount", "status", "submitted_date", "response_deadline",
                "extended_deadline", "decision_date"],
        order_by="creation desc",
        limit=1,
    )

    return {
        "name": doc.name,
        "status": doc.status,
        "source": doc.source,
        "matching_score": doc.matching_score,
        "success_prediction": doc.success_prediction,
        "accepted_on": doc.accepted_on,
        "archived_on": doc.archived_on,
        "archive_reason": doc.archive_reason,
        "project": {
            "name": project.name,
            "title": project.title,
            "description": project.description,
            "need_type": project.need_type,
            "category": project.category,
            "sub_category": project.sub_category,
            "budget_min": project.budget_min,
            "budget_max": project.budget_max,
            "location": project.location,
            "delivery_delay_days": project.delivery_delay_days,
            "has_cdc": bool(project.cdc_file),
        },
        "proposal": proposal[0] if proposal else None,
    }


# ---------------------------------------------------------------------------
# 3. accept_opportunity  (Étape 1)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def accept_opportunity(opportunity):
    """
    Étape 1 du workflow (§1.3.3) : l'agence signale son intérêt.

    Le projet reste "Postulé" côté client — seul l'Opportunity change de
    statut. Le client est notifié que sa demande est prise en charge,
    conformément au point d'attention §1.3.3.
    """
    doc = _get_opportunity(opportunity)

    if doc.status != OPP_STATUS_RECEIVED:
        frappe.throw(_("Cette opportunité ne peut plus être acceptée (statut actuel : {0}).")
                     .format(doc.status))

    doc.status = OPP_STATUS_ACCEPTED
    doc.accepted_on = now_datetime()
    doc.save(ignore_permissions=True)

    client_user = _get_client_user(doc.project)
    _notify(
        user=client_user,
        ntype="Proposal",
        title=_("Demande prise en charge"),
        message=_("Une agence a accepté votre demande et prépare un devis."),
        reference_doctype="Project",
        reference_name=doc.project,
    )

    return {"name": doc.name, "status": doc.status, "accepted_on": doc.accepted_on}


# ---------------------------------------------------------------------------
# 4. send_proposal  (Étape 2/3)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def send_proposal(opportunity, amount, description=None, devis_file=None):
    """
    Étape 2/3 du workflow (§1.3.3) : envoi du devis chiffré, qui vaut
    acceptation définitive du projet par l'agence.

    Effets :
        - Proposal créé (status="Sent"), response_deadline = maintenant +
          PlatformSettings.quote_response_hours (48h par défaut).
        - Opportunity.status -> "Devis envoyé"
        - Project.status -> "Awaiting" (En attente) côté client
        - Notification client (badge "devis envoyé")
    """
    doc = _get_opportunity(opportunity)

    if doc.status != OPP_STATUS_ACCEPTED:
        frappe.throw(_("Vous devez d'abord accepter l'opportunité avant d'envoyer un devis."))

    if not amount or float(amount) <= 0:
        frappe.throw(_("Le montant du devis doit être positif."))

    quote_response_hours = frappe.db.get_single_value(
        "PlatformSettings", "quote_response_hours"
    ) or 48
    response_deadline = add_to_date(now_datetime(), hours=cint(quote_response_hours))

    proposal = frappe.get_doc({
        "doctype": "Proposal",
        "opportunity": doc.name,
        "project": doc.project,
        "agency": doc.agency,
        "amount": amount,
        "description": description,
        "status": "Sent",
        "submitted_date": now_datetime(),
        "response_deadline": response_deadline,
        "devis_file": devis_file,
        "cancellation_status": "None",
    })
    proposal.insert(ignore_permissions=True)

    doc.status = OPP_STATUS_QUOTE_SENT
    doc.save(ignore_permissions=True)

    project = frappe.get_doc("Project", doc.project)
    project.status = "Awaiting"
    project.save(ignore_permissions=True)

    client_user = _get_client_user(doc.project)
    _notify(
        user=client_user,
        ntype="Proposal",
        title=_("Devis envoyé"),
        message=_("Vous avez reçu un devis pour votre projet « {0} ». Vous avez 48h pour répondre.")
                .format(project.title),
        reference_doctype="Proposal",
        reference_name=proposal.name,
        action_required=True,
    )

    return {
        "opportunity": doc.name,
        "opportunity_status": doc.status,
        "proposal": proposal.name,
        "project_status": project.status,
        "response_deadline": response_deadline,
    }


# ---------------------------------------------------------------------------
# 5. cancel_acceptance
# ---------------------------------------------------------------------------

@frappe.whitelist()
def cancel_acceptance(opportunity):
    """
    Annule librement une acceptation tant qu'aucun devis n'a été envoyé
    (§1.3.3, point d'attention : "une agence qui a Accepté mais n'a pas
    encore envoyé de devis peut annuler librement son acceptation").

    Au-delà (devis déjà envoyé), l'annulation devient une exception
    encadrée par la modération — hors périmètre de cette fonction.
    """
    doc = _get_opportunity(opportunity)

    if doc.status != OPP_STATUS_ACCEPTED:
        frappe.throw(_(
            "L'annulation libre n'est possible qu'avant l'envoi du devis "
            "(statut actuel : {0}). Passé ce stade, contactez la modération."
        ).format(doc.status))

    doc.status = OPP_STATUS_RECEIVED
    doc.accepted_on = None
    doc.save(ignore_permissions=True)

    return {"name": doc.name, "status": doc.status}


# ---------------------------------------------------------------------------
# 6. download_cdc
# ---------------------------------------------------------------------------

@frappe.whitelist()
def download_cdc(opportunity):
    """Retourne l'URL du CDC du projet lié à cette opportunité (§2.3)."""
    doc = _get_opportunity(opportunity)
    cdc_file = frappe.db.get_value("Project", doc.project, "cdc_file")

    if not cdc_file:
        frappe.throw(_("Aucun CDC disponible pour ce projet."))

    return {"cdc_file": get_url(cdc_file)}


# ---------------------------------------------------------------------------
# 7. get_matching_score
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_matching_score(opportunity):
    """
    Score de pertinence de l'opportunité (§2.3, "Scoring de pertinence").
    Si l'opportunité provient de la Shortlist IA, on tente aussi de
    récupérer un éventuel détail stocké dans Project.shortlist_ia.
    """
    doc = _get_opportunity(opportunity)

    detail = None
    if doc.source == "Shortlist IA":
        shortlist_raw = frappe.db.get_value("Project", doc.project, "shortlist_ia")
        if shortlist_raw:
            for row in json.loads(shortlist_raw):
                if row.get("agency") == doc.agency:
                    detail = row
                    break

    return {
        "opportunity": doc.name,
        "matching_score": doc.matching_score,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 8. get_success_prediction
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_success_prediction(opportunity):
    """Prédiction de succès de la collaboration pour cette opportunité (§3.3)."""
    doc = _get_opportunity(opportunity)
    return {
        "opportunity": doc.name,
        "success_prediction": doc.success_prediction,
    }
