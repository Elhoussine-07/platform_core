# -*- coding: utf-8 -*-
"""
platform_core/api/auth.py

API Frontend ? Module Authentification unifiée (cf. CDC v5 §4.2)
Utilisée par : Frontend React (via API Gateway Spring Cloud, port 8080)

Principe fonctionnel (CDC v5) :
- Une seule page d'authentification, deux entrées : Client (Entreprise) / Agence
- Inscription simplifiée : email + mot de passe + vérification OTP par email
- Après connexion : détection du type de compte -> redirection dashboard adapté
- Agence : gestion de la détection de doublon de nom (cf. §2.1) et du
  rattachement multi-agences (cf. §2.1.1) dès l'inscription

Note d'implémentation :
- Les codes OTP ne correspondent à aucun DocType du cahier des charges
  (42 DocTypes officiels). Ils sont donc stockés temporairement dans le
  cache Redis de Frappe (frappe.cache) avec expiration, plutôt que
  persistés en base -> évite de polluer le schéma avec une donnée
  volatile et sécurise leur durée de vie.
- validate_session() est l'endpoint dédié consommé par l'API Gateway
  (Spring Cloud Gateway) à chaque requête entrante pour vérifier la
  session et transmettre l'identité (rôle, client_profile / agence
  active) aux microservices internes via headers.
"""

import frappe
from frappe import _
from frappe.utils import random_string, now_datetime, cint

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 5 * 60          # 5 minutes
RESET_OTP_EXPIRY_SECONDS = 15 * 60   # 15 minutes
ACCOUNT_TYPES = ("Client", "Agency")


# ---------------------------------------------------------------------------
# Helpers internes (non exposés à l'API)
# ---------------------------------------------------------------------------

def _otp_cache_key(email: str, purpose: str) -> str:
    return f"auth_otp:{purpose}:{email.strip().lower()}"


def _generate_otp() -> str:
    digits = "0123456789"
    return "".join(__import__("random").choice(digits) for _ in range(OTP_LENGTH))


def _store_otp(email: str, purpose: str, otp: str, expiry: int):
    frappe.cache().set_value(_otp_cache_key(email, purpose), otp, expires_in_sec=expiry)


def _get_stored_otp(email: str, purpose: str):
    return frappe.cache().get_value(_otp_cache_key(email, purpose))


def _clear_otp(email: str, purpose: str):
    frappe.cache().delete_value(_otp_cache_key(email, purpose))


def _send_otp_email(email: str, otp: str, purpose: str):
    subject_map = {
        "signup": _("Votre code de vérification - Inscription"),
        "login": _("Votre code de connexion"),
        "reset": _("Votre code de réinitialisation de mot de passe"),
    }
    frappe.sendmail(
        recipients=[email],
        subject=subject_map.get(purpose, _("Votre code de vérification")),
        message=f"""
            <p>{_('Votre code de vérification est :')}</p>
            <h2>{otp}</h2>
            <p>{_('Ce code expire dans quelques minutes. Ne le partagez avec personne.')}</p>
        """,
        now=True,
    )


def _issue_and_send_otp(email: str, purpose: str, expiry: int = OTP_EXPIRY_SECONDS) -> None:
    otp = _generate_otp()
    _store_otp(email, purpose, otp, expiry)
    _send_otp_email(email, otp, purpose)


def _verify_otp_or_throw(email: str, purpose: str, otp: str):
    stored = _get_stored_otp(email, purpose)
    if not stored or str(stored) != str(otp).strip():
        frappe.throw(_("Code de vérification invalide ou expiré."), frappe.AuthenticationError)
    _clear_otp(email, purpose)
    
    # Marquer l'utilisateur comme vérifié pour le signup
    if purpose == "signup":
        frappe.db.set_value("User", email, "verified", 1)
        frappe.db.commit()


