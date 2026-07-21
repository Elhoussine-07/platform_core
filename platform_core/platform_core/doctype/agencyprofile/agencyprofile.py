import frappe
from frappe.model.document import Document


class AgencyProfile(Document):
    """Logique cœur Frappe : onboarding (unicité du nom), PQI qualitatif déterministe
    (CDC 2.4) et complétion de profil. Le score de pertinence/matching (Opportunity)
    et les recommandations IA (2.4 "Recommandations IA") restent hors Frappe — le
    microservice correspondant écrit ses résultats directement via l'API REST.
    """

    def before_insert(self):
        user_roles = frappe.get_roles(frappe.session.user)
        if "Agency" not in user_roles and "Administrator" not in user_roles:
            frappe.throw("Seuls les Agences et Administrateurs peuvent créer un profil Agence.")

        if frappe.db.exists("AgencyProfile", {"agency_name": self.agency_name}):
            frappe.throw(f"Une agence nommée « {self.agency_name} » existe déjà.")

    def validate(self):
        self._calculate_pqi_score()
        self._calculate_profile_completion()

    def _calculate_pqi_score(self):
        """PQI = Transparence(20) + Talent(20) + Équipe(20) + Portfolio(20) + Confiance(20)
        (CDC 2.4.1). Règle déterministe sur champs existants — pas d'appel IA ici.
        """
        transparency = self._score_transparency()
        talent = self._score_talent()
        team = self._score_team()
        portfolio = self._score_portfolio()
        trust = self._score_trust()

        self.pqi_score = transparency + talent + team + portfolio + trust

    def _score_transparency(self):
        """Score de transparence (max 20)"""
        score = 0
        if self.description:
            score += 5
        if self.website:
            score += 5
        if self.social_links:
            score += 5
        if self.coverage and self.location:
            score += 5
        return score

    def _score_talent(self):
        """Score de talent basé sur les services et certifications (max 20)"""
        score = 0

        # Services (max 10)
        if self.services:
            count = len(self.services)
            if count >= 3:
                score += 10
            elif count >= 1:
                score += 5

        # Certifications (max 10)
        if self.certifications:
            count = len(self.certifications)
            if count >= 3:
                score += 10
            elif count >= 1:
                score += 5

        return min(score, 20)

    def _score_team(self):
        """Score de l'équipe basé sur le nombre de membres (max 20)"""
        if not self.team_size:
            return 0
        if self.team_size >= 10:
            return 20
        if self.team_size >= 5:
            return 12
        return 6

    def _score_portfolio(self):
        """Score du portfolio basé sur le nombre de réalisations (max 20)"""
        if not self.portfolio:
            return 0

        count = len(self.portfolio)
        if count >= 5:
            return 20
        if count >= 3:
            return 15
        if count >= 1:
            return 10
        return 0

    def _score_trust(self):
        """Score de confiance (max 20)"""
        score = 0
        if self.legal_id_verified:
            score += 10
        if self.email_verified:
            score += 5
        if (self.rating or 0) >= 4:
            score += 5
        return score

    def _calculate_profile_completion(self):
        """Calcule le taux de complétion du profil (0-100)"""
        weights = {
            "agency_name": 10,
            "description": 10,
            "logo": 10,
            "website": 5,
            "location": 10,
            "country": 10,
            "legal_id": 15,
            "email": 10,
            "phone": 10,
            "social_links": 10,
        }
        self.profile_completion = min(
            sum(weight for field, weight in weights.items() if getattr(self, field, None)),
            100
        )
