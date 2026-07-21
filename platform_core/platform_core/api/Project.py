# -*- coding: utf-8 -*-
"""
platform_core/api/project.py

API Frontend — Module Entreprise / Mes Projets (CDC v5, §1.2 à §1.3.4).

Couvre :
    - create_project, get_projects, get_project_detail, update_project,
      delete_project, submit_project, repost_project, duplicate_project,
      download_cdc, get_shortlist_ia, confirm_project_completion,
      validate_project_completion

Règles métier clés respectées (cf. CDC v5) :
    - §1.3.1  : statuts Draft / Posted / Awaiting / In Progress / Suspended /
                Completed / Rejected (+ rejection_substatus).
    - §1.3.2  : le CDC n'est modifiable QUE par le client, jusqu'au passage
                en "In Progress" où il devient verrouillé (cdc_locked).
    - §1.3.2 bis : génération automatique du CDC en PDF, mécanisme partagé
                avec quick_actions.py (Unicast/Multicast) — TODO extraire
                dans un module commun (ex: platform_core/utils/cdc.py).
    - §1.3.1 (Repostuler) : uniquement depuis le statut "Posted", jamais
                depuis "Rejected".
    - Terminé  : nécessite confirmation client (confirm_project_completion)
                PUIS validation modérateur (validate_project_completion) —
                deux étapes distinctes, comme le devis (§1.3.3).
    - Système de crédits supprimé en v5 : aucune vérification de solde ici.

NOTE : les helpers d'ownership (_get_client_profile, _get_active_agency)
sont dupliqués ici pour que ce module reste autonome. Ils devront être
centralisés dans platform_core/api/utils.py dès que ce fichier sera codé,
pour éviter toute divergence de logique entre modules.
"""

import json

import frappe
from frappe import _
from frappe.utils import now_datetime, nowdate, cint, get_url


# ---------------------------------------------------------------------------
# Constantes (alignées strictement sur les Select du DocType Project)
# ---------------------------------------------------------------------------

STATUS_DRAFT = "Draft"
STATUS_POSTED = "Posted"
STATUS_AWAITING = "Awaiting"
STATUS_IN_PROGRESS = "In Progress"
STATUS_SUSPENDED = "Suspended"
STATUS_COMPLETED = "Completed"
STATUS_REJECTED = "Rejected"

SUBSTATUS_REFUSED = "Refusé"
SUBSTATUS_DELETED = "Supprimé"
SUBSTATUS_CLIENT_INACTIVE = "Client inactif"

# Champs que le client a le droit de modifier via update_project()
# (on exclut délibérément : client, status, rejection_substatus, cdc_file,
# cdc_locked, shortlist_ia, toutes les dates calculées, repost_count)
UPDATABLE_FIELDS = {
    "title",
    "description",
    "need_type",
    "channel",
    "category",
    "sub_category",
    "budget_min",
    "budget_max",
    "delivery_delay_days",
    "location",
}


# ---------------------------------------------------------------------------
# Helpers internes (ownership / contexte)
# ---------------------------------------------------------------------------

def _get_client_profile(user=None):
    """Retourne le nom du ClientProfile lié à l'utilisateur courant, ou lève."""
    user = user or frappe.session.user
    client = frappe.db.get_value("ClientProfile", {"user": user}, "name")
    if not client:
        frappe.throw(_("Aucun profil Entreprise associé à cet utilisateur."),
                      frappe.PermissionError)
    return client


def _get_active_agency(user=None):
    """
    Retourne le nom de l'AgencyProfile actif pour l'utilisateur courant
    (contexte de bascule multi-agences, cf. CDC §2.1.1).

    TODO: centraliser dans utils.py. Ici, fallback simple : première
    AgencyMember active trouvée (à remplacer par la lecture du contexte
    de switch réellement stocké côté session/cache lorsque agency.py
    sera codé).
    """
    user = user or frappe.session.user
    agency = frappe.db.get_value(
        "AgencyMember",
        {"user": user, "status": "Active"},
        "agency",
        order_by="joined_on desc",
    )
    if not agency:
        frappe.throw(_("Aucune agence active pour cet utilisateur."),
                      frappe.PermissionError)
    return agency


