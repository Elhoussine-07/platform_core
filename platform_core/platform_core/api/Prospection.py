# -*- coding: utf-8 -*-
"""
platform_core/api/prospection.py

API Frontend (Client/Agency) + API interne consommée par le microservice
Node.js prospection-service (CDC v5, §2.6 et §2.6.1).

Couvre :
    Frontend :
        track_activity, get_lead_score, get_detected_leads,
        generate_prospection_email, send_prospection_email,
        get_campaign_stats, sync_to_crm
    Interne (Node.js, prospection-service) :
        get_scoring_rules (allow_guest=True + vérification token)
        update_lead_score (Token API)

Distinction de modèle importante (déduite du schéma des 42 DocTypes) :
    - AgencyActivity  : activité d'un CLIENT CONNU/authentifié sur un profil
      agence (profile_view, portfolio_view, ...). Sert à l'analytics agence
      et aux "Emails automatiques Premium" (§2.4) — PAS de scoring associé
      (aucun champ points sur ce DocType).
    - VisitorLog / DetectedLead : visiteurs ANONYMES résolus par IP (§2.6),
      scorés via LeadScoringRule (§2.6.1) et classés chaud/tiède/froid.
      Ces données sont écrites par le microservice Node.js via l'API
      interne update_lead_score(), pas par le frontend client.

ÉCART DE SCHÉMA SIGNALÉ : ni DetectedLead ni ProspectionEmail ne
contiennent de champ "email destinataire" dans les 42 DocTypes fournis
(seuls ip_hash / resolved_company_name existent). send_prospection_email()
ne peut donc PAS réellement expédier un email — il marque uniquement le
statut "Sent". Il manque soit un champ contact_email sur DetectedLead
(résolu par enrichment externe), soit ce flux est géré ailleurs (CRM).
À clarifier avant mise en production.

AUTH INTERNE : token partagé simple via header `X-Service-Token`, comparé
à `frappe.conf.get("prospection_service_token")` (site_config.json). Choix
pragmatique par défaut — à remplacer par JWT/HMAC signé si vous voulez
un mécanisme plus robuste entre stacks hétérogènes.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime, add_days, cint, flt


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ACTIVE_AGENCY_CACHE_PREFIX = "active_agency"

ACTIVITY_TYPES = {
    "profile_view", "portfolio_view", "reviews_view", "team_view",
    "certificates_view", "add_favorite", "send_message", "request_quote",
    "share_profile",
}

CLASSIFICATION_HOT = "hot"
CLASSIFICATION_WARM = "warm"
CLASSIFICATION_COLD = "cold"


# ---------------------------------------------------------------------------
# Helpers internes — session Frappe (Frontend)
# ---------------------------------------------------------------------------

def _get_client_profile(user=None):
    user = user or frappe.session.user
    client = frappe.db.get_value("ClientProfile", {"user": user}, "name")
    if not client:
        frappe.throw(_("Aucun profil Entreprise associé à cet utilisateur."),
                      frappe.PermissionError)
    return client


def _get_active_agency(user=None):
    """cf. agency.py — à centraliser dans utils.py."""
    user = user or frappe.session.user
    cache_key = f"{ACTIVE_AGENCY_CACHE_PREFIX}:{user}"

    agency = frappe.cache().get_value(cache_key)
    if agency:
        if frappe.db.exists("AgencyMember", {"user": user, "agency": agency, "status": "Active"}):
            return agency
        frappe.cache().delete_value(cache_key)

    fallback = frappe.db.get_value(
        "AgencyMember", {"user": user, "status": "Active"}, "agency",
        order_by="member_role asc, joined_on asc",
    )
    if not fallback:
        frappe.throw(_("Aucune agence active pour cet utilisateur."), frappe.PermissionError)

    frappe.cache().set_value(cache_key, fallback)
    return fallback


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
# Helpers internes — auth microservice (Node.js)
# ---------------------------------------------------------------------------

def _verify_service_token():
    """
    Vérifie le header `X-Service-Token` envoyé par prospection-service.
    Lève une PermissionError si absent ou invalide.
    """
    expected = frappe.conf.get("prospection_service_token")
    if not expected:
        frappe.throw(_("Token de service non configuré côté serveur."), frappe.PermissionError)

    received = frappe.get_request_header("X-Service-Token")
    if not received or received != expected:
        frappe.throw(_("Token de service invalide."), frappe.PermissionError)


def _get_platform_thresholds():
    settings = frappe.get_single("PlatformSettings")
    return {
        "lead_hot_threshold": cint(settings.lead_hot_threshold) or 40,
        "lead_warm_min": cint(settings.lead_warm_min) or 15,
        "lead_warm_max": cint(settings.lead_warm_max) or 39,
        "lead_score_window_days": cint(settings.lead_score_window_days) or 7,
    }


def _classify(cumulative_score, has_favorited, thresholds):
    if has_favorited or cumulative_score >= thresholds["lead_hot_threshold"]:
        return CLASSIFICATION_HOT
    if thresholds["lead_warm_min"] <= cumulative_score <= thresholds["lead_warm_max"]:
        return CLASSIFICATION_WARM
    return CLASSIFICATION_COLD


# =============================================================================
# FRONTEND
# =============================================================================

# ---------------------------------------------------------------------------
# 1. track_activity (Client)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def track_activity(agency, action_type, action_details=None, time_spent=0):
    """
    Enregistre une activité d'un client CONNU sur le profil d'une agence
    (§2.6 / doctype AgencyActivity). Déclenche la notification "Emails
    automatiques Premium" côté agence (§2.4) en temps réel.

    NB : ceci alimente l'analytics agence (AgencyActivity), PAS le score
    de prospection anonyme (chaud/tiède/froid), qui repose sur le flux
    IP anonyme via update_lead_score() (Node.js).
    """
    client = _get_client_profile()

    if action_type not in ACTIVITY_TYPES:
        frappe.throw(_("Type d'action invalide : {0}").format(action_type))

    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable."))

    activity = frappe.get_doc({
        "doctype": "AgencyActivity",
        "client": client,
        "agency": agency,
        "action_type": action_type,
        "action_details": json.dumps(action_details) if action_details else None,
        "time_spent": cint(time_spent),
        "created_date": now_datetime(),
    })
    activity.insert(ignore_permissions=True)

    owners = frappe.get_all(
        "AgencyMember", filters={"agency": agency, "status": "Active"}, pluck="user"
    )
    for owner in owners:
        _notify(
            user=owner, ntype="System Alert",
            title=_("Activité sur votre profil"),
            message=_("Un client a effectué une action « {0} » sur votre profil.")
                    .format(action_type),
            reference_doctype="AgencyActivity", reference_name=activity.name,
            agency_context=agency,
        )

    return {"name": activity.name}


# ---------------------------------------------------------------------------
# 2. get_lead_score (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_lead_score(lead=None):
    """
    Score d'intention d'un lead détecté (§2.6.1).

    Sans `lead` : retourne un résumé agrégé (répartition chaud/tiède/froid)
    pour l'agence active. Avec `lead` : détail d'un DetectedLead précis,
    y compris les événements VisitorLog qui ont contribué au score dans
    la fenêtre glissante configurée (PlatformSettings.lead_score_window_days).
    """
    agency = _get_active_agency()

    if not lead:
        counts = frappe.db.get_all(
            "DetectedLead",
            filters={"agency": agency},
            fields=["classification", "count(name) as total"],
            group_by="classification",
        )
        return {row.classification: row.total for row in counts}

    doc = frappe.get_doc("DetectedLead", lead)
    if doc.agency != agency:
        frappe.throw(_("Ce lead n'appartient pas à votre agence."), frappe.PermissionError)

    thresholds = _get_platform_thresholds()
    window_start = add_days(now_datetime(), -thresholds["lead_score_window_days"])

    events = frappe.get_all(
        "VisitorLog",
        filters={"agency": agency, "lead": lead, "visit_date": [">=", window_start]},
        fields=["visit_date", "page_url", "points_awarded", "bonus_awarded"],
        order_by="visit_date desc",
    )

    return {
        "lead": doc.name,
        "classification": doc.classification,
        "cumulative_score": doc.cumulative_score,
        "has_favorited": doc.has_favorited,
        "first_seen": doc.first_seen,
        "last_seen": doc.last_seen,
        "window_days": thresholds["lead_score_window_days"],
        "contributing_events": events,
    }


# ---------------------------------------------------------------------------
# 3. get_detected_leads (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_detected_leads(classification=None, limit_start=0, limit_page_length=20,
                        order_by="cumulative_score desc"):
    """Liste des leads détectés pour l'agence active, filtrable par classification."""
    agency = _get_active_agency()

    filters = {"agency": agency}
    if classification:
        if classification not in (CLASSIFICATION_HOT, CLASSIFICATION_WARM, CLASSIFICATION_COLD):
            frappe.throw(_("Classification invalide : {0}").format(classification))
        filters["classification"] = classification

    return frappe.get_all(
        "DetectedLead",
        filters=filters,
        fields=["name", "resolved_company_name", "cumulative_score", "classification",
                "has_favorited", "first_seen", "last_seen"],
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        order_by=order_by,
    )


