# -*- coding: utf-8 -*-
"""Tests du rappel de pause (unittest, bibliothèque standard uniquement).

    python3 -m unittest discover -s rappel-pause/tests -v

Les tests d'intégration lancent réellement la boucle de rappels et, sous Windows et macOS,
peuvent faire sonner l'ordinateur et ouvrir le message : ils ne s'exécutent que si
RAPPEL_PAUSE_TESTS_INTEGRATION=1 (c'est le cas en intégration continue).
"""

from __future__ import annotations

import array
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
import wave
from pathlib import Path
from unittest import mock

DOSSIER_SOURCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DOSSIER_SOURCE))

import rappel_pause as rp  # noqa: E402

INTEGRATION = os.environ.get("RAPPEL_PAUSE_TESTS_INTEGRATION") == "1"
EST_LINUX = sys.platform.startswith("linux")


class TestTemporaire(unittest.TestCase):
    def setUp(self) -> None:
        temporaire = tempfile.TemporaryDirectory()
        self.addCleanup(temporaire.cleanup)
        self.tmp = Path(temporaire.name).resolve()


def environnement_isole(racine: Path, graphique: bool) -> dict:
    """Variables d'environnement redirigeant tous les dossiers utilisateur vers `racine`."""
    env = dict(os.environ)
    env.update(
        HOME=str(racine),
        USERPROFILE=str(racine),
        LOCALAPPDATA=str(racine / "AppData" / "Local"),
        XDG_DATA_HOME=str(racine / "donnees"),
        XDG_CONFIG_HOME=str(racine / "config"),
    )
    for variable in ("DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(variable, None)
    if graphique and EST_LINUX:
        env["DISPLAY"] = ":99"  # seulement pour que l'installation démarre la boucle
    return env


def attendre_texte(fichier: Path, texte: str, delai_s: float = 60.0) -> bool:
    echeance = time.monotonic() + delai_s
    while time.monotonic() < echeance:
        if fichier.exists() and texte in fichier.read_text(encoding="utf-8"):
            return True
        time.sleep(0.2)
    return False


# Clés de registre de test (Windows) : les tests ne touchent jamais aux vraies clés.
CLE_TEST_DEMARRAGE = r"Software\RappelPauseTests\Run"
CLE_TEST_DESINSTALLATION = r"Software\RappelPauseTests\Desinstallation"


def nettoyer_cles_de_test() -> None:
    import winreg

    for cle in (CLE_TEST_DEMARRAGE, CLE_TEST_DESINSTALLATION, r"Software\RappelPauseTests"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, cle)
        except FileNotFoundError:
            pass


def valeur_registre(cle: str, nom: str):
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cle) as ouverte:
            return winreg.QueryValueEx(ouverte, nom)[0]
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------------- configuration


class TestConfiguration(TestTemporaire):
    def test_fichier_absent_valeurs_par_defaut(self) -> None:
        self.assertEqual(rp.charger_config(self.tmp / "absent.json"), rp.config_par_defaut())

    def test_fichier_illisible_valeurs_par_defaut(self) -> None:
        fichier = self.tmp / "config.json"
        fichier.write_text("{pas du JSON", encoding="utf-8")
        with self.assertLogs(rp.journal, "WARNING"):
            self.assertEqual(rp.charger_config(fichier), rp.config_par_defaut())

    def test_valeur_invalide_remplacee_individuellement(self) -> None:
        fichier = self.tmp / "config.json"
        fichier.write_text(json.dumps({"intervalle_minutes": -5, "message": "Bougez"}), encoding="utf-8")
        with self.assertLogs(rp.journal, "WARNING"):
            config = rp.charger_config(fichier)
        self.assertEqual(config, {"intervalle_minutes": rp.INTERVALLE_DEFAUT_MIN, "message": "Bougez"})

    def test_aller_retour_et_bom(self) -> None:
        fichier = self.tmp / "sous-dossier" / "config.json"
        config = {"intervalle_minutes": 90, "message": "Été : levez-vous\u00a0!"}
        rp.enregistrer_config(fichier, config)
        self.assertEqual(rp.charger_config(fichier), config)
        self.assertEqual(list(fichier.parent.iterdir()), [fichier])  # aucun fichier temporaire restant
        fichier.write_bytes(b"\xef\xbb\xbf" + fichier.read_bytes())  # BOM ajouté par le Bloc-notes
        self.assertEqual(rp.charger_config(fichier), config)

    def test_valider_intervalle(self) -> None:
        self.assertEqual(rp.valider_intervalle(120), 120)
        self.assertIsInstance(rp.valider_intervalle(90.0), int)
        self.assertEqual(rp.valider_intervalle("1.5"), 1.5)
        for invalide in (0, -1, float("nan"), float("inf"), True, "abc", None, [2]):
            with self.subTest(invalide=invalide), self.assertRaises(ValueError):
                rp.valider_intervalle(invalide)

    def test_valider_message(self) -> None:
        self.assertEqual(rp.valider_message("  Bougez !  "), "Bougez !")
        for invalide in ("", "   ", None, 3):
            with self.subTest(invalide=invalide), self.assertRaises(ValueError):
                rp.valider_message(invalide)

    def test_formater_minutes(self) -> None:
        attendus = {120: "2 h", 90: "1 h 30", 150.0: "2 h 30", 45: "45 min", 1.5: "1,5 min", 0.25: "0,25 min"}
        for minutes, texte in attendus.items():
            with self.subTest(minutes=minutes):
                self.assertEqual(rp.formater_minutes(minutes), texte)

    def test_message_par_defaut(self) -> None:
        self.assertEqual(rp.MESSAGE_DEFAUT.replace("\u00a0", " "), "C'est l'heure de se lever pour marcher !")
        self.assertEqual(rp.INTERVALLE_DEFAUT_MIN, 120)


