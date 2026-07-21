# -*- coding: utf-8 -*-
"""
platform_core/api/agency.py

API Frontend — Module Agence / Profil, PQI, Multi-agences (CDC v5, §2.1 à §2.4).

Couvre (25 endpoints) :
    get_profile, update_profile, upload_logo, upload_cover,
    add_service, update_service, delete_service,
    add_portfolio, update_portfolio, delete_portfolio,
    add_team_member, update_team_member, delete_team_member,
    add_certification, update_certification, delete_certification,
    add_social_link, update_social_link, delete_social_link,
    get_pqi_score, get_profile_completion, request_verification,
    get_linked_agencies, switch_agency_context, request_agency_join

HYPOTHÈSES STRUCTURANTES (à valider / corriger côté DocType JSON réel) :
    1. Fieldnames des Table fields sur AgencyProfile — non listés explicitement
       dans le CDC (seul `social_links` est confirmé, champ #21). J'assume :
           services       -> Table AgencyService
           portfolio      -> Table AgencyPortfolio
           team           -> Table AgencyTeam
           certifications -> Table AgencyCertification
           social_links   -> Table AgencySocialLink (confirmé)
       Si tes fieldnames réels diffèrent, corrige uniquement les constantes
       *_FIELDNAME ci-dessous — le reste du code n'a pas besoin de changer.

    2. Contexte agence actif (bascule multi-agences, §2.1.1) : stocké en
       cache Redis (frappe.cache()) sous la clé `active_agency:{user}`,
       avec fallback sur la première AgencyMember Active de l'utilisateur
       (Owner en priorité) si le cache est vide (ex: après redémarrage).
       Cette logique DEVRA être centralisée dans utils.py dès que ce
       fichier sera créé, pour être réutilisée par opportunity.py,
       proposal.py, invoice.py, etc.

    3. request_verification() : le CDC détaille la vérification légale
       pour le Client (§1.1) via CountryLegalIDRule (registre public par
       pays). Aucun ModerationTask type ne correspond explicitement à une
       "vérification d'identifiant légal" dans l'énumération fournie —
       j'interprète donc : tentative de vérification automatique via
       l'API du registre si configurée (registry_check_enabled), sinon
       fallback sur un ModerationTask de type générique "Validation"
       pour traitement manuel. À confirmer/ajuster si un type dédié
       existe dans tes DocTypes réels.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime, cint, flt


# ---------------------------------------------------------------------------
# Constantes — fieldnames des child tables (cf. hypothèse #1 ci-dessus)
# ---------------------------------------------------------------------------

SERVICES_FIELDNAME = "services"
PORTFOLIO_FIELDNAME = "portfolio"
TEAM_FIELDNAME = "team"
CERTIFICATIONS_FIELDNAME = "certifications"
SOCIAL_LINKS_FIELDNAME = "social_links"

ACTIVE_AGENCY_CACHE_PREFIX = "active_agency"

# Champs éditables au niveau racine d'AgencyProfile (on exclut les champs
# système/calculés : rating, pqi_score, profile_completion, reviews_count,
# legal_id_verified, email_verified, offers_suspended)
AGENCY_PROFILE_UPDATABLE_FIELDS = {
    "agency_name", "slogan", "description", "year_founded", "team_size",
    "website", "languages", "remote_work", "coverage", "location",
    "annual_revenue", "country", "legal_id", "phone", "email",
}

SERVICE_FIELDS = {
    "service_name", "description", "price_range", "tech_stack",
    "skills", "projects_in_progress",
}
PORTFOLIO_FIELDS = {
    "title", "status", "image", "video_url", "result_url", "budget",
    "collaboration_period", "agency_feedback", "problem_solution",
    # client_confirmed volontairement exclu : rempli automatiquement
    # côté client/projet (§2.2.3 "saisies automatiquement"), pas par l'agence.
}
TEAM_FIELDS = {
    "member", "photo", "role", "description", "history",
    "linkedin_url", "show_linked_agencies",
}
CERTIFICATION_FIELDS = {
    "photo", "title", "description", "issuing_organization",
    "obtained_date", "expiration_date", "verification_url",
    "level", "technology",
    # is_verified volontairement exclu : uniquement modifiable par la
    # plateforme après vérification (cf. Perm Level 1 sur ce champ).
}
SOCIAL_LINK_FIELDS = {"platform", "url"}
SOCIAL_PLATFORMS = {"LinkedIn", "Facebook", "Instagram", "X", "Autre"}


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _get_active_agency(user=None):
    """
    Retourne le nom de l'AgencyProfile actif pour l'utilisateur (bascule
    multi-agences, §2.1.1). Cache Redis en priorité, fallback DB sinon.
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
        order_by="member_role asc, joined_on asc",  # Owner avant Member
    )
    if not fallback:
        frappe.throw(_("Aucune agence active pour cet utilisateur."), frappe.PermissionError)

    frappe.cache().set_value(cache_key, fallback)
    return fallback


