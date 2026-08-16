KOTONE — refaktor AOTY
=====================

UKŁAD PLIKÓW
------------
bot.py
  Minimalny entry point: Discord, rejestracja komend, guild sync i start monitora.

settings.py
  Jedno źródło config.json, assetów, ID Discorda, formatów AOTY i limitów fetchowania.

state.py
  Odczyt/zapis data.json z zachowaniem kompatybilności ze starym stanem.

aoty.py
  HTTP, parsery AOTY, wyszukiwanie userów/artystów/wydań, ratings_count,
  wszystkie formaty, review/track-ratings/like oraz profile.

shared.py
  Wspólne nazwy zmiennych dla wydań (ReleaseVariables) i profilu
  (ProfileVariables), kolory, ikony, asset AOTY i autocomplete username.

views.py
  Interaktywne buttony/selecty: główny embed, recenzja, track ratings,
  wybór usera w /album oraz paging /profile.

monitor.py
  Cały automatyczny system nowych/zmienionych ocen + ta sama logika dla /check.

display_utils.py
  Romanizacja nazw tylko przy wyświetlaniu.

commands/
  last.py, recent.py, artist.py, album.py, profile.py, check.py.

CO ZOSTAŁO ZMIENIONE
--------------------
1. ratings_count obsługuje m.in. "Based on 2,096 ratings" i "User Score (2,096)".
2. Wspólne zmienne/defaulty są w shared.py; config/assety/formaty w settings.py.
3. Monitor automatyczny jest w monitor.py.
4. /last ma opcjonalny format; "Wszystkie formaty" wybiera najnowszą ocenę ogólnie.
5. /profile pokazuje tylko aktualny/defaultowy typ Favorites: Albums ALBO Artists.
6. Każda komenda korzysta ze wspólnych settings/shared zamiast własnych kopii.
7. /check ręcznie sprawdza usera z config.json i działa tylko na GUILD_ID z configu.
8. /last, /recent i /profile mają autocomplete AOTY usernames; /check podpowiada USERS z configu.
9. Ratingi przechowują flagi review / track ratings / like. Komendy z konkretną oceną
   mają interaktywne widoki do przełączenia na recenzję albo oceny tracklisty.
10. /profile pobiera do 50 ostatnich ocen i pokazuje maks. 10 stron po 5 ocen.
    Strzałki pojawiają się tylko, gdy istnieje poprzednia/następna strona.
11. Usunięto zduplikowaną logikę z bot.py i naprawiono martwy/stary monitor.

WAŻNE PRZY PIERWSZYM DEPLOYU
----------------------------
- Zachowaj assets/aoty.jpg.
- DISCORD_TOKEN może nadal być Railway Variable.
- DATA_DIR=/app/data nadal współpracuje z dotychczasowym Volume.
- config.json i data.json z paczki są zachowane.
- Pierwszy check po migracji monitora seeduje pokrycie nowych formatów bez wysyłania
  starych singli/EP/etc. jako nowych powiadomień. Kolejne cykle działają normalnie.
- rating_fetch_limits w config.json steruje osobno głębokością każdego formatu; 0 wyłącza format.

UWAGA O AOTY
------------
Parsery review / track ratings / like są celowo ostrożne, żeby nie oznaczać zwykłej
szarej ikonki serca jako "liked". Buttony robią dodatkowy live-check strony konkretnej
oceny usera. Jeśli AOTY zmieni HTML, te selektory mogą wymagać aktualizacji.
