# Pollinator (Telegram day-range poll bot)

Super simple Telegram bot that creates a poll where each day in a chosen range is one option.
Hauptfluss isch button-basiert mit Monatskalender (7-Spalte Raster).

## 1) Setup

```bash
cd ~/Desktop/Programming/Utility/pollinator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Bot token

1. In Telegram, open `@BotFather`
2. Run `/newbot`
3. Create a local `.env` file:

```bash
cp .env.example .env
# then edit .env and paste your token
```

## 3) Run

```bash
python bot.py
```

## 4) Use in your group

1. Add the bot to your group.
2. Run `/start` once.
3. Tap `Umfrog starte`.
4. Wähle Starttag und Endtag im Monatskalender.
5. Optional: `weli zit?` usfülle.

Der Poll fragt immer: `chasch no?`

Optional typed commands:

```text
/poll today +6
/poll 2026-03-10 2026-03-15 ab 19:00
```

Supported day formats:
- `YYYY-MM-DD`
- `today`
- `+N` (N days from today)

Notes:
- Poll is non-anonymous and allows multiple selections.
- Lange Datumsbereiche werden automatisch in mehrere Polls aufgeteilt (je max 10 Optionen pro Poll).
- Optionali Zyt-Notiz wird i d Poll-Frag integriert (`chasch no? | weli zit? ...`).
- Nach dr Erstellung löscht dr Bot alli Flow-Nachrichte, so dass nur Poll(s) bliibe.
- Dafür bruucht dr Bot i de Gruppe Admin-Recht zum Lösche vo Nachrichtä.