def _require_active_member(agency, user=None):
    """Vérifie que l'utilisateur est membre actif de l'agence donnée."""
    user = user or frappe.session.user
    if not frappe.db.exists("AgencyMember", {"user": user, "agency": agency, "status": "Active"}):
        frappe.throw(_("Vous n'êtes pas membre actif de cette agence."), frappe.PermissionError)


def _get_agency_doc(agency=None):
    """Charge le AgencyProfile courant (contexte actif, sauf agence explicite)."""
    agency = agency or _get_active_agency()
    _require_active_member(agency)
    return frappe.get_doc("AgencyProfile", agency)


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


def _attach_image(doc, fieldname, file_url=None, content=None, filename=None):
    """
    Attache une image à un champ Attach Image. Deux modes :
      - file_url : le fichier a déjà été uploadé via l'endpoint standard
        Frappe /api/method/upload_file, on ne fait que lier l'URL.
      - content (base64) + filename : upload direct depuis cette API.
    """
    if file_url:
        doc.set(fieldname, file_url)
        doc.save(ignore_permissions=True)
        return file_url

    if content and filename:
        import base64
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": filename,
            "content": base64.b64decode(content),
            "attached_to_doctype": doc.doctype,
            "attached_to_name": doc.name,
            "attached_to_field": fieldname,
            "is_private": 0,
        })
        file_doc.save(ignore_permissions=True)
        doc.set(fieldname, file_doc.file_url)
        doc.save(ignore_permissions=True)
        return file_doc.file_url

    frappe.throw(_("Fournissez soit file_url, soit content + filename."))


def _append_row(doc, table_field, allowed_fields, values, required_fields=None):
    row = {k: v for k, v in values.items() if k in allowed_fields}
    for req in (required_fields or []):
        if not row.get(req):
            frappe.throw(_("Le champ « {0} » est requis.").format(req))
    doc.append(table_field, row)
    doc.save(ignore_permissions=True)
    return doc.get(table_field)[-1]


def _get_row(doc, table_field, row_name):
    for row in doc.get(table_field):
        if row.name == row_name:
            return row
    frappe.throw(_("Élément introuvable dans {0}.").format(table_field))


def _update_row(doc, table_field, row_name, allowed_fields, values):
    row = _get_row(doc, table_field, row_name)
    for k, v in values.items():
        if k in allowed_fields:
            row.set(k, v)
    doc.save(ignore_permissions=True)
    return row


def _delete_row(doc, table_field, row_name):
    row = _get_row(doc, table_field, row_name)
    doc.get(table_field).remove(row)
    doc.save(ignore_permissions=True)


# ---------------------------------------------------------------------------
# 1-2. Profil
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_profile():
    """Profil complet de l'agence active (édition), toutes sections (§2.2)."""
    doc = _get_agency_doc()
    data = doc.as_dict()
    return data


@frappe.whitelist()
def update_profile(**fields):
    """Met à jour les champs racine du profil agence (§2.2.1)."""
    doc = _get_agency_doc()
    for k, v in fields.items():
        if k in AGENCY_PROFILE_UPDATABLE_FIELDS:
            doc.set(k, v)
    doc.save(ignore_permissions=True)
    return {"agency": doc.name}


# ---------------------------------------------------------------------------
# 3-4. Logo / Couverture
# ---------------------------------------------------------------------------

@frappe.whitelist()
def upload_logo(file_url=None, content=None, filename=None):
    """Met à jour le logo de l'agence (§2.2.1)."""
    doc = _get_agency_doc()
    url = _attach_image(doc, "logo", file_url=file_url, content=content, filename=filename)
    return {"logo": url}


@frappe.whitelist()
def upload_cover(file_url=None, content=None, filename=None):
    """Met à jour l'image de couverture de l'agence (§2.2.1)."""
    doc = _get_agency_doc()
    url = _attach_image(doc, "cover_image", file_url=file_url, content=content, filename=filename)
    return {"cover_image": url}


