"""Wise : assistant personnel en terminal, propulsé par l'API Claude.

La personnalité vient de personnalite.md (dans le même dossier).
Lancer : python wise.py   (la clé ANTHROPIC_API_KEY doit être définie)
"""

import datetime
import json
import os
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:  # pragma: no cover - message pour l'utilisateur
    print("Le module « anthropic » n'est pas installé. Lancez : pip install -r requirements.txt")
    sys.exit(1)

DOSSIER = Path(__file__).resolve().parent
FICHIER_PERSONNALITE = DOSSIER / "personnalite.md"
FICHIER_MEMOIRE = Path(os.environ.get("WISE_MEMOIRE", Path.home() / ".wise" / "memoire.json"))
FICHIER_AJUSTEMENTS = Path(os.environ.get("WISE_AJUSTEMENTS",
                                          Path.home() / ".wise" / "ajustements.json"))

MODELE = os.environ.get("WISE_MODELE", "claude-opus-5-5")
EFFORT = os.environ.get("WISE_EFFORT", "medium")
MAX_TOKENS = 64000
MAX_REPRISES = 5  # garde-fou pour les tours mis en pause par la recherche web

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

AIDE = """Commandes :
  /aide         affiche cette aide
  /memoire      montre ce que Wise a retenu sur vous
  /oublier      efface toute la mémoire de Wise
  /ajustements  montre comment vous avez ajusté sa personnalité
  /retablir     annule tous les ajustements de personnalité
  /nouveau      commence une nouvelle conversation
  /quitter      quitte Wise (ou Ctrl+C)"""