def _get_role_profile(user: str):
    """
    Retourne le profil applicatif (Client ou Agence) rattaché à un utilisateur
    Frappe, ainsi que le contexte agence actif en cas de multi-agences
    (cf. CDC §2.1.1 - un seul contexte actif à la fois).
    """
    client_profile = frappe.db.get_value("ClientProfile", {"user": user}, "name")
    if client_profile:
        return {
            "account_type": "Client",
            "client_profile": client_profile,
            "agencies": [],
            "active_agency": None,
        }

    memberships = frappe.get_all(
        "AgencyMember",
        filters={"user": user, "status": "Active"},
        fields=["name", "agency", "member_role"],
    )
    if memberships:
        active_agency = frappe.cache().get_value(f"active_agency:{user}")
        if not active_agency or not any(m.agency == active_agency for m in memberships):
            active_agency = memberships[0].agency
            frappe.cache().set_value(f"active_agency:{user}", active_agency)
        return {
            "account_type": "Agency",
            "client_profile": None,
            "agencies": memberships,
            "active_agency": active_agency,
        }

    return {
        "account_type": None,
        "client_profile": None,
        "agencies": [],
        "active_agency": None,
    }


class _run_as_administrator:
    """
    Context manager élevant temporairement la session Frappe à Administrator.

    Nécessaire car signup() s'exécute en contexte Guest (allow_guest=True) :
    `ignore_permissions=True` bypasse le système de permissions Frappe, mais
    pas les vérifications explicites de rôle codées à la main dans les hooks
    before_insert des DocTypes (ex: ClientProfile.before_insert vérifie que
    frappe.session.user a le rôle Client/Administrator). On élève donc la
    session le temps de l'insertion système, puis on restaure l'état
    précédent (Guest) avant de retourner la réponse.
    """

    def __enter__(self):
        self._previous_user = frappe.session.user
        frappe.set_user("Administrator")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        frappe.set_user(self._previous_user)


def _create_user(email: str, first_name: str, last_name: str, password: str, role: str):
    if frappe.db.exists("User", email):
        frappe.throw(_("Un compte existe déjà avec cet email."), frappe.DuplicateEntryError)

    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": first_name,
        "last_name": last_name or "",
        "send_welcome_email": 0,
        "enabled": 1,
        "new_password": password,
        "verified": 0,  # Nouveau compte non vérifié
        "roles": [{"role": role}],
    })
    user.flags.ignore_permissions = True
    user.insert(ignore_permissions=True)
    return user


