# -*- coding: utf-8 -*-
"""
api/utils.py
============
APIs Frontend utilitaires : référentiels publics (pays, catégories,
sous-catégories, secteurs), paramètres plateforme exposables, et gestion
générique des fichiers (upload/téléchargement contrôlé).

Références CDC v5 :
- 1.2   Identifiant légal dynamique / pays (utilisé par get_countries)
- 4.1   Filtres catégorie / sous-catégorie (navigation à facettes)
- 1.3.1 Délais (48h/24h/24h) affichés côté client (get_platform_settings)
- 2.5.1 Paramètres de facturation (commission, échéances)
- 2.6.1 Seuils de scoring de prospection (transparence pour les agences)
- 1.3.2 bis / 1.3.3 Consultation du CDC PDF par l'agence, devis PDF par le client

Références doctypes_final.pdf :
- Country (core Frappe), ServiceCategory, ServiceSubCategory, Industry,
  PlatformSettings, File (core Frappe)

IMPORTANT - Factorisation :
Les fonctions `get_active_agency()` et `get_client_profile()` ci-dessous
sont désormais la référence partagée pour tous les modules API (elles
étaient dupliquées localement dans payment.py, review.py, notification.py
et search.py). Il est recommandé de remplacer ces duplications par :

    from platform_core.api.utils import get_active_agency, get_client_profile

dans ces fichiers, afin d'avoir une seule source de vérité pour la logique
de contexte multi-agences (2.1.1).
"""

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.file_manager import save_file, get_file_path


# ---------------------------------------------------------------------------
# Helpers partagés (contexte utilisateur) - à réutiliser depuis les autres API
# ---------------------------------------------------------------------------

def get_active_agency(user=None):
    """
    Retourne le nom (AgencyProfile) de l'agence active pour l'utilisateur
    connecté, selon le contexte de bascule multi-agences (2.1.1).
    Convention : cache Redis `active_agency:<user>` positionné par l'API de
    switch d'agence (cf. api/agency.py -> switch_agency). Retourne None si
    aucun contexte ne peut être déterminé (au lieu de lever une exception) -
    c'est aux appelants de décider si l'absence de contexte est bloquante.
    """
    user = user or frappe.session.user
    if user == "Guest":
        return None

    cached = frappe.cache().get_value(f"active_agency:{user}")
    if cached:
        return cached

    memberships = frappe.get_all(
        "AgencyMember", filters={"user": user, "status": "Active"}, pluck="agency",
    )
    if len(memberships) == 1:
        return memberships[0]
    return None


def get_active_agency_or_throw(user=None):
    """Variante stricte : lève une exception si aucun contexte agence n'est résolu."""
    agency = get_active_agency(user)
    if not agency:
        frappe.throw(
            _("Aucune agence active déterminée. Sélectionnez une agence via le switch."),
            frappe.PermissionError,
        )
    return agency


def get_client_profile(user=None):
    """Retourne le nom du ClientProfile de l'utilisateur connecté, ou None."""
    user = user or frappe.session.user
    if user == "Guest":
        return None
    return frappe.db.get_value("ClientProfile", {"user": user})


def get_client_profile_or_throw(user=None):
    """Variante stricte : lève une exception si aucun ClientProfile n'est trouvé."""
    client = get_client_profile(user)
    if not client:
        frappe.throw(_("Aucun profil Entreprise trouvé pour cet utilisateur."), frappe.PermissionError)
    return client


# ---------------------------------------------------------------------------
# 1. get_countries
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_countries():
    """
    Liste des pays disponibles (DocType core `Country`), utilisée notamment
    par l'identifiant légal dynamique (1.1/1.2 - RCCM, ICE, SIREN/SIRET selon
    le pays sélectionné) et l'inscription Agence (2.1).
    """
    countries = frappe.get_all(
        "Country",
        fields=["name", "country_name", "code"],
        order_by="country_name asc",
    )
    return {"countries": countries}


# ---------------------------------------------------------------------------
# 2. get_categories
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_categories():
    """
    Liste des catégories de service actives (ServiceCategory), utilisée pour
    la catégorisation du Smart Briefing IA (1.2) et les filtres de recherche
    publique à facettes (4.1).
    """
    categories = frappe.get_all(
        "ServiceCategory",
        filters={"is_active": 1},
        fields=["name", "category_name", "description", "icon"],
        order_by="category_name asc",
    )
    return {"categories": categories}


