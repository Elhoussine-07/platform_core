# -*- coding: utf-8 -*-
"""
platform_core/api/settings.py

API Frontend — Module Paramètres, transversal Client/Agency (CDC v5, §6).

Couvre :
    get_user_preferences, update_user_preferences, update_password,
    enable_2fa, get_notification_settings, update_notification_settings

CHAMPS MANQUANTS SIGNALÉS (ajout requis sur le DocType UserPreference
avant que certaines fonctions marchent réellement — ne bloque pas le
collage du fichier, seulement son exécution) :
    - notification_preferences (Long Text / JSON, défaut "{}") : utilisé
      par get/update_notification_settings. Le CDC §6.2/§7.2 mentionne
      des réglages par canal (email/plateforme) et catégorie, mais aucun
      champ ne les stocke dans les 42 DocTypes fournis.
    - two_factor_enabled (Check, défaut 0) : utilisé par enable_2fa().

NOTE 2FA : dans Frappe, l'authentification à deux facteurs est nativement
une config SITE-WIDE (System Settings > enable_two_factor_auth), pas un
simple toggle par utilisateur prêt à l'emploi. enable_2fa() ci-dessous
est un best-effort : il stocke la préférence et tente d'utiliser le
module frappe.twofactor pour générer un secret OTP, mais l'application
réelle de la vérification 2FA à la connexion nécessite en plus un hook
sur le processus de login (hors périmètre de ce fichier) qui consulte
ce flag. À valider/adapter selon votre version de Frappe (v14/v15).
"""

import json

import frappe
from frappe import _
from frappe.utils import cint


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

USER_PREFERENCE_UPDATABLE_FIELDS = {
    "language", "auto_detect_language", "theme",
    "follow_system_theme", "font_family", "font_size",
}

# Catégories de notification (cf. §7.2 : "relances de devis, alertes PQI,
# demandes de rattachement multi-agences, notifications de projet...")
NOTIFICATION_CATEGORIES = {
    "quote_reminders": _("Relances de devis"),
    "pqi_alerts": _("Alertes PQI"),
    "agency_join_requests": _("Demandes de rattachement multi-agences"),
    "project_notifications": _("Notifications de projet"),
    "reviews": _("Avis"),
    "system": _("Notifications système"),
}
NOTIFICATION_CHANNELS = ("email", "platform")

DEFAULT_NOTIFICATION_PREFERENCES = {
    category: {channel: True for channel in NOTIFICATION_CHANNELS}
    for category in NOTIFICATION_CATEGORIES
}


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _require_logged_in():
    if frappe.session.user == "Guest":
        frappe.throw(_("Connexion requise."), frappe.PermissionError)
    return frappe.session.user


def _get_or_create_preference(user=None):
    """UserPreference est unique par utilisateur (champ `user` Unique) — auto-création si absent."""
    user = user or _require_logged_in()

    name = frappe.db.get_value("UserPreference", {"user": user}, "name")
    if name:
        return frappe.get_doc("UserPreference", name)

    doc = frappe.get_doc({
        "doctype": "UserPreference",
        "user": user,
        "language": "fr",
        "auto_detect_language": 1,
        "theme": "Clair",
        "follow_system_theme": 0,
        "font_size": "M",
    })
    doc.insert(ignore_permissions=True)
    return doc


def _get_notification_prefs_dict(doc):
    raw = getattr(doc, "notification_preferences", None)
    if not raw:
        return dict(DEFAULT_NOTIFICATION_PREFERENCES)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return dict(DEFAULT_NOTIFICATION_PREFERENCES)

    # Complète les catégories manquantes avec les valeurs par défaut
    # (utile si de nouvelles catégories sont ajoutées après coup).
    merged = dict(DEFAULT_NOTIFICATION_PREFERENCES)
    for category, channels in parsed.items():
        if category in merged:
            merged[category].update({k: v for k, v in channels.items() if k in NOTIFICATION_CHANNELS})
    return merged


def _password_strength_ok(password):
    """Heuristique simple : longueur >= 8, au moins une lettre et un chiffre."""
    if not password or len(password) < 8:
        return False
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    return has_letter and has_digit


# ---------------------------------------------------------------------------
# 1. get_user_preferences
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_user_preferences():
    """Préférences profil : langue, thème, police (§6.2 à §6.5)."""
    doc = _get_or_create_preference()
    return {
        "language": doc.language,
        "auto_detect_language": doc.auto_detect_language,
        "theme": doc.theme,
        "follow_system_theme": doc.follow_system_theme,
        "font_family": doc.font_family,
        "font_size": doc.font_size,
    }