# ---------------------------------------------------------------------------
# 1. signup ? Inscription Client ou Agency
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def signup(account_type: str, email: str, password: str, first_name: str,
           last_name: str = None, phone: str = None, agency_name: str = None,
           country: str = None):
    """
    Inscription simplifiée (cf. CDC §1.0 / §2.1 / §4.2).

    Client  : login/register simple, complétion progressive du profil ensuite.
    Agency  : détection de doublon sur agency_name ->
              - si l'agence existe déjà : création d'une AgencyJoinRequest
                (statut Pending, contexte "At Signup") pour rattachement,
                soumise à validation (cf. §2.1) ;
              - sinon : création d'une nouvelle AgencyProfile avec
                l'utilisateur comme premier membre (Owner).

    Dans les deux cas, un OTP est envoyé par email pour vérifier le compte ;
    la session n'est ouverte qu'après vérification via login().
    """
    if account_type not in ACCOUNT_TYPES:
        frappe.throw(_("Type de compte invalide. Valeurs autorisées : Client, Agency."))

    email = email.strip().lower()
    if not email or not password or not first_name:
        frappe.throw(_("Email, mot de passe et prénom sont obligatoires."))

    result = {}

    with _run_as_administrator():
        if account_type == "Client":
            user = _create_user(email, first_name, last_name, password, role="Client")

            client_profile = frappe.get_doc({
                "doctype": "ClientProfile",
                "user": user.name,
                "first_name": first_name,
                "last_name": last_name or "",
                "phone": phone,
                "country": country,
            })
            client_profile.flags.ignore_permissions = True
            client_profile.insert(ignore_permissions=True)

            result = {
                "account_type": "Client",
                "client_profile": client_profile.name,
                "status": "pending_verification",
            }

        else:  # Agency
            if not agency_name:
                frappe.throw(_("Le nom de l'agence est obligatoire."))

            user = _create_user(email, first_name, last_name, password, role="Agency")

            existing_agency = frappe.db.get_value("AgencyProfile", {"agency_name": agency_name}, "name")

            if existing_agency:
                # Détection de doublon (§2.1) -> demande de rattachement
                join_request = frappe.get_doc({
                    "doctype": "AgencyJoinRequest",
                    "user": user.name,
                    "agency": existing_agency,
                    "context": "At Signup",
                    "status": "Pending",
                })
                join_request.flags.ignore_permissions = True
                join_request.insert(ignore_permissions=True)

                result = {
                    "account_type": "Agency",
                    "agency": existing_agency,
                    "join_request": join_request.name,
                    "status": "pending_join_approval",
                }
            else:
                agency = frappe.get_doc({
                    "doctype": "AgencyProfile",
                    "agency_name": agency_name,
                    "email": email,
                    "phone": phone,
                    "country": country,
                })
                agency.flags.ignore_permissions = True
                agency.insert(ignore_permissions=True)

                member = frappe.get_doc({
                    "doctype": "AgencyMember",
                    "user": user.name,
                    "agency": agency.name,
                    "member_role": "Owner",
                    "status": "Active",
                })
                member.flags.ignore_permissions = True
                member.insert(ignore_permissions=True)

                frappe.cache().set_value(f"active_agency:{user.name}", agency.name)

                result = {
                    "account_type": "Agency",
                    "agency": agency.name,
                    "status": "pending_verification",
                }

    _issue_and_send_otp(email, purpose="signup", expiry=OTP_EXPIRY_SECONDS)
    frappe.db.commit()

    result["email"] = email
    result["message"] = _("Compte créé. Un code de vérification a été envoyé par email.")
    return result


# ---------------------------------------------------------------------------
# 2. verify_signup_otp ? Vérification OTP d'inscription
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def verify_signup_otp(email: str, otp: str):
    """
    Vérifie l'OTP envoyé lors de l'inscription et active le compte.
    Après vérification, l'utilisateur peut se connecter.
    """
    email = (email or "").strip().lower()
    if not email or not otp:
        frappe.throw(_("Email et code OTP sont obligatoires."))

    if not frappe.db.exists("User", email):
        frappe.throw(_("Aucun compte associé à cet email."), frappe.AuthenticationError)

    # Vérifier l'OTP - cette fonction va marquer l'utilisateur comme vérifié
    _verify_otp_or_throw(email, purpose="signup", otp=otp)

    return {
        "status": "verified",
        "message": _("Compte vérifié avec succès. Vous pouvez maintenant vous connecter."),
        "email": email
    }


# ---------------------------------------------------------------------------
# 3. login ? Connexion (sans OTP)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def login(email: str, password: str):
    """
    Connexion simple par email et mot de passe (sans OTP).
    Vérifie que le compte a été validé par OTP.
    """
    email = (email or "").strip().lower()
    if not email:
        frappe.throw(_("Email requis."))
    
    if not password:
        frappe.throw(_("Mot de passe requis."))

    if not frappe.db.exists("User", email):
        frappe.throw(_("Aucun compte associé à cet email."), frappe.AuthenticationError)

    # Vérifier si l'utilisateur est vérifié
    user = frappe.get_doc("User", email)
    if not user.get("verified", 0):
        frappe.throw(_("Veuillez d'abord vérifier votre email avec le code OTP reçu."), frappe.AuthenticationError)

    # Vérification des identifiants
    try:
        frappe.local.login_manager.authenticate(email, password)
    except frappe.AuthenticationError:
        frappe.throw(_("Email ou mot de passe incorrect."), frappe.AuthenticationError)

    frappe.local.login_manager.user = email
    frappe.local.login_manager.post_login()

    profile = _get_role_profile(email)
    frappe.db.commit()

    return {
        "status": "logged_in",
        "user": email,
        "sid": frappe.session.sid,
        **profile,
    }


