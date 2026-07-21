# -*- coding: utf-8 -*-
"""
platform_core/api/ia.py

API interne consommée par le microservice FastAPI ia-service (CDC v5,
§1.2 "Enrichissement du brief", §2.6 "Emails IA", §3.3 "Prédiction de succès").

Couvre (3 endpoints, tous Token API, aucun accès Frontend direct) :
    generate_project_brief, predict_success, generate_email_content

MODÈLE RETENU : "push résultats" — symétrique à matching.py et
prospection.py. Le calcul réel (appel OpenAI, NLP) a lieu entièrement
dans ia-service (openai_client.py / nlp_service.py, hors périmètre
Frappe) ; ia-service appelle ensuite ces 3 endpoints pour PERSISTER le
résultat côté Frappe.

CONFLIT ARCHITECTURAL SIGNALÉ (important) :
    prospection.py::generate_prospection_email() a été construit de façon
    SYNCHRONE — Frappe appelle ia-service, attend la réponse HTTP, et crée
    directement le ProspectionEmail rempli en un seul temps. Ce fichier
    suppose au contraire un flux asynchrone (ia-service calcule, puis
    rappelle Frappe séparément). Les deux ne sont pas rigoureusement
    compatibles : le schéma ProspectionEmail (statuts Draft/Sent/Replied,
    sans état intermédiaire "Generating"/"Pending") colle mieux au modèle
    synchrone déjà en place.

    Pour ne rien casser : generate_email_content() ci-dessous gère les
    DEUX cas (met à jour un ProspectionEmail existant si fourni, sinon en
    crée un nouveau). Mais il faudra trancher explicitement à un moment :
    - Option A (simplicité) : garder prospection.py synchrone tel quel,
      et ce fichier devient largement redondant pour les emails.
    - Option B (robustesse) : passer tout le flux IA en asynchrone
      (meilleur pour des appels LLM potentiellement lents), ce qui
      demande d'ajouter un statut intermédiaire au DocType ProspectionEmail
      et de revoir prospection.py en conséquence.

    Je n'ai pas tranché à ta place — dis-moi laquelle tu veux et j'ajuste.

AUTH INTERNE : même mécanisme que les autres microservices — token
partagé via header `X-Service-Token`, comparé à
`frappe.conf.get("ia_service_token")` (clé distincte des autres services).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, cint, flt, add_months


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _verify_service_token():
    """Vérifie le header `X-Service-Token` envoyé par ia-service."""
    expected = frappe.conf.get("ia_service_token")
    if not expected:
        frappe.throw(_("Token de service non configuré côté serveur."), frappe.PermissionError)

    received = frappe.get_request_header("X-Service-Token")
    if not received or received != expected:
        frappe.throw(_("Token de service invalide."), frappe.PermissionError)


def _notify(user, ntype, title, message, reference_doctype=None,
            reference_name=None, action_required=False, agency_context=None):
    if not user:
        return
    frappe.get_doc({
        "doctype": "Notification", "user": user, "type": ntype, "title": title,
        "message": message, "reference_doctype": reference_doctype,
        "reference_name": reference_name, "agency_context": agency_context,
        "action_required": cint(action_required), "is_read": 0,
        "created_date": now_datetime(),
    }).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# 1. generate_project_brief
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def generate_project_brief(project, enriched_description=None, suggested_category=None,
                            suggested_sub_category=None, suggested_budget_min=None,
                            suggested_budget_max=None):
    """
    Persiste l'enrichissement du brief calculé par ia-service (§1.2) :
    reformulation de la description, catégorisation automatique, et
    suggestion de budget moyen du secteur.

    Règle : on ne complète QUE les champs vides — l'IA n'écrase jamais
    une valeur déjà renseignée par le client ("le questionnaire alimente
    la génération... ce document peut ensuite être modifié... uniquement
    par le client lui-même", §1.2).
    """
    _verify_service_token()

    doc = frappe.get_doc("Project", project)

    if enriched_description and not doc.description:
        doc.description = enriched_description

    if suggested_category and not doc.category:
        doc.category = suggested_category

    if suggested_sub_category and not doc.sub_category:
        doc.sub_category = suggested_sub_category

    if suggested_budget_min and not doc.budget_min:
        doc.budget_min = suggested_budget_min

    if suggested_budget_max and not doc.budget_max:
        doc.budget_max = suggested_budget_max

    doc.save(ignore_permissions=True)

    client_user = frappe.db.get_value("ClientProfile", doc.client, "user")
    _notify(
        user=client_user, ntype="Project",
        title=_("Brief enrichi par l'IA"),
        message=_("Votre projet « {0} » a été complété automatiquement, vous pouvez le relire.")
                .format(doc.title),
        reference_doctype="Project", reference_name=doc.name,
    )

    return {
        "project": doc.name,
        "description": doc.description,
        "category": doc.category,
        "sub_category": doc.sub_category,
        "budget_min": doc.budget_min,
        "budget_max": doc.budget_max,
    }


# ---------------------------------------------------------------------------
# 2. predict_success
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def predict_success(project, agency, success_prediction, factors=None):
    """
    Persiste une prédiction de succès de collaboration (§3.3) pour un
    couple (projet, agence).

    Si une Opportunity existe déjà pour ce couple (cas Shortlist IA /
    Unicast / Multicast), la valeur est écrite directement dessus. Sinon
    (ex: le client consulte une agence hors shortlist avant tout contact),
    aucun champ dédié n'existe dans le schéma fourni pour stocker une
    prédiction "pré-contact" : la valeur est mise en cache (courte durée)
    pour être réutilisée si un contact est initié peu après. Si vous
    voulez cette prédiction affichable durablement hors contexte
    d'Opportunity, il faudra ajouter un champ/DocType dédié.
    """
    _verify_service_token()

    if not frappe.db.exists("Project", project):
        frappe.throw(_("Projet introuvable : {0}").format(project))
    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable : {0}").format(agency))

    score = max(0.0, min(100.0, flt(success_prediction)))

    opportunity_name = frappe.db.get_value(
        "Opportunity", {"project": project, "agency": agency}, "name"
    )

    if opportunity_name:
        frappe.db.set_value("Opportunity", opportunity_name, "success_prediction", score)
        return {"persisted_to": "Opportunity", "opportunity": opportunity_name,
                "success_prediction": score}

    cache_key = f"success_prediction:{project}:{agency}"
    frappe.cache().set_value(cache_key, {"score": score, "factors": factors}, expires_in_sec=3600)

    return {"persisted_to": "cache", "cache_key": cache_key, "success_prediction": score}


# ---------------------------------------------------------------------------
# 3. generate_email_content
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def generate_email_content(lead, agency, ai_generated_body, subject=None,
                            prospection_email=None):
    """
    Persiste le contenu d'un email de prospection généré par ia-service
    (§2.6). Gère deux cas pour rester compatible avec les deux modèles
    possibles (cf. note de conflit en tête de fichier) :

        - `prospection_email` fourni : met à jour un ProspectionEmail
          existant (flux asynchrone : un brouillon vide avait été créé
          en amont, en attente de contenu).
        - `prospection_email` absent : crée directement un nouveau
          ProspectionEmail (flux synchrone équivalent à celui déjà
          utilisé dans prospection.py::generate_prospection_email).
    """
    _verify_service_token()

    if not frappe.db.exists("DetectedLead", lead):
        frappe.throw(_("Lead introuvable : {0}").format(lead))
    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable : {0}").format(agency))

    if prospection_email:
        doc = frappe.get_doc("ProspectionEmail", prospection_email)
        if doc.agency != agency or doc.lead != lead:
            frappe.throw(_("Ce brouillon ne correspond pas au couple lead/agence fourni."))
        doc.ai_generated_body = ai_generated_body
        if subject:
            doc.subject = subject
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({
            "doctype": "ProspectionEmail",
            "lead": lead,
            "agency": agency,
            "subject": subject or _("Découvrez comment nous pouvons vous aider"),
            "ai_generated_body": ai_generated_body,
            "status": "Draft",
        })
        doc.insert(ignore_permissions=True)

    return {"prospection_email": doc.name, "status": doc.status}