# ---------------------------------------------------------------------------
# 3. get_sub_categories
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_sub_categories(category=None):
    """
    Liste des sous-catégories de service actives (ServiceSubCategory),
    optionnellement filtrées par catégorie parente (4.1 - navigation à
    facettes catégorie -> sous-catégorie).
    """
    filters = {"is_active": 1}
    if category:
        if not frappe.db.exists("ServiceCategory", category):
            frappe.throw(_("Catégorie introuvable."), frappe.DoesNotExistError)
        filters["parent_category"] = category

    sub_categories = frappe.get_all(
        "ServiceSubCategory",
        filters=filters,
        fields=["name", "subcategory_name", "parent_category"],
        order_by="subcategory_name asc",
    )
    return {"sub_categories": sub_categories}


# ---------------------------------------------------------------------------
# 4. get_industries
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_industries():
    """Liste des secteurs d'activité actifs (Industry), utilisée sur ClientProfile (1.1)."""
    industries = frappe.get_all(
        "Industry",
        filters={"is_active": 1},
        fields=["name", "industry_name", "description"],
        order_by="industry_name asc",
    )
    return {"industries": industries}


# ---------------------------------------------------------------------------
# 5. get_platform_settings
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_platform_settings():
    """
    Sous-ensemble public/exposable de PlatformSettings (Single). Le DocType
    brut interdit la lecture aux rôles Client/Agency/Guest (protection des
    paramètres sensibles) ; cette API expose donc explicitement, via
    `ignore_permissions`, uniquement les valeurs utiles à l'affichage côté
    frontend, avec un niveau de détail croissant selon le rôle :

    - Tous (y compris invité) : délais du workflow de devis (1.3.1/1.3.3),
      utiles pour afficher des comptes à rebours côté client.
    - Agence authentifiée : + paramètres de facturation (2.5.1) et seuils de
      scoring de prospection (2.6.1 - transparence explicable du score,
      différenciateur clé assumé par le CDC).
    """
    settings = frappe.get_single("PlatformSettings")

    public_settings = {
        "quote_response_hours": settings.quote_response_hours,
        "reminder_extra_hours": settings.reminder_extra_hours,
        "suspension_grace_hours": settings.suspension_grace_hours,
    }

    roles = frappe.get_roles(frappe.session.user)
    if "Agency" in roles or "System Manager" in roles:
        public_settings.update({
            "commission_rate": settings.commission_rate,
            "invoice_due_days": settings.invoice_due_days,
            "auto_debit_notice_hours": settings.auto_debit_notice_hours,
            "lead_hot_threshold": settings.lead_hot_threshold,
            "lead_warm_min": settings.lead_warm_min,
            "lead_warm_max": settings.lead_warm_max,
            "lead_score_window_days": settings.lead_score_window_days,
        })

    return public_settings


# ---------------------------------------------------------------------------
# Résolution générique de propriété (upload_file / get_file)
# ---------------------------------------------------------------------------

# Doctypes autorisés en cible d'upload/téléchargement via ces APIs génériques,
# avec la stratégie de vérification de propriété associée. Étendre cette
# liste avec prudence : chaque entrée doit correspondre à un besoin réel
# du CDC (photo de profil, portfolio, certificats, devis, CDC PDF, etc.).
_ATTACHABLE_DOCTYPES = {
    "ClientProfile", "AgencyProfile", "AgencyPortfolio", "AgencyTeam",
    "AgencyCertification", "Project", "Proposal",
}


def _resolve_owner_check(doctype, docname):
    """
    Détermine si l'utilisateur courant est autorisé à ÉCRIRE (upload) sur ce
    document, selon une résolution générique de propriété :
    - ClientProfile : doc.user == utilisateur courant
    - AgencyProfile : agence == agence active de l'utilisateur
    - Child tables d'AgencyProfile (Portfolio/Team/Certification) :
      résolues via leur `parent` (AgencyProfile)
    - Project : client == ClientProfile de l'utilisateur (seul le client
      peut modifier ses propres pièces jointes, cf. 1.3.2 bis)
    - Proposal : agence == agence active (l'agence gère son fichier de devis)
    Retourne True/False.
    """
    if doctype not in _ATTACHABLE_DOCTYPES:
        return False

    if doctype == "ClientProfile":
        owner_user = frappe.db.get_value("ClientProfile", docname, "user")
        return owner_user == frappe.session.user

    if doctype == "AgencyProfile":
        return docname == get_active_agency()

    if doctype in ("AgencyPortfolio", "AgencyTeam", "AgencyCertification"):
        parent_agency = frappe.db.get_value(doctype, docname, "parent")
        return parent_agency == get_active_agency()

    if doctype == "Project":
        owner_client = frappe.db.get_value("Project", docname, "client")
        return owner_client == get_client_profile()

    if doctype == "Proposal":
        owner_agency = frappe.db.get_value("Proposal", docname, "agency")
        return owner_agency == get_active_agency()

    return False


