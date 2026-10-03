# Wise, mon assistant personnel

Wise est un assistant qui discute avec vous dans le terminal. Il s'appuie sur l'API Claude
d'Anthropic, et sa personnalité est décrite dans [personnalite.md](personnalite.md) : modifiez
ce fichier pour changer sa façon de parler, puis relancez Wise.

Ce qu'il sait faire :

- répondre en français, de façon directe et chaleureuse ;
- chercher sur le web pour l'actualité ou les chiffres récents, en citant ses sources ;
- donner la date et l'heure exactes ;
- se souvenir de vous d'une conversation à l'autre (prénom, métier, projets, préférences).

## Installation

Il faut Python 3.10 ou plus récent ([python.org](https://www.python.org/downloads/) ; sous
Windows, cochez « Add python.exe to PATH » pendant l'installation).

1. Créez une clé API sur [console.anthropic.com](https://console.anthropic.com/settings/keys).
   L'API est payante à l'usage : ajoutez un peu de crédit dans « Billing ».

2. Ouvrez un terminal dans le dossier `mon-ia` et installez la dépendance :

   ```
   python -m pip install -r requirements.txt
   ```

3. Donnez votre clé à Wise.

   Windows (PowerShell), une fois pour toutes, puis fermez et rouvrez le terminal :

   ```
   setx ANTHROPIC_API_KEY "sk-ant-..."
   ```

   macOS / Linux (ajoutez la ligne à `~/.bashrc` ou `~/.zshrc` pour la garder) :

   ```
   export ANTHROPIC_API_KEY="sk-ant-..."
   ```

## Utilisation

```
python wise.py
```

Écrivez votre message puis Entrée. Commandes disponibles :

| Commande   | Effet                                         |
|------------|-----------------------------------------------|
| `/aide`    | affiche l'aide                                |
| `/memoire` | montre ce que Wise a retenu sur vous          |
| `/oublier` | efface toute sa mémoire                       |
| `/nouveau` | commence une nouvelle conversation            |
| `/quitter` | quitte Wise (Ctrl+C marche aussi)             |

La mémoire est enregistrée dans `~/.wise/memoire.json` (sous Windows :
`C:\Users\<vous>\.wise\memoire.json`). C'est un simple fichier texte que vous pouvez lire ou
modifier.

## Réglages facultatifs

Variables d'environnement :

- `WISE_MODELE` : le modèle Claude utilisé (par défaut `claude-opus-5-5`) ;
- `WISE_EFFORT` : `low`, `medium` (par défaut) ou `high`. Plus bas, Wise répond plus vite et
  coûte moins cher ; plus haut, il réfléchit davantage ;
- `WISE_MEMOIRE` : un autre emplacement pour le fichier de mémoire.

Si Claude refuse une demande pour des raisons de sécurité, l'API la confie automatiquement à un
autre modèle Claude (option « fallbacks ») avant d'abandonner.

## Tests

```
python -m unittest discover -s tests
```

Les tests n'utilisent pas l'API et ne coûtent rien.
