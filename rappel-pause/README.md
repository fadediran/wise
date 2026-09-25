# Rappel de pause active

Toutes les **2 heures**, l'ordinateur sonne puis affiche au premier plan :

> **C'est l'heure de se lever pour marcher !**

Objectif : sensibiliser aux risques de la position assise prolongée en incitant à une courte marche régulière.

- **Windows 10/11, macOS, Linux** : un seul fichier Python (3.8 ou plus récent), aucune dépendance à installer.
- Installation **par utilisateur, sans droits administrateur** ; le rappel démarre tout seul à chaque ouverture de session.
- Les rappels ne s'empilent jamais ; un ordinateur mis en veille plus de 5 minutes repart pour 2 heures pleines.

## Installation

**Prérequis : Python 3.8 ou plus récent.**

| Système | Python |
|---|---|
| Windows | installeur de [python.org](https://www.python.org/downloads/) (éviter la version du Microsoft Store, voir [Limites](#limites-connues)). Sans Python : voir [Déploiement](#déploiement-sur-un-parc-windows-sans-python). |
| macOS | `python3` : proposé automatiquement (outils de ligne de commande Xcode) au premier lancement, ou [python.org](https://www.python.org/downloads/). |
| Linux | `python3`, déjà présent. Fenêtre : `zenity` (GNOME, Xfce…) ou `kdialog` (KDE) ; son : `paplay`, `pw-play` ou `aplay`. Généralement déjà installés. |

### Windows

Copier le dossier `rappel-pause` sur le poste, puis **double-cliquer sur `installer_windows.bat`**.

### macOS et Linux

```sh
python3 rappel-pause/rappel_pause.py --installer
```

Le programme se copie dans le dossier de données de l'utilisateur : le dossier d'origine peut ensuite être supprimé.
L'installation affiche un compte rendu et démarre le rappel immédiatement : **première sonnerie 2 heures plus tard**.

## Utilisation

Sous Windows, remplacer `python3` par `py`.

| Commande | Effet |
|---|---|
| `python3 rappel_pause.py --test` | sonne et affiche le rappel **immédiatement** (vérification du son et de la fenêtre) |
| `python3 rappel_pause.py --statut` | lancement automatique inscrit ou non, rappel en cours (PID), réglages, journal |
| `python3 rappel_pause.py --installer --intervalle 90 --message "Bougez !"` | modifie les réglages (réinstallation sans risque, redémarre le rappel) |
| `python3 rappel_pause.py --desinstaller` | arrête le rappel et supprime tout (Windows : `desinstaller_windows.bat`) |

`--intervalle` est en minutes, décimales acceptées (`1,5`). Les réglages sont conservés dans `config.json` (dossier de
données) ; après une modification à la main de ce fichier, relancer `--installer` pour les appliquer.

## Comportement précis

1. Premier rappel 2 heures après l'ouverture de session (ou l'installation).
2. À l'échéance : carillon d'environ 1,6 s, puis message au premier plan.
3. Le délai suivant démarre **à la fermeture du message** : si l'utilisateur est absent, un seul message l'attend à
   son retour.
4. Une mise en veille de plus de 5 minutes (portable refermé, pause déjeuner…) remet le compte à rebours à zéro au
   réveil, au lieu de sonner aussitôt. Une veille plus courte compte comme du temps assis.
5. Une seule boucle de rappels par utilisateur (verrou fichier) ; chaque utilisateur d'un même poste a la sienne.

Le carillon est synthétisé par le programme : il est identique partout et ne dépend pas du thème sonore du système.

## Fichiers installés

| | Windows | macOS | Linux |
|---|---|---|---|
| Dossier de données | `%LOCALAPPDATA%\RappelPause` | `~/Library/Application Support/RappelPause` | `~/.local/share/rappel-pause` |
| Lancement automatique | registre `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, valeur `RappelPause` | agent `~/Library/LaunchAgents/local.rappel-pause.plist` | `~/.config/autostart/rappel-pause.desktop` |
| Fenêtre | boîte de dialogue Windows | boîte de dialogue macOS | zenity, kdialog, Tk ou notification |
| Son | winsound | afplay | paplay, pw-play ou aplay |

Le dossier de données contient le programme, `config.json`, le journal `rappel_pause.log` (2 × 256 Kio au plus),
le verrou `instance.lock` et le carillon `carillon.wav`.

## Déploiement sur un parc Windows sans Python

Le programme se compile en un exécutable autonome `RappelPause.exe` :

- chaque exécution du workflow GitHub Actions **Rappel de pause** le produit (artefact `RappelPause-windows`) ;
- ou, sur un poste Windows disposant de Python :
  ```bat
  py -m pip install pyinstaller
  py -m PyInstaller --onefile --noconsole --name RappelPause rappel_pause.py
  ```
  → `dist\RappelPause.exe`.

Sur chaque poste, pour chaque utilisateur : `RappelPause.exe --installer` (compte rendu dans une fenêtre). Pour un
script de connexion, une GPO ou Intune : `RappelPause.exe --installer --silencieux` (code de sortie 0 en cas de
succès, 1 sinon ; la réinstallation est sans risque). Désinstallation : `RappelPause.exe --desinstaller`.

Un exécutable non signé déclenche SmartScreen, voire l'antivirus : le signer avec le certificat de l'organisation.

## Dépannage

1. `--statut` indique si le lancement automatique est inscrit et si le rappel tourne.
2. Le journal (`rappel_pause.log`, dans le dossier de données) trace chaque démarrage, rappel, veille et erreur.
3. Linux sans fenêtre ni son : `sudo apt install zenity pulseaudio-utils` (ou l'équivalent de la distribution).
4. Windows, rien à l'ouverture de session : vérifier que « RappelPause » est activé dans Gestionnaire des tâches →
   Applications de démarrage.

## Limites connues

- L'inactivité du clavier et de la souris n'est pas détectée : seule la veille remet le compte à rebours à zéro. Une
  session simplement verrouillée continue le décompte (le carillon peut alors retentir en l'absence de l'utilisateur).
- Le message s'affiche aussi pendant une présentation en plein écran ou un partage d'écran.
- Python du Microsoft Store : Windows isole les écritures de cette version (registre, AppData), ce qui peut empêcher le
  lancement automatique. Préférer python.org ou l'exécutable autonome ; l'installation affiche un avertissement.

## Tests

```sh
python3 -m unittest discover -s rappel-pause/tests -v
# avec l'installation réelle dans des dossiers temporaires (peut sonner et ouvrir le message sous Windows et macOS) :
RAPPEL_PAUSE_TESTS_INTEGRATION=1 python3 -m unittest discover -s rappel-pause/tests -v
```

Le workflow `.github/workflows/rappel-pause.yml` exécute ces tests sous Windows, macOS et Linux (Python 3.8 et la
dernière version), puis construit et vérifie `RappelPause.exe`.
