# -*- coding: utf-8 -*-
"""
api/payment.py
================
APIs Frontend (Agence) + Webhook Stripe.

Références CDC v5 :
- 2.5   Facturation
- 2.5.1 Cycle de facturation « À payer »
- 2.6   Prospection (InactivityDispute lié au litige client inactif)

Références doctypes_final.pdf :
- Invoice, Payment, PaymentMethod, CommissionCredit, CreditApplication,
  InactivityDispute, PlatformSettings, ModerationTask, Notification, AgencyProfile

Note d'implémentation (PCI-DSS) :
Aucune donnée de carte brute ne transite ni n'est stockée côté Frappe.
Le frontend utilise Stripe.js / Stripe Elements pour obtenir un
`payment_method_id` (ou un `SetupIntent` confirmé) côté client ; seul cet
identifiant Stripe (non sensible) est transmis à `add_payment_method` et
stocké dans `PaymentMethod.provider_token` (Password, perm_level 1).

Note d'implémentation (Stripe Customer) :
Le schéma `AgencyProfile` ne comporte pas de champ `stripe_customer_id`.
Ce module suppose l'existence d'un champ personnalisé
`AgencyProfile.stripe_customer_id` (Data, hidden, perm_level 1) à ajouter
via Customize Form / fixture. À défaut, `_get_or_create_stripe_customer`
recrée un customer Stripe et le stocke à la volée dans ce champ.
"""

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate

try:
    import stripe
except ImportError:
    stripe = None


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------

def _stripe_client():
    """Configure et retourne le module stripe avec la clé secrète du site."""
    if not stripe:
        frappe.throw(_("La librairie Python 'stripe' n'est pas installée sur ce site."))
    secret_key = frappe.conf.get("stripe_secret_key")
    if not secret_key:
        frappe.throw(_("Clé secrète Stripe non configurée (site_config.json: stripe_secret_key)."))
    stripe.api_key = secret_key
    return stripe


def _get_active_agency(user=None):
    """
    Retourne le nom (AgencyProfile) de l'agence actuellement active pour
    l'utilisateur connecté, en tenant compte du switch multi-agences (2.1.1).

    Convention retenue : le contexte actif est stocké en cache Redis sous la
    clé `active_agency:<user>`, positionnée par l'API de bascule d'agence
    (cf. api/agency.py -> switch_agency). À défaut de contexte en cache,
    on retombe sur l'unique agence rattachée si l'utilisateur n'en a qu'une.
    """
    user = user or frappe.session.user
    cached = frappe.cache().get_value(f"active_agency:{user}")
    if cached:
        return cached

    memberships = frappe.get_all(
        "AgencyMember",
        filters={"user": user, "status": "Active"},
        pluck="agency",
    )
    if not memberships:
        frappe.throw(_("Aucune agence active trouvée pour cet utilisateur."), frappe.PermissionError)
    if len(memberships) > 1:
        frappe.throw(
            _("Plusieurs agences rattachées : sélectionnez une agence active via le switch avant de continuer."),
        )
    return memberships[0]


def _check_agency_owns(doctype, name, agency, agency_field="agency"):
    """Vérifie que le document appartient bien à l'agence active (if_owner)."""
    owner_agency = frappe.db.get_value(doctype, name, agency_field)
    if not owner_agency:
        frappe.throw(_("{0} introuvable.").format(doctype), frappe.DoesNotExistError)
    if owner_agency != agency:
        frappe.throw(_("Vous n'avez pas accès à ce document."), frappe.PermissionError)


def _notify(user, notif_type, title, message, link=None, reference_doctype=None,
            reference_name=None, agency_context=None, action_required=False):
    """Crée une Notification (module 7 - Historique des notifications)."""
    frappe.get_doc({
        "doctype": "Notification",
        "user": user,
        "type": notif_type,
        "title": title,
        "message": message,
        "link": link,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "agency_context": agency_context,
        "action_required": 1 if action_required else 0,
        "is_read": 0,
        "created_date": now_datetime(),
    }).insert(ignore_permissions=True)


def _get_available_credit_balance(agency):
    """Somme des soldes restants (balance) des CommissionCredit non épuisés d'une agence."""
    total = frappe.db.sql(
        """
        SELECT COALESCE(SUM(balance), 0)
        FROM `tabCommissionCredit`
        WHERE agency = %s AND balance > 0
        """,
        (agency,),
    )[0][0]
    return flt(total)


