# yt-digest

Codzienny digest wartościowych filmów z YouTube, poza algorytmem.

**Jak to działa:**
1. GitHub Actions codziennie o ~4:30 uruchamia `fetch.py`. Skrypt zbiera nowe filmy z kanałów z `channels.txt` (przez RSS) i z wyszukiwań z `queries.txt`, odrzuca Shortsy i transmisje, próbuje pobrać transkrypty i zapisuje wynik w `data/latest.json`.
2. O 6:00 Claude czyta `data/latest.json` i `profil.md`, ocenia filmy i publikuje digest.

**Co możesz edytować (bezpośrednio na GitHubie, ołówek ✏️):**
- `channels.txt`: kanały do śledzenia
- `queries.txt`: tematy do odkrywania nowych kanałów
- `profil.md`: co chcesz, czego nie, zablokowane kanały

**Ręczne uruchomienie:** zakładka Actions → „Zbierz filmy” → Run workflow.

**Sprawdzenie rozpoznanych kanałów:** `data/channels_resolved.json`. Jeśli jakiś kanał rozpoznał się źle, wpisz w `channels.txt` jego link `https://www.youtube.com/@handle` zamiast nazwy.