# ------------------------------------------------------------------------ ligne de commande


class TestArguments(unittest.TestCase):
    def analyser(self, *argv: str):
        return rp.analyser_arguments(list(argv))

    def refuser(self, *argv: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as contexte:
            self.analyser(*argv)
        self.assertEqual(contexte.exception.code, 2)

    def test_valeurs_acceptees(self) -> None:
        arguments = self.analyser("--installer", "--intervalle", "1,5", "--message", "  Bougez  ")
        self.assertTrue(arguments.installer)
        self.assertEqual(arguments.intervalle, 1.5)
        self.assertEqual(arguments.message, "Bougez")
        self.assertIsNone(self.analyser().intervalle)

    def test_valeurs_refusees(self) -> None:
        self.refuser("--installer", "--desinstaller")
        self.refuser("--test", "--statut")
        self.refuser("--intervalle", "0")
        self.refuser("--intervalle", "deux heures")
        self.refuser("--message", "   ")
        self.refuser("--desinstaller", "--intervalle", "5")
        self.refuser("--statut", "--message", "x")


# ------------------------------------------------------------------------------- attente


class HorlogeSimulee:
    """Horloge murale et sommeil simulés ; `sauts` : n° d'appel à dormir -> saut d'horloge."""

    def __init__(self, sauts: dict | None = None) -> None:
        self.temps = 1_700_000_000.0
        self.dormi = 0.0
        self.appels = 0
        self.sauts = sauts or {}

    def horloge(self) -> float:
        return self.temps

    def dormir(self, secondes: float) -> None:
        self.appels += 1
        self.dormi += secondes
        self.temps += secondes + self.sauts.get(self.appels, 0.0)


class TestAttente(unittest.TestCase):
    def attendre(self, horloge: HorlogeSimulee, duree_s: float = 7200.0) -> None:
        rp.attendre_activite(duree_s, pas_s=30.0, seuil_veille_s=300.0, horloge=horloge.horloge, dormir=horloge.dormir)

    def test_sans_veille(self) -> None:
        horloge = HorlogeSimulee()
        self.attendre(horloge)
        self.assertEqual(horloge.dormi, 7200.0)
        self.assertEqual(horloge.appels, 240)

    def test_dernier_pas_ajuste(self) -> None:
        horloge = HorlogeSimulee()
        self.attendre(horloge, duree_s=45.0)
        self.assertEqual(horloge.dormi, 45.0)

    def test_veille_longue_remet_a_zero(self) -> None:
        horloge = HorlogeSimulee(sauts={10: 3600.0})  # 1 h de veille après 5 min de travail
        with self.assertLogs(rp.journal, "INFO") as journaux:
            self.attendre(horloge)
        self.assertEqual(horloge.dormi, 300.0 + 7200.0)  # 2 h pleines après le réveil
        self.assertIn("Veille détectée (60 min)", journaux.output[0])

    def test_veille_courte_comptee(self) -> None:
        horloge = HorlogeSimulee(sauts={10: 120.0})  # 2 min de veille : sous le seuil
        self.attendre(horloge)
        self.assertEqual(horloge.dormi, 7200.0 - 120.0)

    def test_horloge_reculee(self) -> None:
        horloge = HorlogeSimulee(sauts={10: -3600.0})
        self.attendre(horloge)
        self.assertEqual(horloge.dormi, 7200.0)


# ------------------------------------------------------------------------------ carillon


class TestCarillon(TestTemporaire):
    def test_wav_valide_sans_clic_ni_saturation(self) -> None:
        chemin = rp.generer_carillon(self.tmp / "carillon.wav")
        with wave.open(str(chemin), "rb") as fichier:
            self.assertEqual((fichier.getnchannels(), fichier.getsampwidth()), (1, 2))
            taux = fichier.getframerate()
            echantillons = array.array("h", fichier.readframes(fichier.getnframes()))
        if sys.byteorder == "big":
            echantillons.byteswap()
        self.assertEqual(taux, 22050)
        self.assertTrue(1.5 <= len(echantillons) / taux <= 2.0)
        crete = max(abs(e) for e in echantillons) / 32767
        self.assertTrue(0.65 <= crete <= 0.71, crete)  # audible, sans saturation
        self.assertEqual(echantillons[0], 0)
        self.assertLess(abs(echantillons[-1]), 0.01 * 32767)

    def test_preparation_sans_fichier_temporaire_restant(self) -> None:
        chemin = rp._preparer_carillon(self.tmp)
        self.assertEqual(chemin, self.tmp / rp.FICHIER_CARILLON)
        self.assertEqual([f.name for f in self.tmp.iterdir()], [rp.FICHIER_CARILLON])


# ------------------------------------------------------------------------ instance unique


SCRIPT_DETENTEUR = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import rappel_pause as rp
verrou = rp.VerrouInstance(Path(sys.argv[2]) / rp.FICHIER_VERROU)
if sys.argv[3:] == ["ignorer-sigterm"]:  # comme un processus bloqué dans une boîte de dialogue Tk
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
assert verrou.acquerir()
print("pret", flush=True)
time.sleep(120)
"""


class TestVerrou(TestTemporaire):
    def test_exclusif(self) -> None:
        premier = rp.VerrouInstance(self.tmp / "instance.lock")
        second = rp.VerrouInstance(self.tmp / "instance.lock")
        self.assertTrue(premier.acquerir())
        self.addCleanup(premier.liberer)
        self.assertFalse(second.acquerir())
        self.assertEqual(second.lire_pid(), os.getpid())
        premier.liberer()
        self.assertTrue(second.acquerir())
        second.liberer()

    def test_aucune_instance(self) -> None:
        self.assertIsNone(rp.instance_en_cours(self.tmp))
        self.assertFalse((self.tmp / rp.FICHIER_VERROU).exists())  # la consultation ne crée rien
        self.assertFalse(rp.arreter_instance(self.tmp))

    def lancer_detenteur(self, *options: str) -> subprocess.Popen:
        processus = subprocess.Popen(
            [sys.executable, "-c", SCRIPT_DETENTEUR, str(DOSSIER_SOURCE), str(self.tmp), *options],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(processus.stdout.close)
        self.addCleanup(lambda: processus.poll() is None and processus.kill())
        self.assertEqual(processus.stdout.readline().strip(), "pret")
        return processus

    def test_detection_et_arret_d_un_autre_processus(self) -> None:
        processus = self.lancer_detenteur()
        self.assertEqual(rp.instance_en_cours(self.tmp), processus.pid)
        self.assertTrue(rp.arreter_instance(self.tmp))
        processus.wait(timeout=10)
        self.assertIsNone(rp.instance_en_cours(self.tmp))

    @unittest.skipIf(sys.platform == "win32", "SIGKILL n'existe pas sous Windows (SIGTERM y est déjà définitif)")
    def test_arret_force_si_sigterm_sans_effet(self) -> None:
        processus = self.lancer_detenteur("ignorer-sigterm")
        debut = time.monotonic()
        self.assertTrue(rp.arreter_instance(self.tmp, delai_s=1.0))
        self.assertGreaterEqual(time.monotonic() - debut, 1.0)  # SIGTERM d'abord, SIGKILL ensuite
        self.assertEqual(processus.wait(timeout=10), -9)


# ------------------------------------------------------------- son et affichage (simulés)


def resultat(code: int = 0, erreur: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, None, erreur)


class TestSonEtAffichage(unittest.TestCase):
    def simuler(self, systeme: str, disponibles: tuple, env: dict | None = None, retour=None):
        """Simule la plateforme, les commandes présentes et leur résultat ; retourne le faux _executer."""
        env = {} if env is None else env
        executer = mock.Mock(return_value=resultat() if retour is None else retour)
        for correctif in (
            mock.patch.object(rp, "plateforme", return_value=systeme),
            mock.patch.object(rp.shutil, "which", side_effect=lambda nom: f"/usr/bin/{nom}" if nom in disponibles else None),
            mock.patch.object(rp, "_executer", executer),
            mock.patch.object(rp, "_afficher_tk", return_value=False),
            mock.patch.dict(os.environ, env),
        ):
            correctif.start()
            self.addCleanup(correctif.stop)
        for variable in ("DISPLAY", "WAYLAND_DISPLAY"):
            if variable not in env:
                os.environ.pop(variable, None)
        return executer

    def test_linux_zenity_avec_balisage_echappe(self) -> None:
        executer = self.simuler("linux", ("zenity", "kdialog", "notify-send"), {"DISPLAY": ":0"})
        self.assertTrue(rp.afficher_message("Titre", "A & B <c>"))
        commande = executer.call_args[0][0]
        self.assertEqual(commande[:2], ["zenity", "--info"])
        self.assertIn("A &amp; B &lt;c&gt;", commande)
        executer.assert_called_once()

    def test_linux_kdialog_si_zenity_absent(self) -> None:
        executer = self.simuler("linux", ("kdialog",), {"WAYLAND_DISPLAY": "wayland-0"})
        self.assertTrue(rp.afficher_message("Titre", "Texte"))
        self.assertEqual(executer.call_args[0][0], ["kdialog", "--title", "Titre", "--msgbox", "Texte"])

    def test_linux_sans_ecran_notification_seulement(self) -> None:
        executer = self.simuler("linux", ("zenity", "kdialog", "notify-send"))
        self.assertTrue(rp.afficher_message("Titre", "Texte"))
        executer.assert_called_once()
        self.assertEqual(executer.call_args[0][0][0], "notify-send")

    def test_linux_rien_de_disponible(self) -> None:
        self.simuler("linux", ())
        with self.assertLogs(rp.journal, "ERROR"):
            self.assertFalse(rp.afficher_message("Titre", "Texte"))

    def test_macos_textes_passes_en_arguments(self) -> None:
        executer = self.simuler("macos", ())
        self.assertTrue(rp.afficher_message("Titre", 'Guillemets " et \\ sans échappement'))
        commande = executer.call_args[0][0]
        self.assertEqual(commande[:3], ["osascript", "-e", rp._APPLESCRIPT_DIALOGUE])
        self.assertEqual(commande[3:], ["Titre", 'Guillemets " et \\ sans échappement', rp.LIBELLE_BOUTON])

    def test_macos_fermeture_clavier_vaut_fermeture(self) -> None:
        self.simuler("macos", (), retour=resultat(1, b"execution error: User canceled. (-128)"))
        self.assertTrue(rp.afficher_message("Titre", "Texte"))
        rp._afficher_tk.assert_not_called()

    def test_macos_erreur_repli_sur_tk(self) -> None:
        self.simuler("macos", (), retour=resultat(1, b"execution error: No user interaction allowed. (-1713)"))
        with self.assertLogs(rp.journal, "WARNING"):
            self.assertFalse(rp.afficher_message("Titre", "Texte"))
        rp._afficher_tk.assert_called_once_with("Titre", "Texte")

    def test_son_linux_premier_lecteur_disponible(self) -> None:
        executer = self.simuler("linux", ("aplay",))
        self.assertTrue(rp.jouer_son(Path("/tmp/carillon.wav")))
        self.assertEqual(executer.call_args[0][0], ["aplay", "-q", str(Path("/tmp/carillon.wav"))])

    def test_son_linux_lecteur_en_echec_puis_suivant(self) -> None:
        executer = self.simuler("linux", ("paplay", "aplay"))
        executer.side_effect = [resultat(1, b"Connection refused"), resultat(0)]
        with self.assertLogs(rp.journal, "WARNING"):
            self.assertTrue(rp.jouer_son(Path("carillon.wav")))
        self.assertEqual([appel[0][0][0] for appel in executer.call_args_list], ["paplay", "aplay"])

    def test_son_macos_et_absence_de_lecteur(self) -> None:
        executer = self.simuler("macos", ("afplay",))
        self.assertTrue(rp.jouer_son(Path("carillon.wav")))
        self.assertEqual(executer.call_args[0][0][0], "afplay")
        self.simuler("linux", ())
        with self.assertLogs(rp.journal, "WARNING"):
            self.assertFalse(rp.jouer_son(Path("carillon.wav")))
        self.assertFalse(rp.jouer_son(None))


# ------------------------------------------------------------------ lancement automatique


def decoder_exec_desktop(valeur: str) -> list:
    """Lecture de référence d'une clé Exec, selon la spécification freedesktop."""
    echappements = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    texte, i = "", 0
    while i < len(valeur):  # 1. échappements de la valeur chaîne
        if valeur[i] == "\\" and i + 1 < len(valeur) and valeur[i + 1] in echappements:
            texte += echappements[valeur[i + 1]]
            i += 2
        else:
            texte += valeur[i]
            i += 1
    arguments, courant, i = [], None, 0
    while i < len(texte):  # 2. découpage ; entre guillemets, \ protège " ` $ \
        caractere = texte[i]
        if caractere == " ":
            if courant is not None:
                arguments.append(courant)
            courant = None
        elif caractere == '"':
            courant = courant or ""
            i += 1
            while texte[i] != '"':
                if texte[i] == "\\" and texte[i + 1] in '"`$\\':
                    i += 1
                courant += texte[i]
                i += 1
        else:
            courant = (courant or "") + caractere
        i += 1
    if courant is not None:
        arguments.append(courant)
    return [argument.replace("%%", "%") for argument in arguments]  # 3. codes de champ


class TestLancementAutomatique(TestTemporaire):
    def test_exec_desktop_aller_retour(self) -> None:
        commande = [
            "/usr/bin/python3",
            "/home/Jean Dupont/.local/share/rappel-pause/rappel_pause.py",
            'guillemet " accent grave ` dollar $ barre \\ fin',
            "100 % l'été ; & | * ? # ( ) < > ~",
            "tabulation\tretour\nligne",
        ]
        ligne = " ".join(rp.echapper_exec_desktop(argument) for argument in commande)
        self.assertNotIn("\n", ligne)
        self.assertEqual(decoder_exec_desktop(ligne), commande)

    def test_contenu_desktop(self) -> None:
        commande = ["/usr/bin/python3", "/home/jean/.local/share/rappel-pause/rappel_pause.py"]
        lignes = rp.contenu_desktop(commande).splitlines()
        self.assertEqual(lignes[0], "[Desktop Entry]")
        self.assertIn("Type=Application", lignes)
        self.assertIn("Terminal=false", lignes)
        ligne_exec = next(ligne for ligne in lignes if ligne.startswith("Exec="))
        self.assertEqual(decoder_exec_desktop(ligne_exec[len("Exec="):]), commande)

    def test_contenu_plist(self) -> None:
        donnees = rp.plistlib.loads(rp.contenu_plist(["/usr/bin/python3", "/Users/jean/rappel_pause.py"]))
        self.assertEqual(donnees["Label"], rp.ETIQUETTE_LAUNCHD)
        self.assertEqual(donnees["ProgramArguments"], ["/usr/bin/python3", "/Users/jean/rappel_pause.py"])
        self.assertIs(donnees["RunAtLoad"], True)
        self.assertEqual(donnees["LimitLoadToSessionType"], "Aqua")

    def test_commande_script_et_executable_autonome(self) -> None:
        programme = self.tmp / "rappel_pause.py"
        with mock.patch.object(rp, "plateforme", return_value="linux"):
            self.assertEqual(rp.commande_lancement(programme), [sys.executable, str(programme)])
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(rp.commande_lancement(self.tmp / "RappelPause.exe"), [str(self.tmp / "RappelPause.exe")])

    def test_commande_windows_prefere_pythonw(self) -> None:
        python = self.tmp / "python.exe"
        python.touch()
        with mock.patch.object(rp, "plateforme", return_value="windows"), mock.patch.object(
            sys, "executable", str(python)
        ):
            self.assertEqual(rp.commande_lancement(Path("p.py"))[0], str(python))  # pythonw.exe absent
            (self.tmp / "pythonw.exe").touch()
            self.assertEqual(rp.commande_lancement(Path("p.py"))[0], str(self.tmp / "pythonw.exe"))

    def test_reessayer(self) -> None:
        operation = mock.Mock(side_effect=[PermissionError("utilisé"), PermissionError("utilisé"), "fait"])
        with mock.patch.object(rp.time, "sleep"):
            self.assertEqual(rp._reessayer(operation), "fait")
        self.assertEqual(operation.call_count, 3)
        with self.assertRaises(PermissionError):
            rp._reessayer(mock.Mock(side_effect=PermissionError("toujours utilisé")), delai_s=0.3)

    def test_desinstallation_depuis_le_dossier_installe(self) -> None:
        # Le programme de désinstallation peut être lancé avec le dossier d'installation comme dossier courant.
        dossier = self.tmp / "rappel-pause"
        (dossier / "sous-dossier").mkdir(parents=True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(str(dossier / "sous-dossier"))
        with mock.patch.object(rp, "plateforme", return_value="linux"), mock.patch.object(
            rp, "_desinscrire_demarrage", return_value=False
        ), mock.patch.object(rp, "arreter_instance", return_value=False):
            lignes = rp.desinstaller(dossier)
        self.assertFalse(dossier.exists())
        self.assertIn(f"Dossier supprimé : {dossier}", lignes)
        self.assertEqual(Path(os.getcwd()).resolve(), Path(tempfile.gettempdir()).resolve())

    def test_garde_fou_dossier(self) -> None:
        for dossier in (Path("RappelPause"), self.tmp, self.tmp / "autre"):
            with self.subTest(dossier=dossier), self.assertRaises(RuntimeError):
                rp._verifier_dossier_appli(dossier)
        rp._verifier_dossier_appli(self.tmp / "rappel-pause")

    def test_dossier_donnees_ignore_chemin_relatif(self) -> None:
        with mock.patch.object(rp, "plateforme", return_value="linux"), mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": "relatif", "HOME": str(self.tmp), "USERPROFILE": str(self.tmp)}
        ):
            self.assertEqual(rp.dossier_donnees(), self.tmp / ".local" / "share" / "rappel-pause")

    @unittest.skipUnless(EST_LINUX, "autostart XDG (Linux)")
    def test_inscription_linux(self) -> None:
        with mock.patch.dict(os.environ, environnement_isole(self.tmp, graphique=False), clear=True):
            emplacement = rp._inscrire_demarrage(["/usr/bin/python3", "/x/rappel_pause.py"])
            self.assertEqual(Path(emplacement), self.tmp / "config" / "autostart" / rp.FICHIER_DESKTOP)
            self.assertEqual(rp._demarrage_inscrit(), emplacement)
            self.assertTrue(rp._desinscrire_demarrage())
            self.assertIsNone(rp._demarrage_inscrit())
            self.assertFalse(rp._desinscrire_demarrage())

    @unittest.skipUnless(sys.platform == "darwin", "launchd (macOS)")
    def test_inscription_macos_plist_valide(self) -> None:
        etiquette = "local.rappel-pause.tests"
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}), mock.patch.object(rp, "ETIQUETTE_LAUNCHD", etiquette):
            emplacement = rp._inscrire_demarrage(["/usr/bin/python3", "/x/rappel_pause.py"])
            self.assertEqual(Path(emplacement), self.tmp / "Library" / "LaunchAgents" / f"{etiquette}.plist")
            verification = subprocess.run(["plutil", "-lint", emplacement], capture_output=True, text=True)
            self.assertEqual(verification.returncode, 0, verification.stdout + verification.stderr)
            self.assertTrue(rp._desinscrire_demarrage())
            self.assertIsNone(rp._demarrage_inscrit())

    @unittest.skipUnless(sys.platform == "win32", "registre (Windows)")
    def test_inscription_windows_registre(self) -> None:
        self.addCleanup(nettoyer_cles_de_test)
        commande = [r"C:\Program Files\Python\pythonw.exe", r"C:\Users\Jean Dupont\AppData\Local\RappelPause\rappel_pause.py"]
        with mock.patch.object(rp, "CLE_DEMARRAGE_WINDOWS", CLE_TEST_DEMARRAGE):
            rp._inscrire_demarrage(commande)
            self.assertEqual(rp._demarrage_inscrit(), subprocess.list2cmdline(commande))
            self.assertTrue(rp._desinscrire_demarrage())
            self.assertIsNone(rp._demarrage_inscrit())
            self.assertFalse(rp._desinscrire_demarrage())

    @unittest.skipUnless(sys.platform == "win32", "registre (Windows)")
    def test_entree_applications_installees(self) -> None:
        self.addCleanup(nettoyer_cles_de_test)
        dossier = Path(r"C:\Users\Jean Dupont\AppData\Local\RappelPause")
        with mock.patch.object(rp, "CLE_DESINSTALLATION_WINDOWS", CLE_TEST_DESINSTALLATION):
            rp._inscrire_desinstallation([str(dossier / "RappelPause.exe")], dossier)

            def valeur(nom: str):
                return valeur_registre(CLE_TEST_DESINSTALLATION, nom)

            self.assertEqual(valeur("DisplayName"), rp.NOM_LISIBLE)
            self.assertEqual(valeur("DisplayVersion"), rp.VERSION)
            self.assertEqual(valeur("InstallLocation"), str(dossier))
            self.assertEqual(valeur("UninstallString"), f'"{dossier}\\RappelPause.exe" --desinstaller')
            self.assertEqual((valeur("NoModify"), valeur("NoRepair")), (1, 1))
            self.assertTrue(rp._desinscrire_desinstallation())
            self.assertIsNone(valeur("DisplayName"))
            self.assertFalse(rp._desinscrire_desinstallation())

    @unittest.skipIf(sys.platform == "win32", "launchctl (POSIX)")
    def test_chargement_launchd_reessaie(self) -> None:
        executer = mock.Mock(side_effect=[resultat(5, b"Bootstrap failed: 5: Input/output error"), resultat(0)])
        with mock.patch.object(rp, "_executer", executer), mock.patch.object(rp.time, "sleep"):
            self.assertTrue(rp._charger_agent_launchd())
        self.assertEqual(executer.call_args[0][0], ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(rp.chemin_plist())])