# ---------------------------------------------------------------------------
# 5-7. Services (§2.2.2)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def add_service(service_name, description=None, price_range=None,
                 tech_stack=None, skills=None, projects_in_progress=0):
    doc = _get_agency_doc()
    row = _append_row(doc, SERVICES_FIELDNAME, SERVICE_FIELDS, {
        "service_name": service_name, "description": description,
        "price_range": price_range, "tech_stack": tech_stack,
        "skills": skills, "projects_in_progress": projects_in_progress,
    }, required_fields=["service_name"])
    return {"name": row.name}


@frappe.whitelist()
def update_service(row_name, **fields):
    doc = _get_agency_doc()
    row = _update_row(doc, SERVICES_FIELDNAME, row_name, SERVICE_FIELDS, fields)
    return {"name": row.name}


@frappe.whitelist()
def delete_service(row_name):
    doc = _get_agency_doc()
    _delete_row(doc, SERVICES_FIELDNAME, row_name)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 8-10. Portfolio / Réalisations (§2.2.3)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def add_portfolio(title, status="Completed", image=None, video_url=None,
                   result_url=None, budget=None, collaboration_period=None,
                   agency_feedback=None, problem_solution=None):
    doc = _get_agency_doc()
    row = _append_row(doc, PORTFOLIO_FIELDNAME, PORTFOLIO_FIELDS, {
        "title": title, "status": status, "image": image, "video_url": video_url,
        "result_url": result_url, "budget": budget,
        "collaboration_period": collaboration_period,
        "agency_feedback": agency_feedback, "problem_solution": problem_solution,
    }, required_fields=["title"])
    return {"name": row.name}


@frappe.whitelist()
def update_portfolio(row_name, **fields):
    doc = _get_agency_doc()
    row = _update_row(doc, PORTFOLIO_FIELDNAME, row_name, PORTFOLIO_FIELDS, fields)
    return {"name": row.name}


@frappe.whitelist()
def delete_portfolio(row_name):
    doc = _get_agency_doc()
    _delete_row(doc, PORTFOLIO_FIELDNAME, row_name)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 11-13. Équipe / Staff (§2.2.4)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def add_team_member(role, member=None, photo=None, description=None,
                     history=None, linkedin_url=None, show_linked_agencies=1):
    doc = _get_agency_doc()

    if member:
        valid = frappe.db.exists("AgencyMember", {"name": member, "agency": doc.name})
        if not valid:
            frappe.throw(_("Ce collaborateur n'appartient pas à cette agence."))

    row = _append_row(doc, TEAM_FIELDNAME, TEAM_FIELDS, {
        "member": member, "photo": photo, "role": role, "description": description,
        "history": history, "linkedin_url": linkedin_url,
        "show_linked_agencies": show_linked_agencies,
    }, required_fields=["role"])
    return {"name": row.name}


@frappe.whitelist()
def update_team_member(row_name, **fields):
    doc = _get_agency_doc()
    if fields.get("member"):
        valid = frappe.db.exists("AgencyMember", {"name": fields["member"], "agency": doc.name})
        if not valid:
            frappe.throw(_("Ce collaborateur n'appartient pas à cette agence."))
    row = _update_row(doc, TEAM_FIELDNAME, row_name, TEAM_FIELDS, fields)
    return {"name": row.name}


@frappe.whitelist()
def delete_team_member(row_name):
    doc = _get_agency_doc()
    _delete_row(doc, TEAM_FIELDNAME, row_name)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 14-16. Certifications (§2.2.5)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def add_certification(title, photo=None, description=None, issuing_organization=None,
                       obtained_date=None, expiration_date=None, verification_url=None,
                       level="Intermediate", technology=None):
    doc = _get_agency_doc()
    row = _append_row(doc, CERTIFICATIONS_FIELDNAME, CERTIFICATION_FIELDS, {
        "title": title, "photo": photo, "description": description,
        "issuing_organization": issuing_organization, "obtained_date": obtained_date,
        "expiration_date": expiration_date, "verification_url": verification_url,
        "level": level, "technology": technology,
    }, required_fields=["title"])
    return {"name": row.name}


@frappe.whitelist()
def update_certification(row_name, **fields):
    doc = _get_agency_doc()
    row = _update_row(doc, CERTIFICATIONS_FIELDNAME, row_name, CERTIFICATION_FIELDS, fields)
    return {"name": row.name}


