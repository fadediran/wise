#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rappel de pause active contre la sédentarité.

Toutes les 2 heures (réglable), l'ordinateur sonne puis affiche au premier plan :
« C'est l'heure de se lever pour marcher ! »

* Un seul fichier, bibliothèque standard uniquement (Python 3.8 ou plus récent).
* Windows 10/11, macOS, Linux (GNOME, KDE, Xfce...).
* Installation pour l'utilisateur courant, sans droits administrateur :
  lancement automatique à chaque ouverture de session.

Utilisation :
    python3 rappel_pause.py --installer [--intervalle MINUTES] [--message TEXTE]
    python3 rappel_pause.py --test          sonne et affiche le rappel immédiatement
    python3 rappel_pause.py --statut        état de l'installation
    python3 rappel_pause.py --desinstaller  arrête le rappel et supprime l'installation
    python3 rappel_pause.py                 boucle de rappels (lancée à l'ouverture de session)

Comportement :
* le délai repart à la fermeture du message : les rappels ne s'empilent jamais ;
* une mise en veille de plus de 5 minutes remet le compte à rebours à zéro
  (l'utilisateur s'est éloigné de son poste) au lieu de sonner dès le réveil ;
* une seule instance par utilisateur (verrou fichier) ; journal dans le dossier de données.
"""

from __future__ import annotations

import argparse
import array
import html
import json
import logging
import logging.handlers
import math
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

VERSION = "1.0.0"
NOM_APPLI = "RappelPause"
NOM_LISIBLE = "Rappel de pause active"
TITRE_RAPPEL = "Pause active"
MESSAGE_DEFAUT = "C'est l'heure de se lever pour marcher\u00a0!"  # \u00a0 : espace insécable avant « ! »
LIBELLE_BOUTON = "OK, je me lève\u00a0!"
INTERVALLE_DEFAUT_MIN = 120

# L'horloge est relevée toutes les PAS_SURVEILLANCE_S secondes. Un écart supérieur à
# SEUIL_VEILLE_S entre deux relevés signifie que l'ordinateur était en veille :
# l'utilisateur s'est éloigné de son poste, le compte à rebours repart donc de zéro.
PAS_SURVEILLANCE_S = 30.0
SEUIL_VEILLE_S = 5 * 60.0

FICHIER_CONFIG = "config.json"
FICHIER_JOURNAL = "rappel_pause.log"
FICHIER_VERROU = "instance.lock"
FICHIER_CARILLON = "carillon.wav"

CLE_DEMARRAGE_WINDOWS = r"Software\Microsoft\Windows\CurrentVersion\Run"
ETIQUETTE_LAUNCHD = "local.rappel-pause"
FICHIER_DESKTOP = "rappel-pause.desktop"

# Styles de MessageBoxW (winuser.h).
MB_OK, MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST = 0x0, 0x40, 0x10000, 0x40000

# Windows : empêche l'ouverture d'une console pour chaque commande externe.
_SANS_FENETRE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

journal = logging.getLogger("rappel_pause")


# ---------------------------------------------------------------------------- plateforme


def plateforme() -> str:
    """« windows », « macos » ou « linux » (Linux et autres bureaux Unix XDG)."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _chemin_absolu_env(variable: str, defaut: Path) -> Path:
    """Chemin lu dans une variable d'environnement, ignoré s'il n'est pas absolu."""
    valeur = os.environ.get(variable, "")
    return Path(valeur) if os.path.isabs(valeur) else defaut


def dossier_donnees() -> Path:
    """Dossier propre à l'utilisateur : programme installé, configuration, journal."""
    systeme = plateforme()
    if systeme == "windows":
        return _chemin_absolu_env("LOCALAPPDATA", Path.home() / "AppData" / "Local") / NOM_APPLI
    if systeme == "macos":
        return Path.home() / "Library" / "Application Support" / NOM_APPLI
    return _chemin_absolu_env("XDG_DATA_HOME", Path.home() / ".local" / "share") / "rappel-pause"


def _verifier_dossier_appli(dossier: Path) -> None:
    """Garde-fou avant toute écriture ou suppression récursive."""
    if not dossier.is_absolute() or dossier.name not in (NOM_APPLI, "rappel-pause"):
        raise RuntimeError(f"dossier d'installation inattendu : {dossier}")


# ------------------------------------------------------------------------- configuration


def valider_intervalle(valeur: object) -> float:
    """Nombre de minutes fini et strictement positif (entier si possible, pour un JSON lisible)."""
    if isinstance(valeur, bool):
        raise ValueError(f"intervalle invalide : {valeur!r}")
    try:
        minutes = float(valeur)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"intervalle invalide : {valeur!r} (nombre de minutes attendu)") from None
    if not (math.isfinite(minutes) and minutes > 0):
        raise ValueError(f"intervalle invalide : {valeur!r} (nombre de minutes > 0 attendu)")
    return int(minutes) if minutes.is_integer() else minutes


