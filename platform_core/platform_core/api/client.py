# -*- coding: utf-8 -*-
"""
platform_core/api/client.py

API Frontend — Module Entreprise (Client) : Mon Profil (cf. CDC v5 §1.1)
et Collaborations (cf. CDC v5 §1.4).

Toutes les fonctions de ce fichier sont réservées au rôle "Client" et
opèrent sur le profil de l'utilisateur en session (aucun paramètre
d'identité n'est accepté depuis le frontend : le profil ciblé est
toujours celui de frappe.session.user, afin d'empêcher un client de
lire/modifier le profil d'un autre).

Aucune logique de matching/scoring IA ici : ce fichier ne fait
qu'exposer/maintenir les données déterministes du profil (cf.
clientprofile.py : _validate_legal_id, _calculate_trust_score,
_calculate_profile_completion). Le microservice de matching lit ces
champs via ses propres appels internes (matching.py).
"""

import frappe
from frappe import _
from frappe.utils.file_manager import save_file

# Champs éditables directement par le Client via update_profile().
# Les champs calculés/protégés (trust_score, profile_completion,
# legal_id_verified, phone_verified, legal_id_label) sont en
# perm_level 1 côté DocType : le rôle Client n'y a pas accès en
# écriture, donc même transmis, Frappe les ignore silencieusement.
# On les exclut malgré tout explicitement ici par clarté et défense
# en profondeur.
EDITABLE_PROFILE_FIELDS = (
	"first_name",
	"last_name",
	"company_name",
	"sector",
	"phone",
	"country",
	"legal_id",
)

# Champs pris en compte dans le calcul de complétion de profil,
# alignés sur les poids définis dans clientprofile.py
# (_calculate_profile_completion) — dupliqués ici uniquement pour
# construire la liste des champs manquants à retourner au frontend.
PROFILE_COMPLETION_WEIGHTS = {
	"first_name": 10,
	"last_name": 10,
	"company_name": 15,
	"phone": 10,
	"logo": 15,
	"country": 10,
	"legal_id": 15,
	"sector": 15,
}

FIELD_LABELS = {
	"first_name": _("Prénom"),
	"last_name": _("Nom"),
	"company_name": _("Raison sociale"),
	"phone": _("Téléphone"),
	"logo": _("Logo / photo de profil"),
	"country": _("Pays"),
	"legal_id": _("Identifiant légal"),
	"sector": _("Secteur d'activité"),
}

OTP_LENGTH = 6
PHONE_OTP_EXPIRY_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

class _run_as_administrator:
	"""
	Élève temporairement la session à Administrator le temps d'une
	opération système (ex: écriture d'un champ perm_level 1 après
	vérification serveur — OTP téléphone, registre légal). Restaure
	l'utilisateur précédent à la sortie, y compris en cas d'exception.
	"""

	def __enter__(self):
		self._previous_user = frappe.session.user
		frappe.set_user("Administrator")
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		frappe.set_user(self._previous_user)


def _get_client_profile_or_throw():
	"""
	Récupère le ClientProfile de l'utilisateur en session. Réservé au
	rôle Client (Administrator toléré pour le support/débogage).
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Authentification requise."), frappe.AuthenticationError)

	roles = frappe.get_roles(user)
	if "Client" not in roles and "Administrator" not in roles:
		frappe.throw(_("Accès réservé aux comptes Entreprise."), frappe.PermissionError)

	profile_name = frappe.db.get_value("ClientProfile", {"user": user}, "name")
	if not profile_name:
		frappe.throw(_("Aucun profil Entreprise associé à ce compte."), frappe.DoesNotExistError)

	return frappe.get_doc("ClientProfile", profile_name)


def _otp_cache_key(identifier: str, purpose: str) -> str:
	return f"client_otp:{purpose}:{identifier.strip().lower()}"


def _generate_otp() -> str:
	import random
	return "".join(random.choice("0123456789") for _ in range(OTP_LENGTH))


def _missing_completion_fields(profile) -> list:
	missing = []
	for field, weight in PROFILE_COMPLETION_WEIGHTS.items():
		if not getattr(profile, field, None):
			missing.append({
				"field": field,
				"label": FIELD_LABELS.get(field, field),
				"weight": weight,
			})
	return missing


# ---------------------------------------------------------------------------
# 1. get_profile — Profil client
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_profile():
	"""Retourne le profil Entreprise complet de l'utilisateur en session."""
	profile = _get_client_profile_or_throw()
	return profile.as_dict()


