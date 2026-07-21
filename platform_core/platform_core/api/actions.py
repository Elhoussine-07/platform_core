# -*- coding: utf-8 -*-
"""
platform_core/api/quick_actions.py

API Frontend — Module Entreprise / Actions rapides (CDC v5, §1.3.2 et §1.3.2 bis).

Couvre :
    - unicast_contact, multicast_contact, get_favorites, add_favorite,
      remove_favorite, generate_cdc, generate_cdc_from_quick_action

Règles métier clés respectées (cf. CDC v5) :
    - §1.3.2     : Unicast (une agence) / Multicast (plusieurs agences en
                   favoris), formulaire inspiré du briefing pour le type
                   "Projet". Dépôt entièrement gratuit (crédits supprimés
                   en v5, aucune vérification de solde).
    - §1.3.2 bis : génération automatique du CDC structuré en PDF, avec
                   modification possible par le client avant envoi
                   définitif ; verrouillage uniquement au passage "In
                   Progress" (géré par project.py, pas ici).
    - §1.3.2     : "Statut à l'envoi" — la demande place le projet au
                   statut Postulé (Posted), exactement comme un dépôt via
                   Postuler un projet.
    - §2.5.1 (indirect) : une agence dont les offres sont suspendues
      (offers_suspended, cf. AgencyProfile) ne doit pas recevoir de
      nouvelles opportunités.

NOTE IMPORTANTE : ce fichier est volontairement autonome (aucun import
depuis project.py), à la demande explicite du porteur du projet. Cela
signifie que la logique de génération du CDC est dupliquée entre
project.py et quick_actions.py. Si un jour vous voulez un rendu de CDC
strictement garanti identique entre le Smart Briefing IA et les Actions
rapides (recommandé par le CDC : "exactement le même mécanisme", §1.3.2
bis), il faudra extraire cette fonction dans un module commun
(ex: platform_core/utils/cdc.py) et l'importer des deux côtés. Pour
l'instant, je duplique la même implémentation ici pour rester cohérent
tant que ce choix n'est pas fait explicitement.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime, cint, get_url


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

STATUS_DRAFT = "Draft"
STATUS_POSTED = "Posted"

CHANNEL_UNICAST = "Unicast"
CHANNEL_MULTICAST = "Multicast"

NEED_TYPES = ("Projet", "Stage", "Job")

OPPORTUNITY_STATUS_RECEIVED = "Reçue"


# ---------------------------------------------------------------------------
# Helpers internes (dupliqués volontairement, cf. note en tête de fichier)
# ---------------------------------------------------------------------------

def _get_client_profile(user=None):
    user = user or frappe.session.user
    client = frappe.db.get_value("ClientProfile", {"user": user}, "name")
    if not client:
        frappe.throw(_("Aucun profil Entreprise associé à cet utilisateur."),
                      frappe.PermissionError)
    return client


def _require_client_owner(project_doc, client=None):
    client = client or _get_client_profile()
    if project_doc.client != client:
        frappe.throw(_("Vous n'êtes pas propriétaire de ce projet."),
                      frappe.PermissionError)
    return client


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


def _generate_cdc_pdf(project_doc):
    """
    Génère le CDC structuré en PDF et l'attache au projet (champ cdc_file).

    Même structure de document que celle attendue en sortie du Smart
    Briefing IA (§1.2) — la génération n'est déclenchée ici que pour les
    demandes de type "Projet" (Stage/Job n'ont pas de CDC formel).
    """
    client_doc = frappe.get_doc("ClientProfile", project_doc.client)

    html = frappe.render_template(
        "platform_core/templates/cdc/cdc_template.html",
        {
            "project": project_doc,
            "client": client_doc,
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


def _check_agency_available(agency):
    """Vérifie qu'une agence existe et n'a pas ses offres suspendues (impayé, §2.5.1)."""
    agency_doc = frappe.db.get_value(
        "AgencyProfile", agency, ["name", "offers_suspended", "agency_name"], as_dict=True
    )
    if not agency_doc:
        frappe.throw(_("Agence introuvable : {0}").format(agency))
    return agency_doc


def _notify_agency_new_opportunity(agency, project_doc):
    """Notifie le(s) propriétaire(s)/membres de l'agence d'une nouvelle demande."""
    owners = frappe.get_all(
        "AgencyMember",
        filters={"agency": agency, "status": "Active"},
        fields=["user"],
    )
    for row in owners:
        _notify(
            user=row.user,
            ntype="Project",
            title=_("Nouvelle demande reçue"),
            message=_("Vous avez reçu une nouvelle demande pour le projet « {0} ».")
                    .format(project_doc.title),
            reference_doctype="Project",
            reference_name=project_doc.name,
            action_required=True,
            agency_context=agency,
        )


# ---------------------------------------------------------------------------
# 1. generate_cdc_from_quick_action
# ---------------------------------------------------------------------------

@frappe.whitelist()
def generate_cdc_from_quick_action(need_type="Projet", title=None, category=None,
                                    sub_category=None, budget_min=None, budget_max=None,
                                    delivery_delay_days=None, location=None,
                                    description=None):
    """
    Étape 1 des Actions rapides (§1.3.2 / §1.3.2 bis) : crée un projet en
    brouillon à partir du formulaire de contact direct (inspiré du
    briefing) et génère le CDC PDF pour les demandes de type "Projet".

    Ce projet reste en Draft : il n'est envoyé à aucune agence tant que
    unicast_contact() ou multicast_contact() n'a pas été appelé. Le client
    peut relire/modifier le CDC avant l'envoi définitif (§1.3.2 bis).
    """
    client = _get_client_profile()

    if need_type not in NEED_TYPES:
        frappe.throw(_("Type de besoin invalide : {0}").format(need_type))

    if need_type == "Projet" and not delivery_delay_days:
        frappe.throw(_("Le délai de réalisation est requis pour un besoin de type Projet."))

    project = frappe.get_doc({
        "doctype": "Project",
        "client": client,
        "title": title or _("Contact direct — {0}").format(category or need_type),
        "need_type": need_type,
        "channel": CHANNEL_UNICAST,  # valeur provisoire, ajustée à l'envoi définitif
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

    cdc_file = None
    if need_type == "Projet":
        cdc_file = _generate_cdc_pdf(project)

    return {"project": project.name, "status": project.status, "cdc_file": cdc_file}


# ---------------------------------------------------------------------------
# 2. generate_cdc
# ---------------------------------------------------------------------------

@frappe.whitelist()
def generate_cdc(project):
    """
    Régénère le CDC PDF d'un projet existant, non encore verrouillé
    (typiquement après une modification du brief via update_project()).
    Utilisé pour la relecture/modification avant envoi (§1.3.2 bis).
    """
    doc = frappe.get_doc("Project", project)
    _require_client_owner(doc)

    if doc.cdc_locked:
        frappe.throw(_("Le CDC est verrouillé, il ne peut plus être régénéré."))

    if doc.need_type != "Projet":
        frappe.throw(_("Aucun CDC formel n'est requis pour ce type de besoin ({0}).")
                     .format(doc.need_type))

    if not doc.delivery_delay_days or not doc.category:
        frappe.throw(_("Le brief est incomplet : impossible de générer le CDC."))

    cdc_file = _generate_cdc_pdf(doc)
    return {"project": doc.name, "cdc_file": cdc_file}


# ---------------------------------------------------------------------------
# 3. unicast_contact
# ---------------------------------------------------------------------------

@frappe.whitelist()
def unicast_contact(project, agency):
    """
    Envoi définitif d'un projet (déjà généré via generate_cdc_from_quick_action)
    à une seule agence (§1.3.2 — Unicast).

    Effets :
        - channel = "Unicast", status = "Posted" (Postulé)
        - création d'une Opportunity (source="Unicast", status="Reçue")
        - notification de l'agence destinataire
    """
    doc = frappe.get_doc("Project", project)
    client = _require_client_owner(doc)

    if doc.status != STATUS_DRAFT:
        frappe.throw(_("Ce projet a déjà été envoyé."))

    if doc.need_type == "Projet" and not doc.cdc_file:
        frappe.throw(_("Générez le CDC avant l'envoi (generate_cdc_from_quick_action)."))

    agency_info = _check_agency_available(agency)
    if cint(agency_info.offers_suspended):
        frappe.throw(_("Cette agence ne peut pas recevoir de nouvelle demande pour le moment."))

    doc.channel = CHANNEL_UNICAST
    doc.status = STATUS_POSTED
    doc.save(ignore_permissions=True)

    opportunity = frappe.get_doc({
        "doctype": "Opportunity",
        "project": doc.name,
        "agency": agency,
        "status": OPPORTUNITY_STATUS_RECEIVED,
        "source": CHANNEL_UNICAST,
    })
    opportunity.insert(ignore_permissions=True)

    _notify_agency_new_opportunity(agency, doc)

    return {"project": doc.name, "status": doc.status, "opportunity": opportunity.name}


# ---------------------------------------------------------------------------
# 4. multicast_contact
# ---------------------------------------------------------------------------

@frappe.whitelist()
def multicast_contact(project, agencies):
    """
    Envoi d'un même CDC à plusieurs agences sélectionnées en favoris
    (§1.3.2 — Multicast). `agencies` est une liste (ou une chaîne JSON)
    de noms d'AgencyProfile ; toutes doivent figurer dans les favoris du
    client (cf. CDC : "le client sélectionne plusieurs agences en
    favoris").

    Les agences dont les offres sont suspendues (impayé) sont ignorées
    et rapportées dans `skipped`, sans bloquer l'envoi aux autres.
    """
    doc = frappe.get_doc("Project", project)
    client = _require_client_owner(doc)

    if doc.status != STATUS_DRAFT:
        frappe.throw(_("Ce projet a déjà été envoyé."))

    if doc.need_type != "Projet":
        frappe.throw(_("Le Multicast n'est disponible que pour les besoins de type Projet."))

    if not doc.cdc_file:
        frappe.throw(_("Générez le CDC avant l'envoi (generate_cdc_from_quick_action)."))

    if isinstance(agencies, str):
        agencies = frappe.parse_json(agencies)

    if not agencies or not isinstance(agencies, list):
        frappe.throw(_("Veuillez sélectionner au moins une agence."))

    favorite_agencies = set(frappe.get_all(
        "FavoriteAgency", filters={"client": client}, pluck="agency"
    ))
    invalid = [a for a in agencies if a not in favorite_agencies]
    if invalid:
        frappe.throw(_("Les agences suivantes ne sont pas dans vos favoris : {0}")
                     .format(", ".join(invalid)))

    sent_to = []
    skipped = []

    for agency in agencies:
        agency_info = _check_agency_available(agency)
        if cint(agency_info.offers_suspended):
            skipped.append(agency)
            continue

        opportunity = frappe.get_doc({
            "doctype": "Opportunity",
            "project": doc.name,
            "agency": agency,
            "status": OPPORTUNITY_STATUS_RECEIVED,
            "source": CHANNEL_MULTICAST,
        })
        opportunity.insert(ignore_permissions=True)
        _notify_agency_new_opportunity(agency, doc)
        sent_to.append({"agency": agency, "opportunity": opportunity.name})

    if not sent_to:
        frappe.throw(_("Aucune agence disponible parmi la sélection (offres suspendues)."))

    doc.channel = CHANNEL_MULTICAST
    doc.status = STATUS_POSTED
    doc.save(ignore_permissions=True)

    return {"project": doc.name, "status": doc.status, "sent_to": sent_to, "skipped": skipped}


# ---------------------------------------------------------------------------
# 5. get_favorites
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_favorites():
    """Liste des agences favorites du client courant, avec résumé du profil."""
    client = _get_client_profile()

    favorites = frappe.get_all(
        "FavoriteAgency",
        filters={"client": client},
        fields=["name", "agency", "date_added"],
        order_by="date_added desc",
    )
    if not favorites:
        return []

    agency_names = [f.agency for f in favorites]
    profiles = {
        p.name: p for p in frappe.get_all(
            "AgencyProfile",
            filters={"name": ["in", agency_names]},
            fields=["name", "agency_name", "logo", "rating", "pqi_score",
                    "location", "offers_suspended"],
        )
    }

    result = []
    for fav in favorites:
        profile = profiles.get(fav.agency)
        if not profile:
            continue
        result.append({
            "favorite_id": fav.name,
            "agency": profile.name,
            "agency_name": profile.agency_name,
            "logo": profile.logo,
            "rating": profile.rating,
            "pqi_score": profile.pqi_score,
            "location": profile.location,
            "offers_suspended": profile.offers_suspended,
            "date_added": fav.date_added,
        })

    return result


# ---------------------------------------------------------------------------
# 6. add_favorite
# ---------------------------------------------------------------------------

@frappe.whitelist()
def add_favorite(agency):
    """Ajoute une agence aux favoris du client courant."""
    client = _get_client_profile()

    _check_agency_available(agency)  # vérifie juste l'existence, n'exclut pas les suspendues

    if frappe.db.exists("FavoriteAgency", {"client": client, "agency": agency}):
        frappe.throw(_("Cette agence est déjà dans vos favoris."))

    fav = frappe.get_doc({
        "doctype": "FavoriteAgency",
        "client": client,
        "agency": agency,
        "date_added": now_datetime(),
    })
    fav.insert()

    return {"favorite_id": fav.name, "agency": agency}


# ---------------------------------------------------------------------------
# 7. remove_favorite
# ---------------------------------------------------------------------------

@frappe.whitelist()
def remove_favorite(agency):
    """Retire une agence des favoris du client courant."""
    client = _get_client_profile()

    fav_name = frappe.db.get_value("FavoriteAgency", {"client": client, "agency": agency}, "name")
    if not fav_name:
        frappe.throw(_("Cette agence n'est pas dans vos favoris."))

    frappe.delete_doc("FavoriteAgency", fav_name, ignore_permissions=True)
    return {"removed": True, "agency": agency}
