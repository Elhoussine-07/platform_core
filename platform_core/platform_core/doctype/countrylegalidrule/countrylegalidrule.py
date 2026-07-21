import re

import frappe
from frappe.model.document import Document


class CountryLegalIDRule(Document):
    """Logique cœur Frappe : intégrité de la règle de validation d'identifiant
    légal par pays (CDC 1.1). Utilisée en lecture par ClientProfile/AgencyProfile
    pour valider legal_id ; la vérification externe (registry_api_url) reste un
    appel qui devra être fait par un microservice dédié, pas dans ce hook.
    """

    def validate(self):
        # La présence de country et id_label est déjà gérée par Mandatory dans le DocType
        self._validate_regex_syntax()
        self._validate_registry_check_config()

    def _validate_regex_syntax(self):
        """Vérifie que la regex de validation est syntaxiquement valide"""
        if not self.validation_regex:
            return

        try:
            re.compile(self.validation_regex)
        except re.error:
            frappe.throw(f"La regex de validation « {self.validation_regex} » n'est pas syntaxiquement valide.")

    def _validate_registry_check_config(self):
        """Si la vérification externe est activée, l'URL de l'API du registre
        doit être renseignée — sans quoi le contrôle croisé (CDC 1.1) est
        impossible à exécuter côté microservice de vérification."""
        if not self.registry_check_enabled:
            return

        if not self.registry_api_url:
            frappe.throw("registry_api_url est obligatoire lorsque registry_check_enabled est activé.")

        # Vérifier que l'URL est valide (format simple)
        if not self.registry_api_url.startswith(("http://", "https://")):
            frappe.throw("registry_api_url doit être une URL valide (commençant par http:// ou https://).")

    def before_insert(self):
        """Avant l'insertion, s'assurer que la règle est active par défaut"""
        if self.is_active is None:
            self.is_active = 1