# ---------------------------------------------------------------------------
# 4. logout ? Déconnexion
# ---------------------------------------------------------------------------

@frappe.whitelist()
def logout():
    """Termine la session Frappe en cours."""
    frappe.local.login_manager.logout()
    frappe.db.commit()
    return {"status": "logged_out"}


# ---------------------------------------------------------------------------
# 5. validate_session ? Validation session pour l'API Gateway
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def validate_session():
    """
    Endpoint interne consommé par l'API Gateway (Spring Cloud Gateway,
    cf. architecture) à chaque requête entrante, pour vérifier la validité
    de la session/cookie sid et transmettre l'identité de l'utilisateur
    (rôle, profil, agence active) aux microservices internes via headers
    (matching, prospection, ia, notifications...).
    """
    user = frappe.session.user

    if not user or user == "Guest":
        frappe.local.response.http_status_code = 401
        return {"valid": False}

    profile = _get_role_profile(user)

    return {
        "valid": True,
        "user": user,
        "roles": frappe.get_roles(user),
        **profile,
    }


# ---------------------------------------------------------------------------
# 6. get_current_user ? Utilisateur connecté
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_current_user():
    """
    Retourne les informations de l'utilisateur actuellement connecté,
    y compris son type de compte, son profil et, pour une agence
    multi-comptes, la liste des agences rattachées et l'agence active
    (cf. §2.1.1 - sélecteur d'agence / bascule).
    """
    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw(_("Aucune session active."), frappe.AuthenticationError)

    user_doc = frappe.get_doc("User", user)
    profile = _get_role_profile(user)

    return {
        "user": user,
        "full_name": user_doc.full_name,
        "email": user_doc.email,
        "roles": frappe.get_roles(user),
        **profile,
    }


# ---------------------------------------------------------------------------
# 7. reset_password ? Demande de réinitialisation
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def reset_password(email: str):
    """
    Envoie un OTP de réinitialisation par email si le compte existe.
    Réponse volontairement identique que le compte existe ou non,
    pour ne pas divulguer l'existence d'un email en base.
    """
    email = (email or "").strip().lower()
    if not email:
        frappe.throw(_("Email requis."))

    if frappe.db.exists("User", email):
        _issue_and_send_otp(email, purpose="reset", expiry=RESET_OTP_EXPIRY_SECONDS)
        frappe.db.commit()

    return {
        "status": "otp_sent_if_exists",
        "message": _("Si un compte existe avec cet email, un code de réinitialisation a été envoyé."),
    }


# ---------------------------------------------------------------------------
# 8. confirm_reset_password ? Confirmation réinitialisation
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def confirm_reset_password(email: str, otp: str, new_password: str):
    """
    Vérifie l'OTP de réinitialisation puis applique le nouveau mot de passe.
    """
    email = (email or "").strip().lower()
    if not email or not otp or not new_password:
        frappe.throw(_("Email, code et nouveau mot de passe sont obligatoires."))

    if not frappe.db.exists("User", email):
        frappe.throw(_("Aucun compte associé à cet email."), frappe.AuthenticationError)

    if len(new_password) < 8:
        frappe.throw(_("Le mot de passe doit contenir au moins 8 caractères."))

    _verify_otp_or_throw(email, purpose="reset", otp=otp)

    user_doc = frappe.get_doc("User", email)
    user_doc.new_password = new_password
    user_doc.flags.ignore_permissions = True
    user_doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {"status": "password_reset", "message": _("Mot de passe réinitialisé avec succès.")}