# ---------------------------------------------------------------------------
# 4. generate_prospection_email (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def generate_prospection_email(lead, subject=None):
    """
    Génère un email de prospection personnalisé via le microservice IA
    (ia-service, FastAPI — §2.6 "Génération automatique d'emails IA").

    Le service IA est externe : configurez son URL dans site_config.json
    sous la clé `ia_service_url`. Cette fonction crée le ProspectionEmail
    en "Draft" uniquement si la génération réussit.
    """
    agency = _get_active_agency()

    lead_doc = frappe.get_doc("DetectedLead", lead)
    if lead_doc.agency != agency:
        frappe.throw(_("Ce lead n'appartient pas à votre agence."), frappe.PermissionError)

    ia_service_url = frappe.conf.get("ia_service_url")
    if not ia_service_url:
        frappe.throw(_("Service IA non configuré (ia_service_url manquant)."))

    try:
        import requests
        response = requests.post(
            f"{ia_service_url.rstrip('/')}/generate-prospection-email",
            json={
                "agency": agency,
                "company_name": lead_doc.resolved_company_name,
                "classification": lead_doc.classification,
            },
            timeout=15,
        )
        response.raise_for_status()
        generated_body = response.json().get("body")
    except Exception:
        frappe.log_error(title="Échec génération email IA (prospection)")
        frappe.throw(_("La génération de l'email a échoué. Réessayez plus tard."))

    if not generated_body:
        frappe.throw(_("Le service IA n'a retourné aucun contenu."))

    email = frappe.get_doc({
        "doctype": "ProspectionEmail",
        "lead": lead,
        "agency": agency,
        "subject": subject or _("Découvrez comment nous pouvons vous aider"),
        "ai_generated_body": generated_body,
        "status": "Draft",
    })
    email.insert(ignore_permissions=True)

    return {"name": email.name, "subject": email.subject, "ai_generated_body": generated_body}