# ---------------------------------------------------------------------------
# 2. update_profile — Mise à jour du profil
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_profile(**kwargs):
	"""
	Met à jour les champs éditables du profil (cf. CDC §1.1 -
	"complétion progressive du profil"). Chaque complément déclenche
	un recalcul du score de confiance et du taux de complétion via le
	hook validate() de ClientProfile.

	Seuls les champs listés dans EDITABLE_PROFILE_FIELDS sont pris en
	compte ; tout autre paramètre transmis est ignoré.
	"""
	profile = _get_client_profile_or_throw()

	updated_fields = []
	for field in EDITABLE_PROFILE_FIELDS:
		if field in kwargs and kwargs[field] is not None:
			profile.set(field, kwargs[field])
			updated_fields.append(field)

	if not updated_fields:
		frappe.throw(_("Aucun champ valide à mettre à jour."))

	# Sauvegarde en tant qu'utilisateur réel : les champs perm_level 1
	# (trust_score, profile_completion, legal_id_verified,
	# phone_verified) restent protégés par le système de permissions
	# Frappe même si transmis par erreur.
	profile.save()
	frappe.db.commit()

	return {
		"status": "updated",
		"updated_fields": updated_fields,
		"profile": profile.as_dict(),
	}


# ---------------------------------------------------------------------------
# 3. upload_logo — Télécharger logo / photo de profil
# ---------------------------------------------------------------------------

@frappe.whitelist()
def upload_logo(file_base64: str = None, file_name: str = None):
	"""
	Attache le logo / photo de profil au ClientProfile (cf. CDC §1.1 -
	"Informations variables").

	Deux modes d'appel supportés :
	- multipart/form-data classique (fichier lu depuis frappe.request.files)
	- JSON avec file_base64 + file_name (upload encodé en base64)
	"""
	profile = _get_client_profile_or_throw()

	uploaded_file = None

	if frappe.request and frappe.request.files:
		file_key = next(iter(frappe.request.files), None)
		if file_key:
			f = frappe.request.files[file_key]
			uploaded_file = save_file(
				fname=f.filename,
				content=f.read(),
				dt="ClientProfile",
				dn=profile.name,
				is_private=0,
			)

	if not uploaded_file:
		if not file_base64 or not file_name:
			frappe.throw(_("Aucun fichier reçu (multipart ou file_base64/file_name requis)."))
		import base64
		content = base64.b64decode(file_base64)
		uploaded_file = save_file(
			fname=file_name,
			content=content,
			dt="ClientProfile",
			dn=profile.name,
			is_private=0,
			decode=False,
		)

	profile.logo = uploaded_file.file_url
	profile.save()
	frappe.db.commit()

	return {
		"status": "uploaded",
		"file_url": uploaded_file.file_url,
		"profile_completion": profile.profile_completion,
	}


# ---------------------------------------------------------------------------
# 4. verify_phone — Vérification téléphone par OTP
# ---------------------------------------------------------------------------

