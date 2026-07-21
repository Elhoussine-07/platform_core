import frappe
from frappe.model.document import Document
from frappe.utils import add_days, nowdate


class Invoice(Document):
    """Logique cœur Frappe : calculs financiers déterministes et cycle de facturation
    (CDC 2.5.1). Purement transactionnel — aucune dépendance microservice.
    """

    def validate(self):
        # Calcul des montants
        self.total = (self.amount or 0) + (self.tax or 0)
        self.commission_amount = (self.amount or 0) * ((self.commission_rate or 0) / 100)
        self.amount_due = self.total - (self.credit_applied or 0)

        # Vérifications
        if self.amount_due < 0:
            frappe.throw("Le montant net dû ne peut pas être négatif.")

        if self.commission_rate and self.commission_rate < 0:
            frappe.throw("Le taux de commission ne peut pas être négatif.")

        if self.amount and self.amount <= 0:
            frappe.throw("Le montant de la facture doit être supérieur à 0.")

    def before_insert(self):
        self.invoice_number = self._generate_invoice_number()
        self.issue_date = nowdate()

        invoice_due_days = frappe.db.get_single_value("PlatformSettings", "invoice_due_days") or 7
        self.due_date = add_days(self.issue_date, invoice_due_days)

        # Si le statut n'est pas défini, le mettre en "Pending"
        if not self.status:
            self.status = "Pending"

    def _generate_invoice_number(self):
        """Génère un numéro de facture unique au format INV-YYYY-XXXXX"""
        year = nowdate()[:4]
        count = frappe.db.count("Invoice", filters={"invoice_number": ["like", f"INV-{year}-%"]})
        return f"INV-{year}-{count + 1:05d}"

    def on_update(self):
        """Gère les changements de statut de la facture"""
        # Récupérer l'ancien statut
        old_status = frappe.db.get_value(self.doctype, self.name, "status") if not self.is_new() else None

        # Si le statut passe à "Paid"
        if self.status == "Paid" and old_status != "Paid":
            self._handle_payment()

        # Si le statut passe à "Overdue"
        elif self.status == "Overdue" and old_status != "Overdue":
            self._handle_overdue()

        # Si le statut passe à "Cancelled"
        elif self.status == "Cancelled" and old_status != "Cancelled":
            self._handle_cancellation()

    def _handle_payment(self):
        """Gère le passage au statut 'Paid'"""
        # Définir la date de paiement
        frappe.db.set_value(self.doctype, self.name, "payment_date", nowdate())

        # Mettre à jour le paiement associé
        self._update_associated_payment()

        # Mettre à jour le statut de l'offre/projet si nécessaire
        self._update_related_documents()

    def _handle_overdue(self):
        """Gère le passage au statut 'Overdue' (facture en retard)"""
        # Envoyer une notification à l'agence
        self._notify_agency(
            title="Facture en retard",
            message=f"La facture {self.invoice_number} est en retard de paiement."
        )

    def _handle_cancellation(self):
        """Gère le passage au statut 'Cancelled'"""
        # Rétablir le statut de l'opportunité si nécessaire
        # (à implémenter selon la logique métier)

    def _update_associated_payment(self):
        """Met à jour le statut du paiement associé"""
        payment = frappe.get_all("Payment", filters={"invoice": self.name}, limit=1)
        if payment:
            frappe.db.set_value("Payment", payment[0].name, "status", "Completed")
            frappe.db.set_value("Payment", payment[0].name, "payment_date", nowdate())

    def _update_related_documents(self):
        """Met à jour les documents liés (projet, offre) lors du paiement"""
        # Récupérer le projet associé via l'offre
        if self.proposal:
            proposal = frappe.get_doc("Proposal", self.proposal)
            if proposal:
                # Mettre à jour le statut du projet si nécessaire
                frappe.db.set_value("Project", proposal.project, "payment_status", "Paid")

    def _notify_agency(self, title, message):
        """Notifie l'agence propriétaire de la facture"""
        agency_user = frappe.db.get_value("AgencyProfile", self.agency, "user")
        if not agency_user:
            return

        frappe.get_doc(
            {
                "doctype": "Notification",
                "user": agency_user,
                "type": "Invoice",
                "title": title,
                "message": message,
                "reference_doctype": "Invoice",
                "reference_name": self.name,
            }
        ).insert(ignore_permissions=True)