# ---------------------------------------------------------------------------
# 5. send_prospection_email (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def send_prospection_email(prospection_email):
    """
    Marque l'email de prospection comme envoyé.

    LIMITATION CONNUE (cf. note en tête de fichier) : aucun champ email
    destinataire n'existe sur DetectedLead/ProspectionEmail dans le schéma
    fourni. Cette fonction ne dispatche donc PAS réellement l'email — elle
    met seulement à jour le statut. À corriger dès qu'un champ de contact
    (ou un flux d'enrichment externe) sera disponible.
    """
    agency = _get_active_agency()

    doc = frappe.get_doc("ProspectionEmail", prospection_email)
    if doc.agency != agency:
        frappe.throw(_("Cet email n'appartient pas à votre agence."), frappe.PermissionError)

    if doc.status != "Draft":
        frappe.throw(_("Seul un email en brouillon peut être envoyé."))

    # TODO : dispatch réel une fois un champ de contact disponible.
    doc.status = "Sent"
    doc.sent_on = now_datetime()
    doc.save(ignore_permissions=True)

    return {"name": doc.name, "status": doc.status, "sent_on": doc.sent_on}


# ---------------------------------------------------------------------------
# 6. get_campaign_stats (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_campaign_stats(date_from=None, date_to=None):
    """Statistiques de prospection pour l'agence active (§2.6)."""
    agency = _get_active_agency()

    lead_filters = {"agency": agency}
    email_filters = {"agency": agency}
    if date_from:
        lead_filters["first_seen"] = [">=", date_from]
        email_filters["sent_on"] = [">=", date_from]
    if date_to:
        lead_filters.setdefault("first_seen", ["<=", date_to])
        email_filters.setdefault("sent_on", ["<=", date_to])

    leads_by_classification = frappe.db.get_all(
        "DetectedLead", filters={"agency": agency},
        fields=["classification", "count(name) as total"], group_by="classification",
    )

    emails_sent = frappe.db.count("ProspectionEmail", {"agency": agency, "status": "Sent"})
    emails_replied = frappe.db.count("ProspectionEmail", {"agency": agency, "status": "Replied"})
    emails_draft = frappe.db.count("ProspectionEmail", {"agency": agency, "status": "Draft"})

    conversion_rate = (emails_replied / emails_sent * 100) if emails_sent else 0

    return {
        "leads_by_classification": {r.classification: r.total for r in leads_by_classification},
        "emails_sent": emails_sent,
        "emails_replied": emails_replied,
        "emails_draft": emails_draft,
        "reply_rate_percent": round(conversion_rate, 2),
    }


