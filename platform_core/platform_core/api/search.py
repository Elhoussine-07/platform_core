# -*- coding: utf-8 -*-
"""
api/search.py
=============
APIs Frontend publiques (allow_guest=True) de recherche & découverte.

Références CDC v5 :
- 3.1 Recherche sémantique avancée (recherche en langage naturel, par
  similarité, indexation continue) - déléguée au microservice
  `search-service` (Python + Elasticsearch, port 8082, cf. architecture).
- 4.1 Page d'accueil & Recherche publique (moteur de recherche public,
  filtres catégorie/sous-catégorie, "consultation des profils sans
  connexion" pour maximiser le SEO et le trafic entrant).
- 2.3 Opportunités - onglet "Disponibles" (vue exhaustive de tous les
  projets publics, filtres avancés : budget, localisation, date de
  publication, sous-catégorie, type de besoin).

Positionnement architectural (important) :
Ce fichier reste un pur module de RETRAIT/FILTRAGE de données. Le calcul
du score de matching IA (matching-service, Spring Boot, port 8081) et de
la recherche sémantique par embeddings (search-service, FastAPI/ES, port
8082) sont des microservices EXTERNES. Ce module :
  1) tente d'appeler ces microservices quand une clé de recherche en
     langage naturel est fournie ou qu'une similarité doit être calculée ;
  2) retombe (fallback) sur une recherche SQL structurée locale si le
     microservice est indisponible ou non configuré, afin de ne jamais
     bloquer l'expérience utilisateur.
La génération/écriture du `matching_score` sur `Opportunity` reste de la
responsabilité de `api/matching.py` (bridge vers matching-service) — ce
fichier se contente de le lire s'il existe déjà.

Configuration attendue dans site_config.json :
- search_service_url (ex: "http://search-service:8082")

Contrat d'API supposé côté search-service (à ajuster selon l'implémentation
réelle du microservice) :
- POST {search_service_url}/api/search/agencies  {query, filters, page, page_size}
- POST {search_service_url}/api/search/similar-agencies {agency, page_size}
"""

import requests