@frappe.whitelist()
def verify_phone(phone: str = None, otp: str = None):
	"""
	Vérification du numéro de téléphone par OTP (cf. CDC §1.1).

	Flux en 2 temps, identique dans l'esprit à auth.login() :
	1) Appel sans `otp` -> envoie un code au numéro fourni (ou au
	   numéro déjà enregistré sur le profil si `phone` est omis) et le
	   sauvegarde comme numéro en attente de vérification.
	2) Appel avec `otp` -> vérifie le code et marque phone_verified=1,
	   ce qui déclenche le recalcul du score de confiance.

	Note d'implémentation : aucune passerelle SMS n'est spécifiée dans
	l'architecture (Twilio/Vonage à intégrer en production). En
	attendant, l'OTP est journalisé côté serveur en mode développeur,
	suivant le même principe que auth.py.
	"""
	profile = _get_client_profile_or_throw()
	target_phone = phone or profile.phone

	if not target_phone:
		frappe.throw(_("Numéro de téléphone requis."))

	if not otp:
		code = _generate_otp()
		frappe.cache().set_value(
			_otp_cache_key(target_phone, "phone_verify"), code, expires_in_sec=PHONE_OTP_EXPIRY_SECONDS
		)
		# TODO(prod): remplacer par un envoi SMS réel (Twilio/Vonage...)
		if frappe.conf.get("developer_mode"):
			frappe.logger().info(f"[DEV OTP téléphone] {target_phone} -> {code}")

		if phone and phone != profile.phone:
			profile.phone = phone
			profile.save()
			frappe.db.commit()

		return {"status": "otp_sent", "phone": target_phone}

	stored = frappe.cache().get_value(_otp_cache_key(target_phone, "phone_verify"))
	if not stored or str(stored) != str(otp).strip():
		frappe.throw(_("Code de vérification invalide ou expiré."), frappe.AuthenticationError)

	frappe.cache().delete_value(_otp_cache_key(target_phone, "phone_verify"))

	with _run_as_administrator():
		profile.reload()
		profile.phone = target_phone
		profile.phone_verified = 1
		profile.flags.ignore_permissions = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()

	return {
		"status": "phone_verified",
		"phone": target_phone,
		"trust_score": profile.trust_score,
	}


# ---------------------------------------------------------------------------
# 5. verify_legal_id — Demande de vérification d'identifiant légal
# ---------------------------------------------------------------------------

@frappe.whitelist()
def verify_legal_id():
	"""
	Déclenche le contrôle croisé de l'identifiant légal auprès du
	registre public configuré pour le pays (cf. CDC §1.1 - "Vérification
	d'identité", anti-faux profils), via CountryLegalIDRule.

	Le format a déjà été validé de façon synchrone dans
	ClientProfile.validate() (_validate_legal_id) à chaque sauvegarde ;
	cet endpoint effectue en plus la vérification externe si le pays a
	`registry_check_enabled=1` et une `registry_api_url` configurée.

	Si la vérification externe n'est pas disponible pour ce pays, le
	profil reste en attente de vérification manuelle (le format valide
	suffit néanmoins à laisser le client publier des projets, la
	fiabilité étant construite progressivement, cf. §1.2 bis).
	"""
	profile = _get_client_profile_or_throw()

	if not profile.country or not profile.legal_id:
		frappe.throw(_("Le pays et l'identifiant légal doivent être renseignés avant vérification."))

	rule = frappe.db.get_value(
		"CountryLegalIDRule",
		{"country": profile.country, "is_active": 1},
		["name", "registry_check_enabled", "registry_api_url", "id_label"],
		as_dict=True,
	)

	if not rule:
		return {
			"status": "no_rule_configured",
			"message": _("Aucune règle de vérification configurée pour ce pays."),
		}

	if not rule.registry_check_enabled or not rule.registry_api_url:
		return {
			"status": "manual_review_pending",
			"message": _("Vérification automatique indisponible pour ce pays. Format validé, contrôle manuel en attente."),
		}

	# Vérification externe auprès du registre public du pays.
	verified = False
	try:
		import requests
		api_key = frappe.utils.password.get_decrypted_password(
			"CountryLegalIDRule", rule.name, fieldname="registry_api_key", raise_exception=False
		)
		response = requests.get(
			rule.registry_api_url,
			params={"id": profile.legal_id},
			headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
			timeout=8,
		)
		verified = response.status_code == 200
	except Exception:
		frappe.log_error(title="verify_legal_id - échec appel registre externe")
		verified = False

	if verified:
		with _run_as_administrator():
			profile.reload()
			profile.legal_id_verified = 1
			profile.flags.ignore_permissions = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()

		return {
			"status": "verified",
			"trust_score": profile.trust_score,
		}

	return {
		"status": "verification_failed",
		"message": _("Le registre n'a pas pu confirmer cet identifiant. Un contrôle manuel sera effectué."),
	}