@frappe.whitelist()
def delete_certification(row_name):
    doc = _get_agency_doc()
    _delete_row(doc, CERTIFICATIONS_FIELDNAME, row_name)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 17-19. Réseaux sociaux (§2.2.7)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def add_social_link(platform, url):
    if platform not in SOCIAL_PLATFORMS:
        frappe.throw(_("Plateforme invalide : {0}").format(platform))
    doc = _get_agency_doc()
    row = _append_row(doc, SOCIAL_LINKS_FIELDNAME, SOCIAL_LINK_FIELDS, {
        "platform": platform, "url": url,
    }, required_fields=["platform", "url"])
    return {"name": row.name}


@frappe.whitelist()
def update_social_link(row_name, **fields):
    if fields.get("platform") and fields["platform"] not in SOCIAL_PLATFORMS:
        frappe.throw(_("Plateforme invalide : {0}").format(fields["platform"]))
    doc = _get_agency_doc()
    row = _update_row(doc, SOCIAL_LINKS_FIELDNAME, row_name, SOCIAL_LINK_FIELDS, fields)
    return {"name": row.name}


@frappe.whitelist()
def delete_social_link(row_name):
    doc = _get_agency_doc()
    _delete_row(doc, SOCIAL_LINKS_FIELDNAME, row_name)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# 20. get_pqi_score
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_pqi_score():
    """
    Score PQI qualitatif (§2.4, §2.4.1) : score global + détail par
    critère de la dernière évaluation (PQIEvaluation / PQICriterionResult).
    """
    doc = _get_agency_doc()

    latest_eval = frappe.get_all(
        "PQIEvaluation",
        filters={"agency": doc.name},
        fields=["name", "evaluation_date", "total_score"],
        order_by="evaluation_date desc",
        limit=1,
    )

    if not latest_eval:
        return {"pqi_score": doc.pqi_score, "evaluation_date": None, "criteria": []}

    evaluation = frappe.get_doc("PQIEvaluation", latest_eval[0].name)
    criteria = []
    for row in evaluation.results:
        criteria.append({
            "criterion": row.criterion,
            "score": row.score,
            "penalty_reason": row.penalty_reason,
            "ai_recommendation": row.ai_recommendation,
        })

    return {
        "pqi_score": doc.pqi_score,
        "evaluation_date": evaluation.evaluation_date,
        "total_score": evaluation.total_score,
        "criteria": criteria,
    }


# ---------------------------------------------------------------------------
# 21. get_profile_completion
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_profile_completion():
    """
    Taux de complétion du profil (§2.2). La valeur est maintenue par un
    hook côté DocType (champ Read Only) ; cette API se contente de la
    restituer. Si votre hook n'existe pas encore, il faudra l'ajouter à
    doctype/agency_profile/agency_profile.py (recalcul à chaque save()).
    """
    doc = _get_agency_doc()
    return {"profile_completion": flt(doc.profile_completion)}


# ---------------------------------------------------------------------------
# 22. request_verification
# ---------------------------------------------------------------------------

