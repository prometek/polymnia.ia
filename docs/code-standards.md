# Code Standards — Polymnia

Référence des conventions du projet. Règles courtes, impératives. Le fichier n'explique pas, il prescrit.

**Stack** : Backend Python 3.14 (uv) — pipeline IA + API FastAPI + PostgreSQL via **SQLModel** (driver psycopg 3). Rendu : TypeScript 5.7 + React 19 + Remotion 4 (`render-motor`). Docker (worker de rendu + Postgres local).

---

## Général

### Architecture & responsabilité
- Garder les modules petits et à responsabilité unique.
- Pas de logique métier dans les routes/handlers — déléguer à une couche service.
- Séparer strictement I/O et logique pure : la logique pure doit être testable sans filesystem, DB, ni réseau.
- Composition plutôt qu'héritage. Pas de god object.
- **Cohérence inter-scènes = état global** : numérotation, registre de kickers, références croisées se résolvent dans l'étage `outline` (vue globale), jamais dans le `fill` isolé par scène.

### Robustesse
- Corriger la cause racine, jamais empiler des workarounds.
- Aucun `any`, `as`, `@ts-ignore`, `except: pass`, `unwrap` implicite — sauf justification explicite en commentaire.
- Valider toute entrée externe (HTTP, fichier, env, sortie LLM) aux frontières, avant la logique métier.
- Ne jamais faire confiance à l'input utilisateur ni à la sortie d'un LLM — suspects jusqu'à validation.
- Erreurs typées explicitement. Pas de magic numbers : constantes nommées.

### Tests
- Toute logique non triviale a des tests : happy path + un cas d'erreur principal par fonction publique.
- Tests déterministes : pas d'ordre d'exécution, pas de temps réel non mocké, pas d'appel réseau externe.
- Pas de mock de la DB en intégration — DB de test réelle.

### Lisibilité
- Commentaires pour le *pourquoi* non-évident, jamais le *quoi*.
- Nommage explicite : pas de `tmp`, `data2`, `obj`, `foo`.
- Fonction > ~50 lignes ou composant > ~150 lignes → suspecter une responsabilité multiple.

### Sécurité
- Vérifier authentification ET ownership avant toute lecture/mutation de ressource sensible (multi-tenant par `user_id`).
- Aucun secret en clair dans le repo. Pas de logs contenant PII ou secrets.

---

## Python

- **uv** pour tout : dépendances, venv, lock. Jamais `pip install` direct dans le venv projet.
- **Toute dépendance utilisée est déclarée** dans `pyproject.toml` et figée dans `uv.lock` — pas de dépendance installée à la main hors manifeste.
- Cible **Python 3.14**. `requires-python` et `.python-version` alignés.
- Type hints sur toute fonction publique (signatures + retour).

### Ruff (lint + format)

- Lint **et** formatage par Ruff. Pas de Black, isort, ni flake8. Config sous `[tool.ruff]` du `pyproject.toml`.
- `target-version = "py314"`, `line-length = 100`.
- Familles de règles activées (`[tool.ruff.lint] select`) : `E`, `W` (pycodestyle/PEP 8), `F` (pyflakes), `I` (tri des imports), `UP` (pyupgrade), `B` (bugbear), `SIM` (simplify), `BLE` (blind-except), `RUF`.
- `print` interdit partout **sauf** dans les scripts CLI du pipeline (`per-file-ignores` : `"pipeline/*" = ["T201"]`) — stdout y est la sortie chaînable.
- `ruff format` est la source de vérité du layout : aucune mise en forme manuelle qui le contredit. `# noqa: <CODE>` toujours avec le code précis + raison si non-évident.
- pre-commit : `ruff check --fix` + `ruff format` (auto-fix). CI : `ruff check` + `ruff format --check` (blocants).

### mypy (strict)

