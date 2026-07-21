import frappe
from frappe.model.document import Document
from frappe.utils import nowdate


class Payment(Document):
    """Logique cœur Frappe : validation et rattachement facture/paiement
    (CDC 2.5.1). Transactionnel pur, aucune dépendance microservice.
    """

    def validate(self):
        # 1. Vérifier le montant
        if not self.amount or self.amount <= 0:
            frappe.throw("Le montant du paiement doit être supérieur à 0.")

        # 2. Vérifier que la facture existe
        if not self.invoice:
            frappe.throw("Une facture doit être associée au paiement.")

        # 3. Vérifier le statut de la facture
        invoice_status = frappe.db.get_value("Invoice", self.invoice, "status")
        if invoice_status != "Pending":
            frappe.throw("La facture associée doit être au statut Pending.")

        # 4. Vérifier que le montant payé correspond au montant dû
        invoice_due = frappe.db.get_value("Invoice", self.invoice, "amount_due") or 0
        if self.amount > invoice_due:
            frappe.throw(
                f"Le montant payé ({self.amount}) dépasse le montant dû ({invoice_due})."
            )

        # 5. Vérifier que l'agence du paiement correspond à celle de la facture
        invoice_agency = frappe.db.get_value("Invoice", self.invoice, "agency")
        if self.agency and self.agency != invoice_agency:
            frappe.throw(
                "L'agence du paiement ne correspond pas à l'agence de la facture."
            )

    def after_insert(self):
        """Met à jour le statut de la facture après insertion du paiement"""
        # Récupérer le montant dû restant
        invoice_due = frappe.db.get_value("Invoice", self.invoice, "amount_due") or 0
        remaining = invoice_due - self.amount

        if remaining <= 0:
            # Paiement complet
            frappe.db.set_value("Invoice", self.invoice, "status", "Paid")
            frappe.db.set_value("Invoice", self.invoice, "payment_date", nowdate())
            frappe.db.set_value("Invoice", self.invoice, "amount_due", 0)

            # Mettre à jour le statut de l'opportunité/projet associé
            self._update_related_documents()
        else:
            # Paiement partiel : mettre à jour le montant dû restant
            frappe.db.set_value("Invoice", self.invoice, "amount_due", remaining)
            # Le statut reste "Pending" tant que le paiement n'est pas complet

        # Mettre à jour le paiement avec la date
        frappe.db.set_value(self.doctype, self.name, "payment_date", nowdate())

    def _update_related_documents(self):
        """Met à jour les documents liés (projet, offre) lors du paiement complet"""
        # Récupérer le projet via la facture
        project = frappe.db.get_value("Invoice", self.invoice, "project")
        if project:
            frappe.db.set_value("Project", project, "payment_status", "Paid")
