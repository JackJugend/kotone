KOTONE — AOTY 429 / BUTTON DETAIL FIX

Przyczyna:
- Button Track ratings / Recenzja wywoływał get_user_rating_for_album().
- Gdy bezpośrednia strona użytkownika nie została odnaleziona, kod uruchamiał
  fallback po ratings pages.
- To potrafiło wykonać wiele requestów i AOTY zwracało HTTP 429.

Zmiany:
1. Bot buduje teraz bezpośredni user-release URL także z tytułu wydania:
   /user/<username>/album/<id>-<album-title-slug>/
   np. /user/enso/album/1225702-uncut-gem/
2. /last, /recent i /profile mają fallback ograniczony do 60 pozycji,
   bo pracują na ostatnich ocenach.
3. AOTYRateLimit w buttonach nie powoduje już tracebacka discord.ui.view.
   Użytkownik dostaje ephemeral:
   "AOTY chwilowo ogranicza liczbę zapytań..."
4. Odpowiedź 429 nie jest cache'owana, więc można ponowić próbę.
5. Timeout 900 widoczny w tracebacku jest poprawny:
   900 sekund = 15 minut.