def valider_message(valeur: object) -> str:
    if not isinstance(valeur, str) or not valeur.strip():
        raise ValueError("le message ne peut pas être vide")
    return valeur.strip()


def config_par_defaut() -> dict:
    return {"intervalle_minutes": INTERVALLE_DEFAUT_MIN, "message": MESSAGE_DEFAUT}


def charger_config(chemin: Path) -> dict:
    """Configuration enregistrée ; toute valeur absente ou invalide prend sa valeur par défaut."""
    config = config_par_defaut()
    try:
        # utf-8-sig : tolère le BOM ajouté par certains éditeurs (Bloc-notes).
        brut = json.loads(chemin.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as exc:
        journal.warning("Configuration %s illisible (%s) : valeurs par défaut utilisées.", chemin, exc)
        return config
    if not isinstance(brut, dict):
        journal.warning("Configuration %s invalide : valeurs par défaut utilisées.", chemin)
        return config
    for cle, valider in (("intervalle_minutes", valider_intervalle), ("message", valider_message)):
        if cle in brut:
            try:
                config[cle] = valider(brut[cle])
            except ValueError as exc:
                journal.warning("Configuration %s : %s ; valeur par défaut utilisée.", chemin, exc)
    return config


def enregistrer_config(chemin: Path, config: dict) -> None:
    """Écriture atomique : un fichier à moitié écrit n'est jamais lu."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    temporaire = chemin.with_name(chemin.name + ".tmp")
    temporaire.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporaire), str(chemin))


def formater_minutes(minutes: float) -> str:
    """120 -> « 2 h », 90 -> « 1 h 30 », 45 -> « 45 min », 1.5 -> « 1,5 min »."""
    if minutes >= 60 and float(minutes).is_integer():
        heures, reste = divmod(int(minutes), 60)
        return f"{heures} h {reste:02d}" if reste else f"{heures} h"
    return ("%f" % minutes).rstrip("0").rstrip(".").replace(".", ",") + " min"


# ------------------------------------------------------------------------------- carillon


def generer_carillon(chemin: Path, taux: int = 22050) -> Path:
    """Écrit un carillon d'environ 1,6 s (arpège do-mi-sol-do) en WAV PCM 16 bits mono.

    Le son est synthétisé plutôt que pris dans le système : il est identique sur toutes les
    machines et ne dépend ni d'un fichier système ni du thème sonore choisi par l'utilisateur.
    """
    notes = ((523.25, 0.00), (659.25, 0.15), (783.99, 0.30), (1046.50, 0.45))  # (Hz, départ en s)
    n_note = int(1.2 * taux)
    n_attaque = int(0.005 * taux)  # montée de 5 ms : pas de « clic » à l'attaque
    n_relachement = int(0.05 * taux)  # fondu final de 50 ms vers zéro
    total = int(notes[-1][1] * taux) + n_note
    signal_ = [0.0] * total
    for frequence, depart in notes:
        debut = int(depart * taux)
        omega = 2.0 * math.pi * frequence / taux
        for k in range(n_note):
            enveloppe = min(1.0, k / n_attaque, (n_note - k) / n_relachement) * math.exp(-5.0 * k / taux)
            signal_[debut + k] += enveloppe * (math.sin(omega * k) + 0.25 * math.sin(2.0 * omega * k))
    echelle = 0.7 * 32767 / max(abs(v) for v in signal_)
    echantillons = array.array("h", (int(round(v * echelle)) for v in signal_))
    if sys.byteorder == "big":  # le format WAV est petit-boutiste
        echantillons.byteswap()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(chemin), "wb") as fichier:
        fichier.setnchannels(1)
        fichier.setsampwidth(2)
        fichier.setframerate(taux)
        fichier.writeframes(echantillons.tobytes())
    return chemin


def _preparer_carillon(dossier: Path) -> Path | None:
    """(Re)génère le carillon ; en cas d'échec, garde la version précédente s'il y en a une."""
    chemin = dossier / FICHIER_CARILLON
    temporaire = dossier / f"{FICHIER_CARILLON}.{os.getpid()}.tmp"
    try:
        generer_carillon(temporaire)
        os.replace(str(temporaire), str(chemin))
    except OSError as exc:
        journal.warning("Carillon non généré (%s).", exc)
        try:
            temporaire.unlink()
        except OSError:
            pass
        return chemin if chemin.exists() else None
    return chemin


# ------------------------------------------------------------------------ instance unique


class VerrouInstance:
    """Verrou fichier garantissant une seule boucle de rappels par utilisateur.

    Le système libère le verrou automatiquement si le processus s'arrête, même brutalement.
    Le fichier contient le PID du détenteur (lu par --statut et --desinstaller).
    """

    # Windows impose des verrous obligatoires : on verrouille un octet situé loin après
    # le PID (au-delà de la fin du fichier, ce que Windows autorise) pour que le PID reste lisible.
    OCTET_VERROU_WINDOWS = 1 << 20

    def __init__(self, chemin: Path) -> None:
        self.chemin = chemin
        self._fd: int | None = None

    def acquerir(self, ecrire_pid: bool = True) -> bool:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.chemin), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, self.OCTET_VERROU_WINDOWS, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        if ecrire_pid:
            os.lseek(fd, 0, os.SEEK_SET)
            # Largeur fixe : recouvre entièrement le PID d'une exécution précédente.
            os.write(fd, f"{os.getpid():<20}\n".encode("ascii"))
        self._fd = fd
        return True

    def liberer(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, self.OCTET_VERROU_WINDOWS, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def lire_pid(self) -> int | None:
        try:
            return int(self.chemin.read_bytes().split()[0])
        except (OSError, ValueError, IndexError):
            return None


def instance_en_cours(dossier: Path) -> int | None:
    """PID de la boucle de rappels active (0 s'il est illisible), ou None si aucune ne tourne."""
    verrou = VerrouInstance(dossier / FICHIER_VERROU)
    if not verrou.chemin.exists():
        return None
    if verrou.acquerir(ecrire_pid=False):
        verrou.liberer()
        return None
    return verrou.lire_pid() or 0


def arreter_instance(dossier: Path, delai_s: float = 5.0) -> bool:
    """Arrête la boucle de rappels active ; True si une instance a été arrêtée.

    Le PID est fiable tant que le verrou est détenu : seul son détenteur actuel l'a écrit.
    SIGTERM permet un arrêt propre (Windows : TerminateProcess) ; SIGKILL (POSIX) prend le
    relais si le processus ne répond pas, par exemple bloqué dans une boîte de dialogue Tk.
    """
    pid = instance_en_cours(dossier)
    if pid is None:
        return False
    if pid <= 0 or pid == os.getpid():
        raise RuntimeError("une instance est active mais son PID est illisible : fermez la session pour l'arrêter")
    for signal_arret in (signal.SIGTERM, getattr(signal, "SIGKILL", None)):
        if signal_arret is None or instance_en_cours(dossier) != pid:
            break
        try:
            os.kill(pid, signal_arret)
        except OSError as exc:  # déjà terminée entre-temps
            journal.debug("Arrêt du PID %d : %s", pid, exc)
        echeance = time.monotonic() + delai_s
        while time.monotonic() < echeance:
            if instance_en_cours(dossier) is None:
                return True
            time.sleep(0.1)
    if instance_en_cours(dossier) is None:
        return True
    raise RuntimeError(f"la boucle de rappels (PID {pid}) ne s'est pas arrêtée")


def _attendre_instance(dossier: Path, delai_s: float = 15.0) -> bool:
    echeance = time.monotonic() + delai_s
    while time.monotonic() < echeance:
        if instance_en_cours(dossier) is not None:
            return True
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------------------- attente


def attendre_activite(
    duree_s: float,
    pas_s: float = PAS_SURVEILLANCE_S,
    seuil_veille_s: float = SEUIL_VEILLE_S,
    horloge=time.time,
    dormir=time.sleep,
) -> None:
    """Rend la main après `duree_s` secondes d'utilisation de l'ordinateur.

    Le temps est mesuré sur l'horloge murale : elle avance pendant la veille sur tous les
    systèmes, ce qui n'est pas garanti pour time.sleep() (Linux et macOS ne comptent pas
    la veille). Une veille plus longue que `seuil_veille_s` remet le compte à rebours à zéro.
    """
    ecoule = 0.0
    precedent = horloge()
    while ecoule < duree_s:
        sommeil = min(pas_s, duree_s - ecoule)
        dormir(sommeil)
        maintenant = horloge()
        ecart = maintenant - precedent
        precedent = maintenant
        if ecart < 0:
            ecoule += sommeil  # horloge système reculée (réglage manuel) : on compte le temps dormi
        elif ecart - sommeil > seuil_veille_s:
            minutes = round((ecart - sommeil) / 60)
            journal.info("Veille détectée (%d min) : compte à rebours remis à zéro.", minutes)
            ecoule = 0.0
        else:
            ecoule += ecart


# ---------------------------------------------------------------------------------- son


def _executer(commande: list, delai: float | None = None) -> subprocess.CompletedProcess | None:
    """Exécute une commande externe jusqu'à sa fin ; None si elle n'a pas pu être lancée."""
    try:
        return subprocess.run(
            commande,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=delai,
            check=False,
            creationflags=_SANS_FENETRE,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        journal.warning("Échec de %s : %s", commande[0], exc)
        return None


def _erreur(resultat: subprocess.CompletedProcess | None) -> str:
    if resultat is None:
        return "lancement impossible"
    detail = (resultat.stderr or b"").decode("utf-8", "replace").strip()
    return f"code {resultat.returncode}" + (f" : {detail}" if detail else "")


def jouer_son(fichier: Path | None) -> bool:
    """Joue le carillon jusqu'au bout (≈ 1,6 s) ; False si aucun son n'a pu être joué."""
    if fichier is None:
        return False
    systeme = plateforme()
    if systeme == "windows":
        try:
            import winsound

            winsound.PlaySound(str(fichier), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
            return True
        except (ImportError, RuntimeError) as exc:
            journal.warning("Lecture du son impossible : %s", exc)
            return False
    lecteurs = [["afplay"]] if systeme == "macos" else [["paplay"], ["pw-play"], ["aplay", "-q"]]
    for lecteur in lecteurs:
        if shutil.which(lecteur[0]) is None:
            continue
        resultat = _executer(lecteur + [str(fichier)], delai=15)
        if resultat is not None and resultat.returncode == 0:
            return True
        journal.warning("%s n'a pas pu jouer le son (%s).", lecteur[0], _erreur(resultat))
    journal.warning("Aucun lecteur audio utilisable : le rappel sera silencieux.")
    return False


# ------------------------------------------------------------------------------ affichage


def _afficher_windows(titre: str, message: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        user32.SetProcessDPIAware()  # texte net sur les écrans haute résolution
    except AttributeError:
        pass
    boite = user32.MessageBoxW
    boite.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    boite.restype = ctypes.c_int
    if boite(None, message, titre, MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST) == 0:
        journal.warning("MessageBoxW a échoué (erreur Windows %d).", ctypes.get_last_error())
        return False
    return True


# Titre, message et libellé du bouton sont passés en arguments (argv) :
# aucun échappement de chaîne AppleScript n'est nécessaire.
_APPLESCRIPT_DIALOGUE = (
    "on run argv\n"
    "activate\n"
    "display dialog (item 2 of argv) with title (item 1 of argv) "
    "buttons {item 3 of argv} default button 1 with icon note\n"
    "end run"
)


def _afficher_macos(titre: str, message: str) -> bool:
    resultat = _executer(["osascript", "-e", _APPLESCRIPT_DIALOGUE, titre, message, LIBELLE_BOUTON])
    if resultat is None:
        return False
    if resultat.returncode == 0 or b"(-128)" in (resultat.stderr or b""):  # -128 : fermé au clavier
        return True
    journal.warning("osascript : %s", _erreur(resultat))
    return False


def _session_graphique() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _afficher_zenity(titre: str, message: str) -> bool:
    if not _session_graphique() or shutil.which("zenity") is None:
        return False
    # --text est interprété comme du balisage Pango : & < > doivent être échappés.
    commande = ["zenity", "--info", "--title", titre, "--text", html.escape(message, quote=False), "--width", "420"]
    resultat = _executer(commande)
    return resultat is not None and resultat.returncode in (0, 1)  # 1 : fenêtre fermée par la croix


def _afficher_kdialog(titre: str, message: str) -> bool:
    if not _session_graphique() or shutil.which("kdialog") is None:
        return False
    resultat = _executer(["kdialog", "--title", titre, "--msgbox", message])
    return resultat is not None and resultat.returncode in (0, 1)


def _afficher_tk(titre: str, message: str) -> bool:
    """Repli universel : boîte de dialogue Tk, si Python a été installé avec tkinter."""
    if plateforme() == "linux" and not _session_graphique():
        return False
    try:
        import tkinter
        from tkinter import messagebox
    except ImportError:
        return False
    try:
        racine = tkinter.Tk()
    except tkinter.TclError as exc:
        journal.debug("Tk indisponible : %s", exc)
        return False
    try:
        racine.withdraw()
        racine.attributes("-topmost", True)
        messagebox.showinfo(titre, message, parent=racine)
    finally:
        racine.destroy()
    return True


def _notifier_linux(titre: str, message: str) -> bool:
    """Dernier recours sous Linux : notification du bureau (non bloquante)."""
    if shutil.which("notify-send") is None:
        return False
    resultat = _executer(["notify-send", "--urgency=critical", "--icon=dialog-information", titre, message], delai=15)
    return resultat is not None and resultat.returncode == 0


def afficher_message(titre: str, message: str) -> bool:
    """Affiche le message au premier plan et attend sa fermeture (sauf simple notification).

    Essaie d'abord la boîte de dialogue native du système, puis les solutions de repli.
    """
    systeme = plateforme()
    if systeme == "windows":
        afficheurs = (_afficher_windows, _afficher_tk)
    elif systeme == "macos":
        afficheurs = (_afficher_macos, _afficher_tk)
    else:
        afficheurs = (_afficher_zenity, _afficher_kdialog, _afficher_tk, _notifier_linux)
    for afficheur in afficheurs:
        try:
            if afficheur(titre, message):
                return True
        except Exception:  # un afficheur défaillant ne doit pas empêcher d'essayer le suivant
            journal.exception("Échec de %s.", afficheur.__name__)
    journal.error("Aucune interface graphique disponible : message non affiché.")
    return False


def rappeler(message: str, carillon: Path | None) -> bool:
    """Sonne, puis affiche le message et attend qu'il soit fermé ; False s'il n'a pas pu être affiché."""
    journal.info("Rappel.")
    jouer_son(carillon)
    affiche = afficher_message(TITRE_RAPPEL, message)
    if affiche:
        journal.info("Rappel affiché.")
    return affiche


# ------------------------------------------------------------------ lancement automatique


def chemin_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{ETIQUETTE_LAUNCHD}.plist"


def chemin_desktop() -> Path:
    return _chemin_absolu_env("XDG_CONFIG_HOME", Path.home() / ".config") / "autostart" / FICHIER_DESKTOP


def echapper_exec_desktop(argument: str) -> str:
    """Encode un argument pour la clé Exec d'un fichier .desktop (spécification freedesktop).

    Dans l'ordre inverse de la lecture : guillemets doubles avec \\ devant " ` $ \\,
    puis % doublé (codes de champ), puis échappements de la valeur chaîne (\\ -> \\\\, etc.).
    """
    entre_guillemets = '"' + "".join("\\" + c if c in '"`$\\' else c for c in argument) + '"'
    entre_guillemets = entre_guillemets.replace("%", "%%")
    return (
        entre_guillemets.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    )


def contenu_desktop(commande: list) -> str:
    ligne_exec = " ".join(echapper_exec_desktop(str(argument)) for argument in commande)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={NOM_LISIBLE}\n"
        "Comment=Sonne et rappelle régulièrement de se lever pour marcher\n"
        f"Exec={ligne_exec}\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def contenu_plist(commande: list) -> bytes:
    return plistlib.dumps(
        {
            "Label": ETIQUETTE_LAUNCHD,
            "ProgramArguments": [str(argument) for argument in commande],
            "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua",
        }
    )


def _inscrire_demarrage(commande: list) -> str:
    """Inscrit le lancement à l'ouverture de session ; retourne l'emplacement de l'inscription."""
    systeme = plateforme()
    if systeme == "windows":
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, CLE_DEMARRAGE_WINDOWS, 0, winreg.KEY_SET_VALUE) as cle:
            winreg.SetValueEx(cle, NOM_APPLI, 0, winreg.REG_SZ, subprocess.list2cmdline(commande))
        return f"HKEY_CURRENT_USER\\{CLE_DEMARRAGE_WINDOWS}\\{NOM_APPLI}"
    if systeme == "macos":
        fichier, contenu = chemin_plist(), contenu_plist(commande)
    else:
        fichier, contenu = chemin_desktop(), contenu_desktop(commande).encode("utf-8")
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_bytes(contenu)
    return str(fichier)


def _desinscrire_demarrage() -> bool:
    """Supprime le lancement automatique (macOS : arrête aussi l'agent) ; True s'il existait."""
    systeme = plateforme()
    if systeme == "windows":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_DEMARRAGE_WINDOWS, 0, winreg.KEY_SET_VALUE) as cle:
                winreg.DeleteValue(cle, NOM_APPLI)
        except FileNotFoundError:
            return False
        return True
    if systeme == "macos":
        # Arrête et décharge l'agent ; l'erreur « service introuvable » est sans conséquence.
        _executer(["launchctl", "bootout", f"gui/{os.getuid()}/{ETIQUETTE_LAUNCHD}"], delai=30)
    fichier = chemin_plist() if systeme == "macos" else chemin_desktop()
    if not fichier.exists():
        return False
    fichier.unlink()
    return True


def _demarrage_inscrit() -> str | None:
    """Commande ou fichier du lancement automatique, ou None s'il n'est pas inscrit."""
    systeme = plateforme()
    if systeme == "windows":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLE_DEMARRAGE_WINDOWS) as cle:
                return str(winreg.QueryValueEx(cle, NOM_APPLI)[0])
        except FileNotFoundError:
            return None
    fichier = chemin_plist() if systeme == "macos" else chemin_desktop()
    return str(fichier) if fichier.exists() else None


def _lancer_detache(commande: list) -> bool:
    """Lance la boucle de rappels en arrière-plan, indépendamment du terminal courant."""
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(commande, **options)
    return True


def _charger_agent_launchd() -> bool:
    domaine = f"gui/{os.getuid()}"
    resultat = None
    for _ in range(5):  # juste après un « bootout », launchd peut refuser brièvement
        resultat = _executer(["launchctl", "bootstrap", domaine, str(chemin_plist())], delai=30)
        if resultat is not None and resultat.returncode == 0:
            return True
        time.sleep(1)
    journal.warning("launchctl bootstrap : %s", _erreur(resultat))
    return False


def _demarrer(commande: list) -> bool:
    """Démarre la boucle maintenant ; False si elle ne démarrera qu'à la prochaine session."""
    systeme = plateforme()
    if systeme == "macos":
        return _charger_agent_launchd()
    if systeme == "linux" and not _session_graphique():
        return False  # pas d'écran (ex. connexion SSH) : l'autostart s'en chargera
    return _lancer_detache(commande)


# ---------------------------------------------------------------- installation et statut


def commande_lancement(programme: Path) -> list:
    """Ligne de commande lançant la boucle de rappels depuis le programme installé."""
    if getattr(sys, "frozen", False):  # exécutable autonome (PyInstaller)
        return [str(programme)]
    if not sys.executable:
        raise RuntimeError("chemin de l'interpréteur Python introuvable (sys.executable vide)")
    interpreteur = Path(sys.executable)
    if plateforme() == "windows":
        sans_console = interpreteur.with_name("pythonw.exe")  # même interpréteur, sans fenêtre
        if sans_console.exists():
            interpreteur = sans_console
    return [str(interpreteur), str(programme)]


def _commande_console(programme: Path) -> list:
    return [str(programme)] if getattr(sys, "frozen", False) else [sys.executable, str(programme)]


def _commande_affichable(arguments: list) -> str:
    if plateforme() == "windows":
        return subprocess.list2cmdline(arguments)
    return " ".join(shlex.quote(argument) for argument in arguments)


def _reessayer(operation, delai_s: float = 10.0):
    """Réessaie une opération sur des fichiers : sous Windows, un programme qui vient de s'arrêter
    (processus parent d'un exécutable PyInstaller) ou l'antivirus peut les garder ouverts un instant."""
    echeance = time.monotonic() + delai_s
    while True:
        try:
            return operation()
        except OSError:
            if time.monotonic() >= echeance:
                raise
            time.sleep(0.2)


def _copier_programme(dossier: Path) -> Path:
    """Copie le programme dans le dossier de données : le dossier téléchargé peut être supprimé."""
    if getattr(sys, "frozen", False):
        source = Path(sys.executable).resolve()
        cible = dossier / source.name
    else:
        source = Path(__file__).resolve()
        cible = dossier / "rappel_pause.py"
    if not (cible.exists() and os.path.samefile(str(source), str(cible))):
        _reessayer(lambda: shutil.copy2(str(source), str(cible)))
    return cible


def _est_python_microsoft_store() -> bool:
    return (
        plateforme() == "windows"
        and not getattr(sys, "frozen", False)
        and "WindowsApps" in (sys.executable or "") + sys.base_prefix
    )


def installer(dossier: Path, reglages: dict) -> list:
    """Installe (ou réinstalle) pour l'utilisateur courant et démarre le rappel ; retourne le compte rendu."""
    _verifier_dossier_appli(dossier)
    # Arrêt de la version en place : un .exe en cours d'exécution ne peut pas être remplacé sous Windows.
    _desinscrire_demarrage()
    arreter_instance(dossier)
    dossier.mkdir(parents=True, exist_ok=True)
    programme = _copier_programme(dossier)
    fichier_config = dossier / FICHIER_CONFIG
    config = charger_config(fichier_config)
    config.update(reglages)
    enregistrer_config(fichier_config, config)
    commande = commande_lancement(programme)
    emplacement = _inscrire_demarrage(commande)
    intervalle = formater_minutes(config["intervalle_minutes"])
    lignes = [
        f"{NOM_LISIBLE} installé pour l'utilisateur courant.",
        f"  Message        : {config['message']}",
        f"  Intervalle     : {intervalle}",
        f"  Programme      : {programme}",
        f"  Configuration  : {fichier_config}",
        f"  Lancement auto : {emplacement}",
        f"  Journal        : {dossier / FICHIER_JOURNAL}",
    ]
    if not _demarrer(commande):
        lignes.append("Le rappel démarrera à la prochaine ouverture de session.")
    elif _attendre_instance(dossier):
        lignes.append(f"Le rappel est actif : première sonnerie dans {intervalle}.")
    else:
        raise RuntimeError(
            "installation effectuée, mais le rappel n'a pas démarré ; "
            f"consultez le journal {dossier / FICHIER_JOURNAL}"
        )
    if _est_python_microsoft_store():
        lignes.append(
            "ATTENTION : Python du Microsoft Store détecté. Windows peut isoler ses écritures (registre, "
            "AppData) et empêcher le lancement automatique ; en cas de problème, installez Python depuis "
            "https://www.python.org puis relancez l'installation."
        )
    lignes.append(f"Tester maintenant : {_commande_affichable(_commande_console(programme) + ['--test'])}")
    lignes.append(f"Désinstaller      : {_commande_affichable(_commande_console(programme) + ['--desinstaller'])}")
    return lignes


def desinstaller(dossier: Path) -> list:
    """Arrête le rappel, supprime le lancement automatique et le dossier de données."""
    _verifier_dossier_appli(dossier)
    lignes = []
    if _desinscrire_demarrage():
        lignes.append("Lancement automatique supprimé.")
    if arreter_instance(dossier):
        lignes.append("Rappel en cours arrêté.")
    if dossier.exists():
        try:
            _reessayer(lambda: dossier.exists() and shutil.rmtree(str(dossier)))
            lignes.append(f"Dossier supprimé : {dossier}")
        except OSError as exc:
            lignes.append(f"Suppression incomplète de {dossier} ({exc}) : supprimez-le manuellement.")
    lignes.insert(0, f"{NOM_LISIBLE} désinstallé." if lignes else f"{NOM_LISIBLE} n'était pas installé.")
    return lignes


def statut(dossier: Path) -> list:
    config = charger_config(dossier / FICHIER_CONFIG)
    pid = instance_en_cours(dossier)
    if pid is None:
        etat = "non"
    else:
        etat = f"oui (PID {pid})" if pid > 0 else "oui"
    return [
        f"Dossier              : {dossier}" + ("" if dossier.exists() else " (absent : non installé)"),
        f"Lancement automatique : {_demarrage_inscrit() or 'non inscrit'}",
        f"Rappel en cours      : {etat}",
        f"Intervalle           : {formater_minutes(config['intervalle_minutes'])}",
        f"Message              : {config['message']}",
        f"Journal              : {dossier / FICHIER_JOURNAL}",
    ]


# ------------------------------------------------------------------------ boucle principale


def _terminer(_signum, _frame) -> None:
    raise SystemExit(0)  # déroule les « finally » : message ouvert fermé, verrou libéré


def boucle_rappels(config: dict, dossier: Path) -> int:
    """Attendre l'intervalle, sonner, afficher le message, recommencer."""
    verrou = VerrouInstance(dossier / FICHIER_VERROU)
    for _ in range(5):  # un contrôle ponctuel (--statut, installation) peut tenir le verrou un instant
        if verrou.acquerir():
            break
        time.sleep(0.2)
    else:
        journal.info("Une boucle de rappels est déjà active (PID %s) : rien à faire.", verrou.lire_pid())
        return 0
    if os.name != "nt":
        signal.signal(signal.SIGTERM, _terminer)
    try:
        carillon = _preparer_carillon(dossier)
        journal.info(
            "Démarrage v%s (PID %d) : rappel toutes les %s.",
            VERSION,
            os.getpid(),
            formater_minutes(config["intervalle_minutes"]),
        )
        while True:
            attendre_activite(config["intervalle_minutes"] * 60.0)
            try:
                rappeler(config["message"], carillon)
            except Exception:  # un incident ponctuel ne doit pas arrêter les rappels suivants
                journal.exception("Erreur pendant le rappel.")
    except Exception:
        journal.exception("Erreur fatale.")
        return 1
    finally:
        journal.info("Arrêt.")
        verrou.liberer()


# ------------------------------------------------------------------------- ligne de commande


def configurer_journal(fichier: Path | None = None) -> None:
    """Boucle : journal fichier tournant (2 × 256 Kio). Commandes : avertissements en console."""
    journal.setLevel(logging.INFO)
    journal.propagate = False
    for gestionnaire in list(journal.handlers):
        journal.removeHandler(gestionnaire)
        gestionnaire.close()
    if fichier is not None:
        fichier.parent.mkdir(parents=True, exist_ok=True)
        vers_fichier = logging.handlers.RotatingFileHandler(
            str(fichier), maxBytes=256 * 1024, backupCount=1, encoding="utf-8"
        )
        vers_fichier.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        journal.addHandler(vers_fichier)
    if sys.stderr is not None and (fichier is None or sys.stderr.isatty()):
        vers_console = logging.StreamHandler()
        vers_console.setLevel(logging.INFO if fichier is not None else logging.WARNING)
        vers_console.setFormatter(logging.Formatter("%(levelname)s : %(message)s"))
        journal.addHandler(vers_console)


def informer(texte: str) -> None:
    """Compte rendu en console, ou en boîte de dialogue pour un exécutable sans console."""
    if sys.stdout is not None:
        print(texte)
    else:
        afficher_message(NOM_LISIBLE, texte)


def _type_intervalle(texte: str) -> float:
    try:
        return valider_intervalle(texte.strip().replace(",", "."))  # accepte « 1,5 »
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _type_message(texte: str) -> str:
    try:
        return valider_message(texte)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def analyser_arguments(argv: list | None = None) -> argparse.Namespace:
    analyseur = argparse.ArgumentParser(
        prog="rappel_pause",
        description=f"{NOM_LISIBLE} : sonne et affiche un message à intervalle régulier "
        f"(par défaut toutes les {formater_minutes(INTERVALLE_DEFAUT_MIN)}).",
        add_help=False,
    )
    analyseur.add_argument("-h", "--help", action="help", help="affiche cette aide et quitte")
    actions = analyseur.add_mutually_exclusive_group()
    actions.add_argument(
        "--installer",
        action="store_true",
        help="installe pour l'utilisateur courant et démarre le rappel (sans droits administrateur)",
    )
    actions.add_argument("--desinstaller", action="store_true", help="arrête le rappel et supprime l'installation")
    actions.add_argument("--test", action="store_true", help="sonne et affiche le rappel immédiatement, une fois")
    actions.add_argument("--statut", action="store_true", help="affiche l'état de l'installation")
    analyseur.add_argument(
        "--intervalle",
        type=_type_intervalle,
        metavar="MINUTES",
        help=f"minutes entre deux rappels, décimales acceptées (défaut : {INTERVALLE_DEFAUT_MIN})",
    )
    analyseur.add_argument("--message", type=_type_message, metavar="TEXTE", help="texte du rappel")
    analyseur.add_argument(
        "--silencieux", action="store_true", help="aucun compte rendu affiché (déploiement automatisé)"
    )
    analyseur.add_argument(
        "--version", action="version", version=f"%(prog)s {VERSION}", help="affiche la version et quitte"
    )
    arguments = analyseur.parse_args(argv)
    if (arguments.desinstaller or arguments.statut) and (
        arguments.intervalle is not None or arguments.message is not None
    ):
        analyseur.error("--intervalle et --message ne s'appliquent ni à --desinstaller ni à --statut")
    return arguments


def main(argv: list | None = None) -> int:
    arguments = analyser_arguments(argv)
    dossier = dossier_donnees()
    reglages = {}
    if arguments.intervalle is not None:
        reglages["intervalle_minutes"] = arguments.intervalle
    if arguments.message is not None:
        reglages["message"] = arguments.message

    if arguments.installer or arguments.desinstaller or arguments.statut:
        configurer_journal()
        try:
            if arguments.installer:
                lignes = installer(dossier, reglages)
            elif arguments.desinstaller:
                lignes = desinstaller(dossier)
            else:
                lignes = statut(dossier)
        except (OSError, RuntimeError) as exc:
            if arguments.silencieux:
                journal.error("%s", exc)
            else:
                informer(f"Erreur : {exc}")
            return 1
        if not arguments.silencieux:
            informer("\n".join(lignes))
        return 0

    config = charger_config(dossier / FICHIER_CONFIG)
    config.update(reglages)  # réglages ponctuels : non enregistrés
    if arguments.test:
        configurer_journal()
        with tempfile.TemporaryDirectory() as temporaire:
            return 0 if rappeler(config["message"], _preparer_carillon(Path(temporaire))) else 1
    configurer_journal(dossier / FICHIER_JOURNAL)
    try:
        return boucle_rappels(config, dossier)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
