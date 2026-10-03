"""Tests de Wise, sans appel réseau : l'API Claude est remplacée par un faux client."""

import contextlib
import datetime
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wise  # noqa: E402
from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock  # noqa: E402


def message(contenu, stop_reason):
    return SimpleNamespace(content=contenu, stop_reason=stop_reason)


class FauxFlux:
    def __init__(self, reponse):
        self.reponse = reponse

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for bloc in self.reponse.content:
            if bloc.type == "text":
                yield SimpleNamespace(type="text", text=bloc.text)

    def get_final_message(self):
        return self.reponse


class FauxClient:
    """Rejoue une liste de réponses et garde une copie de chaque requête."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.requetes = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.requetes.append({**params, "messages": list(params["messages"])})
        return FauxFlux(self.reponses.pop(0))


class TestWise(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.memoire = Path(self.dossier.name) / "memoire.json"

    def tearDown(self):
        self.dossier.cleanup()

    def discuter(self, client, texte):
        assistant = wise.Wise(client, self.memoire)
        with contextlib.redirect_stdout(io.StringIO()) as sortie:
            assistant.repondre(texte)
        return assistant, sortie.getvalue()

    def test_reponse_simple(self):
        client = FauxClient([message([BetaTextBlock(type="text", text="Bonjour !")], "end_turn")])
        assistant, sortie = self.discuter(client, "Salut")
        self.assertIn("Bonjour !", sortie)
        self.assertEqual([m["role"] for m in assistant.messages], ["user", "assistant"])
        requete = client.requetes[0]
        self.assertIn("Tu es Wise", requete["system"])
        self.assertEqual(requete["model"], "claude-opus-5-5")
        self.assertEqual(requete["fallbacks"], "default")

    def test_memoriser_puis_repondre(self):
        appel = BetaToolUseBlock(type="tool_use", id="t1", name="memoriser",
                                 input={"information": "Il s'appelle Femi."})
        client = FauxClient([
            message([appel], "tool_use"),
            message([BetaTextBlock(type="text", text="Enchanté, Femi.")], "end_turn"),
        ])
        assistant, sortie = self.discuter(client, "Je m'appelle Femi")
        self.assertIn("Enchanté", sortie)
        self.assertEqual(wise.charger_memoire(self.memoire), ["Il s'appelle Femi."])
        resultat = client.requetes[1]["messages"][-1]["content"][0]
        self.assertEqual(resultat["tool_use_id"], "t1")
        self.assertNotIn("is_error", resultat)
        # La mémoire est reprise dans la personnalité d'une nouvelle conversation.
        assistant.nouvelle_conversation()
        self.assertIn("Il s'appelle Femi.", assistant.systeme)

    def test_outil_invalide_renvoie_une_erreur(self):
        appel = BetaToolUseBlock(type="tool_use", id="t1", name="memoriser", input={})
        resultat = wise.executer_outil(appel, self.memoire)
        self.assertTrue(resultat["is_error"])
        self.assertEqual(wise.charger_memoire(self.memoire), [])

    def test_refus_retire_le_tour(self):
        client = FauxClient([message([], "refusal")])
        assistant, sortie = self.discuter(client, "…")
        self.assertEqual(assistant.messages, [])
        self.assertIn("ne peut pas répondre", sortie)

    def test_erreur_reseau_retire_le_tour(self):
        class ClientEnPanne(FauxClient):
            def _stream(self, **params):
                raise ConnectionError("hors ligne")

        assistant = wise.Wise(ClientEnPanne([]), self.memoire)
        with self.assertRaises(ConnectionError):
            assistant.repondre("Salut")
        self.assertEqual(assistant.messages, [])

    def test_date_heure_en_francais(self):
        m = datetime.datetime(2026, 10, 3, 9, 5, tzinfo=datetime.timezone.utc)
        self.assertEqual(wise.outil_date_heure(m),
                         "Nous sommes le samedi 3 octobre 2026, il est 09:05 (fuseau UTC).")

    def test_relais_de_secours_retire_les_blocs_internes(self):
        blocs = [
            SimpleNamespace(model_dump=lambda **k: {"type": "thinking", "thinking": ""}),
            SimpleNamespace(model_dump=lambda **k: {"type": "text", "text": "Début"}),
            SimpleNamespace(model_dump=lambda **k: {"type": "fallback"}),
            SimpleNamespace(model_dump=lambda **k: {"type": "text", "text": "Suite"}),
        ]
        types = [b["type"] for b in wise.contenu_a_renvoyer(blocs)]
        self.assertEqual(types, ["text", "fallback", "text"])


if __name__ == "__main__":
    unittest.main()