@frappe.whitelist()
def request_verification():
    """
    Déclenche la vérification de l'identifiant légal de l'agence.

    Tente une vérification automatique via le registre public du pays
    (CountryLegalIDRule) si `registry_check_enabled` est actif ; sinon,
    crée une tâche de modération pour vérification manuelle.
    """
    doc = _get_agency_doc()

    if not doc.legal_id or not doc.country:
        frappe.throw(_("Le pays et l'identifiant légal doivent être renseignés."))

    rule = frappe.db.get_value(
        "CountryLegalIDRule",
        {"country": doc.country, "is_active": 1},
        ["name", "registry_check_enabled", "registry_api_url", "validation_regex"],
        as_dict=True,
    )

    if not rule:
        frappe.throw(_("Aucune règle de vérification n'est configurée pour ce pays."))

    import re
    if rule.validation_regex and not re.match(rule.validation_regex, doc.legal_id):
        frappe.throw(_("Le format de l'identifiant légal ne correspond pas à celui attendu."))

    if rule.registry_check_enabled and rule.registry_api_url:
        try:
            import requests
            api_key = frappe.utils.password.get_decrypted_password(
                "CountryLegalIDRule", rule.name, "registry_api_key", raise_exception=False
            )
            response = requests.get(
                rule.registry_api_url,
                params={"id": doc.legal_id},
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            verified = bool(result.get("valid"))
        except Exception:
            frappe.log_error(title="Échec vérification registre légal (Agency)")
            verified = None
        else:
            doc.db_set("legal_id_verified", 1 if verified else 0)
            return {"verified": verified, "mode": "automatic"}

    # Fallback : vérification manuelle par un modérateur
    task = frappe.get_doc({
        "doctype": "ModerationTask",
        "task_type": "Validation",
        "reference_doctype": "AgencyProfile",
        "reference_name": doc.name,
        "status": "Open",
    })
    task.insert(ignore_permissions=True)

    for moderator in frappe.get_all("Has Role", filters={"role": "Moderator"}, fields=["parent"]):
        _notify(
            user=moderator.parent, ntype="System Alert",
            title=_("Vérification d'identifiant légal requise"),
            message=_("L'agence {0} demande une vérification manuelle de son identifiant légal.")
                    .format(doc.agency_name),
            reference_doctype="ModerationTask", reference_name=task.name, action_required=True,
        )

    return {"verified": None, "mode": "manual_review", "moderation_task": task.name}


# ---------------------------------------------------------------------------
# 23. get_linked_agencies
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_linked_agencies():
    """
    Liste des agences auxquelles l'utilisateur courant est rattaché
    (multi-agences, §2.1.1), avec le rôle et l'agence actuellement active.
    """
    user = frappe.session.user
    active_agency = _get_active_agency(user)

    memberships = frappe.get_all(
        "AgencyMember",
        filters={"user": user, "status": "Active"},
        fields=["name", "agency", "member_role", "joined_on"],
    )

    agency_names = [m.agency for m in memberships]
    profiles = {
        p.name: p for p in frappe.get_all(
            "AgencyProfile",
            filters={"name": ["in", agency_names]},
            fields=["name", "agency_name", "logo"],
        )
    }

    result = []
    for m in memberships:
        profile = profiles.get(m.agency)
        result.append({
            "agency": m.agency,
            "agency_name": profile.agency_name if profile else None,
            "logo": profile.logo if profile else None,
            "role": m.member_role,
            "joined_on": m.joined_on,
            "is_active_context": m.agency == active_agency,
        })
    return result


# ---------------------------------------------------------------------------
# 24. switch_agency_context
# ---------------------------------------------------------------------------

@frappe.whitelist()
def switch_agency_context(agency):
    """
    Bascule le contexte de dashboard sur une autre agence rattachée
    (switch, §2.1.1). Un seul contexte actif à la fois par utilisateur.
    """
    user = frappe.session.user
    _require_active_member(agency, user=user)

    cache_key = f"{ACTIVE_AGENCY_CACHE_PREFIX}:{user}"
    frappe.cache().set_value(cache_key, agency)

    profile = frappe.db.get_value("AgencyProfile", agency, ["agency_name", "logo"], as_dict=True)
    return {"active_agency": agency, "agency_name": profile.agency_name, "logo": profile.logo}


# ---------------------------------------------------------------------------
# 25. request_agency_join
# ---------------------------------------------------------------------------

@frappe.whitelist()
def request_agency_join(agency):
    """
    Envoie une demande de rattachement à une agence via le bouton « + »
    (§2.1.1, context="Via Plus Button"). Soumise à validation par un
    collaborateur déjà membre (ou un modérateur si l'agence n'a qu'un
    seul membre).
    """
    user = frappe.session.user

    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable."))

    if frappe.db.exists("AgencyMember", {"user": user, "agency": agency, "status": "Active"}):
        frappe.throw(_("Vous êtes déjà membre de cette agence."))

    if frappe.db.exists("AgencyJoinRequest", {"user": user, "agency": agency, "status": "Pending"}):
        frappe.throw(_("Une demande est déjà en attente pour cette agence."))

    request = frappe.get_doc({
        "doctype": "AgencyJoinRequest",
        "user": user,
        "agency": agency,
        "context": "Via Plus Button",
        "status": "Pending",
    })
    request.insert()

    member_count = frappe.db.count("AgencyMember", {"agency": agency, "status": "Active"})

    if member_count <= 1:
        recipients = [m.parent for m in frappe.get_all(
            "Has Role", filters={"role": "Moderator"}, fields=["parent"]
        )]
    else:
        recipients = frappe.get_all(
            "AgencyMember",
            filters={"agency": agency, "status": "Active", "member_role": "Owner"},
            pluck="user",
        )

    for recipient in recipients:
        _notify(
            user=recipient, ntype="System Alert",
            title=_("Nouvelle demande de rattachement"),
            message=_("Un utilisateur souhaite rejoindre votre agence."),
            reference_doctype="AgencyJoinRequest", reference_name=request.name,
            action_required=True, agency_context=agency,
        )

    return {"request": request.name, "status": request.status}
