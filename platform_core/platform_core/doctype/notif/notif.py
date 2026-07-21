import frappe
from frappe.model.document import Document
from frappe.utils import now


class Notification(Document):
    """Rôle Frappe : enregistrement et cycle de vie basique (créée -> lue -> archivée,
    CDC module 7). L'envoi multicanal (email, push) et toute logique d'escalade
    restent au futur microservice notifications.
    """

    def before_insert(self):
        # Définir la date de création
        if not self.created_date:
            self.created_date = now()

        # Une notification non lue n'est pas archivée
        if not self.is_read:
            self.archived_date = None

        # Valider les données
        self._validate_data()

    def _validate_data(self):
        """Valide les champs obligatoires"""
        # Vérifier que l'utilisateur existe
        if not self.user:
            frappe.throw("L'utilisateur destinataire est obligatoire.")

        if not frappe.db.exists("User", self.user):
            frappe.throw(f"L'utilisateur {self.user} n'existe pas.")

        # Vérifier que le type est valide
        valid_types = ["Project", "Proposal", "Message", "Review", "System", "Alert"]
        if self.type and self.type not in valid_types:
            frappe.throw(f"Type de notification invalide: {self.type}")

        # Vérifier que le titre et le message sont présents
        if not self.title:
            frappe.throw("Le titre de la notification est obligatoire.")

        if not self.message:
            frappe.throw("Le message de la notification est obligatoire.")

        # Vérifier que le docType de référence existe si renseigné
        if self.reference_doctype and self.reference_name:
            if not frappe.db.exists(self.reference_doctype, self.reference_name):
                frappe.throw(
                    f"Le document de référence {self.reference_doctype}/{self.reference_name} n'existe pas."
                )

    def on_update(self):
        """Gère les changements de statut de la notification"""
        # Récupérer l'ancien statut
        old_is_read = frappe.db.get_value(self.doctype, self.name, "is_read") if not self.is_new() else 0

        # Si la notification passe de non-lue à lue et qu'elle n'est pas archivée
        if self.is_read and not old_is_read and not self.archived_date:
            frappe.db.set_value(self.doctype, self.name, "archived_date", now())

        # Si la notification est marquée comme non-lue, retirer la date d'archivage
        elif not self.is_read and self.archived_date:
            frappe.db.set_value(self.doctype, self.name, "archived_date", None)