- `strict = true` sous `[tool.mypy]`, `python_version = "3.14"`. Ne pas réactiver les flags un par un.
- Conséquence directe : pas de générique nu — `dict` → `dict[str, Any]`, `list` → `list[T]` ; pas d'`Optional` implicite ; pas de retour `Any` là où un type concret est déclaré.
- `# type: ignore[<code>]` toujours avec le code d'erreur précis et une justification ; jamais nu. `Any` est un choix explicite, pas un défaut pour faire taire mypy.
- Layout à imports frères (`pipeline/`) : `explicit_package_bases = true` + `mypy_path = "pipeline"` ; `ignore_missing_imports = true` toléré pour les libs sans stubs (psycopg, urllib).
- **Adoption progressive** : strict est la cible partout ; `api/` est conforme, le legacy (`pipeline/`, `db.py`) est typé au fil du temps. mypy en CI uniquement (trop lent en pre-commit), blocant.
- `logging` structuré, jamais `print` en code applicatif (les scripts CLI du pipeline peuvent écrire sur stdout/stderr — stdout réservé à la sortie chaînable, logs sur stderr).
- Configuration via **pydantic-settings** / `os.getenv` + `dotenv` aux frontières, jamais d'accès env dispersé dans la logique.
- Imports absolus ; les modules `pipeline/` s'importent en frères (le dossier du script est sur `sys.path`).

---

## Pipeline de génération (IA)

- **L'IA SÉLECTIONNE, ne génère pas la structure** (ADR-04) : layouts/styles viennent d'un catalogue fini, jamais inventés à la volée.
- Le choix de composant = **function call contraint** (ADR-09) : chaque layout est un tool, le LLM remplit ses `props`. Les `*_ref` d'asset sont des `enum` des ids du kit → impossible d'inventer un asset.
- **Valider toute sortie LLM** contre le catalogue/schéma avant persistance (id de layout, asset_refs).
- Pipeline en **étapes découplées** (ADR-07) : `plan → outline → fill (par scène) → tts+alignement`. Chaque étape a un artefact reprenable.
- **Fill isolé par scène** : une scène se régénère seule (édition scope-scène, US-06) sans toucher les autres. Ne jamais coupler la génération de plusieurs scènes dans un seul appel.
- **Scène maigre** (ADR-05) : `{type, props}` + références au kit (`asset_refs`), jamais le style/les assets copiés dedans.
- **Timing piloté par la voix** (ADR-08) : durée et `startFrame` dérivent de l'audio (alignement forcé), pas de durées en dur.
- Prompts et schémas de tools versionnés avec le code ; un changement de catalogue = un changement de code testé.

---

## FastAPI

- **Couches** : `api/main.py` (routes, validation, délégation — aucune logique métier) → `api/service.py` (orchestration pipeline + persistance) → `api/db.py` (accès données SQLModel) ; modèles de table dans `api/models.py`, engine/session dans `api/session.py`. Respecter cette séparation.
- Réponses typées via `response_model` (schémas Pydantic `Read`), jamais une ligne DB brute renvoyée telle quelle (pas de fuite de colonnes type `user_id`). `status_code` explicite (`202` job lancé, `201` création, `200` lecture, `404/409` erreurs).
- Fetch + 404 / récupération de ressource via **dependencies `Depends`** (cache par requête → une seule lecture même si plusieurs deps en dépendent), pas de helpers répétés dans les handlers.
- Toute mutation vérifie l'existence de la ressource (404) et, à terme, l'ownership.
- Erreurs via `HTTPException` avec message explicite et stable (matchable côté client).
- **Tâches longues (génération, rendu) en `BackgroundTasks`** : l'orchestration + les transitions de statut (`generating`/`rendering`/`ready`/`error`) vivent dans `service.py`, pas dans la route. Passer à une vraie file (queue + workers) quand la charge le justifie — `BackgroundTasks` n'est ni durable ni monitoré.
- Démarrage via **lifespan** (`@asynccontextmanager`), pas `@app.on_event` (déprécié).
- Endpoints `def` (exécutés en threadpool, I/O SQLModel/psycopg sync) — ne pas mélanger avec de l'I/O async dans le même handler.

---