def _require_client_owner(project_doc):
    client = _get_client_profile()
    if project_doc.client != client:
        frappe.throw(_("Vous n'êtes pas propriétaire de ce projet."),
                      frappe.PermissionError)
    return client


def _require_moderator():
    if not frappe.has_role("Moderator") and not frappe.has_role("Administrator"):
        frappe.throw(_("Action réservée aux modérateurs."), frappe.PermissionError)


def _notify(user, ntype, title, message, reference_doctype=None,
            reference_name=None, action_required=False, agency_context=None):
    """Crée une Notification (cf. DocType Notification, module 7 - Historique)."""
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


def _generate_and_attach_cdc(project_doc):
    """
    Génère le CDC structuré en PDF et l'attache au projet (champ cdc_file).

    Mécanisme partagé avec le formulaire Unicast/Multicast (§1.3.2 bis) :
    même structure de document quel que soit le canal d'origine.

    TODO: extraire cette fonction dans un module commun
    (ex: platform_core/utils/cdc.py) et l'importer depuis project.py
    ET quick_actions.py, pour garantir un rendu strictement identique.
    """
    html = frappe.render_template(
        "platform_core/templates/cdc/cdc_template.html",
        {
            "project": project_doc,
            "client": frappe.get_doc("ClientProfile", project_doc.client),
            "generated_on": now_datetime(),
        },
    )
    pdf_content = frappe.utils.pdf.get_pdf(html)

    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": f"CDC_{project_doc.name}.pdf",
        "content": pdf_content,
        "attached_to_doctype": "Project",
        "attached_to_name": project_doc.name,
        "attached_to_field": "cdc_file",
        "is_private": 1,
    })
    file_doc.save(ignore_permissions=True)

    project_doc.db_set("cdc_file", file_doc.file_url)
    return file_doc.file_url


# ---------------------------------------------------------------------------
# 1. create_project
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_project(title, need_type="Projet", category=None, sub_category=None,
                    budget_min=None, budget_max=None, delivery_delay_days=None,
                    location=None, description=None):
    """
    Crée un projet en brouillon (Draft) à partir du Smart Briefing IA (§1.2).

    Sauvegarde de brouillon : les champs peuvent être incomplets à ce stade
    (le questionnaire conversationnel est multi-étapes). Seul `title` est
    requis pour créer l'enregistrement ; le reste peut être complété par
    update_project() avant l'appel à submit_project().
    """
    client = _get_client_profile()

    if not title or not title.strip():
        frappe.throw(_("Le titre du projet est requis."))

    project = frappe.get_doc({
        "doctype": "Project",
        "client": client,
        "title": title.strip(),
        "need_type": need_type,
        "channel": "Smart Briefing",
        "category": category,
        "sub_category": sub_category,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "delivery_delay_days": delivery_delay_days,
        "location": location,
        "description": description,
        "status": STATUS_DRAFT,
    })
    project.insert()

    return {"name": project.name, "status": project.status}


# ---------------------------------------------------------------------------
# 2. get_projects
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_projects(status=None, need_type=None, channel=None,
                  limit_start=0, limit_page_length=20, order_by="modified desc"):
    """
    Liste des projets du client courant ("Mes Projets", §1.3).

    Le filtrage est forcé sur le ClientProfile de l'utilisateur courant,
    quel que soit ce qui serait passé en paramètre — l'ownership n'est
    jamais déterminée par l'appelant.
    """
    client = _get_client_profile()

    filters = {"client": client}
    if status:
        filters["status"] = status
    if need_type:
        filters["need_type"] = need_type
    if channel:
        filters["channel"] = channel

    projects = frappe.get_list(
        "Project",
        filters=filters,
        fields=[
            "name", "title", "need_type", "channel", "category", "sub_category",
            "budget_min", "budget_max", "status", "rejection_substatus",
            "delivery_delay_days", "expected_end_date", "cdc_locked",
            "repost_count", "modified",
        ],
        limit_start=cint(limit_start),
        limit_page_length=cint(limit_page_length),
        order_by=order_by,
    )
    return projects


