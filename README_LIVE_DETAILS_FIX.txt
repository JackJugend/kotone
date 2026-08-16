KOTONE — LIVE DETAILS FIX

1. TRACK RATINGS
- get_user_rating_for_album() now prefers the exact /user/.../album/... URL
  captured from AOTY rating cards.
- The previous code derived that URL from the public album URL, even though
  AOTY uses a different slug on user pages.
- After fallback lookup the bot now fetches the exact user-release page
  instead of returning only a track-rating icon flag.
- One rated track is enough to enable Track ratings.
- Missing track scores on the complete tracklist remain NR.

2. RATINGS COUNT
- ratings_count is now anchored strictly to the release User Score block:
  "User Score ... Based on 574 ratings".
- Generic page-wide "N ratings" matching was removed.
- This prevents unrelated values such as song rating counts from being used.

3. AOTY LOGO
- Footers now use:
  https://cdn.albumoftheyear.org/images/favicon.png
- The local aoty.jpg is no longer uploaded with /last or /profile, so it
  cannot become a giant standalone image after switching embeds.

4. BUTTON TIMEOUT
- Every interactive View uses a 15 minute timeout.
- After timeout all buttons/selects are disabled visually when the message
  can still be edited.
