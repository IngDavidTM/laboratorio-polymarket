# Laboratorio Polymarket (versión desplegada)

Parte ligera del laboratorio de inversión simulada. **Solo dinero virtual y datos públicos.**

- `collector/run.py`: se ejecuta cada día hábil en GitHub Actions (gratis en repositorios públicos).
  Descarga mercados de Polymarket, detecta saltos de probabilidad, anota señales de las hipótesis
  F3-PM1..PM5 (reglas congeladas el 2026-11-04) y las resuelve con precios de Yahoo o Stooq.
- `site/`: web estática en GitHub Pages con tres pestañas: Polymarket hoy, Prueba en vivo y Conocimiento.
- `site/data/*.json`: datos acumulados (los escribe el recolector; no editarlos a mano).

Ejecutar en local: `pip install -r collector/requirements.txt && python collector/run.py site/data`,
luego `python -m http.server -d site` y abrir http://localhost:8000.