# ---------------------------------------------------------------------------
# 3. get_project_detail
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_project_detail(project):
    """Détail complet d'un projet (vue « CDC », §1.3.4), client propriétaire uniquement."""
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    return {
        "name": doc.name,
        "title": doc.title,
        "description": doc.description,
        "need_type": doc.need_type,
        "channel": doc.channel,
        "category": doc.category,
        "sub_category": doc.sub_category,
        "budget_min": doc.budget_min,
        "budget_max": doc.budget_max,
        "delivery_delay_days": doc.delivery_delay_days,
        "location": doc.location,
        "status": doc.status,
        "rejection_substatus": doc.rejection_substatus,
        "cdc_file": doc.cdc_file,
        "cdc_locked": doc.cdc_locked,
        "shortlist_ia": json.loads(doc.shortlist_ia) if doc.shortlist_ia else [],
        "acceptance_date": doc.acceptance_date,
        "start_date": doc.start_date,
        "initial_end_date": doc.initial_end_date,
        "total_suspension_days": doc.total_suspension_days,
        "expected_end_date": doc.expected_end_date,
        "completion_confirmed_by_client": doc.completion_confirmed_by_client,
        "completion_validated_by_moderator": doc.completion_validated_by_moderator,
        "repost_count": doc.repost_count,
    }


# ---------------------------------------------------------------------------
# 4. update_project
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_project(project, **fields):
    """
    Met à jour un projet. Autorisé uniquement tant que le CDC n'est pas
    verrouillé (cdc_locked=0), soit avant le passage en "In Progress"
    (cf. §1.3.2 : "modifiable uniquement par le client, jusqu'au passage
    au statut En cours, où il devient verrouillé").
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    if doc.cdc_locked:
        frappe.throw(_("Le CDC est verrouillé : le projet est déjà en cours."))

    if doc.status in (STATUS_COMPLETED, STATUS_REJECTED):
        frappe.throw(_("Ce projet ne peut plus être modifié (statut {0}).").format(doc.status))

    for fieldname, value in fields.items():
        if fieldname in UPDATABLE_FIELDS:
            doc.set(fieldname, value)

    doc.save()
    return {"name": doc.name, "status": doc.status}


# ---------------------------------------------------------------------------
# 5. delete_project
# ---------------------------------------------------------------------------

@frappe.whitelist()
def delete_project(project):
    """
    Supprime un projet.

    - Si Draft : suppression physique (aucun engagement pris, rien à tracer).
    - Si Posted et qu'aucune agence n'a encore accepté : archivage logique
      (status=Rejected, rejection_substatus=Supprimé), jamais de suppression
      physique dès qu'une opportunité existe — cf. §1.3.1 "sous-statut
      Supprimé (archivage logique, non une suppression physique)".
    - Sinon : refusé (le projet est engagé dans un workflow actif).
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    if doc.status == STATUS_DRAFT:
        frappe.delete_doc("Project", project, ignore_permissions=True)
        return {"deleted": True, "mode": "physical"}

    if doc.status == STATUS_POSTED:
        has_engagement = frappe.db.exists("Opportunity", {
            "project": project,
            "status": ["not in", ["Reçue"]],
        })
        if has_engagement:
            frappe.throw(_("Impossible de supprimer : une agence a déjà réagi à ce projet."))

        doc.status = STATUS_REJECTED
        doc.rejection_substatus = SUBSTATUS_DELETED
        doc.save(ignore_permissions=True)
        return {"deleted": True, "mode": "archived", "status": doc.status}

    frappe.throw(_("Ce projet ne peut pas être supprimé dans son statut actuel ({0}).")
                 .format(doc.status))


# ---------------------------------------------------------------------------
# 6. submit_project
# ---------------------------------------------------------------------------

