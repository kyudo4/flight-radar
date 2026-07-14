# ✈️ Flight Radar — monitor lotów premium do Azji

Automatyczny monitoring okazji business/first do Azji (priorytet: Bangkok, wrzesień 2026, one-way).
Działa co godzinę w tle na Twoim Macu. Zero zależności — czysty Python systemowy.

## Jak to działa

Co godzinę skrypt:
1. **Odbiera Twój feedback** z Telegrama (kliknięcia przycisków) i aktualizuje preferencje.
2. **Skanuje Google Flights na żywo** (≈22 zapytania/h): business WAW/GDN/POZ/OSL/ARN/CPH/VIE/BUD/MXP → BKK + rotacyjnie pozostałe kierunki, na rotujące daty 1–14.09.2026. Google Flights agreguje praktycznie cały rynek (linie + OTA), więc to jest źródło całościowe — pokrywa to, co widać w Skyscannerze/Kayaku. Do oceny dokłada własny sygnał Google "ceny niższe niż zwykle".
3. **Skanuje deal-blogi** (Secret Flying, Fly4Free.pl/.com, Travel Dealz, LoyaltyLobby, OMAAT, VFTW) i filtruje: kierunek Azja + klasa business/first + wylot z Europy. Odrzuca oferty za mile/punkty i z wylotem spoza Europy. Blogi łapią error fares i promocje z kodami, których wyszukiwarki nie oznaczą.
4. **(Opcjonalnie) Odpytuje Amadeus API** — drugie, oficjalne źródło twardych cen z GDS.
4. **Ocenia gwiazdkami** (cena vs budżet, linia priorytetowa, error fare, mediana historyczna, przesiadki, czas lotu) i wysyła na Telegram **tylko oferty ≥ 4⭐** (próg w config: `notify.min_stars`).
5. **Nie wysyła duplikatów.** Jeśli cena znanej oferty spadnie o >7% — wysyła ponownie z dopiskiem 📉.
6. **Bonus:** jeśli SIN/KUL/SGN + tani dolot wychodzi taniej niż BKK — zgłasza alternatywę.

## Uruchomienie krok po kroku

### 1. Telegram (wymagane do powiadomień; obsługuje wielu odbiorców)

1. Na Telegramie napisz do **@BotFather** → `/newbot` → nadaj nazwę (np. `FlightRadarBartosz_bot`).
2. Skopiuj token (wygląda jak `1234567:AAF...`) i wklej do `config.json` → `telegram.bot_token`.
3. **Każdy odbiorca** (Ty, kumpel…) otwiera link `t.me/<username_bota>` i klika **Start**.
4. W terminalu: `cd ~/Apki/flight-radar && venv/bin/python monitor.py setup` — wszyscy, którzy napisali do bota, zostają zarejestrowani jako odbiorcy (`telegram.chat_ids`).
5. Test: `venv/bin/python monitor.py test` — przykładowe powiadomienie przyjdzie do wszystkich.

Przekazanie kumplowi = wysłanie mu linku `t.me/<username_bota>` (+ ewentualnie adres dashboardu). Przyciski feedbacku działają dla każdego odbiorcy, system uczy się ze wszystkich kliknięć.

Bez tokenu monitor działa "na sucho" i tylko loguje znaleziska do `state/log.txt`.

### 2. Automat co godzinę

```
cd ~/Apki/flight-radar && ./install.sh
```

Wyłączenie: `launchctl unload ~/Library/LaunchAgents/com.bartosz.flight-radar.plist`

### 3. Amadeus — ⚰️ NIEDOSTĘPNE (portal self-service zamknięty)

Amadeus **zamknął darmowy portal API dla deweloperów 17.07.2026** (nowe rejestracje wstrzymane od 12.2025). Kod obsługi Amadeusa zostaje w monitorze uśpiony — gdyby kiedyś wrócił odpowiednik, wystarczy wkleić klucze do `config.json`. Nie jest potrzebny: Google Flights pokrywa pełne dane rynkowe na żywo.

### 4. Travelpayouts / Aviasales (skonfigurowane — żywe ceny dolotów)