import frappe
from frappe import _
from frappe.utils import flt, cint


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _call_search_service(path, payload, timeout=3):
    """
    Appelle le microservice search-service. Retourne None (silencieusement,
    avec log d'avertissement) en cas d'échec/indisponibilité, pour permettre
    un fallback SQL local sans casser l'expérience utilisateur.
    """
    base_url = frappe.conf.get("search_service_url")
    if not base_url:
        return None
    try:
        response = requests.post(f"{base_url}{path}", json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        frappe.logger("search_service").warning(f"search-service indisponible ({path}): {e}")
        return None


def _get_client_profile_if_any(user=None):
    """Retourne le ClientProfile de l'utilisateur connecté, ou None (guest/agence)."""
    user = user or frappe.session.user
    if user == "Guest":
        return None
    return frappe.db.get_value("ClientProfile", {"user": user})


def _get_active_agency_if_any(user=None):
    """Retourne l'agence active de l'utilisateur connecté, ou None (guest/client)."""
    user = user or frappe.session.user
    if user == "Guest":
        return None
    cached = frappe.cache().get_value(f"active_agency:{user}")
    if cached:
        return cached
    memberships = frappe.get_all(
        "AgencyMember", filters={"user": user, "status": "Active"}, pluck="agency",
    )
    return memberships[0] if len(memberships) == 1 else None


def _record_profile_view(agency, client):
    """
    Journalise la consultation d'un profil agence par un client authentifié
    (AgencyActivity - alimente le scoring de prospection 2.6.1 côté agence
    ET la prédiction de succès / recommandation IA). La détection IP des
    visiteurs anonymes (VisitorLog) relève du microservice prospection
    (cf. api/prospection.py) et n'est volontairement PAS gérée ici.
    """
    if not client:
        return
    frappe.get_doc({
        "doctype": "AgencyActivity",
        "client": client,
        "agency": agency,
        "action_type": "profile_view",
        "created_date": frappe.utils.now_datetime(),
    }).insert(ignore_permissions=True)


AGENCY_PUBLIC_FIELDS = [
    "name", "agency_name", "logo", "slogan", "description", "year_founded",
    "team_size", "website", "languages", "remote_work", "cover_image",
    "coverage", "location", "country", "rating", "reviews_count",
    "profile_completion",
]


def _serialize_agency_teaser(agency_doc):
    """Carte de résultat de recherche (liste), sans les sections détaillées."""
    return {field: agency_doc.get(field) for field in AGENCY_PUBLIC_FIELDS}


# ---------------------------------------------------------------------------
# 1. search_agencies (public)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def search_agencies(q=None, category=None, sub_category=None, location=None,
                     remote_work=None, min_rating=None, sort_by="relevance",
                     page=1, page_size=20):
    """
    Moteur de recherche public d'agences (4.1). Si `q` est fourni (recherche
    en langage naturel, ex : "je cherche une agence pour refaire mon identité
    visuelle"), tente une recherche sémantique via search-service (3.1) ;
    sinon, ou en cas d'indisponibilité du microservice, effectue une
    recherche structurée par filtres à facettes directement en base.

    Accessible sans connexion (allow_guest=True) - "Consultation des profils
    sans connexion" (4.1), pour maximiser le SEO et le trafic entrant.
    """
    page = cint(page) or 1
    page_size = cint(page_size) or 20
    filters_payload = {
        "category": category, "sub_category": sub_category, "location": location,
        "remote_work": remote_work, "min_rating": min_rating,
    }

    if q:
        remote_result = _call_search_service(
            "/api/search/agencies",
            {"query": q, "filters": filters_payload, "page": page, "page_size": page_size},
        )
        if remote_result and remote_result.get("agency_names"):
            agencies = frappe.get_all(
                "AgencyProfile",
                filters={"name": ["in", remote_result["agency_names"]]},
                fields=AGENCY_PUBLIC_FIELDS,
            )
            # Préserve l'ordre de pertinence renvoyé par le moteur sémantique.
            order_map = {n: i for i, n in enumerate(remote_result["agency_names"])}
            agencies.sort(key=lambda a: order_map.get(a["name"], 999999))
            return {
                "agencies": [_serialize_agency_teaser(a) for a in agencies],
                "source": "search-service",
                "page": page,
                "page_size": page_size,
                "total_count": remote_result.get("total_count", len(agencies)),
            }
        # Fallback : recherche texte simple sur nom/description/slogan.

    conditions = []
    values = {}

    if q:
        conditions.append(
            "(ap.agency_name LIKE %(q)s OR ap.description LIKE %(q)s OR ap.slogan LIKE %(q)s)"
        )
        values["q"] = f"%{q}%"
    if location:
        conditions.append("ap.location LIKE %(location)s")
        values["location"] = f"%{location}%"
    if remote_work is not None:
        conditions.append("ap.remote_work = %(remote_work)s")
        values["remote_work"] = cint(remote_work)
    if min_rating:
        conditions.append("ap.rating >= %(min_rating)s")
        values["min_rating"] = flt(min_rating)
    if category:
        conditions.append(
            """ap.name IN (
                SELECT parent FROM `tabAgencyService` WHERE service_name LIKE %(category)s
            )"""
        )
        values["category"] = f"%{category}%"
    if sub_category:
        conditions.append(
            """ap.name IN (
                SELECT parent FROM `tabAgencyService` WHERE service_name LIKE %(sub_category)s
            )"""
        )
        values["sub_category"] = f"%{sub_category}%"

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    order_clause = {
        "relevance": "ap.reviews_count DESC, ap.rating DESC",
        "rating": "ap.rating DESC",
        "recent": "ap.creation DESC",
    }.get(sort_by, "ap.rating DESC")

    offset = (page - 1) * page_size
    agencies = frappe.db.sql(
        f"""
        SELECT ap.name, ap.agency_name, ap.logo, ap.slogan, ap.description,
               ap.year_founded, ap.team_size, ap.website, ap.languages,
               ap.remote_work, ap.cover_image, ap.coverage, ap.location,
               ap.country, ap.rating, ap.reviews_count, ap.profile_completion
        FROM `tabAgencyProfile` ap
        WHERE {where_clause}
        ORDER BY {order_clause}
        LIMIT %(page_size)s OFFSET %(offset)s
        """,
        {**values, "page_size": page_size, "offset": offset},
        as_dict=True,
    )
    total_count = frappe.db.sql(
        f"SELECT COUNT(*) AS cnt FROM `tabAgencyProfile` ap WHERE {where_clause}",
        values, as_dict=True,
    )[0].cnt

    return {
        "agencies": agencies,
        "source": "local_fallback",
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# 2. search_projects (public - teaser pour invités, vue complète pour agences)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def search_projects(category=None, sub_category=None, need_type=None, location=None,
                     budget_min=None, budget_max=None, from_date=None, to_date=None,
                     sort_by="recent", page=1, page_size=20):
    """
    Vue exhaustive des projets publics (2.3 - onglet "Disponibles"), avec
    filtres avancés (budget, localisation, date de publication, sous-
    catégorie, type de besoin - Projet/Stage/Job).

    Note de sécurité : le DocType `Project` refuse la lecture au rôle Guest
    au niveau permissions Frappe (protection du CDC/coordonnées client).
    Cette API expose donc volontairement, en mode invité, une version
    "teaser" anonymisée (catégorie, budget, localisation, délai, type de
    besoin, date de publication) SANS l'identité du client ni le contenu
    détaillé du brief - à des fins de SEO/marketing (4.1). Les agences
    authentifiées reçoivent la vue complète (titre, description, score de
    matching s'il a déjà été calculé par matching-service).
    """
    page = cint(page) or 1
    page_size = cint(page_size) or 20
    agency = _get_active_agency_if_any()

    conditions = ["p.status = 'Posted'"]
    values = {}

    if category:
        conditions.append("p.category = %(category)s")
        values["category"] = category
    if sub_category:
        conditions.append("p.sub_category = %(sub_category)s")
        values["sub_category"] = sub_category
    if need_type:
        conditions.append("p.need_type = %(need_type)s")
        values["need_type"] = need_type
    if location:
        conditions.append("p.location LIKE %(location)s")
        values["location"] = f"%{location}%"
    if budget_min:
        conditions.append("p.budget_max >= %(budget_min)s")
        values["budget_min"] = flt(budget_min)
    if budget_max:
        conditions.append("p.budget_min <= %(budget_max)s")
        values["budget_max"] = flt(budget_max)
    if from_date and to_date:
        conditions.append("DATE(p.creation) BETWEEN %(from_date)s AND %(to_date)s")
        values["from_date"] = from_date
        values["to_date"] = to_date

    where_clause = " AND ".join(conditions)
    order_clause = {
        "recent": "p.creation DESC",
        "budget_desc": "p.budget_max DESC",
        "budget_asc": "p.budget_min ASC",
    }.get(sort_by, "p.creation DESC")

    offset = (page - 1) * page_size

    # `ignore_permissions` volontaire : Guest n'a pas de droit de lecture
    # brut sur Project, mais cette méthode whitelisted ne renvoie qu'un
    # sous-ensemble de champs non sensibles pour les invités (voir plus haut).
    rows = frappe.db.sql(
        f"""
        SELECT p.name, p.need_type, p.category, p.sub_category, p.budget_min,
               p.budget_max, p.delivery_delay_days, p.location, p.creation,
               p.title, p.description
        FROM `tabProject` p
        WHERE {where_clause}
        ORDER BY {order_clause}
        LIMIT %(page_size)s OFFSET %(offset)s
        """,
        {**values, "page_size": page_size, "offset": offset},
        as_dict=True,
    )
    total_count = frappe.db.sql(
        f"SELECT COUNT(*) AS cnt FROM `tabProject` p WHERE {where_clause}",
        values, as_dict=True,
    )[0].cnt

    is_guest = frappe.session.user == "Guest"
    projects = []
    project_names = [r.name for r in rows]

    matching_scores = {}
    if agency and project_names:
        score_rows = frappe.get_all(
            "Opportunity",
            filters={"project": ["in", project_names], "agency": agency},
            fields=["project", "matching_score", "success_prediction"],
        )
        matching_scores = {r.project: r for r in score_rows}

    for row in rows:
        if is_guest:
            # Vue teaser : ni titre, ni description, ni identité client.
            projects.append({
                "name": row.name,
                "need_type": row.need_type,
                "category": row.category,
                "sub_category": row.sub_category,
                "budget_min": row.budget_min,
                "budget_max": row.budget_max,
                "delivery_delay_days": row.delivery_delay_days,
                "location": row.location,
                "posted_on": row.creation,
            })
        else:
            score = matching_scores.get(row.name)
            projects.append({
                "name": row.name,
                "title": row.title,
                "description": row.description,
                "need_type": row.need_type,
                "category": row.category,
                "sub_category": row.sub_category,
                "budget_min": row.budget_min,
                "budget_max": row.budget_max,
                "delivery_delay_days": row.delivery_delay_days,
                "location": row.location,
                "posted_on": row.creation,
                "matching_score": score.matching_score if score else None,
                "success_prediction": score.success_prediction if score else None,
            })

    return {
        "projects": projects,
        "teaser_mode": is_guest,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# 3. get_agency_by_id (public - profil complet)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_agency_by_id(agency):
    """
    Profil public complet d'une agence (2.2 - les 7 sections : Présentation,
    Prestation, Réalisation, Staff, Certificats, Avis, Contact).
    Accessible sans connexion (4.1). Journalise la consultation côté client
    authentifié (scoring de prospection 2.6.1, cf. _record_profile_view).
    """
    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable."), frappe.DoesNotExistError)

    profile = frappe.get_doc("AgencyProfile", agency)

    client = _get_client_profile_if_any()
    if client:
        _record_profile_view(agency, client)

    recent_reviews = frappe.get_all(
        "AgencyReview",
        filters={"agency": agency, "status": "Approved"},
        fields=["name", "client", "rating", "comment", "creation"],
        order_by="creation desc",
        limit_page_length=3,
    )

    return {
        # 2.2.1 Présentation
        "presentation": {
            "name": profile.name,
            "agency_name": profile.agency_name,
            "logo": profile.logo,
            "slogan": profile.slogan,
            "description": profile.description,
            "year_founded": profile.year_founded,
            "team_size": profile.team_size,
            "website": profile.website,
            "languages": profile.languages,
            "remote_work": profile.remote_work,
            "cover_image": profile.cover_image,
            "coverage": profile.coverage,
            "location": profile.location,
            "annual_revenue": profile.annual_revenue,
            "country": profile.country,
            "legal_id_label": profile.legal_id_label,
        },
        # 2.2.2 Prestation (Services)
        "services": [
            {
                "service_name": s.service_name, "description": s.description,
                "price_range": s.price_range, "tech_stack": s.tech_stack,
                "skills": s.skills, "projects_in_progress": s.projects_in_progress,
            }
            for s in profile.get("agency_service") or []
        ] if profile.meta.has_field("agency_service") else [],
        # 2.2.3 Réalisation (Portfolio)
        "portfolio": [
            {
                "title": p.title, "status": p.status, "image": p.image,
                "video_url": p.video_url, "result_url": p.result_url,
                "budget": p.budget, "collaboration_period": p.collaboration_period,
                "agency_feedback": p.agency_feedback, "problem_solution": p.problem_solution,
                "client_confirmed": p.client_confirmed,
            }
            for p in profile.get("agency_portfolio") or []
        ] if profile.meta.has_field("agency_portfolio") else [],
        # 2.2.4 Staff (Équipe)
        "team": [
            {
                "member": t.member, "photo": t.photo, "role": t.role,
                "description": t.description, "history": t.history,
                "linkedin_url": t.linkedin_url,
                "show_linked_agencies": t.show_linked_agencies,
            }
            for t in profile.get("agency_team") or []
        ] if profile.meta.has_field("agency_team") else [],
        # 2.2.5 Certificats
        "certifications": [
            {
                "photo": c.photo, "title": c.title, "description": c.description,
                "issuing_organization": c.issuing_organization,
                "obtained_date": c.obtained_date, "expiration_date": c.expiration_date,
                "level": c.level, "technology": c.technology, "is_verified": c.is_verified,
            }
            for c in profile.get("agency_certification") or []
        ] if profile.meta.has_field("agency_certification") else [],
        # 2.2.6 Avis
        "reviews_summary": {
            "average_rating": profile.rating,
            "total_reviews": profile.reviews_count,
            "recent_reviews": recent_reviews,
        },
        # 2.2.7 Contact
        "contact": {
            "social_links": [
                {"platform": l.platform, "url": l.url} for l in profile.get("social_links") or []
            ],
            "phone": profile.phone,
            "email": profile.email,
            "address": profile.location,
        },
    }


# ---------------------------------------------------------------------------
# 4. get_similar_agencies (public)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_similar_agencies(agency, page_size=6):
    """
    Suggestions d'agences "similaires" à un profil déjà consulté (3.1 -
    Recherche par similarité). Tente d'abord le microservice search-service
    (similarité par embeddings) ; à défaut, retombe sur une heuristique
    locale : agences partageant au moins une catégorie/compétence de service
    et/ou la même localisation, hors l'agence de référence elle-même.
    """
    if not frappe.db.exists("AgencyProfile", agency):
        frappe.throw(_("Agence introuvable."), frappe.DoesNotExistError)

    page_size = cint(page_size) or 6

    remote_result = _call_search_service(
        "/api/search/similar-agencies", {"agency": agency, "page_size": page_size},
    )
    if remote_result and remote_result.get("agency_names"):
        agencies = frappe.get_all(
            "AgencyProfile",
            filters={"name": ["in", remote_result["agency_names"]]},
            fields=AGENCY_PUBLIC_FIELDS,
        )
        order_map = {n: i for i, n in enumerate(remote_result["agency_names"])}
        agencies.sort(key=lambda a: order_map.get(a["name"], 999999))
        return {"agencies": agencies, "source": "search-service"}

    # Fallback local : agences partageant des compétences/stack ou la même
    # localisation, triées par nombre de correspondances puis par note.
    reference_skills = frappe.get_all(
        "AgencyService", filters={"parent": agency}, pluck="skills",
    )
    skill_keywords = set()
    for skills in reference_skills:
        if skills:
            skill_keywords.update(s.strip().lower() for s in skills.split(",") if s.strip())

    reference_location = frappe.db.get_value("AgencyProfile", agency, "location")

    candidates = frappe.db.sql(
        """
        SELECT ap.name, ap.agency_name, ap.logo, ap.slogan, ap.location,
               ap.rating, ap.reviews_count,
               GROUP_CONCAT(DISTINCT asvc.skills SEPARATOR ',') AS all_skills
        FROM `tabAgencyProfile` ap
        LEFT JOIN `tabAgencyService` asvc ON asvc.parent = ap.name
        WHERE ap.name != %(agency)s
        GROUP BY ap.name
        """,
        {"agency": agency}, as_dict=True,
    )

    scored = []
    for c in candidates:
        candidate_keywords = set()
        if c.all_skills:
            candidate_keywords.update(s.strip().lower() for s in c.all_skills.split(",") if s.strip())
        overlap = len(skill_keywords & candidate_keywords)
        same_location = 1 if reference_location and c.location == reference_location else 0
        score = overlap * 2 + same_location + flt(c.rating) * 0.1
        if overlap > 0 or same_location:
            scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [c for _score, c in scored[:page_size]]

    return {
        "agencies": [
            {
                "name": c.name, "agency_name": c.agency_name, "logo": c.logo,
                "slogan": c.slogan, "location": c.location,
                "rating": c.rating, "reviews_count": c.reviews_count,
            }
            for c in top
        ],
        "source": "local_fallback",
    }