@frappe.whitelist()
def submit_project(project):
    """
    Finalise un brouillon : valide les champs obligatoires, génère le CDC
    PDF (§1.2 / §1.3.2 bis) et passe le projet au statut "Posted" (Postulé).

    Le dépôt est entièrement gratuit (crédits supprimés en v5) : aucune
    vérification de solde n'est effectuée ici.
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    if doc.status != STATUS_DRAFT:
        frappe.throw(_("Seul un projet en brouillon peut être soumis."))

    required = {
        "title": doc.title,
        "need_type": doc.need_type,
        "delivery_delay_days": doc.delivery_delay_days,
    }
    missing = [f for f, v in required.items() if not v]
    if missing:
        frappe.throw(_("Champs obligatoires manquants avant soumission : {0}")
                     .format(", ".join(missing)))

    _generate_and_attach_cdc(doc)

    doc.status = STATUS_POSTED
    doc.save(ignore_permissions=True)

    # Le matching (shortlist IA) est calculé de façon asynchrone par le
    # microservice matching-service, qui viendra remplir `shortlist_ia`
    # via l'API interne matching.py — aucun appel synchrone ici.

    return {"name": doc.name, "status": doc.status, "cdc_file": doc.cdc_file}


# ---------------------------------------------------------------------------
# 7. repost_project
# ---------------------------------------------------------------------------

@frappe.whitelist()
def repost_project(project):
    """
    Republie un projet resté sans réaction d'agence (§1.3.1).

    Autorisé uniquement depuis le statut "Posted" — jamais depuis
    "Rejected" ("aucune republication automatique n'est proposée depuis
    le statut Rejeté").
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    if doc.status != STATUS_POSTED:
        frappe.throw(_("Seul un projet au statut Postulé peut être republié."))

    doc.repost_count = cint(doc.repost_count) + 1
    doc.save(ignore_permissions=True)

    return {"name": doc.name, "repost_count": doc.repost_count}


# ---------------------------------------------------------------------------
# 8. duplicate_project
# ---------------------------------------------------------------------------

@frappe.whitelist()
def duplicate_project(project):
    """
    Duplique un projet existant en nouveau brouillon (champs du brief
    uniquement — aucune donnée de workflow, de CDC ni de statut n'est
    reprise : le client doit re-soumettre pour générer un nouveau CDC).
    """
    source = frappe.get_doc("Project", project)
    client = _require_client_owner(source)

    new_project = frappe.get_doc({
        "doctype": "Project",
        "client": client,
        "title": _("{0} (copie)").format(source.title),
        "need_type": source.need_type,
        "channel": "Smart Briefing",
        "category": source.category,
        "sub_category": source.sub_category,
        "budget_min": source.budget_min,
        "budget_max": source.budget_max,
        "delivery_delay_days": source.delivery_delay_days,
        "location": source.location,
        "description": source.description,
        "status": STATUS_DRAFT,
    })
    new_project.insert()

    return {"name": new_project.name, "status": new_project.status}


# ---------------------------------------------------------------------------
# 9. download_cdc
# ---------------------------------------------------------------------------

@frappe.whitelist()
def download_cdc(project):
    """
    Retourne l'URL du CDC PDF.

    Accessible :
      - au Client propriétaire du projet,
      - à toute Agence disposant d'une Opportunity sur ce projet
        (cf. §2.3 : "En cliquant sur le nom du client, l'agence ouvre
        le cahier des charges PDF").
    """
    doc = frappe.get_doc("Project", project)

    if not doc.cdc_file:
        frappe.throw(_("Aucun CDC disponible pour ce projet."))

    client = frappe.db.get_value("ClientProfile", {"user": frappe.session.user}, "name")
    if client and doc.client == client:
        return {"cdc_file": get_url(doc.cdc_file)}

    agency = frappe.db.get_value(
        "AgencyMember", {"user": frappe.session.user, "status": "Active"}, "agency"
    )
    if agency and frappe.db.exists("Opportunity", {"project": project, "agency": agency}):
        return {"cdc_file": get_url(doc.cdc_file)}

    frappe.throw(_("Vous n'avez pas accès au CDC de ce projet."), frappe.PermissionError)


# ---------------------------------------------------------------------------
# 10. get_shortlist_ia
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_shortlist_ia(project):
    """
    Retourne la shortlist d'agences pertinentes pour ce projet (§1.3.4),
    calculée par le microservice matching-service et stockée dans
    `shortlist_ia` (JSON), enrichie ici avec les données publiques
    de chaque AgencyProfile pour l'affichage.
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

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


# ---------------------------------------------------------------------------
# 11. confirm_project_completion
# ---------------------------------------------------------------------------

@frappe.whitelist()
def confirm_project_completion(project):
    """
    Le client confirme la fin du projet (via le lien reçu par email,
    cf. §1.3.1). Ceci ne change PAS encore le statut : ça déclenche une
    tâche de modération ("Validation Terminé") qui doit être validée par
    un modérateur avant le passage effectif à "Completed".
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    if doc.status != STATUS_IN_PROGRESS:
        frappe.throw(_("Seul un projet en cours peut être confirmé comme terminé."))

    if doc.completion_confirmed_by_client:
        frappe.throw(_("La confirmation a déjà été enregistrée pour ce projet."))

    doc.completion_confirmed_by_client = 1
    doc.save(ignore_permissions=True)

    task = frappe.get_doc({
        "doctype": "ModerationTask",
        "task_type": "Validation Terminé",
        "reference_doctype": "Project",
        "reference_name": doc.name,
        "status": "Open",
    })
    task.insert(ignore_permissions=True)

    for moderator in frappe.get_all("Has Role", filters={"role": "Moderator"}, fields=["parent"]):
        _notify(
            user=moderator.parent,
            ntype="System Alert",
            title=_("Confirmation de fin de projet"),
            message=_("Le client a confirmé la fin du projet {0}, en attente de validation.")
                    .format(doc.title),
            reference_doctype="ModerationTask",
            reference_name=task.name,
            action_required=True,
        )

    return {"name": doc.name, "moderation_task": task.name}


# ---------------------------------------------------------------------------
# 12. validate_project_completion
# ---------------------------------------------------------------------------

@frappe.whitelist()
def validate_project_completion(project, approve=1, decision_note=None):
    """
    Le modérateur valide (ou refuse) le passage définitif du projet au
    statut "Completed" (§1.3.1). Réservé au rôle Moderator/Administrator.
    """
    _require_moderator()
    approve = cint(approve)

    doc = frappe.get_doc("Project", project)

    if doc.status != STATUS_IN_PROGRESS:
        frappe.throw(_("Ce projet n'est pas en cours ; validation impossible."))

    if not doc.completion_confirmed_by_client:
        frappe.throw(_("Le client n'a pas encore confirmé la fin du projet."))

    task_name = frappe.db.get_value(
        "ModerationTask",
        {"reference_doctype": "Project", "reference_name": project, "status": "Open",
         "task_type": "Validation Terminé"},
        "name",
    )

    if approve:
        doc.completion_validated_by_moderator = 1
        doc.status = STATUS_COMPLETED
        doc.save(ignore_permissions=True)

        _notify(
            user=frappe.db.get_value("ClientProfile", doc.client, "user"),
            ntype="Project",
            title=_("Projet terminé"),
            message=_("Votre projet {0} a été validé comme terminé.").format(doc.title),
            reference_doctype="Project",
            reference_name=doc.name,
        )

        agency_user = frappe.db.get_value(
            "AgencyMember",
            {"agency": frappe.db.get_value(
                "Opportunity", {"project": project, "status": "Gagnée"}, "agency"
            ), "member_role": "Owner"},
            "user",
        )
        if agency_user:
            _notify(
                user=agency_user,
                ntype="Project",
                title=_("Projet terminé"),
                message=_("Le projet {0} a été validé comme terminé.").format(doc.title),
                reference_doctype="Project",
                reference_name=doc.name,
            )
    else:
        doc.completion_confirmed_by_client = 0
        doc.save(ignore_permissions=True)

    if task_name:
        task = frappe.get_doc("ModerationTask", task_name)
        task.status = "Approved" if approve else "Rejected"
        task.moderator = frappe.session.user
        task.decision_note = decision_note
        task.decision_date = now_datetime()
        task.save(ignore_permissions=True)

    return {"name": doc.name, "status": doc.status, "approved": bool(approve)}