# ---------------------------------------------------------------------------
# 7. sync_to_crm (Agency)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def sync_to_crm(provider):
    """
    Synchronisation CRM (§2.6, priorité SHOULD) — HubSpot / Pipedrive / Teamleader.

    SCAFFOLDING UNIQUEMENT : la logique d'authentification et de mapping
    de champs réelle par CRM n'est pas implémentée (nécessite les
    identifiants API et les mappings de champs propres à chaque
    fournisseur, non fournis dans le CDC). Cette fonction se contente de
    mettre en file une tâche d'arrière-plan ; le connecteur réel devra
    être développé séparément avant mise en production.
    """
    agency = _get_active_agency()

    supported_providers = {"hubspot", "pipedrive", "teamleader"}
    if provider not in supported_providers:
        frappe.throw(_("Fournisseur CRM non supporté : {0}").format(provider))

    if not frappe.conf.get(f"{provider}_api_key"):
        frappe.throw(_(
            "Aucune configuration trouvée pour {0}. Le connecteur CRM "
            "réel doit être implémenté avant utilisation."
        ).format(provider))

    frappe.enqueue(
        "platform_core.platform_core.api.prospection._run_crm_sync",
        queue="long",
        agency=agency,
        provider=provider,
    )

    return {"queued": True, "provider": provider}


def _run_crm_sync(agency, provider):
    """
    Placeholder d'exécution en arrière-plan. À implémenter : appel API
    réel du CRM cible + mapping DetectedLead/Opportunity -> objets CRM.
    """
    frappe.log_error(
        title="CRM sync non implémenté",
        message=f"Synchronisation demandée pour l'agence {agency} vers {provider}, "
                f"mais aucun connecteur réel n'est encore développé.",
    )


# =============================================================================
# INTERNE — consommé par le microservice Node.js prospection-service
# =============================================================================

# ---------------------------------------------------------------------------
# 8. get_scoring_rules
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def get_scoring_rules():
    """
    Retourne le barème de scoring configurable (§2.6.1) et les seuils de
    classification, pour que prospection-service calcule les points
    localement sans round-trip par événement.

    allow_guest=True car ce endpoint est appelé par un microservice sans
    session utilisateur Frappe — la sécurité repose sur _verify_service_token().
    """
    _verify_service_token()

    rules = frappe.get_all(
        "LeadScoringRule",
        filters={"is_active": 1},
        fields=["action_code", "base_points", "bonus_condition", "bonus_points"],
    )

    return {
        "rules": rules,
        "thresholds": _get_platform_thresholds(),
    }


