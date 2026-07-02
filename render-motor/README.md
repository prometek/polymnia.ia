# render-motor — POC rendu Remotion

POC pour **lever la décision bloquante #1** du dossier d'archi Polymnia : *Remotion confirmé comme moteur de rendu ?*
Objectif : mesurer **coût + temps par minute** de vidéo rendue, en conteneur headless.

Cible mesurée : **1080p @ 60fps**, multi-scène motion design, audio stub (le TTS est un axe de coût séparé).

## Lancer la mesure (Docker)

```bash
docker build -t polymnia-render-poc .

# Mesure 60s @ 60fps, concurrency auto :
docker run --rm -v "$PWD/out:/app/out" polymnia-render-poc

# Variantes :
docker run --rm -e DURATION_S=30 -e CONCURRENCY=4 -e RATE_USD_PER_HR=0.39 \
  -v "$PWD/out:/app/out" polymnia-render-poc
```

Résultats : table en stdout + `out/metrics.json`.

## Preuve de diversité — styles visuels (décision #2)

Rend le même contenu sous 5 **directions artistiques** (whiteboard, kawaii, aquarelle, retro, tech) + extrait des frames preuve :

```bash
docker run --rm -e DURATION_S=8 -v "$PWD/out:/app/out" polymnia-render-poc node scripts/render-pattes.mjs
# -> out/<style>.mp4 + out/<style>-s1..s4.png  pour chaque style
# styles au choix : -e PRESETS=whiteboard,tech
```

Styles visuels (axe dominant, ADR-10/11) : `src/styleSpace/visualStyles.tsx` — chaque style = police + palette + filtres SVG + backdrop + panels themés.
Axes structurels secondaires : `src/styleSpace/{types,v0,resolve}.tsx`.

## En local (sans Docker)

```bash
npm install
DURATION_S=60 npm run measure   # rend out/video.mp4 + out/metrics.json
npm run studio                  # prévisualiser la compo dans Remotion Studio
```

## Variables

| Var | Défaut | Rôle |
|---|---|---|
| `DURATION_S` | 60 | durée vidéo rendue (proxy minute) |
| `CONCURRENCY` | auto | nb d'onglets Chrome // (levier coût/temps) |
| `RATE_USD_PER_HR` | 0.197 | tarif conteneur pour extrapoler le $/min |

## Métrique clé

`wallPerVideoMinuteSeconds` = secondes de rendu par minute de vidéo → alimente le SLO rendu
et le coût `usdPerVideoMinute = wallPerVideoMin/3600 × tarif`.