OUTILS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {
        "name": "date_heure",
        "description": "Donne la date et l'heure locales exactes de l'ordinateur de l'utilisateur.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "eager_input_streaming": True,
    },
    {
        "name": "memoriser",
        "description": (
            "Enregistre durablement une information utile et stable sur l'utilisateur "
            "(prénom, métier, projets, préférences, contraintes). Une information par appel, "
            "formulée en une phrase courte à la troisième personne."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "information": {"type": "string", "description": "L'information à retenir."}
            },
            "required": ["information"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
    {
        "name": "ajuster_personnalite",
        "description": (
            "Modifie durablement ta propre façon d'être quand l'utilisateur te le demande "
            "(ton, longueur des réponses, tutoiement, humour, centres d'intérêt…). La consigne "
            "s'applique dès maintenant et dans toutes les conversations suivantes. Si elle "
            "contredit ou précise un ajustement existant, indique celui-ci dans « remplace »."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "consigne": {
                    "type": "string",
                    "description": "La nouvelle consigne, à la deuxième personne (ex. « Tu tutoies l'utilisateur. »).",
                },
                "remplace": {
                    "type": "string",
                    "description": "Le texte exact d'un ajustement existant à remplacer, s'il y en a un.",
                },
            },
            "required": ["consigne"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
]


# --- Mémoire et ajustements (listes de phrases dans des fichiers JSON) ------

def charger_liste(fichier):
    try:
        donnees = json.loads(Path(fichier).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [str(x) for x in donnees] if isinstance(donnees, list) else []


def sauver_liste(elements, fichier):
    fichier = Path(fichier)
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")


def charger_memoire(fichier=None):
    return charger_liste(fichier or FICHIER_MEMOIRE)


def sauver_memoire(souvenirs, fichier=None):
    sauver_liste(souvenirs, fichier or FICHIER_MEMOIRE)


def charger_ajustements(fichier=None):
    return charger_liste(fichier or FICHIER_AJUSTEMENTS)


def sauver_ajustements(ajustements, fichier=None):
    sauver_liste(ajustements, fichier or FICHIER_AJUSTEMENTS)


# --- Outils exécutés sur l'ordinateur -------------------------------------

def outil_date_heure(maintenant=None):
    m = maintenant or datetime.datetime.now().astimezone()
    return (f"Nous sommes le {JOURS[m.weekday()]} {m.day} {MOIS[m.month - 1]} {m.year}, "
            f"il est {m:%H:%M} (fuseau {m.tzname() or 'local'}).")


def outil_memoriser(entree, fichier=None):
    info = entree.get("information") if isinstance(entree, dict) else None
    if not isinstance(info, str) or not info.strip():
        raise ValueError("le champ « information » doit être un texte non vide")
    info = info.strip()
    souvenirs = charger_memoire(fichier)
    if info not in souvenirs:
        souvenirs.append(info)
        sauver_memoire(souvenirs, fichier)
    return "Information enregistrée."


def outil_ajuster_personnalite(entree, fichier=None):
    consigne = entree.get("consigne") if isinstance(entree, dict) else None
    if not isinstance(consigne, str) or not consigne.strip():
        raise ValueError("le champ « consigne » doit être un texte non vide")
    consigne = consigne.strip()
    remplace = entree.get("remplace")
    ajustements = charger_ajustements(fichier)
    if isinstance(remplace, str) and remplace.strip():
        if remplace.strip() not in ajustements:
            raise ValueError("l'ajustement à remplacer n'existe pas ; recopie-le exactement")
        ajustements.remove(remplace.strip())
    if consigne not in ajustements:
        ajustements.append(consigne)
    sauver_ajustements(ajustements, fichier)
    return "Ajustement enregistré. Applique-le dès ta prochaine phrase."


def executer_outil(bloc, fichier_memoire=None, fichier_ajustements=None):
    """Exécute un appel d'outil et renvoie le bloc tool_result correspondant."""
    try:
        if bloc.name == "date_heure":
            contenu = outil_date_heure()
        elif bloc.name == "memoriser":
            contenu = outil_memoriser(bloc.input, fichier_memoire)
            print("  (Wise a mémorisé une information)")
        elif bloc.name == "ajuster_personnalite":
            contenu = outil_ajuster_personnalite(bloc.input, fichier_ajustements)
            print(f"  (Wise a ajusté sa personnalité : {bloc.input['consigne'].strip()})")
        else:
            raise ValueError(f"outil inconnu : {bloc.name}")
        return {"type": "tool_result", "tool_use_id": bloc.id, "content": contenu}
    except Exception as erreur:  # l'erreur est renvoyée à Claude, qui peut réessayer
        return {"type": "tool_result", "tool_use_id": bloc.id,
                "content": f"Erreur : {erreur}", "is_error": True}


# --- Conversation ----------------------------------------------------------

def construire_systeme(souvenirs, ajustements=()):
    systeme = FICHIER_PERSONNALITE.read_text(encoding="utf-8").strip()
    if ajustements:
        systeme += ("\n\n## Ajustements demandés par l'utilisateur\n\n"
                    "Ils priment sur le reste de ta personnalité en cas de conflit.\n\n")
        systeme += "\n".join(f"- {a}" for a in ajustements)
    if souvenirs:
        systeme += "\n\n## Ce que tu sais déjà sur l'utilisateur\n\n"
        systeme += "\n".join(f"- {s}" for s in souvenirs)
    return systeme


def contenu_a_renvoyer(contenu):
    """Prépare le contenu de la réponse pour l'historique.

    Si un modèle de secours a pris le relais en cours de réponse (bloc « fallback »),
    les blocs internes produits avant ce relais ne doivent pas être renvoyés.
    """
    blocs = [b.model_dump(exclude_none=True) for b in contenu]
    dernier_relais = max((i for i, b in enumerate(blocs) if b["type"] == "fallback"), default=-1)
    if dernier_relais < 0:
        return blocs
    resultats = {b.get("tool_use_id") for b in blocs if b["type"].endswith("_tool_result")}
    gardes = []
    for i, b in enumerate(blocs):
        if i < dernier_relais:
            if b["type"] in ("thinking", "redacted_thinking", "tool_use"):
                continue
            if b["type"] == "server_tool_use" and b.get("id") not in resultats:
                continue
            if b["type"] not in ("text", "server_tool_use") and not b["type"].endswith("_tool_result"):
                continue
        gardes.append(b)
    return gardes


class Wise:
    def __init__(self, client, fichier_memoire=None, fichier_ajustements=None):
        self.client = client
        self.fichier_memoire = fichier_memoire
        self.fichier_ajustements = fichier_ajustements
        self.nouvelle_conversation()

    def nouvelle_conversation(self):
        self.messages = []
        self.systeme = construire_systeme(charger_memoire(self.fichier_memoire),
                                          charger_ajustements(self.fichier_ajustements))

    def _appel(self):
        """Un appel à l'API, en flux : le texte s'affiche au fur et à mesure."""
        with self.client.beta.messages.stream(
            model=MODELE,
            max_tokens=MAX_TOKENS,
            system=self.systeme,
            messages=self.messages,
            tools=OUTILS,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as flux:
            for evenement in flux:
                if evenement.type == "text":
                    print(evenement.text, end="", flush=True)
                elif (evenement.type == "content_block_start"
                      and evenement.content_block.type == "server_tool_use"):
                    print("\n  (recherche sur le web…)\n", flush=True)
            return flux.get_final_message()

    def repondre(self, texte):
        """Envoie un message de l'utilisateur et affiche la réponse complète de Wise."""
        debut = len(self.messages)
        self.messages.append({"role": "user", "content": texte})
        reprises = 0
        try:
            while True:
                reponse = self._appel()
                if reponse.stop_reason == "refusal":
                    del self.messages[debut:]
                    print("\n(Wise ne peut pas répondre à cette demande.)")
                    return
                self.messages.append({"role": "assistant",
                                      "content": contenu_a_renvoyer(reponse.content)})
                if reponse.stop_reason == "tool_use":
                    appels = [b for b in reponse.content if b.type == "tool_use"]
                    self.messages.append({"role": "user", "content": [
                        executer_outil(b, self.fichier_memoire, self.fichier_ajustements)
                        for b in appels]})
                    continue
                if reponse.stop_reason == "pause_turn" and reprises < MAX_REPRISES:
                    reprises += 1
                    continue
                if reponse.stop_reason == "max_tokens":
                    # Un appel d'outil coupé resterait sans résultat : on le retire.
                    dernier = self.messages[-1]
                    dernier["content"] = [b for b in dernier["content"] if b["type"] != "tool_use"]
                    if not dernier["content"]:
                        del self.messages[debut:]
                    print("\n(Réponse coupée : elle était trop longue.)")
                print()
                return
        except BaseException:
            # On retire le tour inachevé pour garder un historique valide.
            del self.messages[debut:]
            raise


def configurer_console():
    for flux in (sys.stdout, sys.stderr, sys.stdin):
        try:
            flux.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        import readline  # noqa: F401  (flèches et historique de saisie, hors Windows)
    except ImportError:
        pass


def main():
    configurer_console()
    if not FICHIER_PERSONNALITE.exists():
        print(f"Fichier de personnalité introuvable : {FICHIER_PERSONNALITE}")
        return 1
    try:
        client = anthropic.Anthropic()
    except Exception as erreur:
        print(f"Impossible d'initialiser le client Claude : {erreur}")
        print("Définissez la variable ANTHROPIC_API_KEY (voir README.md).")
        return 1

    wise = Wise(client)
    print("Wise est prêt. Tapez /aide pour les commandes, /quitter pour sortir.\n")
    while True:
        try:
            texte = input("Vous > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nÀ bientôt !")
            return 0
        if not texte:
            continue
        commande = texte.lower()
        if commande in ("/quitter", "/q", "quitter", "exit"):
            print("À bientôt !")
            return 0
        if commande == "/aide":
            print(AIDE)
            continue
        if commande == "/memoire":
            souvenirs = charger_memoire()
            print("\n".join(f"- {s}" for s in souvenirs) if souvenirs else "Wise n'a encore rien retenu.")
            continue
        if commande == "/oublier":
            sauver_memoire([])
            wise.nouvelle_conversation()
            print("Mémoire effacée. Nouvelle conversation.")
            continue
        if commande == "/ajustements":
            ajustements = charger_ajustements()
            print("\n".join(f"- {a}" for a in ajustements) if ajustements
                  else "Aucun ajustement : Wise suit personnalite.md tel quel.")
            continue
        if commande == "/retablir":
            sauver_ajustements([])
            wise.nouvelle_conversation()
            print("Ajustements annulés. Nouvelle conversation.")
            continue
        if commande == "/nouveau":
            wise.nouvelle_conversation()
            print("Nouvelle conversation.")
            continue

        print("Wise > ", end="", flush=True)
        try:
            wise.repondre(texte)
        except KeyboardInterrupt:
            print("\n(Réponse interrompue.)")
        except anthropic.AuthenticationError:
            print("\nClé API refusée. Vérifiez ANTHROPIC_API_KEY (voir README.md).")
        except TypeError as erreur:
            if "authentication" not in str(erreur):
                raise
            print("\nAucune clé API trouvée. Définissez ANTHROPIC_API_KEY (voir README.md).")
        except anthropic.PermissionDeniedError as erreur:
            print(f"\nAccès refusé par l'API : {erreur.message}")
        except anthropic.RateLimitError:
            print("\nTrop de requêtes ou crédit épuisé. Réessayez dans un moment.")
        except anthropic.APIStatusError as erreur:
            print(f"\nErreur de l'API ({erreur.status_code}) : {erreur.message}")
        except anthropic.APIConnectionError:
            print("\nImpossible de joindre l'API Claude. Vérifiez votre connexion internet.")
        print()


if __name__ == "__main__":
    sys.exit(main())