# ------------------------------------------------------------- RappelPause.exe (simulé)


class TestExecutableWindows(TestTemporaire):
    """Double-clic et désinstallation de l'exécutable autonome, simulés sur tous les systèmes."""

    def setUp(self) -> None:
        super().setUp()
        self.dossier = self.tmp / "RappelPause"
        telecharge = self.tmp / "Téléchargements" / "RappelPause.exe"
        for correctif in (
            mock.patch.object(rp, "plateforme", return_value="windows"),
            mock.patch.object(rp, "dossier_donnees", return_value=self.dossier),
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(telecharge)),
        ):
            correctif.start()
            self.addCleanup(correctif.stop)

    def test_double_clic_sur_le_fichier_telecharge(self) -> None:
        self.assertTrue(rp._lance_par_double_clic([]))
        self.assertFalse(rp._lance_par_double_clic(["--installer"]))  # ligne de commande explicite
        with mock.patch.object(rp, "installer_par_double_clic", return_value=0) as double_clic:
            self.assertEqual(rp.main([]), 0)
        double_clic.assert_called_once_with(self.dossier)

    def test_copie_installee_lancee_a_l_ouverture_de_session(self) -> None:
        self.dossier.mkdir()
        with mock.patch.object(sys, "executable", str(self.dossier / "RappelPause.exe")):
            self.assertTrue(rp._execute_depuis(self.dossier))
            self.assertFalse(rp._lance_par_double_clic([]))  # boucle de rappels, sans question

    def test_script_et_autres_systemes_non_concernes(self) -> None:
        with mock.patch.object(sys, "frozen", False):
            self.assertFalse(rp._execute_depuis(self.dossier))
            self.assertFalse(rp._lance_par_double_clic([]))
        with mock.patch.object(rp, "plateforme", return_value="macos"):
            self.assertFalse(rp._lance_par_double_clic([]))

    def double_clic(self, reponse: bool, installation=None, deja_installe=None):
        simulacres = {
            "demander_confirmation": mock.Mock(return_value=reponse),
            "afficher_message": mock.Mock(return_value=True),
            "installer": installation or mock.Mock(return_value=["compte rendu"]),
            "_demarrage_inscrit": mock.Mock(return_value=deja_installe),
        }
        for nom, simulacre in simulacres.items():
            correctif = mock.patch.object(rp, nom, simulacre)
            correctif.start()
            self.addCleanup(correctif.stop)
        return rp.installer_par_double_clic(self.dossier), simulacres

    def test_double_clic_refuse(self) -> None:
        code, simulacres = self.double_clic(False)
        self.assertEqual(code, 0)
        question = simulacres["demander_confirmation"].call_args[0][1]
        self.assertTrue(question.startswith("Installer le rappel de pause active ?"), question)
        self.assertIn("Toutes les 2 h", question)
        self.assertIn(rp.MESSAGE_DEFAUT, question)
        simulacres["installer"].assert_not_called()
        simulacres["afficher_message"].assert_not_called()

    def test_double_clic_accepte(self) -> None:
        code, simulacres = self.double_clic(True)
        self.assertEqual(code, 0)
        simulacres["installer"].assert_called_once_with(self.dossier, {})
        compte_rendu = simulacres["afficher_message"].call_args[0][1]
        self.assertTrue(compte_rendu.startswith(f"{rp.NOM_LISIBLE} installé."), compte_rendu)
        self.assertIn("Première sonnerie dans 2 h", compte_rendu)
        self.assertIn("Paramètres > Applications", compte_rendu)

    def test_double_clic_mise_a_jour(self) -> None:
        _, simulacres = self.double_clic(False, deja_installe=r"C:\...\RappelPause.exe")
        self.assertTrue(simulacres["demander_confirmation"].call_args[0][1].startswith("Mettre à jour"))

    def test_double_clic_echec_affiche(self) -> None:
        code, simulacres = self.double_clic(True, installation=mock.Mock(side_effect=OSError("disque plein")))
        self.assertEqual(code, 1)
        self.assertEqual(simulacres["afficher_message"].call_args[0][1], "L'installation a échoué : disque plein")

    def test_desinstallation_depuis_la_copie_installee(self) -> None:
        # Bouton Désinstaller de Paramètres > Applications : c'est la copie installée qui s'exécute.
        self.dossier.mkdir()
        (self.dossier / rp.FICHIER_CONFIG).write_text("{}", encoding="utf-8")
        with mock.patch.object(sys, "executable", str(self.dossier / "RappelPause.exe")), mock.patch.object(
            rp, "_desinscrire_demarrage", return_value=True
        ), mock.patch.object(rp, "_desinscrire_desinstallation", return_value=True), mock.patch.object(
            rp, "arreter_instance", return_value=True
        ), mock.patch.object(rp, "_supprimer_apres_arret") as suppression:
            lignes = rp.desinstaller(self.dossier)
        suppression.assert_called_once_with(self.dossier)
        self.assertTrue(self.dossier.exists())  # supprimé par cmd.exe après la fin du programme
        self.assertIn("Entrée retirée de Paramètres > Applications.", lignes)
        self.assertIn(f"Dossier {self.dossier} : supprimé dès la fermeture de ce programme.", lignes)

    def test_commande_de_suppression_differee(self) -> None:
        chemin = r"C:\Users\Jean & Fils (Bureau)\AppData\Local\RappelPause"
        with mock.patch.object(rp.subprocess, "Popen") as popen:
            rp._supprimer_apres_arret(Path(chemin))
        commande = popen.call_args[0][0]
        self.assertTrue(commande.startswith('cmd.exe /d /s /c "for /l %i in (1,1,600) do ('), commande)
        self.assertEqual(commande.count(f'"{chemin}"'), 2)  # rd et if not exist, chemin entre guillemets
        self.assertIn(f'rd /s /q "{chemin}"', commande)
        self.assertTrue(commande.endswith(' exit)"'), commande)
        self.assertEqual(popen.call_args[1]["cwd"], tempfile.gettempdir())