# ---------------------------------------------------------------------------
# 2. update_user_preferences
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_user_preferences(**fields):
    """
    Met à jour langue/thème/police. Enregistré au niveau du compte
    utilisateur (§6, point d'attention : "conservées lors d'une connexion
    depuis un autre appareil"), appliqué immédiatement côté frontend sans
    reconnexion.
    """
    doc = _get_or_create_preference()

    for k, v in fields.items():
        if k in USER_PREFERENCE_UPDATABLE_FIELDS:
            doc.set(k, v)

    doc.save(ignore_permissions=True)
    return {"updated": True}


# ---------------------------------------------------------------------------
# 3. update_password
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_password(old_password, new_password):
    """
    Change le mot de passe de l'utilisateur courant (§6.2, "Gestion du
    mot de passe" — lié au critère PQI "Sécurité du compte").
    """
    user = _require_logged_in()

    from frappe.utils.password import check_password, update_password as frappe_update_password

    try:
        check_password(user, old_password)
    except frappe.AuthenticationError:
        frappe.throw(_("Mot de passe actuel incorrect."))

    if not _password_strength_ok(new_password):
        frappe.throw(_(
            "Le nouveau mot de passe doit contenir au moins 8 caractères, "
            "avec au moins une lettre et un chiffre."
        ))

    frappe_update_password(user, new_password)

    return {"updated": True}


# ---------------------------------------------------------------------------
# 4. enable_2fa
# ---------------------------------------------------------------------------

@frappe.whitelist()
def enable_2fa(enable=1):
    """
    Active/désactive la préférence 2FA de l'utilisateur (§6.2).

    Best-effort (cf. note en tête de fichier) : stocke le flag et tente
    de générer un secret OTP via frappe.twofactor si le module est
    disponible. L'application réelle à la connexion nécessite un hook de
    login supplémentaire consultant `two_factor_enabled`.
    """
    user = _require_logged_in()
    enable = cint(enable)

    doc = _get_or_create_preference(user)

    if not hasattr(doc, "two_factor_enabled"):
        frappe.throw(_(
            "Le champ 'two_factor_enabled' est manquant sur UserPreference. "
            "Ajoutez ce champ (Check) au DocType avant d'utiliser cette fonction."
        ))

    doc.two_factor_enabled = enable
    doc.save(ignore_permissions=True)

    otp_secret = None
    qr_code = None

    if enable:
        try:
            from frappe.twofactor import get_default_totp_uri, get_otpsecret_for_user
            otp_secret = get_otpsecret_for_user(user)
            # La génération du QR exacte dépend de la version de Frappe —
            # à adapter si l'import ci-dessus échoue sur votre installation.
        except Exception:
            frappe.log_error(title="2FA : échec génération OTP secret")

    return {"two_factor_enabled": bool(enable), "otp_secret": otp_secret, "qr_code": qr_code}


# ---------------------------------------------------------------------------
# 5. get_notification_settings
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_notification_settings():
    """Réglages de notification par catégorie et par canal (§6.2, §7.2)."""
    doc = _get_or_create_preference()

    if not hasattr(doc, "notification_preferences"):
        frappe.throw(_(
            "Le champ 'notification_preferences' est manquant sur UserPreference. "
            "Ajoutez ce champ (Long Text) au DocType avant d'utiliser cette fonction."
        ))

    return {
        "categories": {k: str(v) for k, v in NOTIFICATION_CATEGORIES.items()},
        "channels": list(NOTIFICATION_CHANNELS),
        "preferences": _get_notification_prefs_dict(doc),
    }


# ---------------------------------------------------------------------------
# 6. update_notification_settings
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_notification_settings(preferences):
    """
    Met à jour les réglages de notification.

    `preferences` : dict (ou JSON) partiel, ex:
        {"quote_reminders": {"email": false, "platform": true}}
    Seules les catégories/canaux connus sont pris en compte ; le reste
    est ignoré silencieusement pour rester tolérant aux évolutions futures.
    """
    doc = _get_or_create_preference()

    if not hasattr(doc, "notification_preferences"):
        frappe.throw(_(
            "Le champ 'notification_preferences' est manquant sur UserPreference. "
            "Ajoutez ce champ (Long Text) au DocType avant d'utiliser cette fonction."
        ))

    if isinstance(preferences, str):
        preferences = frappe.parse_json(preferences)

    if not isinstance(preferences, dict):
        frappe.throw(_("`preferences` doit être un objet."))

    current = _get_notification_prefs_dict(doc)

    for category, channels in preferences.items():
        if category not in NOTIFICATION_CATEGORIES or not isinstance(channels, dict):
            continue
        for channel, value in channels.items():
            if channel in NOTIFICATION_CHANNELS:
                current[category][channel] = bool(value)

    doc.notification_preferences = json.dumps(current)
    doc.save(ignore_permissions=True)

    return {"preferences": current}