## SQLModel (accès données)

- ORM par défaut : **SQLModel** (sur SQLAlchemy + psycopg 3). Pas de SQL brut pour le CRUD ; SQL brut uniquement si non exprimable par l'ORM, justifié en commentaire.
- Modèles de table (`table=True`) dans `api/models.py` = **source de vérité du schéma**. Types non triviaux via `Field(sa_column=Column(...))` : `JSONB` (props/timing/cosmetic/style/meta/asset_refs), `UUID` PK avec `server_default text("gen_random_uuid()")`, `TIMESTAMPTZ` (`DateTime(timezone=True)`), FK avec `ON DELETE CASCADE` où pertinent, `UniqueConstraint`.
- URL avec driver explicite : `postgresql+psycopg://...` (psycopg 3). `api/session.py` normalise `postgresql://` → `postgresql+psycopg://`.
- Un `engine` par process ; une `Session` par unité de travail. Transaction = périmètre de la session (`with Session(engine) as s: ... s.commit()`), rollback auto sur exception.
- Requêtes via `select(...)` + `session.exec(...)` ; tri/comparaisons sur colonne via `col(...)` (type-clean mypy). Lecture unique : `.one_or_none()` + check `None`, jamais `.one()`.
- Frontière dict : `api/db.py` convertit les modèles ORM → dicts car le pipeline (fill/tts/pack_render) est dict-based. Les schémas API exposés restent des modèles `Read` dans `main.py`, jamais un modèle de table renvoyé directement.
- Schéma appliqué au DB via Alembic en prod ; `SQLModel.metadata.create_all()` réservé au POC/tests (voir ci-dessous).

---

## PostgreSQL