def _apply_available_credits(invoice_doc):
    """
    Applique automatiquement les crédits de commission disponibles (issus de
    litiges d'inactivité client validés, cf. InactivityDispute/CommissionCredit)
    sur le montant restant dû d'une facture, du plus ancien crédit au plus récent.
    Met à jour Invoice.credit_applied / amount_due et CommissionCredit.balance.
    """
    remaining_due = flt(invoice_doc.amount_due)
    if remaining_due <= 0:
        return invoice_doc

    credits = frappe.get_all(
        "CommissionCredit",
        filters={"agency": invoice_doc.agency, "balance": [">", 0]},
        fields=["name", "balance"],
        order_by="creation asc",
    )

    total_applied = flt(invoice_doc.credit_applied)
    for credit in credits:
        if remaining_due <= 0:
            break
        amount_to_apply = min(flt(credit.balance), remaining_due)
        if amount_to_apply <= 0:
            continue

        credit_doc = frappe.get_doc("CommissionCredit", credit.name)
        credit_doc.append("applications", {
            "invoice": invoice_doc.name,
            "amount": amount_to_apply,
            "application_date": nowdate(),
        })
        credit_doc.consumed_amount = flt(credit_doc.consumed_amount) + amount_to_apply
        credit_doc.balance = flt(credit_doc.amount) - flt(credit_doc.consumed_amount)
        credit_doc.save(ignore_permissions=True)

        remaining_due -= amount_to_apply
        total_applied += amount_to_apply

    invoice_doc.credit_applied = total_applied
    invoice_doc.amount_due = flt(invoice_doc.total) - total_applied
    invoice_doc.save(ignore_permissions=True)
    return invoice_doc