# ---------------------------------------------------------------------------
# 9. update_lead_score
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def update_lead_score(agency, ip_hash, points_awarded, bonus_awarded=0,
                       company_name=None, country=None, city=None,
                       page_url=None, referrer=None, user_agent=None,
                       has_favorited=0):
    """
    Enregistre un événement de visite anonyme (VisitorLog) et met à jour
    le lead consolidé (DetectedLead) : score cumulé sur la fenêtre
    glissante, classification chaud/tiède/froid (§2.6.1).

    prospection-service calcule points_awarded/bonus_awarded lui-même à
    partir de get_scoring_rules() ; cette fonction ne fait que persister
    et agréger (elle n'interprète pas bonus_condition, qui est un texte
    libre non exécutable côté serveur).
    """
    _verify_service_token()

    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable : {0}").format(agency))

    thresholds = _get_platform_thresholds()

    lead_name = frappe.db.get_value("DetectedLead", {"agency": agency, "ip_hash": ip_hash}, "name")

    if lead_name:
        lead = frappe.get_doc("DetectedLead", lead_name)
        was_hot = lead.classification == CLASSIFICATION_HOT
        if company_name:
            lead.resolved_company_name = company_name
        lead.last_seen = now_datetime()
        if cint(has_favorited):
            lead.has_favorited = 1
    else:
        lead = frappe.get_doc({
            "doctype": "DetectedLead",
            "agency": agency,
            "ip_hash": ip_hash,
            "resolved_company_name": company_name,
            "cumulative_score": 0,
            "classification": CLASSIFICATION_COLD,
            "has_favorited": cint(has_favorited),
            "first_seen": now_datetime(),
            "last_seen": now_datetime(),
        })
        lead.insert(ignore_permissions=True)
        was_hot = False

    visitor_log = frappe.get_doc({
        "doctype": "VisitorLog",
        "agency": agency,
        "ip_hash": ip_hash,
        "lead": lead.name,
        "company_name": company_name,
        "country": country,
        "city": city,
        "page_url": page_url,
        "referrer": referrer,
        "user_agent": user_agent,
        "visit_date": now_datetime(),
        "points_awarded": cint(points_awarded),
        "bonus_awarded": cint(bonus_awarded),
    })
    visitor_log.insert(ignore_permissions=True)

    window_start = add_days(now_datetime(), -thresholds["lead_score_window_days"])
    cumulative = frappe.db.sql(
        """
        SELECT COALESCE(SUM(points_awarded), 0) + COALESCE(SUM(bonus_awarded), 0) AS total
        FROM `tabVisitorLog`
        WHERE agency = %s AND ip_hash = %s AND visit_date >= %s
        """,
        (agency, ip_hash, window_start),
        as_dict=True,
    )[0].total

    lead.cumulative_score = cumulative
    lead.classification = _classify(cumulative, lead.has_favorited, thresholds)
    lead.save(ignore_permissions=True)

    if lead.classification == CLASSIFICATION_HOT and not was_hot:
        owners = frappe.get_all(
            "AgencyMember", filters={"agency": agency, "status": "Active"}, pluck="user"
        )
        for owner in owners:
            _notify(
                user=owner, ntype="System Alert",
                title=_("Nouveau lead chaud détecté"),
                message=_("Un visiteur ({0}) est désormais classé comme lead chaud.")
                        .format(company_name or ip_hash),
                reference_doctype="DetectedLead", reference_name=lead.name,
                action_required=True, agency_context=agency,
            )

    return {
        "lead": lead.name,
        "cumulative_score": lead.cumulative_score,
        "classification": lead.classification,
        "visitor_log": visitor_log.name,
    }