def _resolve_read_check(doctype, docname):
    """
    Détermine si l'utilisateur courant est autorisé à LIRE un fichier attaché
    à ce document. Plus permissif que l'écriture dans les cas de
    consultation croisée explicitement prévus par le CDC :
    - Project (CDC PDF) : le client propriétaire OU toute agence ayant reçu
      ce projet (Opportunity existante) peut consulter (1.3.2 bis -
      "Consultation par l'agence").
    - Proposal (devis PDF) : l'agence émettrice OU le client du projet
      concerné (1.3.3 - le client doit pouvoir consulter le devis reçu).
    - Autres doctypes : mêmes règles que l'écriture (propriétaire uniquement).
    """
    if doctype == "Project":
        project = frappe.db.get_value("Project", docname, "client")
        if project == get_client_profile():
            return True
        agency = get_active_agency()
        if agency and frappe.db.exists("Opportunity", {"project": docname, "agency": agency}):
            return True
        return False

    if doctype == "Proposal":
        proposal = frappe.db.get_value("Proposal", docname, ["agency", "project"], as_dict=True)
        if not proposal:
            return False
        if proposal.agency == get_active_agency():
            return True
        project_client = frappe.db.get_value("Project", proposal.project, "client")
        return project_client == get_client_profile()

    return _resolve_owner_check(doctype, docname)


# ---------------------------------------------------------------------------
# 6. upload_file
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def upload_file(doctype, docname, fieldname, is_private=1):
    """
    Upload générique d'un fichier attaché à un document (photo de profil,
    logo, image de portfolio, photo de certificat, devis PDF, etc.), avec
    vérification de propriété avant tout enregistrement (cf.
    `_resolve_owner_check`). Le fichier est envoyé en multipart/form-data
    sous la clé `file`.
    """
    if not frappe.db.exists(doctype, docname):
        frappe.throw(_("Document cible introuvable."), frappe.DoesNotExistError)

    if not _resolve_owner_check(doctype, docname):
        frappe.throw(_("Vous n'êtes pas autorisé à modifier ce document."), frappe.PermissionError)

    uploaded = frappe.request.files.get("file")
    if not uploaded:
        frappe.throw(_("Aucun fichier reçu (champ multipart attendu : 'file')."))

    content = uploaded.stream.read()
    if not content:
        frappe.throw(_("Le fichier envoyé est vide."))

    file_doc = save_file(
        fname=uploaded.filename,
        content=content,
        dt=doctype,
        dn=docname,
        is_private=cint(is_private),
    )

    # Met à jour le champ Attach/Attach Image cible avec l'URL du fichier,
    # si un fieldname a été fourni et existe bien sur le doctype (les
    # child tables sont mises à jour de la même façon via leur propre nom).
    if fieldname:
        meta = frappe.get_meta(doctype)
        if meta.has_field(fieldname):
            frappe.db.set_value(doctype, docname, fieldname, file_doc.file_url)
        else:
            frappe.throw(_("Champ '{0}' inconnu sur {1}.").format(fieldname, doctype))

    return {
        "file_url": file_doc.file_url,
        "file_name": file_doc.file_name,
        "is_private": bool(cint(is_private)),
    }


# ---------------------------------------------------------------------------
# 7. get_file
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_file(file_url):
    """
    Récupère (télécharge) un fichier précédemment uploadé, avec vérification
    de droit de lecture selon le document auquel il est rattaché (cf.
    `_resolve_read_check` - gère notamment la consultation croisée du CDC
    PDF par l'agence et du devis PDF par le client).
    """
    file_doc = frappe.db.get_value(
        "File", {"file_url": file_url},
        ["name", "file_name", "attached_to_doctype", "attached_to_name", "is_private"],
        as_dict=True,
    )
    if not file_doc:
        frappe.throw(_("Fichier introuvable."), frappe.DoesNotExistError)

    if file_doc.is_private:
        if not file_doc.attached_to_doctype or not file_doc.attached_to_name:
            frappe.throw(_("Fichier orphelin : accès refusé."), frappe.PermissionError)
        if not _resolve_read_check(file_doc.attached_to_doctype, file_doc.attached_to_name):
            frappe.throw(_("Vous n'êtes pas autorisé à accéder à ce fichier."), frappe.PermissionError)

    full_doc = frappe.get_doc("File", file_doc.name)
    with open(get_file_path(full_doc.file_name), "rb") as f:
        content = f.read()

    frappe.local.response.filename = full_doc.file_name
    frappe.local.response.filecontent = content
    frappe.local.response.type = "download"