# ---------------------------------------------------------------------------
# 1. get_invoices
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_invoices(status=None, from_date=None, to_date=None, page=1, page_size=20):
    """
    Liste des factures de l'agence active (2.5 - Liste des factures).
    Filtrable par statut (Pending/Paid/Overdue/Cancelled) et par période
    d'émission, avec pagination.
    """
    agency = _get_active_agency()

    filters = {"agency": agency}
    if status:
        filters["status"] = status
    if from_date and to_date:
        filters["issue_date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["issue_date"] = [">=", from_date]
    elif to_date:
        filters["issue_date"] = ["<=", to_date]

    page = int(page)
    page_size = int(page_size)

    invoices = frappe.get_all(
        "Invoice",
        filters=filters,
        fields=[
            "name", "invoice_number", "project", "amount", "tax", "total",
            "commission_rate", "commission_amount", "credit_applied",
            "amount_due", "issue_date", "due_date", "status", "payment_date",
            "reminder_sent",
        ],
        order_by="issue_date desc",
        start=(page - 1) * page_size,
        page_length=page_size,
    )
    total_count = frappe.db.count("Invoice", filters=filters)

    return {
        "invoices": invoices,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# 2. get_invoice_detail
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_invoice_detail(invoice_name):
    """Détail complet d'une facture (2.5), avec le devis et le projet associés."""
    agency = _get_active_agency()
    _check_agency_owns("Invoice", invoice_name, agency)

    invoice = frappe.get_doc("Invoice", invoice_name)
    proposal = frappe.db.get_value(
        "Proposal", invoice.proposal,
        ["amount", "submitted_date", "decision_date"], as_dict=True,
    ) if invoice.proposal else None
    project = frappe.db.get_value(
        "Project", invoice.project,
        ["title", "need_type", "status"], as_dict=True,
    ) if invoice.project else None

    return {
        "invoice": invoice.as_dict(),
        "proposal": proposal,
        "project": project,
    }


# ---------------------------------------------------------------------------
# 3. download_invoice_pdf
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def download_invoice_pdf(invoice_name):
    """
    Télécharge la facture au format PDF (2.5 - Historique complet,
    factures téléchargeables). Génère le PDF à la volée via le moteur
    d'impression Frappe (aucun stockage de fichier nécessaire).
    """
    agency = _get_active_agency()
    _check_agency_owns("Invoice", invoice_name, agency)

    invoice = frappe.get_doc("Invoice", invoice_name)
    pdf_content = frappe.get_print(
        "Invoice", invoice_name, print_format="Invoice", as_pdf=True,
    )

    frappe.local.response.filename = f"{invoice.invoice_number or invoice_name}.pdf"
    frappe.local.response.filecontent = pdf_content
    frappe.local.response.type = "download"


# ---------------------------------------------------------------------------
# 4. create_payment_intent
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def create_payment_intent(invoice_name):
    """
    Crée une intention de paiement Stripe pour le règlement manuel en un clic
    d'une facture (2.5.1 étape 4 - "sinon, règlement manuel en un clic").
    Applique d'abord les crédits de commission disponibles sur la facture.
    """
    agency = _get_active_agency()
    _check_agency_owns("Invoice", invoice_name, agency)

    invoice = frappe.get_doc("Invoice", invoice_name)
    if invoice.status not in ("Pending", "Overdue"):
        frappe.throw(_("Cette facture n'est pas payable dans son statut actuel ({0}).").format(invoice.status))

    invoice = _apply_available_credits(invoice)

    if flt(invoice.amount_due) <= 0:
        # Entièrement couverte par les crédits : pas d'appel Stripe nécessaire.
        invoice.status = "Paid"
        invoice.payment_date = nowdate()
        invoice.save(ignore_permissions=True)
        return {"fully_covered_by_credit": True, "invoice": invoice.as_dict()}

    stripe_client = _stripe_client()
    customer_id = _get_or_create_stripe_customer(agency)

    intent = stripe_client.PaymentIntent.create(
        amount=int(flt(invoice.amount_due) * 100),  # montant en centimes
        currency=frappe.conf.get("stripe_currency", "eur"),
        customer=customer_id,
        metadata={
            "invoice": invoice.name,
            "agency": agency,
            "invoice_number": invoice.invoice_number or "",
        },
        description=f"Commission plateforme - Facture {invoice.invoice_number or invoice.name}",
    )

    return {
        "fully_covered_by_credit": False,
        "client_secret": intent["client_secret"],
        "payment_intent_id": intent["id"],
        "amount_due": invoice.amount_due,
    }


def _get_or_create_stripe_customer(agency):
    """Récupère (ou crée) le Customer Stripe associé à l'agence."""
    stripe_client = _stripe_client()
    existing = frappe.db.get_value("AgencyProfile", agency, "stripe_customer_id")
    if existing:
        return existing

    agency_doc = frappe.get_doc("AgencyProfile", agency)
    customer = stripe_client.Customer.create(
        name=agency_doc.agency_name,
        email=agency_doc.email,
        metadata={"agency_profile": agency},
    )
    frappe.db.set_value("AgencyProfile", agency, "stripe_customer_id", customer["id"])
    return customer["id"]


# ---------------------------------------------------------------------------
# 5. get_payment_methods
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_payment_methods():
    """Liste des moyens de paiement enregistrés par l'agence (2.5)."""
    agency = _get_active_agency()
    methods = frappe.get_all(
        "PaymentMethod",
        filters={"agency": agency},
        fields=["name", "method_type", "label", "is_default", "auto_debit_enabled"],
        order_by="is_default desc, creation desc",
    )
    return {"payment_methods": methods}


# ---------------------------------------------------------------------------
# 6. add_payment_method
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def add_payment_method(stripe_payment_method_id, label=None, set_as_default=0, method_type="Card"):
    """
    Enregistre un nouveau moyen de paiement (2.5 - "L'agence enregistre une
    fois ses informations de paiement"). `stripe_payment_method_id` doit être
    un identifiant Stripe déjà tokenisé côté client (Stripe.js), jamais une
    donnée de carte brute.
    """
    agency = _get_active_agency()
    stripe_client = _stripe_client()
    customer_id = _get_or_create_stripe_customer(agency)

    # Attache le moyen de paiement au customer Stripe de l'agence.
    stripe_client.PaymentMethod.attach(stripe_payment_method_id, customer=customer_id)

    set_as_default = int(set_as_default)
    if set_as_default:
        frappe.db.set_value(
            "PaymentMethod", {"agency": agency, "is_default": 1}, "is_default", 0,
        )

    doc = frappe.get_doc({
        "doctype": "PaymentMethod",
        "agency": agency,
        "method_type": method_type,
        "provider_token": stripe_payment_method_id,
        "label": label,
        "is_default": 1 if set_as_default else 0,
        "auto_debit_enabled": 0,
    }).insert(ignore_permissions=True)

    return {"payment_method": doc.name}


# ---------------------------------------------------------------------------
# 7. delete_payment_method
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST", "DELETE"])
def delete_payment_method(payment_method_name):
    """Supprime un moyen de paiement enregistré (2.5)."""
    agency = _get_active_agency()
    _check_agency_owns("PaymentMethod", payment_method_name, agency)

    method_doc = frappe.get_doc("PaymentMethod", payment_method_name)

    stripe_client = _stripe_client()
    try:
        stripe_client.PaymentMethod.detach(method_doc.provider_token)
    except Exception:
        # Le moyen de paiement peut déjà avoir été détaché côté Stripe.
        frappe.log_error(title="Stripe detach payment method failed")

    was_default = method_doc.is_default
    method_doc.delete(ignore_permissions=True)

    if was_default:
        # Promeut un autre moyen de paiement en défaut, s'il en reste un.
        remaining = frappe.get_all(
            "PaymentMethod", filters={"agency": agency}, order_by="creation desc", limit=1,
        )
        if remaining:
            frappe.db.set_value("PaymentMethod", remaining[0].name, "is_default", 1)

    return {"deleted": True}


# ---------------------------------------------------------------------------
# 8. set_default_payment_method
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def set_default_payment_method(payment_method_name):
    """Définit un moyen de paiement comme moyen par défaut (2.5)."""
    agency = _get_active_agency()
    _check_agency_owns("PaymentMethod", payment_method_name, agency)

    frappe.db.set_value(
        "PaymentMethod", {"agency": agency, "is_default": 1}, "is_default", 0,
    )
    frappe.db.set_value("PaymentMethod", payment_method_name, "is_default", 1)

    return {"default_payment_method": payment_method_name}


# ---------------------------------------------------------------------------
# 9. enable_auto_debit
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def enable_auto_debit(payment_method_name, enable=1):
    """
    Active/désactive le prélèvement automatique sur un moyen de paiement
    (2.5.1 étape 4 - "un prélèvement automatique peut être proposé
    (notification préalable de 24h, possibilité de désactiver l'auto-
    prélèvement)").
    """
    agency = _get_active_agency()
    _check_agency_owns("PaymentMethod", payment_method_name, agency)

    enable = int(enable)
    frappe.db.set_value("PaymentMethod", payment_method_name, "auto_debit_enabled", enable)

    if enable:
        settings = frappe.get_single("PlatformSettings")
        method = frappe.get_doc("PaymentMethod", payment_method_name)
        agency_doc = frappe.get_doc("AgencyProfile", agency)
        _notify(
            user=agency_doc.email,
            notif_type="System",
            title=_("Auto-prélèvement activé"),
            message=_(
                "Le prélèvement automatique est activé sur le moyen de paiement {0}. "
                "Un préavis de {1}h sera envoyé avant chaque prélèvement."
            ).format(method.label or method.name, settings.auto_debit_notice_hours),
            reference_doctype="PaymentMethod",
            reference_name=payment_method_name,
            agency_context=agency,
        )

    return {"payment_method": payment_method_name, "auto_debit_enabled": bool(enable)}


# ---------------------------------------------------------------------------
# 10. get_commission_credits
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_commission_credits():
    """
    Liste des crédits de commission de l'agence (issus de litiges d'inactivité
    client validés) avec le solde disponible.
    """
    agency = _get_active_agency()

    credits = frappe.get_all(
        "CommissionCredit",
        filters={"agency": agency},
        fields=["name", "source_dispute", "source_project", "amount", "consumed_amount", "balance"],
        order_by="creation desc",
    )

    return {
        "credits": credits,
        "available_balance": _get_available_credit_balance(agency),
    }


# ---------------------------------------------------------------------------
# 11. report_inactivity_dispute
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def report_inactivity_dispute(project_name, message):
    """
    Signale un litige d'inactivité client sur un projet en cours (l'agence
    estime que le client ne répond plus / ne collabore plus normalement).
    Crée un InactivityDispute + une ModerationTask associée (circuit de
    validation humaine, cf. 1.3.1 - même logique que Suspendu/Rejeté).
    """
    agency = _get_active_agency()

    # Le litige n'a de sens que si l'agence a bien une Opportunity "Gagnée"
    # (donc une collaboration active) sur ce projet.
    if not frappe.db.exists("Opportunity", {"project": project_name, "agency": agency, "status": "Gagnée"}):
        frappe.throw(_("Vous n'avez pas de collaboration active sur ce projet."), frappe.PermissionError)

    project = frappe.db.get_value(
        "Project", project_name, ["status", "client"], as_dict=True,
    )
    if not project:
        frappe.throw(_("Projet introuvable."), frappe.DoesNotExistError)
    if project.status != "In Progress":
        frappe.throw(_("Un litige d'inactivité ne peut être signalé que sur un projet En cours."))

    dispute = frappe.get_doc({
        "doctype": "InactivityDispute",
        "project": project_name,
        "agency": agency,
        "message": message,
        "status": "Submitted",
    }).insert(ignore_permissions=True)

    frappe.get_doc({
        "doctype": "ModerationTask",
        "task_type": "Litige Client Inactif",
        "reference_doctype": "InactivityDispute",
        "reference_name": dispute.name,
        "status": "Open",
    }).insert(ignore_permissions=True)

    moderators = frappe.get_all("Has Role", filters={"role": "Moderator"}, fields=["parent"])
    for mod in moderators:
        _notify(
            user=mod.parent,
            notif_type="Alert",
            title=_("Nouveau litige d'inactivité client"),
            message=_("Une agence a signalé un client inactif sur le projet {0}.").format(project_name),
            reference_doctype="InactivityDispute",
            reference_name=dispute.name,
            action_required=True,
        )

    return {"dispute": dispute.name, "status": dispute.status}


# ---------------------------------------------------------------------------
# 12. stripe_webhook
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def stripe_webhook():
    """
    Webhook Stripe (allow_guest=True). Traite notamment :
    - payment_intent.succeeded -> crée un Payment "Completed" et passe la
      facture en "Paid" (payment_date = aujourd'hui).
    - payment_intent.payment_failed -> crée un Payment "Failed", la facture
      reste Pending/Overdue.

    Vérifie la signature Stripe (en-tête Stripe-Signature) avant tout
    traitement pour garantir l'authenticité de l'appel.
    """
    payload = frappe.request.data
    sig_header = frappe.get_request_header("Stripe-Signature")
    webhook_secret = frappe.conf.get("stripe_webhook_secret")

    if not webhook_secret:
        frappe.throw(_("Webhook secret Stripe non configuré."), frappe.ValidationError)

    stripe_client = _stripe_client()
    try:
        event = stripe_client.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, Exception) as e:  # signature invalide ou payload corrompu
        frappe.local.response.http_status_code = 400
        return {"error": "invalid_signature_or_payload", "detail": str(e)}

    event_type = event["type"]
    data_object = event["data"]["object"]

    if event_type == "payment_intent.succeeded":
        _handle_payment_success(data_object)
    elif event_type == "payment_intent.payment_failed":
        _handle_payment_failure(data_object)
    else:
        frappe.logger("stripe_webhook").info(f"Unhandled Stripe event type: {event_type}")

    frappe.local.response.http_status_code = 200
    return {"received": True}


def _handle_payment_success(payment_intent):
    invoice_name = payment_intent.get("metadata", {}).get("invoice")
    if not invoice_name or not frappe.db.exists("Invoice", invoice_name):
        frappe.log_error(
            title="Stripe webhook: facture introuvable",
            message=json.dumps(payment_intent),
        )
        return

    invoice = frappe.get_doc("Invoice", invoice_name)

    frappe.get_doc({
        "doctype": "Payment",
        "invoice": invoice.name,
        "agency": invoice.agency,
        "amount": flt(payment_intent.get("amount", 0)) / 100,
        "payment_method": "Card",
        "payment_date": nowdate(),
        "transaction_id": payment_intent.get("id"),
        "status": "Completed",
    }).insert(ignore_permissions=True)

    invoice.status = "Paid"
    invoice.payment_date = nowdate()
    invoice.save(ignore_permissions=True)

    agency_email = frappe.db.get_value("AgencyProfile", invoice.agency, "email")
    if agency_email:
        _notify(
            user=agency_email,
            notif_type="System",
            title=_("Paiement reçu"),
            message=_("Le paiement de la facture {0} a été confirmé.").format(invoice.invoice_number or invoice.name),
            reference_doctype="Invoice",
            reference_name=invoice.name,
            agency_context=invoice.agency,
        )


def _handle_payment_failure(payment_intent):
    invoice_name = payment_intent.get("metadata", {}).get("invoice")
    if not invoice_name or not frappe.db.exists("Invoice", invoice_name):
        frappe.log_error(
            title="Stripe webhook: facture introuvable (échec paiement)",
            message=json.dumps(payment_intent),
        )
        return

    invoice = frappe.get_doc("Invoice", invoice_name)

    frappe.get_doc({
        "doctype": "Payment",
        "invoice": invoice.name,
        "agency": invoice.agency,
        "amount": flt(payment_intent.get("amount", 0)) / 100,
        "payment_method": "Card",
        "payment_date": nowdate(),
        "transaction_id": payment_intent.get("id"),
        "status": "Failed",
    }).insert(ignore_permissions=True)

    agency_email = frappe.db.get_value("AgencyProfile", invoice.agency, "email")
    if agency_email:
        _notify(
            user=agency_email,
            notif_type="Alert",
            title=_("Échec du paiement"),
            message=_("Le paiement de la facture {0} a échoué. Veuillez vérifier votre moyen de paiement.").format(
                invoice.invoice_number or invoice.name
            ),
            reference_doctype="Invoice",
            reference_name=invoice.name,
            agency_context=invoice.agency,
            action_required=True,
        )