# ---------------------------------------------------------------------------
# 6. get_trust_score — Score de confiance Entreprise
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_trust_score():
	"""
	Retourne le score de confiance Entreprise actuel (cf. CDC §1.1 -
	symétrique au PQI côté Agence) ainsi qu'un détail des signaux qui
	le composent, pour affichage pédagogique côté frontend.
	"""
	profile = _get_client_profile_or_throw()

	breakdown = {
		"legal_id_verified": bool(profile.legal_id_verified),
		"phone_verified": bool(profile.phone_verified),
		"has_logo": bool(profile.logo),
		"has_company_name": bool(profile.company_name),
		"has_sector": bool(profile.sector),
		"projects_published_count": profile.projects_published_count or 0,
		"account_seniority": profile.account_seniority,
	}

	return {
		"trust_score": profile.trust_score,
		"breakdown": breakdown,
	}


# ---------------------------------------------------------------------------
# 7. get_profile_completion — Taux de complétion du profil
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_profile_completion():
	"""
	Retourne le taux de complétion du profil et la liste des champs
	manquants avec leur poids, pour guider le client dans sa
	complétion progressive (cf. CDC §1.1).
	"""
	profile = _get_client_profile_or_throw()

	return {
		"profile_completion": profile.profile_completion,
		"missing_fields": _missing_completion_fields(profile),
	}


# ---------------------------------------------------------------------------
# 8. get_collaborations — Collaborations terminées
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_collaborations():
	"""
	Liste les agences avec lesquelles le client a au moins un projet au
	statut Terminé (cf. CDC §1.4), avec :
	- l'historique des projets terminés avec cette agence,
	- l'avis déposé par le client (le cas échéant),
	- l'avis reçu de l'agence sur le client (le cas échéant).
	"""
	profile = _get_client_profile_or_throw()

	completed_projects = frappe.get_all(
		"Project",
		filters={"client": profile.name, "status": "Completed"},
		fields=["name", "title", "budget_min", "budget_max", "start_date", "expected_end_date"],
	)

	if not completed_projects:
		return {"collaborations": []}

	project_names = [p.name for p in completed_projects]

	# L'agence d'un projet terminé est celle dont l'opportunité est
	# passée au statut "Terminée" pour ce projet (cf. §2.3).
	opportunities = frappe.get_all(
		"Opportunity",
		filters={"project": ["in", project_names], "status": "Terminée"},
		fields=["project", "agency"],
	)
	project_to_agency = {o.project: o.agency for o in opportunities}

	agencies_map = {}
	for project in completed_projects:
		agency = project_to_agency.get(project.name)
		if not agency:
			continue

		if agency not in agencies_map:
			agency_info = frappe.db.get_value(
				"AgencyProfile", agency, ["agency_name", "logo", "rating"], as_dict=True
			) or {}
			agencies_map[agency] = {
				"agency": agency,
				"agency_name": agency_info.get("agency_name"),
				"logo": agency_info.get("logo"),
				"rating": agency_info.get("rating"),
				"projects": [],
				"client_review": None,
				"agency_review_of_client": None,
			}

		agencies_map[agency]["projects"].append(project)

	for agency, data in agencies_map.items():
		review = frappe.get_all(
			"AgencyReview",
			filters={"client": profile.name, "agency": agency, "project": ["in", [p.name for p in data["projects"]]]},
			fields=["name", "rating", "comment", "status"],
			limit=1,
		)
		data["client_review"] = review[0] if review else None

		received_review = frappe.get_all(
			"ClientReview",
			filters={"client": profile.name, "agency": agency, "project": ["in", [p.name for p in data["projects"]]]},
			fields=["name", "rating", "comment"],
			limit=1,
		)
		data["agency_review_of_client"] = received_review[0] if received_review else None

	return {"collaborations": list(agencies_map.values())}