# --------------------------------------------------------------------------- intégration


@unittest.skipUnless(INTEGRATION, "définir RAPPEL_PAUSE_TESTS_INTEGRATION=1")
class TestIntegration(TestTemporaire):
    """Installation réelle dans des dossiers temporaires (registre : clé de test sous Windows)."""

    def setUp(self) -> None:
        super().setUp()
        correctifs = [mock.patch.dict(os.environ, environnement_isole(self.tmp, graphique=True), clear=True)]
        if sys.platform == "darwin":
            # launchd ne transmet pas l'environnement de test : lancement direct, comme sous Linux.
            correctifs += [
                mock.patch.object(rp, "ETIQUETTE_LAUNCHD", "local.rappel-pause.tests"),
                mock.patch.object(rp, "_demarrer", rp._lancer_detache),
            ]
        if sys.platform == "win32":
            correctifs += [
                mock.patch.object(rp, "CLE_DEMARRAGE_WINDOWS", CLE_TEST_DEMARRAGE),
                mock.patch.object(rp, "CLE_DESINSTALLATION_WINDOWS", CLE_TEST_DESINSTALLATION),
            ]
            self.addCleanup(nettoyer_cles_de_test)
        for correctif in correctifs:
            correctif.start()
            self.addCleanup(correctif.stop)
        self.dossier = rp.dossier_donnees()
        self.assertTrue(str(self.dossier).startswith(str(self.tmp)))
        self.addCleanup(self.desinstaller_si_besoin)

    def desinstaller_si_besoin(self) -> None:
        if self.dossier.exists() or rp._demarrage_inscrit():
            rp.desinstaller(self.dossier)

    def installer(self, reglages: dict) -> str:
        with warnings.catch_warnings():
            # La boucle lancée par l'installation survit volontairement à son lanceur.
            warnings.simplefilter("ignore", ResourceWarning)
            return "\n".join(rp.installer(self.dossier, reglages))

    def test_installation_reinstallation_desinstallation(self) -> None:
        compte_rendu = self.installer({"message": "Test d'intégration"})
        self.assertIn("Le rappel est actif : première sonnerie dans 2 h.", compte_rendu)
        self.assertTrue((self.dossier / "rappel_pause.py").is_file())
        self.assertIsNotNone(rp._demarrage_inscrit())
        if sys.platform == "win32":  # entrée de Paramètres > Applications
            self.assertIn("--desinstaller", valeur_registre(CLE_TEST_DESINSTALLATION, "UninstallString"))
        premier_pid = rp.instance_en_cours(self.dossier)
        self.assertGreater(premier_pid or 0, 0)

        # Réinstallation : remplace l'instance, conserve le message, applique le nouvel intervalle.
        compte_rendu = self.installer({"intervalle_minutes": 90})
        self.assertIn("première sonnerie dans 1 h 30", compte_rendu)
        second_pid = rp.instance_en_cours(self.dossier)
        self.assertNotIn(second_pid, (None, 0, premier_pid))
        self.assertEqual(
            rp.charger_config(self.dossier / rp.FICHIER_CONFIG),
            {"intervalle_minutes": 90, "message": "Test d'intégration"},
        )
        self.assertIn(f"oui (PID {second_pid})", "\n".join(rp.statut(self.dossier)))
        self.assertTrue(attendre_texte(self.dossier / rp.FICHIER_JOURNAL, f"(PID {second_pid})"))

        compte_rendu = "\n".join(rp.desinstaller(self.dossier))
        self.assertIn("désinstallé", compte_rendu)
        self.assertFalse(self.dossier.exists())
        self.assertIsNone(rp._demarrage_inscrit())
        if sys.platform == "win32":
            self.assertIsNone(valeur_registre(CLE_TEST_DESINSTALLATION, "UninstallString"))
        self.assertIn("n'était pas installé", "\n".join(rp.desinstaller(self.dossier)))

    def test_la_boucle_declenche_les_rappels(self) -> None:
        env = environnement_isole(self.tmp, graphique=False)  # aucun écran sous Linux : rien ne s'affiche
        commande = [sys.executable, str(DOSSIER_SOURCE / "rappel_pause.py"), "--intervalle", "0,005"]  # 0,3 s
        processus = subprocess.Popen(commande, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: processus.poll() is None and processus.kill())
        journal = self.dossier / rp.FICHIER_JOURNAL
        self.assertTrue(attendre_texte(journal, "Rappel."), "aucun rappel déclenché en 60 s")
        self.assertEqual(rp.instance_en_cours(self.dossier), processus.pid)
        self.assertTrue(rp.arreter_instance(self.dossier))
        processus.wait(timeout=10)
        self.assertIn("rappel toutes les 0,005 min", journal.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