Token jest wpisany w `config.json`. **Uwaga: to API nie ma już danych business** (sprawdzone 07.2026: "Only economy trip class is supported"), więc nie jest źródłem ofert premium. Służy do logiki hubowej: gdy business do SIN/KUL/SGN wychodzi taniej niż do BKK, monitor dolicza **realną cenę dolotu economy do Bangkoku** (cache 12 h, `state/feeder.json`) zamiast szacunku i zgłasza alternatywę.

## Strona publiczna

Dashboard jest publikowany automatycznie po każdym przebiegu na
**https://kyudo4.github.io/flight-radar/** (repo `kyudo4/flight-radar`, katalog `site/`).
Uwaga: strona jest publiczna — widzi ją każdy z linkiem. Bez tokenów/kluczy — tylko oferty.

## Strona z ofertami (dashboard)

Każdy przebieg zapisuje wszystkie przeanalizowane oferty do bazy i odświeża stronę
**`~/Apki/flight-radar/dashboard.html`** — otwórz ją w przeglądarce (albo `python3 monitor.py dashboard`).
Filtry: gwiazdki / kierunek / źródło / tylko wysłane na Telegram; sortowanie po cenie i dacie.
Strona pokazuje też minimalną cenę historyczną oferty i znaczek 📊, gdy Google ocenia ceny jako niższe niż zwykle.

## Uczenie się Twojego gustu

Każde powiadomienie ma przyciski: **👍 Kupiłbym · 💸 Za drogo · 🙅 Nie interesuje · ⏱ Za długo · ✈️ Zła linia**.

- 2× "zła linia" dla tego samego przewoźnika → linia trwale pomijana,
- 2× "za drogo" → budżet obniża się poniżej najtańszej odrzuconej ceny,
- 2× "za długo" → limit czasu podróży spada poniżej najkrótszego odrzuconego lotu,
- "kupiłbym" → linia dostaje bonus gwiazdki w przyszłości.

Podgląd wyuczonych preferencji: `python3 monitor.py prefs`. Reset: skasuj `state/prefs.json`.

## Dodawanie lotnisk / kierunków

Wszystko w `config.json`:
- `origins` — kody IATA portów wylotu (Amadeus),
- `destinations.secondary` — dodatkowe kierunki,
- `feeds` — dołóż dowolny RSS (dla stron blokujących boty użyj wzorca Google News jak przy Secret Flying),
- słowa kluczowe miast (dla ofert z blogów) — listy `ORIGIN_CORE` / `ORIGIN_EU` na górze `monitor.py`.

## Uczciwe ograniczenia

- **Google Flights jest skanowany nieoficjalnie** (biblioteka `fast-flights` + obejście unijnej ściany zgody). Działa i daje pełne dane rynkowe, ale gdy Google zmieni HTML lub zacznie blokować, monitor zaloguje to w `state/log.txt` i przełączy się na blogi/Amadeus. Naprawa zwykle = `venv/bin/pip install -U fast-flights`. Skyscanner/Kayak/Momondo osobno nie są potrzebne — Google Flights pokrywa te same taryfy; ich wewnętrzne API są martwe lub za captchą (sprawdzone).
- **"Przejście do ekranu płatności"** nie jest w pełni automatyczne: oferty Amadeus to ceny na żywo z GDS (weryfikacja z natury), oferty z blogów są sprawdzane pod kątem martwego linku i dopisku "expired". Finalne kliknięcie do kasy pozostaje po Twojej stronie.
- Oferty z blogów nie zawsze dotyczą dokładnie 1–14.09.2026 — daty z artykułu sprawdź w linku. Amadeus pilnuje dat ściśle.
- Typ fotela to szacunek (mapa linia+samolot → produkt, np. QR 77W → Qsuite).

## Pliki

| Plik | Rola |
|---|---|
| `monitor.py` | cała logika |
| `gflights.py` | źródło Google Flights (tfs + obejście consent wall) |
| `config.json` | trasy, budżety, klucze, progi |
| `state/log.txt` | log przebiegów |
| `state/seen.json` | deduplikacja + historia cen ofert |
| `state/prefs.json` | wyuczone preferencje |
| `state/prices.json` | historia cen Amadeus (mediana rynkowa) |