- `snake_case` partout (tables, colonnes, contraintes, index).
- Clés étrangères `{table}_id` ; index sur toutes les FK et les colonnes filtrées fréquemment.
- **Clé primaire UUID** par défaut (`gen_random_uuid()`), sauf intérêt fonctionnel à un entier (ordre, table d'association).
- Toute table : `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` (+ `updated_at` si mutable). Toujours `TIMESTAMPTZ`, jamais `TIMESTAMP`.
- **JSONB** pour les structures souples versionnées avec le rendu (`props`, `timing`, `cosmetic`, `style`) ; colonnes dédiées pour ce qui est requêté/contraint.
- **Snapshot figé** (ADR-06) : une vidéo référence une `brand_kit_version`, jamais le kit mutable → modifier un kit ne casse pas les vidéos existantes. Un changement de cosmetic/style/assets = nouvelle version.
- **Pas de blobs lourds en base** : audio/vidéo/assets en object storage (S3/CDN), seule la référence (chemin/URL) en base. Servir les MP4 via stockage, pas depuis le filesystem applicatif (cible).
- Layouts / espace de styles **pas en base** : versionnés avec le code de rendu.

---

## Base de données & migrations

- Schéma actuellement dérivé des modèles SQLModel via `SQLModel.metadata.create_all()` (`session.init_db()`, ne crée que les tables manquantes). Acceptable au stade POC.
- Dès que le schéma évolue en prod : passer sous **Alembic** (migrations atomiques, relues, `upgrade head` en CI avant déploiement, pas de DDL manuel en prod).
- Seed (kits de référence) via un chemin idempotent séparé, jamais mélangé à la logique de schéma.

---

## TypeScript / Remotion

> **Remotion = worker de rendu headless, pas une app React.** Ce code tourne dans un conteneur sans navigateur interactif, piloté par CLI (`remotion render`). Les conventions d'app React **ne s'appliquent pas** : pas de state client (`useState`/`useReducer` pour l'UI), pas de data-fetching (TanStack Query, `fetch` au runtime), pas de formulaires (react-hook-form), pas de routing, pas d'effets de bord réseau. React n'est ici qu'un moteur de templating déterministe : composants = fonctions pures `props → frames`. Le seul hook applicatif autorisé est `useTheme` (lecture du thème). `useCurrentFrame`/`useVideoConfig`/`spring`/`interpolate` de Remotion sont la base de l'animation.

- `tsconfig` **strict**. Aucun `any` ni `as` non justifié ; `tsc --noEmit` passe avant commit.
- `type` pour les formes de données (props, scène) ; `interface` réservée aux contrats étendus.
- **Composants theme-generic** : le contenu vient des `props` (sortie pipeline), le look vient du thème (`styleSpace/visualStyles`). Un composant ne hardcode ni texte ni couleur — palette/police via `useTheme`, cosmetic du kit appliqué par-dessus (ADR-10).
- **Rendu déterministe** (ADR-01/11) : filtres SVG/CSS + assets bakés, **aucune génération d'image/IA au rendu**. Pas d'aléatoire non seedé (`random()` Remotion uniquement).
- Une scène = `{type, props}` ; `type` = nom de composant, routé par `SceneByLayout`. Pas de logique de contenu dans le composant.
- Constantes de timing partagées avec le backend (`FPS`, `LEAD`) : garder les valeurs synchronisées des deux côtés.
- Assets résolus au packing (id → emoji/fichier), jamais d'id de kit brut dans le rendu.

---

## Docker

- Image de rendu **multi-stage**, base slim (`node:*-bookworm-slim` + Chrome headless + ffmpeg), utilisateur **non-root**.
- `.dockerignore` exclut `node_modules`, `out/`, `.git`.
- Postgres local via conteneur dédié + **volume persistant** (jamais les données dans la couche image).
- Un service = un conteneur ; pas de logique applicative dans l'`ENTRYPOINT`.

---

## Git & CI

- **Conventional Commits** : `type(scope?): description` à l'impératif minuscule sans point. Types : `feat`, `fix`, `chore`, `refactor`, `docs`, `test`, `perf`, `build`, `ci`.
- Branche principale `main` ; pas de commit direct → tout en PR. Branches `feat/...`, `fix/...`, `refactor/...`.
- **pre-commit** (`.pre-commit-config.yaml`) : format auto-fix (Ruff format, Prettier/Biome), lint, **détection de secrets** (gitleaks), validation du message de commit, fichiers volumineux. Filet rapide (quelques secondes).
- **CI bloquante** (GitHub Actions) sur PR + `main` : lint, format check, type check (mypy, `tsc --noEmit`), tests, build image. Pas de merge si une étape échoue. Cache uv/npm.
- Type check, tests et build **hors pre-commit** (lents) → en CI.

---

## Gestion des secrets

- Secrets dans `.env` (local), **jamais committé** (`.gitignore`). Clés API (Mistral), `DATABASE_URL` hors du code.
- Aucun secret en dur dans le code, les commentaires, ou les logs.
- Secrets partagés / prod : chiffrés au repo via **SOPS + age**, jamais en clair.

---

## Organisation des fichiers

```
backend/
  pipeline/        # moteur de génération (1 fichier par étape) + utils + layout_store
  api/             # FastAPI : main.py (routes + schémas + deps) · service.py (orchestration + persistance) · db.py (accès SQLModel) · models.py (tables SQLModel) · session.py (engine + session)
  inputs/          # données d'entrée (brand kits, texte source, assets sources)
  out/             # artefacts régénérables (audio, json pipeline) — gitignored
  run.sh           # pipeline complet CLI
  pyproject.toml · uv.lock · .python-version · .env (gitignored)

render-motor/
  src/             # Remotion : Root.tsx · PolymniaVideo.tsx · components/ · styleSpace/
  public/          # assets bakés servis au rendu (audio, logo, fonds) — gitignored
  out/             # MP4 rendus — gitignored
  Dockerfile · package.json
```

- Code en **anglais** (identifiants, commentaires, docstrings). Les docs projet et le contenu de marque peuvent rester dans la langue cible.
- Un fichier = une responsabilité : étape de pipeline, composant de rendu, ou couche API.
- Artefacts régénérables (`out/`, `public/audio/`, `render-input*.json`) toujours gitignorés.
